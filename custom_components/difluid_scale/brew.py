"""Pure brew detection for the DiFluid Microbalance (no HA imports).

A brew is the window in which the scale's own timer runs, plus a short
capture tail so drips still count towards the yield. The dose is the last
stable weight plateau inside the dose range seen before the brew started.

Timer signals come from two sources:
- the ``timer`` field of every sensor frame (pushed continuously; counts in
  whole seconds as observed), and
- device status frames (``TIMING`` vs. paused/idle), which only arrive when
  requested (connect, poll, button actions).

Times are plain floats in seconds on the caller's clock (the integration
uses epoch seconds), so the module can be fed synthetic streams in tests.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
from statistics import median

PROFILE_RESOLUTION = 0.25  # seconds per profile point
PROFILE_MAX_POINTS = 400
PREROLL_SECONDS = 5.0  # samples kept before a start is detected
# Seconds per timer count (observed: the display counts whole seconds).
TIMER_UNIT_SECONDS = 1.0
# The timer advances once per second; no advance for this long = stopped.
# This is stop *detection* latency, not a delay: the stop time is set back
# to the moment the next count was due. BLE frames arrive irregularly
# (0.05-0.4 s apart, counts late), so two observed advances can be ~1.8 s
# apart while the timer runs; 2.5 s keeps a running timer from being
# mistaken for a stop.
TIMER_STALL_SECONDS = 2.5
# After a reconnect the timer belongs to the parked brew if it implies the
# same start (``t - timer``) within this tolerance (whole-second counts plus
# BLE jitter).
RESUME_TOLERANCE_SECONDS = 1.5
# Inside the capture tail, a weight this far below the tail's maximum (or
# below zero) means the cup was lifted: the tail ends before that sample.
TAIL_DROP_GRAMS = 5.0
# A stall only counts as a timer stop while samples keep coming: if the last
# sample is older than this, the link is silent (BLE drop not reported yet)
# and the brew is parked instead.
SAMPLE_SILENCE_SECONDS = 1.0
# A stopped timer that counts on again (from its stop value, not reset to 0)
# continues the last brew (pour-over pause, late silence detection). Counts
# needed beyond the stop value, so one late count is not read as a resume.
RESUME_MIN_COUNTS = 2
# Without continuous observation since the stop (BLE gap: a reset could have
# been missed) a resume must imply the same start within this tolerance.
RESUME_OBSERVED_GAP_SECONDS = 5.0

# Backlog (0.7.0b6): frames that reach the integration late and bunched -
# a stalled Home Assistant event loop or BLE stack delivers the queued
# notifications in one go - carry the scale timer of when they were taken.
# Consecutive frames this close together form one burst. The scale sends a
# frame about every 0.1-0.4 s; inside the observed backlogs (2026-11-25)
# frames were at most 16 ms apart, so 50 ms separates the two.
BACKLOG_BURST_SECONDS = 0.05
# A frame counts as late when it arrives this long after its timer second
# ended. Normal arrival jitter reached 0.25 s past the second (2026-11-25):
# twice that, two profile ticks.
BACKLOG_LATE_SECONDS = 0.5
# Same timer count while frames keep coming (no silence) for longer than one
# count plus BACKLOG_LATE_SECONDS: the timer stopped or paused, its counts
# no longer map to the brew clock as before (offset estimate restarts).
BACKLOG_PAUSE_SECONDS = TIMER_UNIT_SECONDS + BACKLOG_LATE_SECONDS
# Frames whose placement may still change, at most (a late run that never
# ends must not make every frame cost the whole run).
BACKLOG_PENDING_MAX = 64
# Pace check: between frames at the normal cadence the timer must count
# about once per second of arrival time, summed over at least this long;
# otherwise it counts in another unit and nothing is moved (old behaviour).
BACKLOG_PACE_SECONDS = 8.0
BACKLOG_PACE_MIN = 0.75
BACKLOG_PACE_MAX = 1.6

# Flow values below this are treated as "the scale reports no flow".
_FLOW_EPSILON = 0.05


@dataclass(frozen=True)
class BrewConfig:
    """Thresholds (all user options)."""

    tail_seconds: float = 3.0
    min_duration: float = 5.0
    min_yield: float = 5.0
    dose_min: float = 12.0
    dose_max: float = 25.0
    stability_tolerance: float = 0.3
    stability_seconds: float = 3.0
    dose_lookback_minutes: float = 30.0
    # Fault backstop: a brew parked by a BLE drop is closed as truncated if
    # the scale has not come back with the same running timer by then.
    park_grace_seconds: float = 30.0
    # Matching window: a stopped timer that counts on from its stop value
    # within this time continues the same brew (not a delay).
    resume_window_seconds: float = 300.0


@dataclass(frozen=True)
class Plateau:
    """A stable weight plateau inside the dose range."""

    weight: float
    t_start: float
    t_end: float


@dataclass(frozen=True)
class BrewResult:
    """A finished, accepted brew."""

    start: float  # caller's clock (epoch seconds in the integration)
    duration: float  # seconds, timer start -> timer stop
    dose: float | None
    yield_g: float
    # (seconds since start, weight g, flow g/s), downsampled
    profile: list[tuple[float, float, float]] = field(repr=False)
    # True when this is a reopened brew that was already accepted before
    # (pause/resume): the stored brew with the same start is replaced.
    updated: bool = False
    # Closed by the park backstop (BLE gone): no tail, yield may be short.
    truncated: bool = False

    @property
    def ratio(self) -> float | None:
        """Yield / dose, or None without a dose."""
        return compute_ratio(self.dose, self.yield_g)


def compute_ratio(dose: float | None, yield_g: float | None) -> float | None:
    """Return yield / dose rounded to 2 places; None if not computable."""
    if dose is None or yield_g is None or dose <= 0:
        return None
    return round(yield_g / dose, 2)


class PlateauDetector:
    """Finds stable plateaus (spread <= 2 x tolerance) inside a range."""

    def __init__(self, config: BrewConfig) -> None:
        """Initialize."""
        self.config = config
        self.last: Plateau | None = None
        self._window: deque[tuple[int, float, float]] = deque()
        self._max: deque[tuple[int, float]] = deque()
        self._min: deque[tuple[int, float]] = deque()
        self._sum = 0.0
        self._seq = 0
        # After a brew the cup may sit inside the dose range; ignore the
        # range until the weight has left it once.
        self._suppressed = False

    def clear_window(self) -> None:
        """Drop the current window (keeps the last plateau)."""
        self._window.clear()
        self._max.clear()
        self._min.clear()
        self._sum = 0.0

    def suppress(self) -> None:
        """Ignore in-range weights until the weight leaves the range."""
        self._suppressed = True
        self.clear_window()

    def feed(self, t: float, weight: float) -> None:
        """Add a sample."""
        cfg = self.config
        if not cfg.dose_min <= weight <= cfg.dose_max:
            self._suppressed = False
            self.clear_window()
            return
        if self._suppressed:
            return
        self._seq += 1
        seq = self._seq
        self._window.append((seq, t, weight))
        self._sum += weight
        while self._max and self._max[-1][1] <= weight:
            self._max.pop()
        self._max.append((seq, weight))
        while self._min and self._min[-1][1] >= weight:
            self._min.pop()
        self._min.append((seq, weight))
        limit = 2 * cfg.stability_tolerance + 1e-9
        while self._max[0][1] - self._min[0][1] > limit:
            old_seq, _, old_w = self._window.popleft()
            self._sum -= old_w
            if self._max[0][0] == old_seq:
                self._max.popleft()
            if self._min[0][0] == old_seq:
                self._min.popleft()
        t_first = self._window[0][1]
        if t - t_first >= cfg.stability_seconds:
            self.last = Plateau(
                weight=round(self._sum / len(self._window), 1),
                t_start=t_first,
                t_end=t,
            )

    def peek(self, t_start: float) -> Plateau | None:
        """The plateau valid for a brew starting at ``t_start`` (not consumed)."""
        plateau = self.last
        if plateau is None:
            return None
        lookback = self.config.dose_lookback_minutes * 60
        if plateau.t_end > t_start or t_start - plateau.t_end > lookback:
            return None
        return plateau

    def consume(self, plateau: Plateau | None) -> None:
        """Mark ``plateau`` as used by an accepted brew.

        One weighed dose belongs to one brew: a second shot without weighing
        again gets no dose instead of a stale one. A discarded run (purge,
        false start) does not consume it.
        """
        if plateau is not None and self.last is plateau:
            self.last = None


@dataclass
class _ResumeCandidate:
    """The last finished brew, kept so a resumed timer can continue it."""

    t0: float
    stop_t: float
    stop_timer: int
    dose_plateau: Plateau | None
    points: list[tuple[float, float, float]]
    accepted: bool
    # False once a reset could have gone unseen (BLE gap); True again when a
    # frame shows the timer still at its stop value.
    observed: bool = True
    # Timer value at ``t0`` (a brew counted on from an earlier stop value).
    base: int = 0
    # First frame after a gap with a count above the stop value but another
    # start: (t, timer, weight, flow). The next frames tell frozen/counting.
    probe: tuple[float, int, float, float] | None = None


@dataclass
class _Frame:
    """One sensor frame of the running brew.

    ``arrival`` and ``placed`` are on the clock's own (monotonic) axis,
    ``rel`` is the arrival on the brew clock (s since the start)."""

    arrival: float
    rel: float
    weight: float
    flow: float
    count: int | None  # timer counts since the brew start (timer - base)
    placed: float = 0.0
    backlog: bool = False
    # Part of a bunch arriving on time (no frame of it late): spread over
    # its timer second up to its arrival in the profile, not a backlog.
    bunched: bool = False

    @property
    def placed_rel(self) -> float:
        """The placement on the brew clock."""
        return self.rel - (self.arrival - self.placed)


class BacklogClock:
    """Places backlog frames of a running brew at their scale time.

    Times are monotonic (a wall clock step must not look like a delay).
    A frame arrives *late* when it comes more than BACKLOG_LATE_SECONDS
    after its timer second ended. Two signs that frames were held back
    while the timer ran (a stalled event loop or BLE stack delivering the
    queued notifications at once):

    - a burst (gaps <= BACKLOG_BURST_SECONDS) spanning two or more timer
      counts with at least one late frame;
    - a run of late frames in which (or by the next frame) the timer counts
      ahead of the arrivals: more timer seconds pass than arrival time, by
      more than BACKLOG_LATE_SECONDS (the queue is caught up), or one right
      before such a burst.

    Those frames go to their timer second, the frames of one count spread
    evenly over it, never later than the frame arrived and never before an
    earlier frame. A bunch with no late frame (a hold-up shorter than the
    lateness threshold) is spread the same way in the profile only
    (``bunched``); it is no backlog. Late frames whose counts are only
    shifted - evenly late, counting at the pace of the arrivals: a timer
    paused while no frames came - re-learn the offset instead.

    The timer second of count ``c`` starts at ``offset + c``. ``offset`` is
    the smallest ``arrival - c`` seen: a count cannot arrive before it
    began, so every frame bounds it from above and late frames never lower
    it. It is re-learned when the count stood still while frames kept
    coming (paused timer), after an even shift (above), and when the count
    goes back (reset, uint16 wrap). Nothing is moved (the old behaviour)
    without a usable count: none, 0, ahead of its arrival, or a timer that
    does not count about once per second of arrival time (another unit).

    ``pending`` holds the trailing frames whose placement can still change
    (current burst, current late run; at most BACKLOG_PENDING_MAX).
    """

    def __init__(self) -> None:
        """Start empty (one clock per brew)."""
        self.offset: float | None = None
        self.pending: list[_Frame] = []
        self.finalized: list[_Frame] = []
        self._prev_placed = -math.inf
        self._last_arrival: float | None = None
        self._last_count: int | None = None
        self._still_count: int | None = None
        self._still_since = 0.0
        self._silence_before = True  # the newest frame came after a silence
        # Pace check since the offset was (re)learned: arrival time and
        # counts summed over steps between frames at the normal cadence
        # (no burst, no silence).
        self._pace_dt = 0.0
        self._pace_dc = 0

    def _relearn(self, pace: bool = True) -> None:
        self.offset = None
        if pace:
            self._pace_dt = 0.0
            self._pace_dc = 0

    def lateness(self, arrival: float, count: int | None) -> float | None:
        """Seconds the frame arrived after its timer second ended (None:
        not known)."""
        if self.offset is None or count is None or count < 1:
            return None
        return arrival - (self.offset + (count + 1) * TIMER_UNIT_SECONDS)

    def _late_count(self, arrival: float, count: int) -> bool:
        late = self.lateness(arrival, count)
        return late is not None and late > BACKLOG_LATE_SECONDS

    def _late(self, f: _Frame) -> bool:
        if not self._usable(f):
            return False
        late = self.lateness(f.arrival, f.count)
        return late is not None and late > BACKLOG_LATE_SECONDS

    @property
    def paced(self) -> bool:
        """The timer counts about once per second of arrival time (or too
        little is known yet to say otherwise)."""
        if self._pace_dt < BACKLOG_PACE_SECONDS:
            return True
        rate = self._pace_dc * TIMER_UNIT_SECONDS / self._pace_dt
        return BACKLOG_PACE_MIN <= rate <= BACKLOG_PACE_MAX

    def _usable(self, f: _Frame) -> bool:
        return (
            self.offset is not None
            and f.count is not None
            and f.count >= 1
            # A count ahead of its arrival is implausible.
            and self.offset + f.count * TIMER_UNIT_SECONDS
            <= f.arrival + BACKLOG_LATE_SECONDS
            and self.paced
        )

    def plain(self, rel: float, arrival: float | None = None) -> None:
        """A sample recorded without a timer: everything pending is final
        (``finalized``)."""
        self.finalized = list(self.pending)
        if self.pending:
            self._prev_placed = max(self._prev_placed, self.pending[-1].placed)
        self.pending = []
        if arrival is not None:
            self._prev_placed = max(self._prev_placed, arrival)
        self._last_arrival = None

    def add(
        self,
        arrival: float,
        rel: float,
        weight: float,
        flow: float,
        count: int | None,
    ) -> int:
        """Add a frame; place the pending frames (the new one last).

        Returns how many frames at the front of ``pending`` are final now
        (removed from it after placing, also in ``finalized``)."""
        last = self._last_arrival
        gap = None if last is None else arrival - last
        self._last_arrival = arrival
        self._silence_before = gap is None or gap > SAMPLE_SILENCE_SECONDS
        prev_count = self._last_count
        if count is not None and prev_count is not None and count < prev_count:
            self._relearn()  # reset or uint16 wrap: another count origin
        elif (
            count is not None
            and prev_count is not None
            and gap is not None
            and BACKLOG_BURST_SECONDS < gap <= SAMPLE_SILENCE_SECONDS
            # Not while it stands still (paused timer: no pace to measure).
            and (count != prev_count or arrival - self._still_since <= BACKLOG_PAUSE_SECONDS)
            # Only frames on time: a queue drained at the normal cadence
            # counts faster than it arrives and is no measure of the unit.
            and not self._late_count(arrival, count)
        ):
            self._pace_dt += gap
            self._pace_dc += count - prev_count
        if count is not None:
            self._last_count = count
        # Paused timer: the same count across steady frames.
        if (
            count is None
            or gap is None
            or gap > SAMPLE_SILENCE_SECONDS
            or count != self._still_count
        ):
            self._still_count, self._still_since = count, arrival
        elif arrival - self._still_since > BACKLOG_PAUSE_SECONDS:
            self._relearn(pace=False)
            self._still_since = arrival
        if count is not None and count >= 1:
            bound = arrival - count * TIMER_UNIT_SECONDS
            if self.offset is None or bound < self.offset:
                self.offset = bound
        frame = _Frame(arrival, rel, weight, flow, count)
        self.pending.append(frame)
        pending = self.pending
        self._place(pending)
        if self._shifted(pending):
            self._place(pending)
        # Still open: the current burst and, while frames come late, the
        # current late run.
        keep = len(pending) - 1
        while keep > 0 and pending[keep].arrival - pending[keep - 1].arrival <= (
            BACKLOG_BURST_SECONDS
        ):
            keep -= 1
        if self._late(frame):
            run = len(pending) - 1
            while run > 0 and self._late(pending[run - 1]):
                run -= 1
            keep = min(keep, run)
        keep = max(keep, len(pending) - BACKLOG_PENDING_MAX)
        self.finalized = pending[:keep]
        if keep:
            self._prev_placed = max(self._prev_placed, pending[keep - 1].placed)
            del pending[:keep]
        return keep

    def provisional(self, f: _Frame) -> float:
        """Placement of the newest frame for consumers that cannot wait
        for the burst to end (the cut rule): a late frame in its timer
        second, everything else at its arrival (clock axis)."""
        if f.backlog:
            return f.placed
        if self._late(f) and self._held_back_hint(f):
            assert self.offset is not None and f.count is not None
            mid = self.offset + (f.count + 0.5) * TIMER_UNIT_SECONDS
            return min(f.arrival, max(mid, self._prev_placed))
        return f.arrival

    def _held_back_hint(self, f: _Frame) -> bool:
        """The late frame comes right after a silence or inside a burst (a
        queue delivered at once). Late frames at the normal cadence (a
        timer paused without frames, until the offset is re-learned) keep
        their arrival, so the cut rule's clock does not stand still."""
        i = self.pending.index(f) if f in self.pending else -1
        if i <= 0:
            return self._silence_before
        return f.arrival - self.pending[i - 1].arrival <= BACKLOG_BURST_SECONDS

    def _shifted(self, frames: list[_Frame]) -> bool:
        """The trailing late frames are evenly late and count at the pace
        of their arrivals (no burst, no catch-up) over at least two counts:
        the timer was paused while no frames came. Re-learn the offset from
        them; True when it changed."""
        run: list[_Frame] = []
        for f in reversed(frames):
            if not self._late(f) or f.backlog:
                break
            run.append(f)
        run.reverse()
        if len(run) < 3 or run[-1].count - run[0].count < 2:  # type: ignore[operator]
            return False
        if any(b.arrival - a.arrival <= BACKLOG_BURST_SECONDS for a, b in zip(run, run[1:])):
            return False
        lates = [self.lateness(f.arrival, f.count) or 0.0 for f in run]
        if max(lates) - min(lates) > TIMER_UNIT_SECONDS + BACKLOG_LATE_SECONDS:
            return False
        span = run[-1].arrival - run[0].arrival
        rate = (run[-1].count - run[0].count) * TIMER_UNIT_SECONDS / span if span > 0 else math.inf  # type: ignore[operator]
        if not BACKLOG_PACE_MIN <= rate <= BACKLOG_PACE_MAX:
            return False  # counting faster than arriving: a queue, not a shift
        self.offset = min(f.arrival - f.count * TIMER_UNIT_SECONDS for f in run)  # type: ignore[operator]
        return True

    @staticmethod
    def _catches_up(run: list[_Frame]) -> bool:
        """The timer ran ahead of the arrivals: between the run's first
        frame and a later one more timer seconds passed (at least the counts
        in between) than arrival time, by more than BACKLOG_LATE_SECONDS.
        A timer that was paused (its counts shifted) never does."""
        first = run[0]
        counts = [f.count for f in run]
        if any(b < a for a, b in zip(counts, counts[1:])):  # type: ignore[operator]
            return False
        return any(
            (f.count - first.count - 1) * TIMER_UNIT_SECONDS  # type: ignore[operator]
            - (f.arrival - first.arrival)
            > BACKLOG_LATE_SECONDS
            for f in run[1:]
        )

    def _backlog_flags(self, frames: list[_Frame]) -> list[bool]:
        """Per frame: backlog. Sets ``bunched`` on on-time bunches."""
        n = len(frames)
        flags = [False] * n
        for f in frames:
            f.bunched = False
        # Bursts.
        i = 0
        while i < n:
            j = i + 1
            while j < n and frames[j].arrival - frames[j - 1].arrival <= (
                BACKLOG_BURST_SECONDS
            ):
                j += 1
            burst = frames[i:j]
            counts = [f.count for f in burst]
            if (
                j - i >= 2
                and all(self._usable(f) for f in burst)
                and all(b >= a for a, b in zip(counts, counts[1:]))  # type: ignore[operator]
            ):
                if counts[-1] > counts[0] and any(self._late(f) for f in burst):  # type: ignore[operator]
                    flags[i:j] = [True] * (j - i)
                elif not any(self._late(f) for f in burst):
                    for f in burst:
                        f.bunched = True
            i = j
        # Late runs the timer evidently ran through.
        i = 0
        while i < n:
            if not self._late(frames[i]):
                i += 1
                continue
            j = i
            while j < n and self._late(frames[j]):
                j += 1
            run = frames[i : j + 1] if j < n else frames[i:j]
            if all(self._usable(f) for f in run) and (
                self._catches_up(run)
                # Right before a burst over several counts: the same queue.
                or (j < n and flags[j] and run[-2].count <= run[-1].count)  # type: ignore[operator]
            ):
                flags[i:j] = [True] * (j - i)
            i = j
        return flags

    def _place(self, frames: list[_Frame]) -> None:
        flags = self._backlog_flags(frames)
        prev = self._prev_placed
        i = 0
        while i < len(frames):
            f = frames[i]
            if not flags[i] and not f.bunched:
                f.backlog = False
                f.placed = max(f.arrival, prev)
                prev = f.placed
                i += 1
                continue
            j = i
            while (
                j < len(frames)
                and flags[j] == flags[i]
                and frames[j].bunched == f.bunched
                and frames[j].count == f.count
            ):
                j += 1
            group = frames[i:j]
            assert self.offset is not None and f.count is not None
            lo = max(self.offset + f.count * TIMER_UNIT_SECONDS, prev)
            hi = max(
                lo,
                min(self.offset + (f.count + 1) * TIMER_UNIT_SECONDS, f.arrival),
            )
            k = len(group)
            for n, g in enumerate(group):
                g.backlog = flags[i]
                g.placed = min(g.arrival, lo + (hi - lo) * (n + 0.5) / k)
            prev = group[-1].placed
            i = j


