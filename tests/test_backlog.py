"""0.7.0b6: frames that arrive in a backlog are placed at their scale time.

Fixture: the real brew of 2026-11-25 (event-loop stall: no frame from
6.53 s to 15.54 s, then the queued frames within milliseconds; see the
fixture's ``note``). Pure tests (no Home Assistant).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from backlog_helpers import profile_from_frames

from custom_components.difluid_scale.brew import (
    PROFILE_RESOLUTION,
    BacklogClock,
    BrewTracker,
    brew_metrics,
    build_profile,
)
from custom_components.difluid_scale.live import LiveBrew

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "backlog_2026-11-25.json").read_text()
)
T0 = 1_795_591_425.582129  # the stored start (epoch s)


def _frames(after: float = 0.0) -> list[tuple[float, float, float, int]]:
    return [(t, w, f, timer) for t, w, f, timer, _ in FIXTURE["frames"] if t >= after]


def _replay(frames=None, *, live: bool = False):
    """Feed the fixture like the coordinator: frames at their arrival, the
    0.25 s brew tick in between, the device states. Returns (results,
    live messages, LiveBrew)."""
    frames = FIXTURE["frames"] if frames is None else frames
    tracker = BrewTracker()
    events = [(t, "frame", (w, f, timer)) for t, w, f, timer, *_ in frames]
    events += [(t, "state", st) for t, st in FIXTURE["device_state"]]
    events.sort(key=lambda e: e[0])
    results = []
    messages: list[dict] = []
    stream: LiveBrew | None = None
    next_tick = events[0][0]
    for t, kind, data in [*events, (45.0, "end", None)]:
        while next_tick <= t:
            if (r := tracker.tick(T0 + next_tick)) is not None:
                results.append(r)
            if live and stream is None and tracker.active:
                stream = LiveBrew(tracker)
                messages.append(stream.start_message(tracker, T0 + next_tick))
            elif live and stream is not None and tracker.active:
                if (m := stream.update_message(tracker, T0 + next_tick)) is not None:
                    messages.append(m)
            next_tick += PROFILE_RESOLUTION
        if kind == "frame":
            w, f, timer = data
            r = tracker.feed_sample(T0 + t, w, f, timer)
        elif kind == "state":
            r = tracker.feed_timer_state(T0 + t, data == "timing", reset=data == "idle")
        else:
            r = None
        if r is not None:
            results.append(r)
    return results, messages, stream


def test_real_backlog_one_brew_at_scale_time() -> None:
    results, _, _ = _replay()
    assert len(results) == 1
    brew = results[0]
    assert not brew.truncated
    times = [p[0] for p in brew.profile]
    assert times == sorted(times)
    stored = FIXTURE["stored_profile"]
    assert len(stored) == 51
    assert len(brew.profile) >= 80
    # The stall stretch (6.5-15.5 s) is filled again; 0.7.0b5 had nothing
    # there and the first weight at 15.62 s.
    stall = [p for p in brew.profile if 6.6 < p[0] < 15.5]
    assert len(stall) >= 25
    first_coffee = next(p for p in brew.profile if p[1] >= 3.0)
    assert 8.0 <= first_coffee[0] <= 9.5, first_coffee
    # Unchanged part after the backlog: the cut frame (40.2 g) at its arrival.
    cut = next(p for p in brew.profile if p[1] == 40.2)
    assert abs(cut[0] - (23.797 - (brew.start - T0))) < 0.01


def test_real_backlog_metrics() -> None:
    results, _, _ = _replay()
    metrics = brew_metrics(results[0].profile)
    old = FIXTURE["stored_metrics"]
    assert old["first_drop"] == 15.6 and old["flow_cv"] == 0.53
    assert 8.0 <= metrics["first_drop"] <= 9.5, metrics
    assert metrics["flow_cv"] < 0.3, metrics
    assert 2.7 <= metrics["mean_flow"] <= 3.1, metrics
    assert metrics["peak_flow"] <= 4.0, metrics


def test_clock_offset_learned_not_zero_and_never_after_arrival() -> None:
    clock = BacklogClock()
    frames = []
    for t, w, f, timer in _frames():
        if t > 31.0:
            break  # the brew's frames (the timer is reset at 41 s)
        clock.add(t, t, w, f, timer)
        frames.append(clock.pending[-1])
    # The scale's counts start about 0.2 s before the brew clock: learned
    # from the frames (earliest arrival per count), not assumed.
    assert clock.offset is not None and -0.3 < clock.offset < -0.15, clock.offset
    last = -1.0
    for fr in frames:
        assert fr.placed <= fr.arrival + 1e-9  # never later than it arrived
        assert fr.placed >= last - 1e-9  # order kept
        last = fr.placed
    assert sum(fr.backlog for fr in frames) >= 30


def test_backlog_frames_land_in_their_timer_second() -> None:
    clock = BacklogClock()
    frames = []
    for t, w, f, timer in _frames():
        if t > 31.0:
            break  # the brew's frames (the timer is reset at 41 s)
        clock.add(t, t, w, f, timer)
        frames.append(clock.pending[-1])
    off = clock.offset
    burst = [fr for fr in frames if 15.5 < fr.arrival < 15.7]
    assert burst and all(fr.backlog for fr in burst if fr.count < 15)
    for fr in burst:
        if fr.backlog:
            assert off + fr.count - 1e-6 <= fr.placed <= off + fr.count + 1 + 1e-6
    # Fresh frames stay at their arrival.
    fresh = [fr for fr in frames if 24.0 < fr.arrival < 30.0]
    assert fresh and all(not fr.backlog and fr.placed == fr.arrival for fr in fresh)


def test_without_timer_or_timer_zero_unchanged() -> None:
    frames = _frames()
    for timer in (None, 0):
        points = profile_from_frames([(t, w, f, timer) for t, w, f, _ in frames])
        buckets: dict[int, tuple] = {}
        for t, w, f, _ in frames:
            buckets[int(t / PROFILE_RESOLUTION)] = (t, w, f)
        assert points == [p for _, p in sorted(buckets.items())]


def test_frozen_timer_late_frames_stay_at_arrival() -> None:
    """A stopped timer (no higher count follows) is no backlog, even after a
    silence and with late frames bunched together."""
    frames = [(0.1 * i, 0.0, 0.0, int(0.1 * i)) for i in range(1, 50)]  # 0.1-4.9 s
    # Timer frozen at 4, a silence, then a bunch of frames still showing 4.
    frames += [(8.0 + 0.002 * i, 30.0, 0.0, 4) for i in range(10)]
    frames += [(8.3 + 0.3 * i, 30.0, 0.0, 4) for i in range(5)]
    clock = BacklogClock()
    out = []
    for frame in frames:
        clock.add(frame[0], *frame)
        out.append(clock.pending[-1])
    assert not any(fr.backlog for fr in out)
    assert all(fr.placed == fr.arrival for fr in out)


def test_unchanged_brews_without_backlog() -> None:
    """A brew with regular frames: profile exactly as before 0.7.0b6
    (bucket per arrival)."""
    frames = [(0.1 * i, 2.0 * 0.1 * i, 2.0, int(0.1 * i + 0.05)) for i in range(1, 300)]
    points = profile_from_frames(frames)
    buckets: dict[int, tuple] = {}
    for t, w, f, _ in frames:
        buckets[int(t / PROFILE_RESOLUTION)] = (t, w, f)
    assert points == [p for _, p in sorted(buckets.items())]


def test_live_real_backlog_curve() -> None:
    """The real stall: nothing after 6.5 s was sent before the backlog came,
    so its frames simply follow at their scale time; the client's curve
    equals the stored profile."""
    results, messages, stream = _replay(live=True)
    assert stream is not None and messages[0]["type"] == "live_start"
    assert all("replace_from" in m for m in messages[1:] if m["type"] == "live_points")
    client = _client(messages)
    assert client == stream.points
    times = [p[0] for p in client]
    assert times == sorted(times)
    assert len([p for p in client if 6.6 < p[0] < 15.5]) >= 20
    stored = [list(p) for p in build_profile(results[0].profile)]
    assert client == stored[: len(client)]


def _client(messages: list[dict]) -> list[list[float]]:
    """A client following the protocol (README: replace_from)."""
    client: list[list[float]] = []
    for m in messages:
        if m["type"] == "live_start":
            client = list(m["points"])
        elif m["type"] == "live_points":
            if m["replace_from"] is not None:
                client = [p for p in client if p[0] < m["replace_from"]]
            client.extend(m["points"])
    return client


def test_live_replaces_points_already_sent() -> None:
    """Held-back frames that first come one by one (late, sent at their
    arrival by the 0.25 s tick), then the rest of the queue at once: the
    live curve is redrawn from the first of them (replace_from)."""
    tracker = BrewTracker()
    t0 = 1000.0
    tracker.feed_timer_state(t0, True)

    def frame(arrival: float, taken: float) -> tuple[float, float, float, int]:
        return (arrival, max(0.0, round(2 * (taken - 1), 1)), 2.0, int(taken))

    frames = [frame(round(0.05 + 0.3 * i, 2), round(0.05 + 0.3 * i, 2)) for i in range(11)]
    # Nothing from 3.05 s to 5.0 s; frames taken at 3.35 and 3.65 s come at
    # 5.0 and 5.3 s, the rest of the queue (4.3, 4.95 s) and a fresh one
    # at 5.6 s within milliseconds, then regular frames again.
    frames += [frame(5.0, 3.35), frame(5.3, 3.65)]
    frames += [frame(5.6, 4.3), frame(5.606, 4.95), frame(5.612, 5.6)]
    frames += [frame(round(5.9 + 0.3 * i, 2), round(5.9 + 0.3 * i, 2)) for i in range(10)]
    stream = None
    messages = []
    tick = 0.0
    for t, w, f, c in frames:
        while tick <= t:
            tracker.tick(t0 + tick)
            if not tracker.active:
                pass
            elif stream is None:
                stream = LiveBrew(tracker)
                messages.append(stream.start_message(tracker, t0 + tick))
            elif (m := stream.update_message(tracker, t0 + tick)) is not None:
                messages.append(m)
            tick += PROFILE_RESOLUTION
        tracker.feed_sample(t0 + t, w, f, c)
    assert tracker.state == "brewing"
    replaced = [m for m in messages if m.get("replace_from") is not None]
    assert len(replaced) == 1, replaced
    # The 5.0 s frame had been sent; it and everything after is resent.
    assert 3.0 <= replaced[0]["replace_from"] <= 5.0, replaced[0]
    assert replaced[0]["points"][0][0] >= replaced[0]["replace_from"]
    # Ordinary updates carry replace_from null.
    assert sum(m.get("replace_from") is None for m in messages[1:]) >= 5
    client = _client(messages)
    assert client == stream.points
    times = [p[0] for p in client]
    assert times == sorted(times)
    # Every frame within its timer second, none left at its late arrival
    # (weight 2 g/s from 1 s: taken = weight / 2 + 1).
    start = tracker.start - t0
    for t, w, _ in client:
        if w > 0:
            taken = w / 2 + 1
            assert abs(t + start - taken) <= 1.0, (t, w)


def _run_clock(frames):
    """Feed (arrival, count) frames (weight = arrival) through a clock;
    returns (clock, frames as placed, max pending)."""
    clock = BacklogClock()
    out = []
    most = 0
    for a, c in frames:
        clock.add(a, a, a, 1.0, c)
        out.append(clock.pending[-1])
        most = max(most, len(clock.pending))
    return clock, out, most


def _regular(start: float, end: float, step: float = 0.3, shift: float = 0.0):
    """On-time frames: taken (and arriving) every ``step``; count =
    whole seconds since the timer start, ``shift`` s behind."""
    out = []
    t = start
    while t < end:
        out.append((round(t, 3), int(t - shift)))
        t += step
    return out


def test_lateness_threshold_behaviour() -> None:
    """0.4 s past its timer second is jitter (stays at its arrival); a
    queue arriving 0.6 s and more late and catching up is moved."""
    base = _regular(0.05, 10.0)
    # A frame of count 9 arriving at 10.4 s (0.4 s after its second): kept.
    _, out, _ = _run_clock([*base, (10.4, 9), *_regular(10.7, 13.0)])
    late = next(f for f in out if f.arrival == 10.4)
    assert not late.backlog and late.placed == 10.4
    # Count-9 frames arriving 1.5 s late, then count 11 right after: the
    # timer ran 2 s while 0.3 s of arrivals passed - a queue caught up.
    _, out, _ = _run_clock([*base, (11.6, 9), (11.75, 9), (11.9, 11)])
    moved = [f for f in out if f.arrival in (11.6, 11.75)]
    # In count 9's second (offset 0.05 s), after the frame before them.
    assert all(f.backlog and 9.05 - 0.1 <= f.placed <= 10.05 for f in moved), moved


def test_on_time_bunch_over_a_second_boundary_is_no_backlog() -> None:
    """Frames held ~0.3 s by BLE coalescing, delivered within ms, spanning
    counts 19/20 but none late: spread in the profile, no backlog."""
    frames = [*_regular(0.1, 19.4), (20.100, 19), (20.103, 19), (20.106, 20),
              *_regular(20.4, 23.0)]
    tracker = BrewTracker()
    tracker.feed_timer_state(1000.0, True)
    for a, c in frames:
        tracker.feed_sample(1000.0 + a, 10.0, 1.0, c)
    assert tracker.backlog_spans == []
    _, out, _ = _run_clock(frames)
    bunch = [f for f in out if 20.0 < f.arrival < 20.2]
    assert all(f.bunched and not f.backlog for f in bunch)
    assert all(f.placed <= f.arrival for f in bunch)


def test_timer_pause_without_frames_relearns_the_offset() -> None:
    """The timer paused 2 s while no frames came (silence), then counts on:
    the frames are evenly late -> offset re-learned, nothing moved, no
    backlog, pending stays small."""
    frames = _regular(0.05, 12.0) + _regular(14.05, 30.0, shift=2.0)
    clock, out, most = _run_clock(frames)
    assert not any(f.backlog for f in out)
    assert all(f.placed == f.arrival for f in out)
    assert most <= 8
    assert clock.lateness(out[-1].arrival, out[-1].count) < 0.5
    assert 1.9 < clock.offset < 2.1  # the counts now 2 s behind


def test_timer_reset_and_wrap_relearn() -> None:
    frames = _regular(0.05, 10.0) + [(round(10.05 + 0.3 * i, 3), int(0.3 * i)) for i in range(20)]
    clock, out, _ = _run_clock(frames)
    assert not any(f.backlog or f.bunched for f in out)
    assert 9.9 < clock.offset < 10.1  # the new count origin
    wrap = [(round(0.05 + 0.3 * i, 3), (65530 + int(0.3 * i)) % 65536) for i in range(80)]
    clock, out, _ = _run_clock(wrap)
    assert not any(f.backlog for f in out)
    assert all(f.placed == f.arrival for f in out)


def test_wall_clock_step_is_no_delay() -> None:
    """A wall clock step of +0.7 s during the brew: the clock runs on the
    monotonic time, no frame turns late, nothing moves."""
    tracker = BrewTracker()
    tracker.feed_timer_state(1000.0, True)
    for a, c in _regular(0.05, 25.0):
        wall = 1000.0 + a + (0.7 if a > 12.0 else 0.0)
        tracker.feed_sample(wall, 10.0, 1.0, c, mono=500.0 + a)
        late = tracker.last_lateness
        assert late is None or late < 0.5, (a, late)
    assert tracker.backlog_spans == []


def test_other_timer_unit_moves_nothing() -> None:
    for unit in (0.1, 2.0):  # counts per 0.1 s or per 2 s
        frames = [(round(0.05 + 0.3 * i, 3), int((0.05 + 0.3 * i) / unit)) for i in range(150)]
        # A burst late in the run (as if held back).
        frames += [(46.0 + 0.002 * i, frames[-1][1] + i) for i in range(5)]
        _, out, _ = _run_clock(frames)
        assert all(f.placed == f.arrival for f in out[30:]), unit


def test_pending_stays_bounded() -> None:
    """Frames that stay late without the timer catching up (a stopped timer
    whose frames come late) never make pending grow without end."""
    frames = _regular(0.05, 10.0)
    frames += [(round(12.0 + 0.1 * i, 3), 9 + i // 40) for i in range(400)]
    _, _, most = _run_clock(frames)
    assert most <= 64


def test_gap_decision_keeps_brewing() -> None:
    """The real stall: the first frames after the gaps (6.0 s, 15.54 s)
    imply another start; the brew stays brewing (the cut rule keeps
    checking) until the next counts show the timer ran on."""
    tracker = BrewTracker()
    states = []
    for t, kind, data in sorted(
        [(t, "f", (w, f, c)) for t, w, f, c, *_ in FIXTURE["frames"]]
        + [(t, "s", st) for t, st in FIXTURE["device_state"]],
        key=lambda e: e[0],
    ):
        if kind == "f":
            tracker.feed_sample(T0 + t, *data)
            if 1.0 < t < 30.0:
                states.append((t, tracker.state))
        else:
            tracker.feed_timer_state(T0 + t, data == "timing", reset=data == "idle")
    assert all(st == "brewing" for t, st in states if t < 30.0), [
        x for x in states if x[1] != "brewing"
    ]


def test_cut_time_never_goes_back() -> None:
    """data_time (cut rule, flow, throttle) never decreases; on-time
    frames get their arrival, late ones their timer second at once."""
    tracker = BrewTracker()
    last = -100.0
    given = {}
    for t, w, f, c, *_ in FIXTURE["frames"]:
        tracker.feed_sample(T0 + t, w, f, c)
        d = tracker.data_time(T0 + t) - T0
        assert d >= last - 1e-6 and d <= t + 1e-6
        last = d
        given[t] = d
    assert abs(given[19.797] - 19.797) < 1e-6  # on time: its arrival
    assert abs(given[23.797] - 23.797) < 1e-6
    assert 8.0 < given[15.549] < 10.0  # count 9 of the queue, at once


def test_wall_clock_back_in_idle_and_between_brews() -> None:
    """data_time follows the arrival outside a brew and starts fresh with
    each brew: a wall clock stepping back (NTP) never freezes it."""
    tracker = BrewTracker()
    for i in range(20):  # idle
        t = 1000.0 + 0.3 * i
        tracker.feed_sample(t, 0.0, 0.0, 0, mono=t)
        assert tracker.data_time(t) == t
    for i in range(10):  # idle, wall clock 60 s back
        t = 950.0 + 0.3 * i
        tracker.feed_sample(t, 0.0, 0.0, 0, mono=1006.0 + 0.3 * i)
        assert tracker.data_time(t) == t
    # A brew, then the clock steps back, then another brew.
    for start in (2000.0, 1900.0):
        tracker.feed_timer_state(start, True)
        last = -math.inf
        for a, c in _regular(0.05, 12.0):
            tracker.feed_sample(start + a, 5.0, 1.0, c, mono=start + a)
            d = tracker.data_time(start + a)
            assert d >= last and abs(d - (start + a)) < 1e-6
            last = d
        tracker.feed_timer_state(start + 12.0, False)
        for k in range(20):
            tracker.tick(start + 12.0 + 0.25 * k)


def test_flow_window_is_capped() -> None:
    """A clock that stands still never makes the flow window grow."""
    from custom_components.difluid_scale.stop_model import (
        FLOW_MAX_FRAMES,
        FlowEstimator,
    )

    est = FlowEstimator()
    for i in range(500):
        est.feed(100.0, float(i))
    assert est.count <= FLOW_MAX_FRAMES


def test_slow_drain_is_placed_within_a_second() -> None:
    """A queue drained at 80 ms per frame (no burst): the frames end up
    within about a timer second of when they were taken (documented
    limit), the timer stays paced."""
    tracker = BrewTracker()
    tracker.feed_timer_state(1000.0, True)
    taken = [round(i / 5, 4) for i in range(1, 150)]
    events = []
    held = []
    for tk in taken:
        if 4.5 <= tk < 9.0:
            held.append(tk)
            continue
        if held:
            events += [(9.0 + 0.08 * j, h) for j, h in enumerate(held)]
            held = []
        events.append((max(tk, events[-1][0] + 0.08) if events else tk, tk))
    for arr, tk in events:
        tracker.feed_sample(1000.0 + arr, 2 * tk, 2.0, int(tk), mono=1000.0 + arr)
    assert tracker._clock.paced
    start = tracker.start - 1000.0
    worst = max(
        abs(p[0] + start - p[1] / 2)
        for _, p in sorted(tracker._buckets.items())
        if p[1] > 0
    )
    assert worst <= 1.5, worst


def test_timer_pause_does_not_stall_the_cut_clock() -> None:
    """After a timer pause without frames the frames come evenly late at
    the normal cadence: data_time keeps advancing with them (no frame is
    put back while the offset is re-learned)."""
    tracker = BrewTracker()
    tracker.feed_timer_state(1000.0, True)
    last = -math.inf
    for a, c in _regular(0.05, 12.0) + _regular(14.05, 25.0, shift=2.0):
        tracker.feed_sample(1000.0 + a, 5.0, 1.0, c, mono=1000.0 + a)
        d = tracker.data_time(1000.0 + a)
        if a > 14.3:
            assert d > last, (a, d - 1000.0)
        last = d
    assert tracker.backlog_spans == []
