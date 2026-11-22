"""Adaptive stop-at-target model (pure maths, no HA imports).

The weight still rises after the stop switch is turned off: the signal
needs time to travel (scale -> BLE -> HA -> switch -> pump stop) and the
puck drips afterwards. The overshoot is modelled as

    overshoot = c + tau * flow_at_cut

- ``tau`` (s): effective latency, including any constant lag of the flow
  estimate;
- ``c`` (g): drip after the pump stopped.

A brew is cut when ``weight + c + tau * flow >= target``. After every cut
brew the settled yield gives one sample (x = flow at cut, y = final yield -
weight at cut); ``theta = (tau, c)`` is learned by recursive least squares
with a forgetting factor, so the model follows slow changes (grind, puck,
machine temperature) and forgets old shots.

Times are plain floats in seconds on the caller's clock, so everything can
be driven with synthetic streams in tests.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Any

# Forgetting factor: effective memory ~ 1 / (1 - LAMBDA) ~ 14 shots.
FORGETTING = 0.93
TAU_PRIOR = 1.0  # s
C_PRIOR = 0.3  # g
# Initial covariance (relative to the measurement noise): moderate, the
# first shots move the estimate noticeably but not all the way.
P_PRIOR = 1.0
# Windup guard: with a forgetting factor, directions the data do not excite
# (flow at cut nearly constant) grow by 1/LAMBDA per shot. The covariance
# is scaled back when its trace exceeds this.
P_MAX_TRACE = 4.0 * P_PRIOR
# Below this many accepted samples only ``tau`` is learned (``c`` stays at
# its prior). The overshoot is dominated by flow x latency, so a wrong tau
# prior cannot be absorbed by ``c`` (it would hit C_MIN and stall); with the
# few, similar flows of the first shots the two cannot be separated anyway.
MIN_FULL_SAMPLES = 3
# Prior phase: the residual used for one step is limited to this (robust).
PRIOR_STEP_CLIP = 2.0  # g
TAU_MIN, TAU_MAX = 0.0, 3.0
C_MIN, C_MAX = -0.5, 3.0
# Plausible overshoot range for a learning sample (cup bumped/lifted).
OVERSHOOT_MIN, OVERSHOOT_MAX = 0.0, 5.0
# Plausible flow (g/s): espresso runs at 1-3 g/s, the fastest stored shot
# peaked at 4.7 g/s (2026-11-21). Above this the flow estimate is broken
# (a frame the outlier filter could not catch); it never drives a cut
# (the cut then falls back to weight + c) and such a shot is not learned.
FLOW_MAX = 8.0
# Outlier frames: a frame more than JUMP_GRAMS below the last accepted
# weight, or more than JUMP_GRAMS + FLOW_MAX x dt above it, is held back;
# the next frame decides (back on the old level: the held frame is dropped;
# near the held one: a real level change, e.g. the cup lifted). The weight
# falls back by 0.3-3.2 g after jumps during a brew (pump vibration), which
# stays inside; same threshold as the cup lift in the brew tracker
# (brew.TAIL_DROP_GRAMS).
JUMP_GRAMS = 5.0
# The upward allowance grows with the time since the last accepted frame
# only up to this (after a BLE gap a single bad frame must not pass).
JUMP_DT_MAX = 1.0  # s
# A frame this far above the robust line through the flow window does not
# trigger the cut on its own (the next frame decides); falls of up to 3.2 g
# are the normal vibration, rises above the line are not.
CUT_RESIDUAL_MAX = 3.0  # g
# Fewer accepted frames than this in the flow window (BLE gap): the flow is
# not trusted for learning.
FLOW_MIN_FRAMES = 4

# Sample interval for the cut anticipation (half an interval ahead):
# running median of the last INTERVAL_COUNT intervals of the active brew.
INTERVAL_COUNT = 15
INTERVAL_MIN_COUNT = 3
INTERVAL_FALLBACK = 0.3  # s
INTERVAL_MIN, INTERVAL_MAX = 0.1, 1.0  # s

# Flow estimate: Theil-Sen slope (median of all pairwise slopes) of the
# accepted frames in this window. 2 s hold 7-10 frames at 0.2-0.3 s, so one
# or two fallen-back or jumped frames (0.3-3.2 g) cannot move the median;
# a 1 s secant (two frames only) read 3.27 g/s for a 2.3-2.7 g/s shot
# (2026-11-21 15:55). The window adds no latency to the cut (decided on
# every frame); it only lags a changing flow by ~1 s, which the learned
# tau absorbs.
FLOW_WINDOW = 2.0  # s

# Settled yield: the weight varied by at most SETTLE_SPREAD over the last
# SETTLE_SECONDS (drip below ~0.1 g/s). Given up SETTLE_TIMEOUT after the cut.
SETTLE_SECONDS = 2.0
SETTLE_SPREAD = 0.2  # g
SETTLE_TIMEOUT = 30.0  # s
# The scale sends no weight while its timer is paused (measured 2026-11-21:
# no frame for 10 s after the stop). So the timer keeps running after the
# cut until the weight has settled plus tail_seconds (late drops, brew
# 2026-11-21 15:55); then it is stopped. Backstop, not a delay: the timer
# is stopped at the latest this long after the cut.
TIMER_STOP_MAX = 20.0  # s after the cut


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def plausible_flow(flow: float) -> bool:
    """Flow (g/s) a real shot can have (see FLOW_MAX)."""
    return math.isfinite(flow) and 0.0 <= flow <= FLOW_MAX


class StopModel:
    """``overshoot = c + tau * flow``, learned by RLS with forgetting."""

    def __init__(self) -> None:
        """Start from the prior."""
        self.tau = TAU_PRIOR
        self.c = C_PRIOR
        # Covariance of (tau, c), row-major 2x2.
        self.p = [[P_PRIOR, 0.0], [0.0, P_PRIOR]]
        self.count = 0

    def reset(self) -> None:
        """Forget everything learned."""
        self.__init__()

    # --------------------------------------------------------- prediction

    def overshoot(self, flow: float) -> float:
        """Predicted weight gain after a cut at ``flow`` (g/s), >= 0."""
        return max(0.0, self.c + self.tau * max(0.0, flow))

    def should_cut(
        self, weight: float, flow: float, target: float, lead: float = 0.0
    ) -> bool:
        """True when cutting now lands the settled weight on ``target``.

        ``lead`` (s) anticipates the sampling: the cut can only happen at a
        sample, so the rule looks ``lead`` seconds ahead (half a sample
        interval, see IntervalEstimator). Fixed, not learned: tau stays the
        latency after the cut sample.
        """
        return weight + self.overshoot(flow) + max(0.0, flow) * lead >= target

    # ----------------------------------------------------------- learning

    @staticmethod
    def plausible(overshoot: float, flow: float = 0.0) -> bool:
        """Overshoot and flow at the cut inside the accepted range (else an
        outlier)."""
        return (
            OVERSHOOT_MIN <= overshoot <= OVERSHOOT_MAX
            and plausible_flow(flow)
        )

    def update(self, flow: float, overshoot: float) -> bool:
        """Learn from one cut brew; False (nothing learned) for an outlier."""
        if not (math.isfinite(flow) and math.isfinite(overshoot)):
            return False
        if not self.plausible(overshoot, flow):
            return False
        x = max(0.0, flow)
        if self.count < MIN_FULL_SAMPLES:
            self._update_offset(x, overshoot)
        else:
            self._update_full(x, overshoot)
        self.tau = _clamp(self.tau, TAU_MIN, TAU_MAX)
        self.c = _clamp(self.c, C_MIN, C_MAX)
        self.count += 1
        return True

    def _update_offset(self, x: float, y: float) -> None:
        """Prior phase: scalar RLS on ``tau`` (regressor = flow), c fixed."""
        p_tt = self.p[0][0]
        residual = _clamp(
            y - self.tau * x - self.c, -PRIOR_STEP_CLIP, PRIOR_STEP_CLIP
        )
        gain = p_tt * x / (FORGETTING + x * x * p_tt)
        self.tau += gain * residual
        self.p[0][0] = (1.0 - gain * x) * p_tt / FORGETTING

    def _update_full(self, x: float, y: float) -> None:
        """Two-parameter RLS step with regressor (flow, 1)."""
        p = self.p
        phi = (x, 1.0)
        p_phi = (
            p[0][0] * phi[0] + p[0][1] * phi[1],
            p[1][0] * phi[0] + p[1][1] * phi[1],
        )
        denom = FORGETTING + phi[0] * p_phi[0] + phi[1] * p_phi[1]
        gain = (p_phi[0] / denom, p_phi[1] / denom)
        residual = y - (self.tau * x + self.c)
        self.tau += gain[0] * residual
        self.c += gain[1] * residual
        # P = (P - K phi^T P) / lambda; phi^T P = p_phi^T (P symmetric)
        new = [
            [
                (p[i][j] - gain[i] * p_phi[j]) / FORGETTING
                for j in range(2)
            ]
            for i in range(2)
        ]
        # Keep it symmetric against rounding.
        off = (new[0][1] + new[1][0]) / 2
        new[0][1] = new[1][0] = off
        trace = new[0][0] + new[1][1]
        if trace > P_MAX_TRACE:
            scale = P_MAX_TRACE / trace
            new = [[v * scale for v in row] for row in new]
        self.p = new

    # -------------------------------------------------------- persistence

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the store."""
        return {
            "tau": self.tau,
            "c": self.c,
            "p": [list(row) for row in self.p],
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, data: Any) -> StopModel:
        """Restore; invalid data gives the prior."""
        model = cls()
        try:
            tau = float(data["tau"])
            c = float(data["c"])
            p = [[float(v) for v in row] for row in data["p"]]
            count = int(data["count"])
        except (KeyError, TypeError, ValueError):
            return model
        if (
            len(p) != 2
            or any(len(row) != 2 for row in p)
            or not all(math.isfinite(v) for v in (tau, c, *p[0], *p[1]))
            or count < 0
        ):
            return model
        model.tau = _clamp(tau, TAU_MIN, TAU_MAX)
        model.c = _clamp(c, C_MIN, C_MAX)
        model.p = p
        model.count = count
        return model