IDLE = "idle"
BREWING = "brewing"
TAIL = "tail"
# BLE link lost while brewing: wait for the scale to come back.
PARKED = "parked"

# Discard reason codes (``last_discard_code``); the text is for logs.
DISCARD_NO_SAMPLES = "no_samples"
DISCARD_TOO_SHORT = "too_short"
DISCARD_LOW_YIELD = "low_yield"


class BrewTracker:
    """Turns a stream of samples and timer states into brews."""

    def __init__(self, config: BrewConfig | None = None) -> None:
        """Initialize."""
        self.config = config or BrewConfig()
        self.plateau = PlateauDetector(self.config)
        self.state = IDLE
        self.last_discard_reason: str | None = None
        self.last_discard_code: str | None = None
        self._preroll: deque[tuple[float, float, float]] = deque()
        self._last_timer: int | None = None
        self._last_sample_t: float | None = None
        self.last_sample: tuple[float, float, float, int | None] | None = None
        self._resume: _ResumeCandidate | None = None
        # A status said "timing" but no frame showed the timer yet (connect,
        # reconnect): the first frame's timer decides start or reopen.
        self._pending_running = False
        # Timer value a new brew counts from: the stop value of a brew that
        # can no longer be continued (cup lifted, new dose) without a reset.
        self._next_base = 0
        self._last_clock: tuple[float, int, BacklogClock] | None = None
        # Newest frame: its provisional placement (caller's clock, see
        # data_time; None: at its arrival) and how late it arrived after its
        # timer second (s, None: unknown), as known when it came.
        self.last_placed: float | None = None
        self.last_lateness: float | None = None
        # Backlog stretches of the brew finished last: (start, spans).
        self.finished_backlog: tuple[float, list[tuple[float, float]]] | None = None
        self._reset_brew()

    def _reset_brew(self) -> None:
        self._t0 = 0.0
        self._stop_t = 0.0
        self._dose_plateau: Plateau | None = None
        self._buckets: dict[int, tuple[float, float, float]] = {}
        self._last_key = -1
        # Every recorded sample in recording order (the buckets keep the
        # last one per tick); the trailing ones from _burst_at on are the
        # clock's current burst, re-placed while it grows.
        self._samples: list[tuple[float, float, float]] = []
        self._burst_at = 0
        self._final_rel = -math.inf  # placement of the last final sample
        # data_time of this brew so far (never goes back within a brew).
        self._data_floor = -math.inf
        self._clock = BacklogClock()
        # Lowest bucket changed after newer ones existed (live replace).
        self._revised: int | None = None
        # Backlog stretches of this brew, (first, last) placed (caller's clock).
        self.backlog_spans: list[tuple[float, float]] = []
        self._last_weight: float | None = None
        self._tail_max: float | None = None
        self._last_timer_change: float | None = None
        self._stop_timer: int | None = None
        self._parked_at = 0.0
        self._park_timer: int | None = None
        self._park_sample_t: float | None = None
        # Scan start for the cup-lift cut (last moment the timer still ran).
        self._stop_ref_t: float | None = None
        # False once a reset was seen: the brew cannot be continued later.
        self._resumable = True
        # Continues an already accepted brew (result replaces it).
        self._reopened = False
        # Timer value at t0 (see _next_base).
        self._base = 0
        # Capture tail for this brew if not tail_seconds (end_without_tail).
        self._tail_override: float | None = None
        # After a gap the timer value of the first frame, (t, timer): the
        # timer only counts as running once a later frame shows a higher one
        # (it may have stopped just before that frame).
        self._probe: tuple[float, int] | None = None
        # First frame after a gap that looked like a frozen timer: (t,
        # timer), and its gap tail sample until the next frame decides
        # (a backlog shows a higher count right behind it).
        self._gap_frame: tuple[float, int, float] | None = None
        self._gap_pending: tuple[float, float, float] | None = None

    @property
    def _tail_seconds(self) -> float:
        if self._tail_override is not None:
            return self._tail_override
        return self.config.tail_seconds

    def end_without_tail(self) -> None:
        """End the active brew at its timer stop, without the capture tail.

        For a timer stopped by the auto-stop after the weight was calm plus
        tail_seconds: the drips are in, and the paused scale sends no weight
        that a further tail could add.
        """
        if self.active:
            self._tail_override = 0.0

    def keep_tail(self) -> None:
        """Undo end_without_tail (the auto-stop could not stop the timer)."""
        self._tail_override = None

    def set_config(self, config: BrewConfig) -> None:
        """Apply new thresholds (takes effect immediately)."""
        self.config = config
        self.plateau.config = config

    @property
    def active(self) -> bool:
        """True while a brew, its capture tail or a parked brew is pending."""
        return self.state != IDLE

    # ------------------------------------------------------- live accessors

    @property
    def start(self) -> float | None:
        """Start of the active brew (caller's clock), None when idle."""
        return self._t0 if self.active else None

    @property
    def dose_candidate(self) -> float | None:
        """Dose the active brew will get if it is accepted."""
        plateau = self._dose_plateau if self.active else None
        return plateau.weight if plateau else None

    def next_dose(self, t: float) -> float | None:
        """Dose a brew starting at ``t`` would get (the active brew's when one runs)."""
        if self.active:
            return self.dose_candidate
        plateau = self.plateau.peek(t)
        return plateau.weight if plateau else None

    @property
    def stop_elapsed(self) -> float | None:
        """Seconds from start to timer stop once known (tail), else None."""
        if self.state == TAIL:
            return round(self._stop_t - self._t0, 1)
        return None

    def live_points_since(
        self, key: int
    ) -> tuple[list[tuple[float, float, float]], int]:
        """Completed profile points after bucket ``key``; returns the new key.

        The newest bucket is still filling and is held back. Cost is
        proportional to the number of new buckets.
        """
        points = [
            self._buckets[k]
            for k in range(key + 1, self._last_key)
            if k in self._buckets
        ]
        return points, max(key, self._last_key - 1)

    # ----------------------------------------------------------------- input

    def feed_sample(
        self,
        t: float,
        weight: float,
        flow: float,
        timer: int | None,
        mono: float | None = None,
    ) -> BrewResult | None:
        """Feed one sensor sample; return a brew when one completes.

        ``mono``: its arrival on a monotonic clock (backlog detection; a
        wall clock step must not look like a delay), default ``t``."""
        if (
            self.state == BREWING
            and self._last_sample_t is not None
            and t - self._last_sample_t > SAMPLE_SILENCE_SECONDS
        ):
            # The ticks of the silence did not run (stalled event loop):
            # catch up with the last sample time as it was, so a stall due
            # in the silence parks the brew (silence, not a timer stop)
            # before this frame is looked at. From BREWING with the old
            # sample time older than SAMPLE_SILENCE_SECONDS, tick() can only
            # park (or do nothing): it never finishes a brew here.
            caught_up = self.tick(t)
            assert caught_up is None, "catching up ticks finished a brew"
        prev_timer, self._last_timer = self._last_timer, timer
        prev_sample_t, self._last_sample_t = self._last_sample_t, t
        self.last_sample = (t, weight, flow, timer)
        self.last_placed = None
        self.last_lateness = None
        if mono is None:
            mono = t
        increased = timer is not None and prev_timer is not None and timer > prev_timer
        # Only a count between close frames is an observed advance (start,
        # resume): across a gap the timer may have counted up and stopped.
        advanced = (
            increased
            and prev_sample_t is not None
            and t - prev_sample_t <= SAMPLE_SILENCE_SECONDS
        )
        went_back = (
            timer is not None and prev_timer is not None and timer < prev_timer
        )
        if timer is not None and timer < self._next_base:
            self._next_base = 0  # reset
        if (res := self._resume) is not None:
            if timer is not None and timer < res.stop_timer:
                self._resume = None  # timer reset: nothing to continue
            else:
                if prev_sample_t is None or t - prev_sample_t > RESUME_OBSERVED_GAP_SECONDS:
                    res.observed = False
                    res.probe = None
                if timer == res.stop_timer:
                    # Still at its stop value: no reset in between.
                    res.observed = True
        if went_back:
            self._resumable = False

        if self._gap_frame is not None:
            self._gap_backlog(t, timer)  # a backlog: back to brewing
        if self.state == PARKED:
            result, handled = self._feed_parked(t, weight, flow, timer)
            if handled:
                return result
            if self.state == IDLE:
                # Parked brew closed; this sample belongs to the idle phase.
                idle_result = self._feed_idle(t, weight, flow, timer, advanced=False)
                return result or idle_result
            # Resolved into BREWING/TAIL: fall through with this sample.
            advanced = False

        if self.state == IDLE:
            return self._feed_idle(t, weight, flow, timer, advanced)

        if self.state == TAIL and self._cup_lifted(weight):
            # Yield = last sample before the drop; the tail ends here.
            return self._finish()
        self._record_frame(t, weight, flow, timer, mono)
        if self.state == BREWING:
            if went_back:  # reset while running
                self._stop(self._expected_stop(t))
            elif self._probe is not None:
                if timer is not None and timer > self._probe[1]:
                    self._probe = None  # counts on: running
                    self._last_timer_change = t
            elif advanced:
                self._last_timer_change = t
            elif increased:
                # Counted up across a short gap: it may have stopped in it.
                self._last_timer_change = None
                self._probe = (t, timer)  # type: ignore[assignment]
        elif self.state == TAIL and advanced and (
            self._stop_timer is None
            or timer >= self._stop_timer + RESUME_MIN_COUNTS  # type: ignore[operator]
        ):
            # The timer kept running after all (a single late count after a
            # status-reported stop is not enough): back to brewing.
            self.state = BREWING
            self._tail_max = None
            self._last_timer_change = t
        return self.tick(t)

    def feed_timer_state(
        self, t: float, running: bool, reset: bool = False
    ) -> BrewResult | None:
        """Feed a device status: True = timing, False = paused/idle/stopped.

        ``reset`` marks a state that means the timer is back at 0 (idle,
        ready): the last brew can no longer be continued.
        """
        if reset:
            self._resume = None
            self._resumable = False
            self._next_base = 0
        if not running:
            self._pending_running = False
        if self.state == PARKED:
            # Decided by the timer field of the next sample (same start or
            # not); the grace period is the backstop.
            return self.tick(t)
        if running:
            if self.state == IDLE:
                if (
                    self._last_timer is None
                    or self._last_sample_t is None
                    or t - self._last_sample_t > TIMER_STALL_SECONDS
                ):
                    # (Re)connect: the status reply comes before the first
                    # frame. Its timer tells the start (t - timer) and
                    # whether the last brew continues.
                    self._pending_running = True
                    return None
                self._start_running(t, self._last_timer)
            elif self.state == TAIL:
                self.state = BREWING
                self._tail_max = None
                self._last_timer_change = t
            return None
        if self.state == BREWING:
            # Right after a gap the timer may have stopped before this.
            self._stop(t if self._probe is None else self._expected_stop(t))
        return self.tick(t)

    def tick(self, t: float) -> BrewResult | None:
        """Advance time without a sample (stall check, end of tail, parking)."""
        if self._gap_frame is not None:
            self._gap_backlog(t, None)
        if self.state == PARKED:
            if t - self._parked_at >= self.config.park_grace_seconds:
                self._stop_t = max(self._park_sample_t or self._t0, self._t0)
                return self._finish(truncated=True)
            return None
        ref = self._probe[0] if self._probe is not None else self._last_timer_change
        if (
            self.state == BREWING
            and ref is not None
            and t - ref >= TIMER_STALL_SECONDS
        ):
            if (
                self._last_sample_t is not None
                and t - self._last_sample_t <= SAMPLE_SILENCE_SECONDS
            ):
                # Samples keep coming with an unchanged timer: stopped.
                self._stop(self._expected_stop(t))
            else:
                # No samples: silence, not a stop. Park until the samples
                # (or the disconnect) tell; the grace period is the backstop.
                self._park(t)
                return None
        if (
            self.state == TAIL
            and t >= self._stop_t + self._tail_seconds
        ):
            return self._finish()
        return None

    def disconnect(self, t: float | None = None) -> BrewResult | None:
        """The scale went away.

        A running brew is parked: if the scale comes back with the same
        running timer it continues as the same brew; otherwise it is closed
        with what we have (timer reset, or ``park_grace_seconds`` passed).
        A brew already in its capture tail just runs out its tail.
        """
        now = t if t is not None else (self._last_sample_t or 0.0)
        if self.state == BREWING:
            self._park(now)
        if self._resume is not None:
            self._resume.observed = False
            self._resume.probe = None
        self._pending_running = False
        self._next_base = 0
        self._last_timer = None
        self._preroll.clear()
        self.plateau.clear_window()
        return None

    # -------------------------------------------------------------- internal

    def _feed_idle(
        self, t: float, weight: float, flow: float, timer: int | None, advanced: bool
    ) -> BrewResult | None:
        self._preroll.append((t, weight, flow))
        while self._preroll and self._preroll[0][0] < t - PREROLL_SECONDS:
            self._preroll.popleft()
        self.plateau.feed(t, weight)
        res = self._resume_candidate(t)
        if res is not None and res.observed and self._interrupted(res, weight):
            # Cup lifted or a new dose weighed since the stop: a count on
            # from the stop value (continue pressed, no reset) is a new shot.
            self._next_base = res.stop_timer
            self._resume = res = None
        if self._pending_running and timer is not None:
            self._pending_running = False
            self._start_running(t, timer)
            return None
        if res is not None and timer is not None and timer > res.stop_timer:
            # Counting on from the stop value (never reset): the last brew
            # continues. Wait for a second count unless the start matches.
            same = self._same_start(res, t, timer)
            if res.observed or same:
                if same or timer >= res.stop_timer + RESUME_MIN_COUNTS:
                    # Unobserved: the timer may have stopped again in the gap.
                    self._reopen(res, t, None if res.observed else timer)
                return None
            return self._probe_gap(res, t, weight, flow, timer)
        if advanced:
            assert timer is not None
            self._start((t - (timer - self._next_base) * TIMER_UNIT_SECONDS), self._next_base)
            self._last_timer_change = t
        return None

    def _start_running(self, t: float, timer: int) -> None:
        """A status said the timer runs; ``timer`` is the current count."""
        res = self._resume_candidate(t)
        if (
            res is not None
            and timer >= res.stop_timer
            and (res.observed or self._same_start(res, t, timer))
        ):
            self._reopen(res, t)
        else:
            base = self._next_base if timer >= self._next_base else 0
            self._start(t - (timer - base) * TIMER_UNIT_SECONDS, base)
        # Stall check needs a reference even if the timer field never
        # advances (status-only start).
        self._last_timer_change = t

    def _interrupted(self, res: _ResumeCandidate, weight: float) -> bool:
        """Cup lifted, or a new dose plateau, since the candidate's stop."""
        if weight < 0:
            return True
        if res.points and weight < res.points[-1][1] - TAIL_DROP_GRAMS:
            return True
        plateau = self.plateau.last
        return plateau is not None and plateau.t_start > res.stop_t

    def _probe_gap(
        self, res: _ResumeCandidate, t: float, weight: float, flow: float, timer: int
    ) -> BrewResult | None:
        """After a gap: a count above the stop value with another start.

        Counting on = a new run (reset in the gap). Frozen = the last brew
        went on in the gap and stopped at that count: continue and close it
        with the first frame after the gap as its final weight.
        """
        probe = res.probe
        if probe is None or timer < probe[1]:
            res.probe = (t, timer, weight, flow)
            return None
        if timer > probe[1]:
            res.probe = None
            self._start(t - timer * TIMER_UNIT_SECONDS)
            self._last_timer_change = t
            return None
        if t - probe[0] < TIMER_STALL_SECONDS:
            return None
        probe_t, probe_timer, probe_w, probe_f = probe
        self._reopen(res, t)
        self._last_timer_change = None
        self._stop(
            min(probe_t, self._t0 + (probe_timer - self._base) * TIMER_UNIT_SECONDS)
        )
        self._gap_tail(probe_t, probe_w, probe_f)
        return self.tick(t)

    def _gap_tail(self, t: float, weight: float, flow: float) -> None:
        """First frame after a gap that held the stop and the whole tail.

        Its weight is the yield (drips done), recorded at the tail end; a
        lift is cut off by ``_cut_at_lift`` as usual.
        """
        end = self._stop_t + self._tail_seconds
        if t > end:
            self._record(end - 1e-6, weight, flow)

    def _feed_parked(
        self, t: float, weight: float, flow: float, timer: int | None
    ) -> tuple[BrewResult | None, bool]:
        """First samples after a reconnect. Returns (result, fully handled)."""
        if timer is None or self._park_timer is None:
            if self._park_timer is None and timer is not None:
                # Nothing to compare against: treat like a timer reset.
                self._stop_t = max(self._park_sample_t or self._t0, self._t0)
                return self._finish(truncated=True, resumable=False), False
            return self.tick(t), True
        if timer < self._park_timer:
            # Timer was reset while we were away: the brew ended in the gap.
            self._stop_t = max(self._park_sample_t or self._t0, self._t0)
            return self._finish(truncated=True, resumable=False), False
        implied_start = t - (timer - self._base) * TIMER_UNIT_SECONDS
        self.state = BREWING
        self._last_timer_change = None
        self._park_timer = None
        if abs(implied_start - self._t0) <= RESUME_TOLERANCE_SECONDS:
            # Same timer: still running, or stopped just before this frame.
            # Running once a later frame shows a higher count.
            self._probe = (t, timer)
            return None, False
        # Another start implied: the timer stopped (frozen) during the gap at
        # its value - or the frames were held back (a backlog, the timer
        # ran on; BacklogClock). Decided by the next frames: a higher count
        # = it runs; the same count for longer than a count (plus the
        # lateness margin) or a lower one = it stopped at this value. Until
        # then the brew stays brewing, so the cut rule keeps checking
        # (0.7.0b6; before, it was stopped at once and resumed two counts
        # later).
        stop_t = min(t, self._t0 + (timer - self._base) * TIMER_UNIT_SECONDS)
        self._gap_frame = (t, timer, stop_t)
        last = self._last_weight
        lifted = weight < 0 or (last is not None and weight < last - TAIL_DROP_GRAMS)
        self._gap_pending = None if lifted else (t, weight, flow)
        return None, False

    def _gap_backlog(self, t: float, timer: int | None) -> bool:
        """After a gap with another implied start: True if this frame shows
        the timer ran on (a higher count: backlog or running); a lower
        count, or the same one for longer than BACKLOG_PAUSE_SECONDS (also
        on a tick, ``timer`` None), stops the brew at the gap frame's count
        with the gap tail."""
        frame = self._gap_frame
        if frame is None or self.state != BREWING:
            self._gap_frame = self._gap_pending = None
            return False
        gap_t, gap_timer, stop_t = frame
        if timer is not None and timer > gap_timer:
            self._gap_frame = self._gap_pending = None
            self._last_timer_change = t
            return True
        if timer is not None and timer == gap_timer and t - gap_t <= BACKLOG_PAUSE_SECONDS:
            return False  # not decided yet
        if timer is None and t - gap_t <= BACKLOG_PAUSE_SECONDS:
            return False
        pending, self._gap_pending, self._gap_frame = self._gap_pending, None, None
        last_timer = self._last_timer
        self._last_timer = gap_timer  # the stop value
        self._stop(stop_t)
        self._last_timer = last_timer
        if pending is not None:
            self._gap_tail(*pending)
        return False

    def _park(self, t: float) -> None:
        self.state = PARKED
        self._parked_at = t
        self._park_timer = self._last_timer
        self._park_sample_t = self._last_sample_t
        self._probe = None

    def _resume_candidate(self, t: float) -> _ResumeCandidate | None:
        res = self._resume
        if res is not None and t - res.stop_t > self.config.resume_window_seconds:
            self._resume = res = None
        return res

    @staticmethod
    def _same_start(res: _ResumeCandidate, t: float, timer: int) -> bool:
        implied = t - (timer - res.base) * TIMER_UNIT_SECONDS
        return abs(implied - res.t0) <= RESUME_TOLERANCE_SECONDS

    def _reopen(
        self, res: _ResumeCandidate, t: float, probe_timer: int | None = None
    ) -> None:
        """Continue the last finished brew (same start, dose and profile).

        ``probe_timer``: reopened after a gap; running only once the timer
        counts beyond it.
        """
        self._reset_brew()
        self._resume = None
        self._pending_running = False
        self._next_base = 0
        self.state = BREWING
        self.last_discard_reason = None
        self.last_discard_code = None
        self._t0 = res.t0
        self._base = res.base
        self._dose_plateau = res.dose_plateau
        self._reopened = res.accepted
        for point in res.points:
            self._put(point)
            self._last_weight = point[1]
        self._burst_at = len(self._samples)
        last = self._last_clock
        if last is not None and last[0] == res.t0 and last[1] == res.base:
            self._clock = last[2]
        if res.points:
            self._clock.plain(res.points[-1][0])
            self._final_rel = res.points[-1][0]
        last_rel = res.points[-1][0] if res.points else -1.0
        self.plateau.clear_window()
        for sample in self._preroll:
            if sample[0] - self._t0 > last_rel:
                self._record(*sample)
        self._preroll.clear()
        if probe_timer is None:
            self._last_timer_change = t
        else:
            self._probe = (t, probe_timer)

    def _cup_lifted(self, weight: float) -> bool:
        if weight < 0:
            return True
        return self._tail_max is not None and weight < self._tail_max - TAIL_DROP_GRAMS

    def _expected_stop(self, t: float) -> float:
        if self._probe is not None:
            # Frozen at the probe count: it stopped when that count was shown.
            probe_t, probe_timer = self._probe
            count_t = self._t0 + (probe_timer - self._base) * TIMER_UNIT_SECONDS
            return min(t, probe_t, count_t)
        if self._last_timer_change is None:
            return t
        return min(t, self._last_timer_change + TIMER_UNIT_SECONDS)

    def _start(self, t0: float, base: int = 0) -> None:
        self._reset_brew()
        self._resume = None
        self._pending_running = False
        self._next_base = 0
        self.state = BREWING
        self.last_discard_reason = None
        self.last_discard_code = None
        self._t0 = t0
        self._base = base
        # Only peek: the dose is consumed when the brew is accepted.
        self._dose_plateau = self.plateau.peek(t0)
        self.plateau.clear_window()
        for sample in self._preroll:
            if sample[0] >= t0:
                self._record(*sample)
        self._preroll.clear()

    def _stop(self, stop_t: float) -> None:
        self.state = TAIL
        self._stop_t = max(stop_t, self._t0)
        self._stop_timer = self._last_timer
        self._tail_max = self._last_weight
        self._probe = None
        # The stop may be set back up to one count; the timer surely still
        # ran at its last observed advance, so the cup was still on then.
        ref = self._last_timer_change
        self._stop_ref_t = self._stop_t if ref is None else min(ref, self._stop_t)

    def _put(self, point: tuple[float, float, float]) -> None:
        """Store a sample (point relative to the start) in its bucket."""
        key = int(point[0] / PROFILE_RESOLUTION)
        self._samples.append(point)
        self._buckets[key] = point
        self._last_key = max(self._last_key, key)

    def _record(self, t: float, weight: float, flow: float) -> None:
        """A sample without a timer count (preroll, gap tail): at ``t``."""
        rel = t - self._t0
        if rel < 0:
            return
        self._put((rel, weight, flow))
        self._clock.plain(rel)
        self._finalize(self._clock.finalized)
        self._final_rel = max(self._final_rel, rel)
        self._burst_at = len(self._samples)
        self._weight_seen(weight)

    def _weight_seen(self, weight: float) -> None:
        self._last_weight = weight
        if self.state == TAIL:
            self._tail_max = (
                weight if self._tail_max is None else max(self._tail_max, weight)
            )

    def _record_frame(
        self,
        t: float,
        weight: float,
        flow: float,
        timer: int | None,
        mono: float,
    ) -> None:
        """A sensor frame: at its scale time if it came in a backlog
        (``BacklogClock``, on the monotonic clock ``mono``), else at its
        arrival ``t``."""
        rel = t - self._t0
        if rel < 0:
            return
        clock = self._clock
        count = None if timer is None else timer - self._base
        frames = [*clock.pending]  # placed again with the new frame
        final = clock.add(mono, rel, weight, flow, count)
        frame = clock.pending[-1]
        frames.append(frame)
        old = self._samples[self._burst_at :]
        new = []
        floor = self._final_rel
        for f in frames:
            floor = max(floor, f.placed_rel)  # brew clock order kept
            new.append((floor, f.weight, f.flow))
        self._weight_seen(weight)
        # For the cut rule: never earlier than what it got before.
        provisional = frame.rel - (frame.arrival - clock.provisional(frame))
        self.last_placed = max(self._data_floor, self._t0 + provisional)
        self.last_lateness = clock.lateness(mono, count)
        if old == new[: len(old)]:
            self._put(new[-1])  # the earlier pending frames stay where they are
            if int(new[-1][0] / PROFILE_RESOLUTION) < self._last_key:
                self._mark_revised(int(new[-1][0] / PROFILE_RESOLUTION))
        else:
            # Re-placed: rebuild the buckets from the lowest one touched.
            del self._samples[self._burst_at :]
            self._samples.extend(new)
            lowest = min(p[0] for p in (*old, *new))
            self._rebucket(int(lowest / PROFILE_RESOLUTION))
        if final:
            self._final_rel = max(self._final_rel, new[final - 1][0])
            self._finalize(clock.finalized)
        self._burst_at += final

    def _finalize(self, frames: list[_Frame]) -> None:
        """Frames placed for good: backlog ones into ``backlog_spans``."""
        for f in frames:
            if f.backlog:
                self._note_backlog(self._t0 + f.placed_rel)

    def _note_backlog(self, t: float) -> None:
        spans = self.backlog_spans
        for i, (lo, hi) in enumerate(spans):
            if lo - 1.0 <= t <= hi + 1.0:
                spans[i] = (min(lo, t), max(hi, t))
                return
        spans.append((t, t))

    def _mark_revised(self, key: int) -> None:
        if self._revised is None or key < self._revised:
            self._revised = key

    def _rebucket(self, key: int) -> None:
        """Rebuild the buckets from ``key`` on out of the samples."""
        if key < self._last_key:
            self._mark_revised(key)
        for k in [k for k in self._buckets if k >= key]:
            del self._buckets[k]
        for point in self._samples:
            k = int(point[0] / PROFILE_RESOLUTION)
            if k >= key:
                self._buckets[k] = point
        self._last_key = max(self._buckets, default=-1)

    def data_time(self, t: float) -> float:
        """Time of the newest frame (arrival ``t``) for the cut rule, the
        flow and the pump throttle: a late frame of a running brew in its
        timer second (as far as known when it came), else ``t``; within a
        brew never earlier than the time given before (they need a clock
        that does not go back). Outside a brew simply ``t``."""
        if not self.active:
            return t  # no brew: the arrival (a wall clock step is not ours)
        placed = self.last_placed
        if placed is None or self.last_sample is None or self.last_sample[0] != t:
            placed = t
        placed = max(self._data_floor, placed)
        self._data_floor = placed
        return placed

    def pop_revision(self) -> int | None:
        """Lowest bucket changed after newer buckets existed since the last
        call (backlog frames placed back in time), or None; clears it."""
        key, self._revised = self._revised, None
        return key

    def backlog_near(
        self, brew_start: float | None, start: float, end: float
    ) -> bool:
        """A backlog frame of the brew that started at ``brew_start`` (the
        active one, or the one finished last) was placed within [start,
        end]. Only frames placed for good count."""
        spans: list[tuple[float, float]] = []
        if self.active and self._t0 == brew_start:
            spans = self.backlog_spans
        elif self.finished_backlog is not None and self.finished_backlog[0] == brew_start:
            spans = self.finished_backlog[1]
        return any(lo <= end and hi >= start for lo, hi in spans)

    def _discard(self, code: str, reason: str) -> None:
        self.last_discard_code = code
        self.last_discard_reason = reason

    def _finish(
        self, truncated: bool = False, resumable: bool = True
    ) -> BrewResult | None:
        cfg = self.config
        end = self._stop_t + (0.0 if truncated else self._tail_seconds)
        points = [p for _, p in sorted(self._buckets.items()) if p[0] <= end - self._t0]
        points = self._cut_at_lift(points)
        duration = round(self._stop_t - self._t0, 1)
        result: BrewResult | None = None
        if not points:
            self._discard(DISCARD_NO_SAMPLES, "no samples")
        else:
            yield_g = round(points[-1][1], 1)
            if duration < cfg.min_duration:
                self._discard(
                    DISCARD_TOO_SHORT, f"duration {duration}s < {cfg.min_duration}s"
                )
            elif yield_g < cfg.min_yield:
                self._discard(DISCARD_LOW_YIELD, f"yield {yield_g}g < {cfg.min_yield}g")
            else:
                plateau = self._dose_plateau
                result = BrewResult(
                    start=self._t0,
                    duration=duration,
                    dose=plateau.weight if plateau else None,
                    yield_g=yield_g,
                    profile=build_profile(points),
                    updated=self._reopened,
                    truncated=truncated,
                )
                self.plateau.consume(plateau)
        stop_timer = self._stop_timer
        if stop_timer is None:
            stop_timer = self._park_timer
        if stop_timer is not None and self._last_timer is not None:
            # Counts that came in after the stop (late frames, a gap) without
            # the timer running on: it stopped at the value it shows now.
            stop_timer = max(stop_timer, self._last_timer)
        self._resume = None
        if resumable and self._resumable and points and stop_timer:
            self._resume = _ResumeCandidate(
                t0=self._t0,
                stop_t=self._stop_t,
                stop_timer=stop_timer,
                dose_plateau=self._dose_plateau,
                points=points,
                accepted=result is not None or self._reopened,
                # Closed without seeing the timer stop (BLE gap): a reset
                # in the gap cannot be ruled out.
                observed=not truncated,
                base=self._base,
            )
        self.state = IDLE
        # A continued brew (same start) keeps its scale clock.
        self._last_clock = (self._t0, self._base, self._clock)
        self._finalize(self._clock.pending)
        self.finished_backlog = (self._t0, list(self.backlog_spans))
        self._reset_brew()
        self.plateau.suppress()
        return result


    def _cut_at_lift(
        self, points: list[tuple[float, float, float]]
    ) -> list[tuple[float, float, float]]:
        """Drop everything from the first sign of a lifted cup after the stop.

        The stop is often noticed late (stall check), so a lift can already
        be recorded as brewing: scanning from one count before the last
        moment the timer still ran, a sample below zero or more than TAIL_DROP_GRAMS under the
        running maximum ends the brew; the yield is the weight before it.
        """
        ref_t = self._stop_ref_t
        if ref_t is None:
            ref_t = self._stop_t
        # The last observed count can itself be late (BLE), so the scan
        # starts one count earlier: a lift right after the stop is caught
        # even if it was recorded before that count arrived.
        scan_rel = ref_t - self._t0 - TIMER_UNIT_SECONDS
        peak: float | None = None
        for i, (rel, weight, _) in enumerate(points):
            if rel <= scan_rel:
                peak = weight
                continue
            if weight < 0 or (peak is not None and weight < peak - TAIL_DROP_GRAMS):
                self._stop_t = min(self._stop_t, self._t0 + rel)
                return points[:i]
            peak = weight if peak is None else max(peak, weight)
        return points

    # ------------------------------------------------------- persistence

    def export_active(self, now: float) -> dict | None:
        """The pending brew as plain data (unload/reload/stop), else None.

        That is the active brew, or (idle) the last brew while it can still
        be continued, e.g. during a pour-over pause.
        """
        if not self.active:
            res = self._resume_candidate(now)
            if res is None:
                return None
            plateau = res.dose_plateau
            return {
                "state": IDLE,
                "resume": {
                    "t0": res.t0,
                    "stop_t": res.stop_t,
                    "stop_timer": res.stop_timer,
                    "base": res.base,
                    "dose": _plateau_data(plateau),
                    "points": [list(p) for p in res.points],
                    "accepted": res.accepted,
                },
            }
        parked = self.state == PARKED
        return {
            "state": TAIL if self.state == TAIL else PARKED,
            "t0": self._t0,
            "base": self._base,
            "stop_t": self._stop_t,
            "stop_timer": self._stop_timer,
            "stop_ref_t": self._stop_ref_t,
            "timer": self._park_timer if parked else self._last_timer,
            "sample_t": self._park_sample_t if parked else self._last_sample_t,
            "parked_at": self._parked_at if parked else now,
            "dose": _plateau_data(self._dose_plateau),
            "points": [list(p) for _, p in sorted(self._buckets.items())],
            "reopened": self._reopened,
            "resumable": self._resumable,
        }

    def restore_active(self, data: dict) -> bool:
        """Take over a brew exported by ``export_active`` (new instance).

        A running brew comes back parked: the next samples decide like after
        a reconnect (same running timer = continue), the grace period is the
        backstop. A brew in its tail just runs out its tail. A continuable
        brew comes back as the resume candidate; since the timer was not
        watched meanwhile, a frame must show it still at its stop value (or
        the same start) before a count on continues it.
        """
        if self.active:
            return False
        try:
            state = data["state"]
            if state == IDLE:
                res = data["resume"]
                candidate = _ResumeCandidate(
                    t0=float(res["t0"]),
                    stop_t=float(res["stop_t"]),
                    stop_timer=int(res["stop_timer"]),
                    dose_plateau=_plateau_from(res.get("dose")),
                    points=[(float(a), float(b), float(c)) for a, b, c in res["points"]],
                    accepted=bool(res["accepted"]),
                    observed=False,
                    base=int(res.get("base", 0)),
                )
            else:
                t0 = float(data["t0"])
                points = [(float(a), float(b), float(c)) for a, b, c in data["points"]]
                plateau = _plateau_from(data.get("dose"))
                stop_t = float(data["stop_t"])
                timer = data.get("timer")
                stop_timer = data.get("stop_timer")
                sample_t = data.get("sample_t")
                stop_ref_t = data.get("stop_ref_t")
                parked_at = float(data["parked_at"])
                base = int(data.get("base", 0))
        except (KeyError, TypeError, ValueError):
            return False
        if state == IDLE:
            self._resume = candidate
            return True
        if state not in (TAIL, PARKED):
            return False
        self._reset_brew()
        self._t0 = t0
        self._base = base
        self._dose_plateau = plateau
        self._reopened = bool(data.get("reopened"))
        self._resumable = bool(data.get("resumable", True))
        for point in points:
            self._put(point)
            self._last_weight = point[1]
        self._burst_at = len(self._samples)
        if points:
            self._clock.plain(points[-1][0])
            self._final_rel = points[-1][0]
        if state == TAIL:
            self.state = TAIL
            self._stop_t = stop_t
            self._stop_timer = None if stop_timer is None else int(stop_timer)
            self._stop_ref_t = None if stop_ref_t is None else float(stop_ref_t)
            self._tail_max = self._last_weight
        else:
            self.state = PARKED
            self._parked_at = parked_at
            self._park_timer = None if timer is None else int(timer)
            self._park_sample_t = None if sample_t is None else float(sample_t)
        self._last_timer = None
        return True


