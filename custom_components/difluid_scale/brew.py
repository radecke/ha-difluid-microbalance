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
        self._reset_brew()

    def _reset_brew(self) -> None:
        self._t0 = 0.0
        self._stop_t = 0.0
        self._dose_plateau: Plateau | None = None
        self._buckets: dict[int, tuple[float, float, float]] = {}
        self._last_key = -1
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
        # After a gap the timer value of the first frame, (t, timer): the
        # timer only counts as running once a later frame shows a higher one
        # (it may have stopped just before that frame).
        self._probe: tuple[float, int] | None = None

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
        self, t: float, weight: float, flow: float, timer: int | None
    ) -> BrewResult | None:
        """Feed one sensor sample; return a brew when one completes."""
        prev_timer, self._last_timer = self._last_timer, timer
        prev_sample_t, self._last_sample_t = self._last_sample_t, t
        self.last_sample = (t, weight, flow, timer)
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
        self._record(t, weight, flow)
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
        if self.state == TAIL and t >= self._stop_t + self.config.tail_seconds:
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
        end = self._stop_t + self.config.tail_seconds
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
        # Timer stopped (frozen) during the gap: it stopped at its value.
        self._stop(min(t, self._t0 + (timer - self._base) * TIMER_UNIT_SECONDS))
        if not self._cup_lifted(weight):
            self._gap_tail(t, weight, flow)
        return None, False

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
            key = int(point[0] / PROFILE_RESOLUTION)
            self._buckets[key] = point
            self._last_key = max(self._last_key, key)
            self._last_weight = point[1]
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

    def _record(self, t: float, weight: float, flow: float) -> None:
        rel = t - self._t0
        if rel < 0:
            return
        key = int(rel / PROFILE_RESOLUTION)
        self._buckets[key] = (rel, weight, flow)
        self._last_key = max(self._last_key, key)
        self._last_weight = weight
        if self.state == TAIL:
            self._tail_max = (
                weight if self._tail_max is None else max(self._tail_max, weight)
            )

    def _discard(self, code: str, reason: str) -> None:
        self.last_discard_code = code
        self.last_discard_reason = reason

    def _finish(
        self, truncated: bool = False, resumable: bool = True
    ) -> BrewResult | None:
        cfg = self.config
        end = self._stop_t + (0.0 if truncated else cfg.tail_seconds)
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
            key = int(point[0] / PROFILE_RESOLUTION)
            self._buckets[key] = point
            self._last_key = max(self._last_key, key)
            self._last_weight = point[1]
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