class SampleFilter:
    """Holds back single implausible weight frames (see JUMP_GRAMS).

    ``feed`` returns the frames to use, in order, as (t, weight, shift):
    normally just the new frame; nothing while a frame is held back; the
    held frame (``shift`` True) and the new one once the new level is
    confirmed by a second frame. A held frame followed by one back on the
    old level is dropped. ``disturbed_t`` is the time of the last dropped
    frame or confirmed level change (None: none so far); ``level_t`` only
    that of the last confirmed level change. A dropped frame is simply left
    out of the flow estimate, so it does not spoil a learning sample; a
    level change (cup touched or lifted) does.
    """

    def __init__(self, jump: float = JUMP_GRAMS, flow_max: float = FLOW_MAX) -> None:
        """Initialize."""
        self.jump = jump
        self.flow_max = flow_max
        self._last: tuple[float, float] | None = None
        self._held: tuple[float, float] | None = None
        self.dropped = 0
        self.disturbed_t: float | None = None
        self.level_t: float | None = None

    def clear(self) -> None:
        """Forget everything."""
        self.__init__(self.jump, self.flow_max)

    @property
    def held(self) -> tuple[float, float] | None:
        """The frame held back, waiting for the next one."""
        return self._held

    def _fits(self, ref: tuple[float, float], t: float, weight: float) -> bool:
        ref_t, ref_w = ref
        rise = self.jump + self.flow_max * _clamp(t - ref_t, 0.0, JUMP_DT_MAX)
        return ref_w - self.jump <= weight <= ref_w + rise

    def feed(self, t: float, weight: float) -> list[tuple[float, float, bool]]:
        """Add a frame; return the frames to use (see class doc)."""
        if not math.isfinite(weight):
            return []
        last = self._last
        if last is None:
            self._last = (t, weight)
            return [(t, weight, False)]
        if self.disturbed_t is not None and self.disturbed_t > t:
            self.disturbed_t = t  # clock went back
        if self.level_t is not None and self.level_t > t:
            self.level_t = t  # clock went back
        # A clock going back counts as no time passed (dt clamped to 0):
        # the last accepted weight stays the reference.
        if self._fits(last, t, weight):
            if self._held is not None:
                self.dropped += 1
                self.disturbed_t = self._held[0]
                self._held = None
            self._last = (t, weight)
            return [(t, weight, False)]
        held = self._held
        if held is not None and self._fits(held, t, weight):
            # Second frame on the new level: a real change (cup lifted or
            # placed), not a single bad frame.
            self._held = None
            self._last = (t, weight)
            self.disturbed_t = held[0]
            self.level_t = held[0]
            return [(held[0], held[1], True), (t, weight, False)]
        if held is not None:
            self.dropped += 1  # a third level: the older held frame goes
            self.disturbed_t = held[0]
        self._held = (t, weight)
        return []


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


