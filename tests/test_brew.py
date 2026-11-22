"""Standalone tests for the pure brew detection module.

Run with plain ``python3 tests/test_brew.py`` (no Home Assistant needed).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_PATH = Path(__file__).resolve().parents[1] / "custom_components/difluid_scale/brew.py"
_spec = importlib.util.spec_from_file_location("difluid_brew", _PATH)
brew = importlib.util.module_from_spec(_spec)
sys.modules["difluid_brew"] = brew
_spec.loader.exec_module(brew)

RATE = 0.1  # 10 Hz sensor frames
T0 = 1_700_000_000.0


class Sim:
    """Feeds a BrewTracker with a synthetic sample stream."""

    def __init__(self, config: brew.BrewConfig | None = None) -> None:
        self.tracker = brew.BrewTracker(config)
        self.t = T0
        self.timer = 0
        self.timer_start: float | None = None
        self.timer_frozen = False
        self.results: list[brew.BrewResult] = []
        self.discards: list[str] = []

    def _collect(self, result: brew.BrewResult | None) -> None:
        if result is not None:
            self.results.append(result)
        elif self.tracker.last_discard_reason:
            self.discards.append(self.tracker.last_discard_reason)
            self.tracker.last_discard_reason = None

    def run(self, seconds: float, weight, flow=lambda t: 0.0) -> None:
        """Advance; ``weight``/``flow`` get seconds since this segment began."""
        start = self.t
        for _ in range(round(seconds / RATE)):
            rel = self.t - start
            if self.timer_start is not None and not self.timer_frozen:
                self.timer = int(self.t - self.timer_start)
            w = weight(rel) if callable(weight) else weight
            f = flow(rel) if callable(flow) else flow
            self._collect(self.tracker.feed_sample(self.t, w, f, self.timer))
            self._collect(self.tracker.tick(self.t))
            self.t = round(self.t + RATE, 3)

    def timer_go(self) -> None:
        self.timer_start = self.t
        self.timer_frozen = False

    def timer_stop(self) -> None:
        self.timer_frozen = True

    def timer_reset(self) -> None:
        self.timer_start = None
        self.timer_frozen = False
        self.timer = 0

    def state(self, running: bool) -> None:
        self._collect(self.tracker.feed_timer_state(self.t, running))

    def gap(self, seconds: float) -> None:
        """No samples (BLE link down); the coordinator still ticks."""
        for _ in range(round(seconds / 0.25)):
            self.t = round(self.t + 0.25, 3)
            if self.timer_start is not None and not self.timer_frozen:
                self.timer = int(self.t - self.timer_start)
            self._collect(self.tracker.tick(self.t))

    def disconnect(self) -> None:
        self._collect(self.tracker.disconnect(self.t))


def weigh_dose(sim: Sim, dose: float = 18.0) -> None:
    """Bowl on + tare, beans in, bowl off (negative), tare, cup on, tare."""
    sim.run(3, 0.0)
    sim.run(2, lambda r: dose * r / 2)  # pouring beans
    sim.run(8, lambda r: dose + (0.1 if int(r * 10) % 3 == 0 else 0.0))
    sim.run(2, -152.3)  # bowl removed
    sim.run(2, 0.0)  # tared
    sim.run(2, 118.4)  # cup on
    sim.run(5, 0.0)  # tared again


def pull_shot(sim: Sim, cut_at: float = 36.0, preinfusion: float = 6.0) -> None:
    """Pump + timer start, flow until cut_at grams, timer stops 2 s later."""
    sim.timer_go()
    state = {"w": 0.0}

    def flow(r: float) -> float:
        if r < preinfusion or state["w"] >= cut_at:
            return 0.0
        return min(2.0, (r - preinfusion) * 1.0)

    def weight(r: float) -> float:
        state["w"] = min(cut_at, state["w"] + flow(r) * RATE)
        return round(state["w"], 1)

    sim.run(preinfusion + 22, weight, flow)  # shot cut at ~36 g
    sim.run(2, cut_at, 0.0)  # pump off, timer still runs 2 s
    sim.timer_stop()
    sim.run(10, lambda r: min(cut_at + 0.8, cut_at + 0.4 * r), 0.0)  # drips
    sim.timer_reset()
    sim.run(3, cut_at + 0.8)


def report(name: str, sim: Sim) -> None:
    rs = ", ".join(
        f"dose={r.dose} yield={r.yield_g} ratio={r.ratio} duration={r.duration}s "
        f"points={len(r.profile)}"
        for r in sim.results
    )
    print(f"{name}: brews=[{rs}] discarded={sim.discards}")


def test_normal_shot_with_dose() -> None:
    sim = Sim()
    weigh_dose(sim, 18.0)
    pull_shot(sim)
    report("normal shot with dose", sim)
    assert len(sim.results) == 1, sim.results
    r = sim.results[0]
    assert r.dose == 18.0 and not r.truncated, r
    assert 36.0 <= r.yield_g <= 36.8, r.yield_g  # drips inside the tail count
    assert 29.0 <= r.duration <= 31.0, r.duration
    assert r.ratio == round(r.yield_g / 18.0, 2)
    assert 0 < len(r.profile) <= brew.PROFILE_MAX_POINTS
    assert r.profile[0][0] >= 0 and max(p[2] for p in r.profile) == 2.0
    gaps = [b[0] - a[0] for a, b in zip(r.profile, r.profile[1:])]
    assert max(gaps) <= 0.31, max(gaps)  # ~0.25 s resolution
    # Profile ends at the tail end, not later.
    assert r.profile[-1][0] <= r.duration + 3.0 + 0.01


def test_shot_without_dose() -> None:
    sim = Sim()
    sim.run(10, 0.0)
    pull_shot(sim)
    report("shot without dose", sim)
    assert len(sim.results) == 1
    assert sim.results[0].dose is None and sim.results[0].ratio is None


def test_too_short_run() -> None:
    sim = Sim()
    weigh_dose(sim)
    sim.timer_go()
    sim.run(3.5, lambda r: 2 * r)
    sim.timer_stop()
    sim.run(6, 7.0)
    sim.timer_reset()
    sim.run(2, 7.0)
    report("too short run", sim)
    assert sim.results == [] and len(sim.discards) == 1
    assert sim.discards[0].startswith("duration")


def test_low_yield_run() -> None:
    sim = Sim()
    sim.timer_go()
    sim.run(10, 1.0)
    sim.timer_stop()
    sim.run(6, 1.0)
    report("low yield run", sim)
    assert sim.results == [] and sim.discards[0].startswith("yield")


def test_timer_reset_without_run() -> None:
    sim = Sim()
    sim.timer = 12  # scale connects with a paused timer from before ...
    sim.run(3, 0.0)
    sim.state(False)  # status: timer paused
    sim.timer_reset()  # ... which is reset to 0
    sim.run(1, 0.0)
    sim.state(False)  # status: idle
    sim.run(1, 0.0)
    sim.state(False)  # status: timer ready (one DLink click)
    sim.run(5, 0.0)
    report("timer reset without run", sim)
    assert sim.results == [] and sim.discards == []
    assert not sim.tracker.active


def test_state_driven_start_and_stop() -> None:
    sim = Sim()
    weigh_dose(sim, 17.5)
    sim.state(True)  # status frame says TIMING before the timer field moves
    sim.timer_go()
    sim.run(25, lambda r: min(40.0, max(0.0, (r - 5) * 2)), lambda r: 2.0 if r > 5 else 0)
    sim.state(False)  # status says paused
    sim.timer_stop()
    sim.run(5, 40.0)
    report("state-driven start/stop", sim)
    assert len(sim.results) == 1
    r = sim.results[0]
    assert r.dose == 17.5 and 24.5 <= r.duration <= 25.5, r


def test_dose_consumed_and_lookback() -> None:
    cfg = brew.BrewConfig(dose_lookback_minutes=1)
    sim = Sim(cfg)
    weigh_dose(sim, 18.0)
    pull_shot(sim)
    pull_shot(sim)  # second shot without weighing: no stale dose
    weigh_dose(sim, 19.0)
    sim.run(70, 0.0)  # longer than the 1 min lookback
    pull_shot(sim)
    report("dose consumed / lookback", sim)
    assert [r.dose for r in sim.results] == [18.0, None, None]


def test_cup_in_dose_range_after_brew_is_no_dose() -> None:
    sim = Sim()
    sim.run(5, 0.0)
    pull_shot(sim, cut_at=20.0)  # ristretto: cup sits at ~20.8 g afterwards
    sim.run(20, 20.8)
    sim.run(2, 0.0)  # tare, next shot
    pull_shot(sim)
    report("cup in dose range after brew", sim)
    assert [r.dose for r in sim.results] == [None, None]


def test_long_brew_is_capped() -> None:
    sim = Sim()
    sim.timer_go()
    sim.run(300, lambda r: r * 0.5, 0.5)
    sim.timer_stop()
    sim.run(5, 150.0)
    report("long brew (pour-over)", sim)
    r = sim.results[0]
    assert len(r.profile) <= brew.PROFILE_MAX_POINTS, len(r.profile)


def test_flow_derived_when_scale_reports_none() -> None:
    sim = Sim()
    sim.timer_go()
    sim.run(20, lambda r: r * 2.0, 0.0)
    sim.timer_stop()
    sim.run(5, 40.0)
    r = sim.results[0]
    mid = [p[2] for p in r.profile if 5 < p[0] < 15]
    print(f"derived flow: mid-brew flow values {sorted(set(mid))}")
    assert all(1.8 <= f <= 2.2 for f in mid), mid


def test_dose_correction() -> None:
    sim = Sim()
    sim.run(5, 0.0)
    pull_shot(sim)
    r = sim.results[0]
    stored = {"dose": r.dose, "yield": r.yield_g, "ratio": r.ratio}
    delta = brew.correct_dose(stored, 18.04)
    print(f"dose correction: {stored} coffee_total += {delta}")
    assert stored["dose"] == 18.0 and stored["dose_source"] == "manual"
    assert stored["ratio"] == round(r.yield_g / 18.0, 2) and delta == 18.0
    delta = brew.correct_dose(stored, 17.0)  # lower: total does not shrink
    assert stored["ratio"] == round(r.yield_g / 17.0, 2) and delta == 0.0
    delta = brew.correct_dose(stored, 17.5)  # still below what was counted
    assert delta == 0.0
    delta = brew.correct_dose(stored, 18.5)
    assert delta == 0.5


def _shot_weight(rate: float = 1.4, cap: float = 36.0):
    return lambda r: min(cap, round(r * rate, 1))


def test_purge_does_not_eat_the_dose() -> None:
    """Regression 1: a discarded run (purge) leaves the dose for the shot."""
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.state(True)  # purge: pump + timer for 4 s, nothing in the cup
    sim.timer_go()
    sim.run(4, 0.0)
    sim.timer_stop()
    sim.run(10, 0.0)
    sim.timer_reset()
    sim.run(3, 0.0)
    pull_shot(sim)
    report("purge then shot", sim)
    assert len(sim.discards) == 1 and sim.discards[0].startswith("duration")
    assert [r.dose for r in sim.results] == [18.0]
    # The accepted shot consumed it: the next one gets none.
    pull_shot(sim)
    assert [r.dose for r in sim.results] == [18.0, None]


def test_disconnect_mid_brew_resumes_same_brew() -> None:
    """Regression 2: BLE drop mid-shot + reconnect = one brew, with dose."""
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    start = sim.t
    sim.run(12, _shot_weight())
    sim.disconnect()
    assert sim.tracker.state == brew.PARKED and sim.tracker.active
    sim.gap(4)  # link down, timer keeps running on the scale
    sim.run(12, lambda r: min(36.0, round((r + 16) * 1.4, 1)))
    sim.timer_stop()
    sim.run(8, 36.0)
    report("disconnect mid-brew, resumed", sim)
    assert len(sim.results) == 1, sim.results
    r = sim.results[0]
    assert r.dose == 18.0 and r.yield_g == 36.0, r
    assert abs(r.start - start) <= 1.0 and 27.0 <= r.duration <= 29.0, r
    assert not sim.tracker.active


def test_disconnect_never_returns_closes_after_grace() -> None:
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    sim.run(20, _shot_weight())
    sim.disconnect()
    sim.gap(29)
    assert sim.results == [] and sim.tracker.state == brew.PARKED
    sim.gap(1.5)
    report("disconnect, grace expired", sim)
    assert len(sim.results) == 1 and not sim.tracker.active
    r = sim.results[0]
    assert r.dose == 18.0 and r.yield_g == 27.9 and 19.5 <= r.duration <= 20.1, r
    assert r.truncated  # the auto-stop learns nothing from such a brew


def test_disconnect_timer_reset_in_gap_closes_truncated() -> None:
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    sim.run(20, _shot_weight())
    sim.disconnect()
    sim.gap(5)
    sim.timer_reset()
    sim.run(3, 36.5)
    report("disconnect, timer reset in gap", sim)
    assert len(sim.results) == 1 and not sim.tracker.active
    assert sim.results[0].yield_g == 27.9 and sim.results[0].dose == 18.0
    pull_shot(sim)  # a later shot is a new brew again
    assert len(sim.results) == 2 and sim.results[1].dose is None


def test_disconnect_timer_stopped_in_gap_uses_frozen_value() -> None:
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    sim.run(20, _shot_weight())
    sim.disconnect()
    sim.gap(6)
    sim.timer_stop()  # stopped at ~26 s while we were away
    sim.gap(6)
    sim.run(5, 36.4)
    report("disconnect, timer stopped in gap", sim)
    assert len(sim.results) == 1, (sim.results, sim.discards)
    assert 25.5 <= sim.results[0].duration <= 26.5, sim.results[0]


def test_disconnect_in_tail_finishes_normally() -> None:
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    sim.run(26, _shot_weight())
    sim.timer_stop()
    sim.run(2, 36.2)  # stall detected, tail running
    assert sim.tracker.state == brew.TAIL
    sim.disconnect()
    sim.gap(3)
    assert len(sim.results) == 1 and sim.results[0].dose == 18.0


def test_status_only_start_without_timer_advance_ends() -> None:
    """Regression 3: TIMING status but the timer never moves."""
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.state(True)
    sim.run(60, 0.0)
    assert not sim.tracker.active, sim.tracker.state
    assert sim.discards and sim.discards[0].startswith("duration")
    pull_shot(sim)
    report("status-only start, then real shot", sim)
    assert len(sim.results) == 1
    r = sim.results[0]
    assert r.dose == 18.0 and 29.0 <= r.duration <= 31.0, r


def test_status_resume_from_tail_without_advance_ends() -> None:
    sim = Sim()
    sim.timer_go()
    sim.run(10, lambda r: r * 2.0, 2.0)
    sim.timer_stop()
    sim.run(2.6, 20.0)
    assert sim.tracker.state == brew.TAIL
    sim.state(True)  # status says TIMING again, but the timer stays frozen
    assert sim.tracker.state == brew.BREWING
    sim.run(10, 20.0)
    assert not sim.tracker.active and len(sim.results) == 1


def test_cup_lifted_in_tail() -> None:
    """Regression 4: cup lifted during the capture tail."""
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    sim.run(26, _shot_weight())
    sim.timer_stop()
    sim.run(2.6, 36.0)  # stall detection, tail begins
    lifted_at = sim.t - T0
    sim.run(5, -320.0)  # cup lifted (tared scale goes negative)
    report("cup lifted in tail", sim)
    assert len(sim.results) == 1, sim.discards
    r = sim.results[0]
    assert r.yield_g == 36.0 and r.dose == 18.0, r
    assert all(p[1] >= 0 for p in r.profile)
    assert r.start - T0 + r.profile[-1][0] < lifted_at


def test_cup_partially_lifted_in_tail() -> None:
    sim = Sim()
    sim.timer_go()
    sim.run(26, _shot_weight())
    sim.timer_stop()
    sim.run(2.6, lambda r: 36.0 + min(0.6, r * 0.3))
    sim.run(3, 12.0)  # cup tilted / partially lifted: still positive
    assert len(sim.results) == 1 and sim.results[0].yield_g == 36.6, sim.results


def test_dose_correction_down_up_does_not_inflate_total() -> None:
    """Regression 5: 18 -> 17 -> 18 -> 17 -> 18 adds 18 once."""
    stored = {"dose": None, "yield": 36.0, "ratio": None}
    deltas = [brew.correct_dose(stored, d) for d in (18.0, 17.0, 18.0, 17.0, 18.0, 19.0)]
    assert deltas == [18.0, 0.0, 0.0, 0.0, 0.0, 1.0], deltas
    assert stored["counted_dose"] == 19.0 and stored["dose"] == 19.0
    auto = {"dose": 18.0, "yield": 36.0, "ratio": 2.0}  # pre-0.3.0b2 brew
    assert [brew.correct_dose(auto, d) for d in (17.0, 18.0, 18.5)] == [0.0, 0.0, 0.5]


def _jitter_shot(seed: int, lag_max: float, status_stop: bool = False):
    import random

    rng = random.Random(seed)
    tracker = brew.BrewTracker()
    results, discards = [], []
    t = T0
    timer_start: float | None = None
    stop_at: float | None = None

    def collect(r):
        if r is not None:
            results.append(r)
        elif tracker.last_discard_reason:
            discards.append(tracker.last_discard_reason)
            tracker.last_discard_reason = None

    def frames(until: float, weight_fn, flow_fn) -> None:
        nonlocal t
        while t < until:
            rel = None if timer_start is None else t - timer_start
            if rel is None:
                timer = 0
            else:
                # The count the frame carries lags behind (late count).
                seen = max(0.0, rel - rng.uniform(0.0, lag_max))
                if stop_at is not None:
                    seen = min(seen, stop_at - timer_start)
                timer = int(seen)
            collect(tracker.feed_sample(t, weight_fn(rel), flow_fn(rel), timer))
            collect(tracker.tick(t))
            t += rng.uniform(0.05, 0.4)

    frames(T0 + 5, lambda r: 0.0, lambda r: 0.0)
    timer_start = t
    frames(timer_start + 28, lambda r: min(36.0, max(0.0, (r - 5) * 1.6)),
           lambda r: 1.6 if 5 < r < 27.5 else 0.0)
    stop_at = t
    if status_stop:
        collect(tracker.feed_timer_state(t, False))
    frames(stop_at + 8, lambda r: 36.0, lambda r: 0.0)
    return results, discards, stop_at - timer_start


def test_ble_jitter_does_not_split_or_stop_early() -> None:
    """Irregular frames (0.05-0.4 s) and late counts around the stall limit."""
    for seed in range(40):
        results, discards, true_duration = _jitter_shot(seed, lag_max=0.4)
        assert len(results) == 1 and discards == [], (seed, results, discards)
        r = results[0]
        assert abs(r.duration - true_duration) <= 1.5, (seed, r.duration, true_duration)
        assert r.yield_g == 36.0, (seed, r)
    for seed in range(40):
        results, discards, true_duration = _jitter_shot(seed, 0.4, status_stop=True)
        assert len(results) == 1 and discards == [], (seed, results, discards)
        assert abs(results[0].duration - true_duration) <= 1.0, seed
    print("ble jitter: 80 randomized shots, one brew each")


def _shot_until_cut(sim: Sim, start: float, cut: float = 36.0, rate: float = 1.4):
    """Weight of a shot that started at ``start`` (absolute), capped at cut."""
    return lambda r: min(cut, round((sim.t - start) * rate, 1))


def test_silence_before_late_disconnect_is_one_brew() -> None:
    """Review 1: the link goes silent ~8 s before the drop is reported."""
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    start = sim.t
    sim.run(12, _shot_until_cut(sim, start))
    sim.gap(8)  # no frames, no disconnect yet: must not read as a stop
    assert sim.results == [] and sim.tracker.state == brew.PARKED
    sim.disconnect()  # reported late
    sim.gap(3)
    sim.run(10, _shot_until_cut(sim, start))  # reconnected, timer still runs
    sim.timer_stop()
    sim.run(8, 36.0)
    report("silence before late disconnect", sim)
    assert len(sim.results) == 1, sim.results
    r = sim.results[0]
    assert r.dose == 18.0 and r.yield_g == 36.0 and not r.updated, r
    assert abs(r.start - start) <= 1.0 and 32.0 <= r.duration <= 34.0, r


def test_proxy_hiccup_without_disconnect_is_one_brew() -> None:
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    start = sim.t
    sim.run(10, _shot_until_cut(sim, start))
    sim.gap(4.5)  # proxy stalls > 4 s, link never reported down
    sim.run(14, _shot_until_cut(sim, start))
    sim.timer_stop()
    sim.run(8, 36.0)
    report("proxy hiccup", sim)
    assert len(sim.results) == 1, sim.results
    r = sim.results[0]
    assert r.dose == 18.0 and r.yield_g == 36.0, r
    assert abs(r.start - start) <= 1.0 and 27.5 <= r.duration <= 29.5, r


def test_silence_longer_than_grace_reopens_same_brew() -> None:
    """Truncated by the grace backstop, then the same timer is back."""
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    start = sim.t
    sim.run(10, _shot_until_cut(sim, start))
    sim.gap(36)  # silent past the 30 s grace: closed as truncated
    assert len(sim.results) == 1 and 13.5 <= sim.results[0].yield_g <= 14.1
    sim.run(4, _shot_until_cut(sim, start))  # same running timer
    sim.timer_stop()
    sim.run(8, 36.0)
    report("silence past grace, reopened", sim)
    assert len(sim.results) == 2, sim.results
    first, merged = sim.results
    assert merged.updated and merged.start == first.start
    assert merged.dose == 18.0 and merged.yield_g == 36.0, merged


def test_reset_in_long_gap_is_not_reopened() -> None:
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    sim.run(20, _shot_weight())
    sim.disconnect()
    sim.gap(40)  # truncated after the grace
    assert len(sim.results) == 1
    sim.timer_go()  # reset + new shot while we were away
    sim.t = round(sim.t + 25, 3)  # its timer is past the old stop value
    sim.run(5, 30.0)
    sim.timer_stop()
    sim.run(8, 30.0)
    assert len(sim.results) == 2 and not sim.results[1].updated, sim.results
    assert sim.results[1].start != sim.results[0].start


def test_cup_lifted_shortly_after_stop() -> None:
    """Review 2: lift before the stall check noticed the stop."""
    for lift in (0.5, 1.0, 1.4, 2.0, 2.4, 3.0):
        sim = Sim()
        weigh_dose(sim, 18.0)
        sim.timer_go()
        sim.run(25.05, lambda r: min(36.0, round(r * 1.6, 1)), 1.6)
        sim.timer_stop()
        stop = sim.t
        sim.run(12, lambda r: 36.0 if sim.t < stop + lift else -320.0)
        assert len(sim.results) == 1, (lift, sim.results, sim.discards)
        r = sim.results[0]
        assert r.yield_g == 36.0 and r.dose == 18.0, (lift, r)
        assert all(p[1] >= 0 for p in r.profile), lift
        assert 24.5 <= r.duration <= 26.5, (lift, r.duration)
    print("cup lifted 0.5-3.0 s after stop: yield 36 g each")


def test_cup_partially_lifted_before_stall_check() -> None:
    sim = Sim()
    sim.timer_go()
    sim.run(25.05, lambda r: min(36.0, round(r * 1.6, 1)), 1.6)
    sim.timer_stop()
    sim.run(1.2, 36.0)
    sim.run(8, 12.0)  # tilted cup, still positive
    assert len(sim.results) == 1 and sim.results[0].yield_g == 36.0, sim.results


def _pour_over(sim: Sim, status_resume: bool) -> float:
    sim.run(6, 15.0)  # dose 15 g
    sim.run(4, 0.0)
    sim.timer_go()
    start = sim.t
    sim.run(30, lambda r: min(50.0, r * 3))  # bloom
    sim.timer_stop()
    sim.run(10, 50.0)  # pause longer than the tail: first part finishes
    assert len(sim.results) == 1 and sim.results[0].yield_g == 50.0
    if status_resume:
        sim.state(True)
    paused = sim.timer
    sim.timer_start = sim.t - paused  # counts on from the stop value
    sim.timer_frozen = False
    sim.run(60, lambda r: 50 + min(200.0, r * 4))
    sim.timer_stop()
    sim.run(10, 250.0)
    return start


def test_pour_over_pause_resume_is_one_brew() -> None:
    """Review 3: a pause longer than the tail continues the same brew."""
    for status_resume in (True, False):
        sim = Sim()
        start = _pour_over(sim, status_resume)
        report(f"pour-over pause (status resume {status_resume})", sim)
        assert len(sim.results) == 2, sim.results
        first, final = sim.results
        assert not first.updated and final.updated
        assert final.start == first.start and abs(final.start - start) <= 1.0
        assert final.dose == 15.0 and final.yield_g == 250.0, final
        assert 99.5 <= final.duration <= 101.5, final.duration  # incl. pause
        assert final.profile[0][0] < 1.0 and final.profile[-1][0] > 99.0
        ts = [p[0] for p in final.profile]
        assert ts == sorted(ts)
        assert not sim.tracker.active


def test_pause_then_reset_is_a_new_brew() -> None:
    sim = Sim()
    sim.run(6, 15.0)
    sim.run(4, 0.0)
    sim.timer_go()
    sim.run(30, lambda r: min(50.0, r * 3))
    sim.timer_stop()
    sim.run(10, 50.0)
    sim.timer_reset()
    sim.run(2, 0.0)
    sim.timer_go()
    sim.run(40, lambda r: min(60.0, r * 2))
    sim.timer_stop()
    sim.run(6, 60.0)
    assert len(sim.results) == 2 and not sim.results[1].updated, sim.results
    assert sim.results[1].dose is None and sim.results[1].duration >= 39


def test_status_reset_clears_resume() -> None:
    sim = Sim()
    sim.timer_go()
    sim.run(20, lambda r: r * 2.0)
    sim.timer_stop()
    sim.run(6, 40.0)
    sim.tracker.feed_timer_state(sim.t, False, reset=True)
    # A count from the stop value now is not a resume (no frame showed 0).
    sim.timer_start = sim.t - sim.timer
    sim.timer_frozen = False
    sim.run(10, lambda r: 40 + r)
    sim.timer_stop()
    sim.run(6, 50.0)
    assert len(sim.results) == 2 and not sim.results[1].updated


def test_late_count_after_status_stop_does_not_reopen() -> None:
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.state(True)
    sim.timer_go()
    sim.run(25.5, lambda r: min(36.0, round(r * 1.6, 1)), 1.6)
    sim.state(False)  # status says paused, frame still shows 25
    late = sim.timer + 1
    sim.timer_frozen = True
    sim.run(0.4, 36.0)
    sim.timer = late  # one late count arrives
    sim.run(12, 36.0)
    report("late count after status stop", sim)
    assert len(sim.results) == 1 and not sim.results[0].updated, sim.results
    assert 25.0 <= sim.results[0].duration <= 25.6 and sim.results[0].yield_g == 36.0


def test_export_restore_running_brew() -> None:
    """Review 4: reload mid-brew hands the brew to the new instance."""
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    start = sim.t
    sim.run(12, _shot_until_cut(sim, start))
    data = sim.tracker.export_active(sim.t)
    assert data is not None and data["state"] == brew.PARKED
    import json

    data = json.loads(json.dumps(data))  # what the Store does
    sim.tracker = brew.BrewTracker()
    assert sim.tracker.restore_active(data) and sim.tracker.state == brew.PARKED
    assert sim.tracker.dose_candidate == 18.0
    sim.gap(1.5)
    sim.run(14, _shot_until_cut(sim, start))
    sim.timer_stop()
    sim.run(8, 36.0)
    report("export/restore running brew", sim)
    assert len(sim.results) == 1, sim.results
    r = sim.results[0]
    assert r.dose == 18.0 and r.yield_g == 36.0 and abs(r.start - start) <= 1.0, r
    assert r.profile[0][0] < 1.0


def test_export_restore_tail_finishes() -> None:
    sim = Sim()
    sim.timer_go()
    sim.run(20, lambda r: r * 2.0)
    sim.timer_stop()
    sim.run(3, 40.0)
    assert sim.tracker.state == brew.TAIL
    data = sim.tracker.export_active(sim.t)
    sim.tracker = brew.BrewTracker()
    assert sim.tracker.restore_active(data) and sim.tracker.state == brew.TAIL
    sim.gap(3)
    assert len(sim.results) == 1 and sim.results[0].yield_g == 40.0
    assert brew.BrewTracker().restore_active({"bogus": 1}) is False


def test_live_points_are_incremental() -> None:
    sim = Sim()
    sim.run(3, 0.0)
    sim.timer_go()
    sim.run(4, lambda r: r)
    tr = sim.tracker
    assert tr.active and tr.start is not None
    pts, key = tr.live_points_since(-1)
    assert pts and all(b[0] > a[0] for a, b in zip(pts, pts[1:]))
    sim.run(1, lambda r: 4 + r)
    more, key2 = tr.live_points_since(key)
    assert more and more[0][0] > pts[-1][0] and key2 > key
    assert tr.live_points_since(key2)[0] == []


def test_dose_candidate_visible_during_brew() -> None:
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    sim.run(3, 0.0)
    assert sim.tracker.dose_candidate == 18.0



# ---------------------------------------------------------------- review 3


def test_reconnect_after_grace_status_first_reopens() -> None:
    """Drop past the grace; the status reply arrives before the first frame."""
    for status_first in (True, False):
        sim = Sim()
        weigh_dose(sim, 18.0)
        sim.timer_go()
        start = sim.t
        sim.run(12, _shot_until_cut(sim, start))
        sim.gap(8)
        sim.disconnect()
        sim.gap(30)  # reconnect ~38 s after the last frame, timer still runs
        if status_first:
            sim.state(True)  # reply to CMD_GET_STATUS comes first
            assert sim.tracker.state == brew.IDLE  # the frame decides
        sim.run(10, _shot_until_cut(sim, start))
        sim.timer_stop()
        sim.run(8, 36.0)
        report(f"reconnect after grace (status first {status_first})", sim)
        assert len(sim.results) == 2, sim.results
        first, merged = sim.results
        assert merged.updated and merged.start == first.start, merged
        assert merged.dose == 18.0 and merged.yield_g == 36.0, merged
        assert 58.5 <= merged.duration <= 61.0, merged.duration


def test_connect_mid_run_status_first_backdates_start() -> None:
    """Timer started before HA connected: start = t - timer, not connect time."""
    sim = Sim()
    sim.run(3, 0.0)
    sim.disconnect()  # link not up yet
    sim.timer_go()
    start = sim.t
    sim.gap(10)
    sim.state(True)  # status TIMING before any frame
    sim.run(15, _shot_until_cut(sim, start))
    sim.timer_stop()
    sim.run(8, 36.0)
    assert len(sim.results) == 1, sim.results
    r = sim.results[0]
    assert abs(r.start - start) <= 1.0 and 24.0 <= r.duration <= 26.0, r


def _gap_shot(
    seed: int,
    gap_at: float,
    gap_len: float,
    disc: bool,
    status_first: bool,
    lift_after: float | None = None,
    status_stop: bool = False,
    lag: float = 0.4,
):
    """A 28 s shot (36 g) with jittered frames and a BLE gap at ``gap_at``.

    The disconnect is reported 8-10 s into the gap; on reconnect the status
    reply may come first. Returns (results, discard codes, gap, lift time)
    with times relative to the timer start.
    """
    import random

    rng = random.Random(seed)
    tracker = brew.BrewTracker()
    results, discards = [], []

    def collect(r):
        if r is not None:
            results.append(r)
        elif tracker.last_discard_reason:
            discards.append(tracker.last_discard_code)
            tracker.last_discard_reason = None

    ts = T0 + 20
    stop_at = ts + 28.0
    shown = [0]

    def weight(t: float) -> float:
        if t < ts:
            if t < T0 + 3:
                return 0.0
            if t < T0 + 10:
                return 18.0
            return -80.0 if t < T0 + 12 else 0.0
        if lift_after is not None and t >= stop_at + lift_after:
            return -300.0
        return min(36.0, max(0.0, (t - ts - 5) * 1.6))

    def timer(t: float) -> int:
        if t < ts:
            return 0
        seen = min(max(0.0, t - ts - rng.uniform(0, lag)), stop_at - ts)
        shown[0] = max(shown[0], int(seen))
        return shown[0]

    gap0, gap1 = ts + gap_at, ts + gap_at + gap_len
    disc_at = gap0 + rng.uniform(8, 10) if disc and gap_len > 8 else None
    disconnected = first_after = False
    status_sent = False
    t = tick_next = T0
    while t < stop_at + 40:
        while tick_next <= t:
            collect(tracker.tick(tick_next))
            tick_next += 0.25
        if disc_at is not None and not disconnected and t >= disc_at:
            collect(tracker.disconnect(t))
            disconnected = True
        if status_stop and not status_sent and t >= stop_at:
            collect(tracker.feed_timer_state(t, False))
            status_sent = True
        if gap0 <= t < gap1:
            t += 0.1
            continue
        if t >= gap1 and not first_after and gap_len > 0:
            first_after = True
            if disconnected and status_first:
                collect(tracker.feed_timer_state(t, t < stop_at))
        collect(tracker.feed_sample(t, weight(t), 1.6, timer(t)))
        t += rng.uniform(0.05, 0.4)
    lift_t = None if lift_after is None else 28.0 + lift_after
    return results, discards, (gap_at, gap_at + gap_len), lift_t


def test_gap_over_stop_and_tail_uses_post_gap_weight() -> None:
    """Review 3: the yield is the first weight after the gap, not a stale one."""
    for gap_at, gap_len in ((20, 15), (22, 12), (25, 10), (18, 20), (5, 29)):
        for seed in range(3):
            results, discards, _, _ = _gap_shot(seed, gap_at, gap_len, True, True)
            assert len(results) == 1 and discards == [], (gap_at, seed, results)
            r = results[0]
            assert r.yield_g == 36.0 and r.dose == 18.0, (gap_at, seed, r)
            assert abs(r.duration - 28.0) <= 1.0, (gap_at, seed, r.duration)
            assert r.profile[-1][1] == 36.0
    # Lifted after the gap ended: the lift is no yield.
    results, _, _, _ = _gap_shot(0, 20, 15, True, True, lift_after=8)
    assert len(results) == 1 and results[0].yield_g == 36.0, results


def test_stopped_just_before_gap_end_is_not_running() -> None:
    """Review 3: a frozen count within the resume tolerance is no advance."""
    for seed in range(20):
        for gap_at, gap_len in ((26, 3.5), (5, 25), (27.5, 1.5)):
            results, _, _, _ = _gap_shot(seed, gap_at, gap_len, gap_len > 8, False)
            assert len(results) == 1, (seed, gap_at, results)
            assert abs(results[0].duration - 28.0) <= 1.0, (
                seed, gap_at, results[0].duration,
            )


def test_long_gap_frozen_timer_is_not_a_new_brew() -> None:
    """Truncated by the grace; the timer shows a frozen higher count."""
    for disc in (False, True):
        results, _, _, _ = _gap_shot(0, 15, 45, disc, True)
        assert len({r.start for r in results}) == 1, results
        assert results[-1].updated and results[-1].yield_g == 36.0, results
        assert abs(results[-1].duration - 28.0) <= 1.0, results


def test_gap_sweep_only_fails_without_data() -> None:
    """The review sweep: every failure left is a cup lifted inside the gap
    (no frame shows the yield). Full sweep: 4 seeds, here 1."""
    bad = []
    for gap_at in (5, 15, 26, 27.5, 28.5, 29.5, 31):
        for gap_len in (0, 1.0, 1.5, 2.5, 3.5, 5, 9, 12, 25, 29, 31, 45):
            for disc in (False, True):
                for sf in (False, True):
                    for lift in (None, 0.5, 1.5, 2.5, 4):
                        for ss in (False, True):
                            res, _, (g0, g1), lift_t = _gap_shot(
                                0, gap_at, gap_len, disc, sf, lift, ss
                            )
                            long_gap = gap_len >= 29 and gap_at < 28
                            last = res[-1] if res else None
                            ok = (
                                res
                                and len({r.start for r in res}) == 1
                                and last.dose == 18.0
                                and (
                                    long_gap
                                    or (
                                        abs(last.duration - 28.0) <= 1.6
                                        and last.yield_g >= 30
                                    )
                                )
                            )
                            if not ok and not (
                                lift_t is not None and gap_len and g0 < lift_t < g1
                            ):
                                bad.append((gap_at, gap_len, disc, sf, lift, ss, res))
    assert bad == [], bad[:5]
    print("gap sweep: no failure outside lift-in-gap")


def test_cup_lifted_right_after_stop_with_late_count() -> None:
    """Review 3: lift 0.3-0.5 s after the stop, final count up to 0.4 s late."""
    for lift in (0.3, 0.5):
        for seed in range(60):
            results, _, _, _ = _gap_shot(seed, 0, 0, False, False, lift, lag=0.4)
            assert len(results) == 1 and results[0].yield_g == 36.0, (
                lift, seed, results,
            )


def test_two_shots_without_reset_are_two_brews() -> None:
    """Review 3: continue pressed after a lift and a new dose = new shot."""
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.timer_go()
    sim.run(28, lambda r: min(36.0, round(r * 1.4, 1)))
    sim.timer_stop()
    sim.run(10, 36.0)
    sim.run(3, -300.0)  # cup lifted
    sim.run(2, 0.0)
    weigh_dose(sim, 17.0)
    second = sim.t
    sim.timer_start = sim.t - sim.timer  # counts on from the stop value
    sim.timer_frozen = False
    sim.run(28, lambda r: min(34.0, round(r * 1.4, 1)))
    sim.timer_stop()
    sim.run(10, 34.0)
    report("two shots without reset", sim)
    assert len(sim.results) == 2, sim.results
    a, b = sim.results
    assert not b.updated and b.start != a.start, b
    assert a.dose == 18.0 and b.dose == 17.0 and b.yield_g == 34.0, (a, b)
    assert abs(b.start - second) <= 1.5 and 27.0 <= b.duration <= 29.5, b


def test_export_restore_during_pause_merges() -> None:
    """Review 3: a reload during a pour-over pause still continues the brew."""
    import json

    sim = Sim()
    sim.run(6, 15.0)
    sim.run(4, 0.0)
    sim.timer_go()
    sim.run(30, lambda r: min(50.0, r * 3))
    sim.timer_stop()
    sim.run(10, 50.0)
    assert len(sim.results) == 1
    data = json.loads(json.dumps(sim.tracker.export_active(sim.t)))
    assert data["state"] == brew.IDLE and data["resume"]["stop_timer"] == 29
    sim.tracker = brew.BrewTracker()
    assert sim.tracker.restore_active(data) and not sim.tracker.active
    sim.disconnect()
    sim.gap(6)  # reload + reconnect
    sim.run(2, 50.0)  # still paused at the stop value
    sim.timer_start = sim.t - sim.timer
    sim.timer_frozen = False
    sim.run(30, lambda r: min(120.0, 50 + r * 3))
    sim.timer_stop()
    sim.run(8, 120.0)
    report("reload during pause", sim)
    assert len(sim.results) == 2, sim.results
    final = sim.results[1]
    assert final.updated and final.start == sim.results[0].start, final
    assert final.dose == 15.0 and final.yield_g == 120.0, final
    # Restored, then the timer was reset: no merge.
    sim2 = Sim()
    assert sim2.tracker.restore_active(data)
    sim2.timer_go()
    sim2.t = data["resume"]["stop_t"] + 20
    sim2.run(3, 0.0)
    assert sim2.tracker._resume is None  # noqa: SLF001



def test_real_world_sequence_with_stop_button() -> None:
    """Beans, bowl off, tares, cup; HA starts the timer at pump start; power
    cut at 36 g, stop button 2 s later (status reply); reset after 10 s;
    cup lifted."""
    sim = Sim()
    weigh_dose(sim, 18.0)
    sim.state(True)
    sim.timer_go()
    start = sim.t
    sim.run(26, lambda r: min(36.0, max(0.0, round((r - 6) * 1.8, 1))))
    sim.run(2, 36.2)  # pump off, timer still runs
    sim.timer_stop()
    sim.state(False)  # stop button -> status request
    sim.run(10, 36.4)
    sim.timer_reset()
    sim.tracker.feed_timer_state(sim.t, False, reset=True)
    sim.run(2, 36.4)
    sim.run(3, -310.0)  # cup lifted
    sim.run(2, 0.0)
    assert len(sim.results) == 1 and sim.discards == [], (sim.results, sim.discards)
    r = sim.results[0]
    assert r.dose == 18.0 and r.yield_g == 36.4 and abs(r.start - start) <= 0.2, r
    assert 27.5 <= r.duration <= 28.5, r.duration


def test_end_without_tail_after_auto_stop() -> None:
    """Timer stopped by the auto-stop (drips already in): the brew ends at
    the stop, not tail_seconds later; otherwise the tail as before."""
    for mode in ("tail", "skip", "undo"):
        skip = mode == "skip"
        sim = Sim()
        weigh_dose(sim, 18.0)
        sim.state(True)
        sim.timer_go()
        sim.run(25, lambda r: min(36.0, max(0.0, round((r - 5) * 1.8, 1))))
        if mode != "tail":
            sim.tracker.end_without_tail()
        if mode == "undo":
            # Auto-stop could not stop the timer: a hand stop keeps the tail.
            sim.tracker.keep_tail()
        sim.timer_stop()
        sim.state(False)
        stop_t = sim.t
        # The paused scale sends no weight: only ticks.
        while not sim.results and sim.t < stop_t + 10:
            sim.t = round(sim.t + 0.25, 3)
            sim._collect(sim.tracker.tick(sim.t))
        assert len(sim.results) == 1, sim.discards
        waited = sim.t - stop_t
        if skip:
            assert waited <= 0.25, waited
        else:
            assert 3.0 <= waited <= 3.25, waited
        assert 35.0 <= sim.results[0].yield_g <= 36.0, sim.results[0]
    # Only for the active brew: the next one has its tail again.
    assert sim.tracker._tail_override is None


# ---------------------------------------------------------------- metrics

def _live_brews() -> list[dict]:
    import json

    path = Path(__file__).resolve().parent / "fixtures/live_2026-11-21.json"
    return json.loads(path.read_text())["brews_store"]["data"]["brews"]


def test_metrics_from_live_profiles() -> None:
    """Real profiles: vibration (0.5 g at 4 s) is no drop; first coffee from
    ~2 g; flow from the monotone weight like the card."""
    first, last = _live_brews()
    # All five live brews (ids end FWEGTY ... RA64AV), checked 2026-11-21:
    # first drop 12.3 / 7.0 / 6.0 / 2.9 / 8.8 s; the two in the fixture:
    m = brew.brew_metrics(first["profile"])
    assert m == {
        "first_drop": 12.3, "peak_flow": 3.2, "mean_flow": 2.64, "flow_cv": 0.25,
    }, m
    m = brew.brew_metrics(last["profile"])
    assert m["first_drop"] == 8.8 and m["peak_flow"] == 3.3, m
    assert 2.5 <= m["mean_flow"] <= 3.0 and 0.1 <= m["flow_cv"] <= 0.3, m


def test_metrics_synthetic_and_edge_cases() -> None:
    # Constant 2 g/s from 6 s to 26 s, 0.4 g vibration before.
    pts = []
    for i in range(121):
        t = i * 0.25
        w = 0.4 if 2 <= t < 6 else 0.0
        if t >= 6:
            w = min(40.0, (t - 6) * 2.0)
        pts.append([t, round(w, 1), 0.0])
    m = brew.brew_metrics(pts)
    assert m["first_drop"] == 6.8, m  # first point >= 1.5 g above the start
    assert abs(m["peak_flow"] - 2.0) <= 0.1 and abs(m["mean_flow"] - 2.0) <= 0.05, m
    assert m["flow_cv"] <= 0.05, m
    # Vibration up to 1.0 g is no drop; nor is a jump at the very end that
    # the profile does not show held for 1 s.
    assert brew.first_drop_time([[t * 0.25, 1.0, 0] for t in range(20)]) is None
    tail = [[t * 0.25, 0.0, 0] for t in range(20)] + [[5.0, 2.0, 0], [5.5, 2.1, 0]]
    assert brew.first_drop_time(tail) is None
    assert brew.first_drop_time([*tail, [6.1, 2.2, 0]]) == 5.0
    # Baseline: median of the first second, at most 0 (cup tared a bit low).
    low = [[t * 0.25, -0.8, 0] for t in range(8)] + [[2.0, 0.8, 0], [2.5, 0.9, 0], [3.5, 1.0, 0]]
    assert brew.first_drop_time(low) == 2.0
    # Container not tared (baseline 200 g): no first drop at all.
    untared = [[t * 0.25, 200.0 + (2.0 if t > 20 else 0.0), 0] for t in range(40)]
    assert brew.first_drop_time(untared) is None
    # A single spike (cup bumped) is not a sustained drop.
    spike = [[0.0, 0.0, 0], [1.0, 3.0, 0], [1.25, 0.0, 0], [2.5, 0.0, 0]]
    assert brew.first_drop_time(spike) is None
    # Nothing in the cup: no drop, no flow, no main phase.
    flat = [[t * 0.25, 0.0, 0.0] for t in range(40)]
    assert brew.brew_metrics(flat) == {
        "first_drop": None, "peak_flow": None, "mean_flow": None, "flow_cv": None,
    }
    assert brew.brew_metrics([]) is None
    # Same smoothing as the card (card_flow_test: slope 2 recovered).
    ts = [0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]
    f = brew.smoothed_flow(ts, [1 + 2 * t for t in ts], 1)
    assert f[:3] == [0.0, 0.0, 0.0] and all(abs(v - 2) < 1e-9 for v in f[3:]), f
    assert brew.monotone_weights([7.0, 3.8, 6.2, 7.3]) == [7.0, 7.0, 7.0, 7.3]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"OK: {len(tests)} tests passed")