def _plateau_data(plateau: Plateau | None) -> list[float] | None:
    return [plateau.weight, plateau.t_start, plateau.t_end] if plateau else None


def _plateau_from(data: list | None) -> Plateau | None:
    return Plateau(*(float(x) for x in data)) if data else None


def build_profile(
    points: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """Downsample to <= PROFILE_MAX_POINTS and round for storage.

    If the scale reported no flow at all, flow is derived from the weight.
    """
    if not points:
        return []
    span = points[-1][0]
    step = max(PROFILE_RESOLUTION, span / PROFILE_MAX_POINTS)
    if step > PROFILE_RESOLUTION:
        buckets: dict[int, tuple[float, float, float]] = {}
        for p in points:
            buckets[min(int(p[0] / step), PROFILE_MAX_POINTS - 1)] = p
        points = [p for _, p in sorted(buckets.items())]
    if all(abs(p[2]) < _FLOW_EPSILON for p in points):
        points = _derive_flow(points)
    return [(round(t, 2), round(w, 1), round(f, 1)) for t, w, f in points]


def _derive_flow(
    points: list[tuple[float, float, float]], window: float = 1.0
) -> list[tuple[float, float, float]]:
    out: list[tuple[float, float, float]] = []
    j = 0
    for t, w, _ in points:
        while points[j][0] < t - window:
            j += 1
        tj, wj, _ = points[j]
        flow = (w - wj) / (t - tj) if t - tj > 1e-6 else 0.0
        out.append((t, w, max(0.0, flow) if math.isfinite(flow) else 0.0))
    return out


# ------------------------------------------------------------ metrics
#
# Shot metrics from a stored profile ([t, weight, flow], t = s since the
# timer start). The flow is the one the brew card draws by default
# (flow_smoothing 3): the weight made monotone (pump vibration makes it fall
# back after upward jumps), then the least-squares slope over the trailing
# METRICS_FLOW_WINDOW seconds, 0 before the weight is FLOW_START_DELTA above
# its start. Same constants as the card (FLOW_START_DELTA, FLOW_MIN_POINTS).

# First drop: the weight is at least FIRST_DROP_GRAMS above the baseline and
# stays there for FIRST_DROP_SUSTAIN seconds (the profile must cover that
# second). Baseline: the median of the first FIRST_DROP_BASELINE seconds,
# at most 0 (a tared cup; above FIRST_DROP_GRAMS the cup was not tared and
# there is no first drop). Pump vibration moves the empty cup by 0.1-1.0 g;
# the first coffee shows from about 2 g.
FIRST_DROP_GRAMS = 1.5
FIRST_DROP_SUSTAIN = 1.0  # s
FIRST_DROP_BASELINE = 1.0  # s
# Live detector only: a baseline above FIRST_DROP_GRAMS but at most this,
# rising by at least LATE_START_RISE within the baseline second, is coffee
# already in the cup (the scale timer started after the first drop), not
# an untared cup.
LATE_START_MAX_GRAMS = 20.0
LATE_START_RISE = 0.5
FLOW_START_DELTA = 1.0  # g, the card's FLOW_START_DELTA
METRICS_FLOW_WINDOW = 3.0  # s, the card's default flow_smoothing
_METRICS_MIN_POINTS = 3
# Main phase: from 20 % to 90 % of the final (monotone) weight (the ramp of
# the first drops and the end of the shot left out).
MAIN_PHASE_LOW = 0.2
MAIN_PHASE_HIGH = 0.9


def monotone_weights(weights: list[float]) -> list[float]:
    """Running maximum: a drop is ignored, the last maximum held."""
    out: list[float] = []
    top = -math.inf
    for w in weights:
        top = max(top, w)
        out.append(top)
    return out


def smoothed_flow(
    times: list[float], weights: list[float], window: float = METRICS_FLOW_WINDOW
) -> list[float]:
    """Least-squares slope of ``weights`` over [t - window, t] (>= 0).

    The brew card's ``smoothedFlow``: 0 until the weight is more than
    FLOW_START_DELTA above its first value and with fewer than three points
    in the window.
    """
    n = len(times)
    out = [0.0] * n
    if not n:
        return out
    first = next(
        (i for i in range(n) if weights[i] > weights[0] + FLOW_START_DELTA), n
    )
    lo = 0
    for i in range(first, n):
        t = times[i]
        while lo < i and times[lo] < t - window - 1e-9:
            lo += 1
        count = i - lo + 1
        if count < _METRICS_MIN_POINTS:
            continue
        ts = times[lo : i + 1]
        ws = weights[lo : i + 1]
        mt = sum(ts) / count
        mw = sum(ws) / count
        den = sum((x - mt) ** 2 for x in ts)
        num = sum((x - mt) * (y - mw) for x, y in zip(ts, ws))
        out[i] = max(0.0, num / den) if den > 0 else 0.0
    return out


def first_drop_time(points: list) -> float | None:
    """Seconds from the timer start to the first sustained drop, or None."""
    if not points:
        return None
    t0 = float(points[0][0])
    baseline = median(
        float(p[1]) for p in points if float(p[0]) <= t0 + FIRST_DROP_BASELINE
    )
    if baseline > FIRST_DROP_GRAMS:
        return None  # container not tared: no reference for the first drop
    threshold = min(0.0, baseline) + FIRST_DROP_GRAMS
    n = len(points)
    for i in range(n):
        t = float(points[i][0])
        if float(points[i][1]) < threshold:
            continue
        held = True
        covered = False
        for j in range(i + 1, n):
            tj = float(points[j][0])
            if tj > t + FIRST_DROP_SUSTAIN:
                covered = True
                break
            if float(points[j][1]) < threshold:
                held = False
                break
            if tj >= t + FIRST_DROP_SUSTAIN:
                covered = True
        if held and covered:
            return round(max(0.0, t), 1)
    return None


class FirstDropDetector:
    """``first_drop_time`` live, frame by frame (pump throttle, 0.6.0).

    Same rule and constants: the baseline is the median of the frames in
    the first FIRST_DROP_BASELINE seconds after the first frame (at most 0;
    above FIRST_DROP_GRAMS the cup was not tared: no first drop at all), the
    first drop is the first frame at least FIRST_DROP_GRAMS above it from
    which on every frame stays there until one at or beyond
    FIRST_DROP_SUSTAIN later. Confirmed only then, i.e. FIRST_DROP_SUSTAIN
    after the drop. Times are seconds since the brew start.
    """

    def __init__(self) -> None:
        """Start empty (one detector per brew)."""
        self._t0: float | None = None
        self._early: list[float] = []
        self._threshold: float | None = None
        self.untared = False
        self.late_start = False
        self._pending: list[tuple[float, float]] = []
        self._candidate: float | None = None
        self.time: float | None = None

    @property
    def tared(self) -> bool:
        """The baseline is known and the cup was tared (pump profiles use
        the weight only then, 0.7.0)."""
        return self._threshold is not None and not self.untared

    def feed(self, t: float, weight: float) -> float | None:
        """Add a frame; the first drop time once confirmed (then stays)."""
        if self.time is not None or self.untared:
            return self.time
        if self._t0 is None:
            self._t0 = t
        if self._threshold is None:
            if t <= self._t0 + FIRST_DROP_BASELINE:
                self._early.append(weight)
                self._pending.append((t, weight))
                return None
            baseline = median(self._early)
            if baseline > FIRST_DROP_GRAMS:
                # No reference for the first drop. Either the cup was not
                # tared, or the scale timer started after the first drops
                # (light weight, already rising in the first second).
                self.untared = True
                rise = self._early[-1] - self._early[0]
                self.late_start = (
                    baseline <= LATE_START_MAX_GRAMS and rise >= LATE_START_RISE
                )
                return None
            self._threshold = min(0.0, baseline) + FIRST_DROP_GRAMS
            pending, self._pending = self._pending, []
            for pt, pw in pending:
                self._check(pt, pw)
                if self.time is not None:
                    return self.time
        self._check(t, weight)
        return self.time

    def _check(self, t: float, weight: float) -> None:
        assert self._threshold is not None
        if weight < self._threshold:
            self._candidate = None
            return
        if self._candidate is None:
            self._candidate = t
        elif t >= self._candidate + FIRST_DROP_SUSTAIN:
            self.time = round(max(0.0, self._candidate), 1)


def brew_metrics(points: list) -> dict[str, float | None] | None:
    """Time to first drop, peak flow, main-phase mean flow and its CV.

    ``None`` without a profile. Values that cannot be computed (no drop,
    too few points in the main phase) are None.
    """
    if not points:
        return None
    times = [float(p[0]) for p in points]
    weights = monotone_weights([float(p[1]) for p in points])
    flow = smoothed_flow(times, weights)
    metrics: dict[str, float | None] = {
        "first_drop": first_drop_time(points),
        "peak_flow": round(max(flow), 1) if any(flow) else None,
        "mean_flow": None,
        "flow_cv": None,
    }
    base = weights[0]
    gain = weights[-1] - base
    if gain <= FLOW_START_DELTA:
        return metrics
    low = base + MAIN_PHASE_LOW * gain
    high = base + MAIN_PHASE_HIGH * gain
    i_lo = next(i for i, w in enumerate(weights) if w >= low)
    i_hi = next(i for i, w in enumerate(weights) if w >= high)
    if times[i_hi] - times[i_lo] > 1e-6:
        metrics["mean_flow"] = round(
            (weights[i_hi] - weights[i_lo]) / (times[i_hi] - times[i_lo]), 2
        )
    phase = flow[i_lo : i_hi + 1]
    if len(phase) >= _METRICS_MIN_POINTS:
        avg = sum(phase) / len(phase)
        if avg > 0:
            std = math.sqrt(sum((f - avg) ** 2 for f in phase) / len(phase))
            metrics["flow_cv"] = round(std / avg, 2)
    return metrics


def correct_dose(brew: dict, dose: float) -> float:
    """Set a stored brew's dose manually and recompute its ratio in place.

    Returns how much the coffee total grows. The total is a total_increasing
    sensor (a decrease would read as a meter reset), so each brew remembers
    the most it ever added (``counted_dose``) and only growth beyond that
    counts: 18 -> 17 -> 18 adds nothing the second time.
    """
    dose = round(dose, 1)
    counted = brew.get("counted_dose")
    if counted is None:
        counted = brew.get("dose") or 0.0
    delta = max(0.0, dose - counted)
    brew["dose"] = dose
    brew["dose_source"] = "manual"
    brew["ratio"] = compute_ratio(dose, brew.get("yield"))
    brew["counted_dose"] = round(max(counted, dose), 1)
    return round(delta, 1)