class FlowEstimator:
    """Weight-derived flow: Theil-Sen slope over the last FLOW_WINDOW seconds.

    The median of the slopes between all pairs of frames inside the window
    (frames already passed through ``SampleFilter``), never below 0. A
    confirmed level change (``shift``) moves the stored frames by the step,
    so the step itself is not read as flow.
    """

    def __init__(self, window: float = FLOW_WINDOW) -> None:
        """Initialize."""
        self.window = window
        self._samples: deque[tuple[float, float]] = deque()
        self._flow = 0.0

    def clear(self) -> None:
        """Forget all samples."""
        self._samples.clear()
        self._flow = 0.0

    @property
    def count(self) -> int:
        """Frames in the window."""
        return len(self._samples)

    def residual(self) -> float:
        """Newest weight minus the robust line through the window at its
        time (median intercept with the current flow); 0 with < 3 frames."""
        samples = self._samples
        if len(samples) < 3:
            return 0.0
        t, w = samples[-1]
        level = _median([ws + self._flow * (t - ts) for ts, ws in samples])
        return w - level

    def feed(self, t: float, weight: float, shift: bool = False) -> float:
        """Add a sample and return the flow at ``t`` (g/s)."""
        samples = self._samples
        if samples and t < samples[-1][0]:
            samples.clear()  # clock went back
        if shift and samples:
            last_t, last_w = samples[-1]
            step = weight - (last_w + self._flow * (t - last_t))
            for i, (ts, ws) in enumerate(samples):
                samples[i] = (ts, ws + step)
        samples.append((t, weight))
        while samples[0][0] < t - self.window:
            samples.popleft()
        points = list(samples)
        slopes = [
            (w2 - w1) / (t2 - t1)
            for i, (t1, w1) in enumerate(points)
            for t2, w2 in points[i + 1 :]
            if t2 - t1 > 1e-6
        ]
        flow = _median(slopes) if slopes else 0.0
        self._flow = max(0.0, flow) if math.isfinite(flow) else 0.0
        return self._flow


class IntervalEstimator:
    """Running median of the recent sample intervals (clamped)."""

    def __init__(self, count: int = INTERVAL_COUNT) -> None:
        """Initialize."""
        self._intervals: deque[float] = deque(maxlen=count)
        self._last_t: float | None = None

    def clear(self) -> None:
        """Forget all intervals (new brew)."""
        self._intervals.clear()
        self._last_t = None

    def feed(self, t: float) -> None:
        """Add a sample time."""
        if self._last_t is not None and t > self._last_t:
            self._intervals.append(t - self._last_t)
        self._last_t = t

    @property
    def interval(self) -> float:
        """Median interval; INTERVAL_FALLBACK until enough are known."""
        if len(self._intervals) < INTERVAL_MIN_COUNT:
            return INTERVAL_FALLBACK
        ordered = sorted(self._intervals)
        mid = len(ordered) // 2
        median = (
            ordered[mid]
            if len(ordered) % 2
            else (ordered[mid - 1] + ordered[mid]) / 2
        )
        return _clamp(median, INTERVAL_MIN, INTERVAL_MAX)

    @property
    def lead(self) -> float:
        """Half the median interval: how far the cut rule looks ahead."""
        return self.interval / 2


def profile_cut(
    points: list[list[float]] | list[tuple[float, ...]], offset: float
) -> tuple[float, float, float, bool] | None:
    """Weight and flow a cut at ``offset`` (s since start) would have seen.

    ``points`` are a stored profile ([t, weight, flow], t ascending). The
    profile is replayed through the same SampleFilter and FlowEstimator as
    the live cut; the last accepted point at or before ``offset`` is the cut
    sample. Returns (t, weight, flow, clean) or None when no point lies at
    or before ``offset``; ``clean`` is False when a frame was dropped (or
    the level changed) within FLOW_WINDOW before the cut, the point at the
    cut itself was held back, or the window held fewer than FLOW_MIN_FRAMES
    frames (gap). A stored profile keeps one point per 0.25 s, so a bad
    frame overwritten in its bucket is not seen here: live and service can
    differ in ``clean`` (the flow itself is clean then).
    """
    filt = SampleFilter()
    est = FlowEstimator()
    cut: tuple[float, float, float] | None = None
    for point in points:
        t, weight = float(point[0]), float(point[1])
        if t > offset:
            break
        for ft, fw, shift in filt.feed(t, weight):
            cut = (ft, fw, est.feed(ft, fw, shift))
    if cut is None:
        return None
    clean = (
        filt.held is None
        and est.count >= FLOW_MIN_FRAMES
        and (filt.level_t is None or filt.level_t < cut[0] - FLOW_WINDOW)
    )
    return (*cut, clean)


class SettleDetector:
    """Finds the settled weight after a cut (drip ended)."""

    def __init__(
        self,
        seconds: float = SETTLE_SECONDS,
        spread: float = SETTLE_SPREAD,
    ) -> None:
        """Initialize."""
        self.seconds = seconds
        self.spread = spread
        self._samples: deque[tuple[float, float]] = deque()

    def feed(self, t: float, weight: float) -> float | None:
        """Add a sample; return the settled weight once it is stable."""
        samples = self._samples
        samples.append((t, weight))
        # Keep one sample at or before the window start (full coverage).
        while len(samples) > 1 and samples[1][0] <= t - self.seconds:
            samples.popleft()
        if samples[0][0] > t - self.seconds:
            return None  # window not covered yet
        weights = [w for _, w in samples]
        if max(weights) - min(weights) <= self.spread:
            return weight
        return None


@dataclass
class Cut:
    """A brew cut by the auto-stop, waiting for its settled yield."""

    start: float  # brew start (tracker clock)
    t: float
    weight: float
    flow: float
    target: float
    predicted: float  # predicted overshoot at the cut (model, no lead)
    source: str = "yield"  # "ratio" (ratio x dose) or "yield" (fixed target)
    dose: float | None = None  # dose the ratio was applied to
    lead: float = 0.0  # s looked ahead (half the sample interval)
    bean: str | None = None  # active bean profile at the cut (analysis only)
    # No frame dropped and no level change within FLOW_WINDOW before the cut
    # (else the shot is not learned: the flow may be distorted).
    clean: bool = True
