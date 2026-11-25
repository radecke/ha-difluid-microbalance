"""Pump throttle for too fast shots (pure maths, no HA imports; 0.6.0).

A shot that would end at least TRIGGER_MARGIN seconds before the target
shot time is slowed down during the brew by lowering the pump power (a
``number`` entity 0-100 %, e.g. an ESPHome PSM dimmer), so that it ends at
the target shot time.

Shot time is the time since the pump relay (the auto-stop's stop switch)
was switched on; the HA side measures it and passes it in with every frame
(``shot_time``). Frame times ``t`` are seconds since the brew start (the
scale timer start, profile t = 0) on the caller's clock.

Prediction (backtest 2026-11-23, 14 stored shots): the auto-stop will cut
at the weight ``W* = target - overshoot(Q) - Q * lead`` (the cut rule of
``StopModel.should_cut``). Two estimates of the remaining time:

- ``pow``: the weight follows a power law from the first drop,
  ``w = alpha * s ** p`` with ``s = t - (first drop - POW_ANCHOR)`` and
  ``p`` learned per installation (``ExponentModel``, start
  PREDICT_EXPONENT); the end is at ``s * (W* / w) ** (1 / p)``.
  Anchored at the first drop it covers the rising flow early on, where the
  2 s flow is often still ~0 (plateau after the first drop).
- ``trend`` (from first drop + TREND_DELAY): flow ``Q`` (Theil-Sen 2 s) that
  rises at ``a`` (Theil-Sen of the flow estimates of the last
  TREND_WINDOW s, clamped to [0, TREND_SLOPE_MAX] g/s^2); the remaining
  time solves ``w + Q r + a r^2 / 2 = W*``.

The prediction is ``pow`` until first drop + TREND_DELAY, then the later of
both (the more careful one for the trigger).

Control: from the trigger on a target weight curve runs linearly in shot
time from the weight at the trigger to W* at the target shot time. A PI
controller on the weight error gives the wanted flow; the power model only
translates a flow *ratio* into a power, always relative to the current
power: ``d_new = g^-1(g(d_now) * Q_wanted / Q_now)``. A correction is made
only once the flow window (FLOW_WINDOW) holds frames after the last change
plus SETTLE_EXTRA (the pump's and the puck's response), i.e. at most one
correction every ~3 s; the PI gains keep the loop bandwidth well below the
~0.5 rad/s that the ~2 s dead time allows.

Power model ``g(d) = Q(d) / Q(100) = (d / 100) ** gamma`` with one learned
parameter ``gamma`` (start 1: flow ~ power x flow at 100 %). A scale factor
(``k * d``) would cancel in the relative correction and could not be
learned from it; the exponent is the one-parameter shape that keeps
g(0) = 0, g(100) = 1, is monotone and invertible for gamma > 0, and can
bend both ways (a vibration pump with PSM may need a minimum power before
it pumps at all: gamma > 1). Learned by RLS with forgetting like the stop
model, in log form ``ln(Q_after / Q_before) = gamma * ln(d_after /
d_before)``, from the first large power step of a shot only, measured once
the flow window holds only frames from SETTLE_EXTRA after the step,
against the flow trend before the step extrapolated to that moment
(separates the puck's own rising flow from the pump's effect).
"""

from __future__ import annotations

from collections import deque
import math
from typing import Any

from .stop_model import (
    FLOW_MIN_FRAMES,
    FLOW_WINDOW,
    FORGETTING,
    LearningJournal,
    StopModel,
    _clamp,
    _median,
)

# ------------------------------------------------------------- prediction

# Start value of the power-law exponent: the mean of 14 shots of one
# machine (backtest 2026-11-23, SD 0.22). Every installation then follows
# its own shots (ExponentModel), bounded to EXPONENT_MIN..EXPONENT_MAX.
PREDICT_EXPONENT = 1.55
EXPONENT_MIN, EXPONENT_MAX = 1.0, 2.5
# Fit range of one brew (as in the backtest): 3 g < weight < weight at the
# end of the brew time, at least EXPONENT_MIN_POINTS profile points.
EXPONENT_FIT_LOW = 3.0  # g
EXPONENT_MIN_POINTS = 8
EXPONENT_SAMPLES_MAX = 50
POW_ANCHOR = 2.0  # s before the first drop: s = 0 of the power law
POW_MIN_WEIGHT = 0.5  # g, below this no power-law estimate
TREND_DELAY = 6.0  # s after the first drop: the trend estimate joins
TREND_WINDOW = 3.0  # s of flow estimates for the flow's slope
TREND_SLOPE_MAX = 0.25  # g/s^2
CHECK_DELAY = 3.0  # s after the first drop: first check
TRIGGER_MARGIN = 5.0  # s: trigger when predicted end <= target time - this
MIN_REMAINING = 4.0  # s: a later trigger would have no effect (log only)

# Trigger on a robust prediction: the predictions of the last
# TRIGGER_WINDOW s (at least TRIGGER_FRAMES frames) must all be at or below
# target - TRIGGER_MARGIN. A noise filter (one frame of a noisy flow
# estimate must not trigger), not a delay: ~3 frames at the scale's rate.
TRIGGER_WINDOW = 1.0  # s
TRIGGER_FRAMES = 3
# The exponent of the running shot: log w against log s from the first
# drop on (weights above LOCAL_FIT_LOW, made monotone), blended with the
# installation's learned exponent: the shot's own fit weighs
# n / (n + LOCAL_PRIOR_FRAMES) for n frames, i.e. 0.75 at the first fit
# (6 frames) and over 0.9 from 20 frames on. So this barely shrinks: the
# shot's own exponent dominates, the learned one mainly steadies the first
# frames. Real shots of one machine spread by SD 0.22 around their mean
# (1.2-1.95); one learned value alone predicts a shot with 1.3 five
# seconds too early. Compared for 2 / 6 / 10 / 20: same result on the 14
# stored shots, in the simulation 2 had by far the fewest false triggers
# (puck 1.25: 8 / 23 / 33 / 37 of 43) at the same detection rate. Frozen
# at the trigger (after it the curve is the throttle's).
LOCAL_FIT_LOW = 2.0  # g
LOCAL_FIT_MIN_FRAMES = 6
LOCAL_PRIOR_FRAMES = 2.0
# Trigger only if the prediction with the local slope CAUTION_SE standard
# errors lower is fast enough too (see ShotThrottle._fit_local).
CAUTION_SE = 2.0

# ---------------------------------------------------------------- control

SETTLE_EXTRA = 1.0  # s after a change before frames count for the flow
# Wait after a change before the next correction: the flow window must hold
# only frames from SETTLE_EXTRA after it, plus the dead time measured at
# the shot's first large step (``dead_time``; 0 until measured).
CORRECTION_WAIT = FLOW_WINDOW + SETTLE_EXTRA
# PI on the weight error (g -> g/s). Kp 0.15 1/s: a 1 g lag asks for
# 0.15 g/s more; with corrections every 3-5 s (window, settling, dead
# time) the loop stays well damped (bandwidth ~0.15 rad/s, well below
# 1 / 2 s). Ki ~ Kp^2 / 4. Chosen in the simulation (tests/throttle_sim.py:
# pump exponents 1-2.5, dead zones, 1.5 s dead time), not on a machine;
# 0.2 swung with a pump exponent of 1.4 (-1.6 s).
KP = 0.15  # 1/s
KI = 0.006  # 1/s^2
INTEGRAL_MAX = 10.0  # g*s, anti-windup bound
# From target - END_GUARD on no normal correction is made (it would act
# after the end); a shot behind its curve (or predicted late) then goes to
# full power at once.
END_GUARD = 1.0  # s, plus the measured dead time beyond the flow lag
# Hysteresis of that boost: behind the curve by at least END_BEHIND_GRAMS,
# or behind (or predicted late) on END_BEHIND_FRAMES frames in a row.
END_BEHIND_GRAMS = 0.5
END_BEHIND_FRAMES = 2
END_GUARD_DEAD_SHARE = 0.25
# A shot that hit the floor and still ended this much before the target
# time is marked unreachable (grind finer, the pump alone cannot do it).
UNREACHABLE_SLACK = 1.0  # s
UNREACHABLE_FLOOR = "floor"
UNREACHABLE_RATE = "rate_limit"
UNREACHABLE_LATE = "late_trigger"
# The first step was held at half the power because the pump model has
# fewer than TRIGGER_FREE_COUNT samples (with learning off: always). Order
# of the reasons: floor (the pump was at its minimum - neither learning nor
# a slower cadence would have helped) before trigger_cap (the start was
# held back; the later rate-limited corrections only catch up with that,
# so learning is the user's lever) before rate_limit before late_trigger.
UNREACHABLE_TRIGGER_CAP = "trigger_cap"
FLOW_MIN = 0.2  # g/s: below this no relative correction is possible
# The measured dead time counts for waits and the end boost up to this
# (it includes the flow estimate's lag; a call/PSM/pump/scale chain of
# ~3.5 s measured ~4 s in the simulation, a slow puck alone 4.2 s).
DEAD_TIME_MAX = 2.5  # s
# With learning on, the first large step holds corrections until it is
# measured, at most this long (the longest counted dead time, the settling
# and the flow window); with learning off it never holds them.
STEP_BLOCK_MAX = DEAD_TIME_MAX + 1.0 + FLOW_WINDOW
# While the pump model has fewer than LEARN_HOLD_COUNT samples the hold may
# last up to STEP_BLOCK_MAX_EARLY (a slow pump responds after ~4 s and
# needs ~7 s to be measured; without this it never learned).
STEP_BLOCK_MAX_EARLY = 8.0
LEARN_HOLD_COUNT = 4
# Until the pump model has TRIGGER_FREE_COUNT samples the trigger step goes
# at most to this share of the current power - with learning off the model
# never gets samples, so then always. With an unknown pump exponent (2
# instead of the start value 1) a step straight to the floor gave 16 %
# flow, which a slow pump never caught up (+2.7 s). Price: fast shots end
# ~0.6 s early on average in the simulation.
TRIGGER_MIN_SHARE = 0.5
TRIGGER_FREE_COUNT = 3
# One correction changes the power by at most these factors (not the
# trigger step, which must act at once): a flow estimate pulled down or up
# by fallen-back frames, or a pump model far from the real pump, must not
# swing the power. Chosen in the simulation.
RAISE_MAX = 1.2
LOWER_MAX = 0.7
FLOW_WANTED_MIN = 0.05  # g/s

POWER_MAX = 100.0
POWER_FLOOR_DEFAULT = 40.0
TARGET_TIME_DEFAULT = 30.0
POWER_STEP_DEFAULT = 1.0

# --------------------------------------------------------------- learning

GAMMA_PRIOR = 1.0
GAMMA_MIN, GAMMA_MAX = 0.3, 3.0
# Covariance of gamma. The regressor ln(d_to / d_from) is small (a step
# 100 -> 60 % gives -0.51, squared 0.26), so a prior of 1 would still
# weigh like ~4 samples after 14 shots and pull gamma towards 1
# (simulation: 1.5 read as 1.39); 4 leaves it ~6 %.
P_PRIOR = 4.0
P_MAX = 4.0 * P_PRIOR
LEARN_STEP_MIN = 15.0  # % points: the first step at least this large
LEARN_TREND_WINDOW = 3.0  # s of flow estimates before the step
LEARN_TREND_MIN_POINTS = 4
LEARN_FLOW_MIN = 0.5  # g/s: extrapolated flow before the step
LEARN_FIT_WINDOW = 4.0  # s of weights before the step for its trend
LEARN_FIT_LOW = 1.0  # g: lighter weights are not in that fit
# Response to the step: the flow estimate has moved at least this share of
# the change the model expects, away from the trend before the step, on
# LEARN_ONSET_FRAMES frames in a row. The step is measured once the flow
# window lies LEARN_SETTLE after that onset (the dead time of call, PSM,
# pump and scale is then out of it, whatever it is on this machine); no
# response within LEARN_RESPONSE_TIMEOUT: no sample.
LEARN_ONSET_SHARE = 0.5
LEARN_ONSET_FRAMES = 3
# Settling after the onset before the measuring window. With learning on,
# if the hold (``_hold_max``) ends earlier, the measurement is taken at its
# end instead, provided the flow window already lies past the onset by
# then; otherwise the next correction drops the sample.
LEARN_SETTLE = 1.0  # s
LEARN_RESPONSE_TIMEOUT = 6.0  # s
# Plausible flow ratio after/before a step (below: blocked pump, above:
# the flow rose for another reason).
RATIO_MIN, RATIO_MAX = 0.05, 1.5

THROTTLE_FIELDS = ("d_from", "d_to", "ratio")

# Modes (select options) and event types.
MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_THROTTLE = "throttle"
MODES = [MODE_OFF, MODE_OBSERVE, MODE_THROTTLE]
MODE_DEFAULT = MODE_OBSERVE

EVENT_TRIGGERED = "triggered"
EVENT_CORRECTED = "corrected"
EVENT_ENDED = "ended"
THROTTLE_EVENT_TYPES = [EVENT_TRIGGERED, EVENT_CORRECTED, EVENT_ENDED]


class ThrottleModel:
    """``Q(d) / Q(100) = (d / 100) ** gamma``, gamma learned by RLS."""

    def __init__(self) -> None:
        """Start from the prior (flow proportional to power)."""
        self.gamma = GAMMA_PRIOR
        self.p = P_PRIOR
        self.count = 0

    def reset(self) -> None:
        """Forget everything learned."""
        self.__init__()

    def flow_ratio(self, d_from: float, d_to: float) -> float:
        """Q(d_to) / Q(d_from)."""
        return (max(d_to, 1e-6) / max(d_from, 1e-6)) ** self.gamma

    def power_for(self, d_now: float, ratio: float) -> float:
        """``g^-1(g(d_now) * ratio)``: power for ``ratio`` x the current flow."""
        return max(0.0, d_now) * max(ratio, 0.0) ** (1.0 / self.gamma)

    @staticmethod
    def plausible(d_from: float, d_to: float, ratio: float) -> bool:
        """A learning sample inside the accepted range."""
        if not all(math.isfinite(v) for v in (d_from, d_to, ratio)):
            return False
        if not (0.0 < d_from <= POWER_MAX and 0.0 < d_to <= POWER_MAX):
            return False
        if abs(d_to - d_from) < LEARN_STEP_MIN:
            return False
        if not RATIO_MIN <= ratio <= RATIO_MAX:
            return False
        # The flow must move the way the power moved.
        if (ratio - 1.0) * (d_to - d_from) <= 0:
            return False
        # The exponent this sample alone implies must be a possible one:
        # one outlier (100 -> 84 % with ratio 0.06 implies 16) would
        # otherwise swing gamma to its bound.
        implied = math.log(ratio) / math.log(d_to / d_from)
        return GAMMA_MIN <= implied <= GAMMA_MAX

    def update(self, d_from: float, d_to: float, ratio: float) -> bool:
        """Learn from one power step; False (nothing learned) for an outlier."""
        if not self.plausible(d_from, d_to, ratio):
            return False
        x = math.log(d_to / d_from)
        y = math.log(ratio)
        gain = self.p * x / (FORGETTING + x * x * self.p)
        self.gamma = _clamp(self.gamma + gain * (y - self.gamma * x), GAMMA_MIN, GAMMA_MAX)
        self.p = min(P_MAX, (1.0 - gain * x) * self.p / FORGETTING)
        self.count += 1
        return True

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the store."""
        return {"gamma": self.gamma, "p": self.p, "count": self.count}

    @classmethod
    def from_dict(cls, data: Any) -> ThrottleModel:
        """Restore; invalid data gives the prior."""
        model = cls.parse(data)
        return model if model is not None else cls()

    @classmethod
    def parse(cls, data: Any) -> ThrottleModel | None:
        """Restore; None for missing or invalid data."""
        model = cls()
        try:
            gamma = float(data["gamma"])
            p = float(data["p"])
            count = int(data["count"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(gamma) and math.isfinite(p)) or p <= 0 or count < 0:
            return None
        model.gamma = _clamp(gamma, GAMMA_MIN, GAMMA_MAX)
        model.p = min(P_MAX, p)
        model.count = count
        return model

    def copy(self) -> ThrottleModel:
        """An independent copy."""
        return ThrottleModel.from_dict(self.to_dict())


def _throttle_update(model: ThrottleModel, entry: dict[str, Any]) -> bool:
    return model.update(entry["d_from"], entry["d_to"], entry["ratio"])


def throttle_journal(
    base: ThrottleModel | None = None, entries: list[dict[str, Any]] | None = None
) -> LearningJournal:
    """A learning journal of the throttle model."""
    return LearningJournal(
        base,
        entries,
        factory=ThrottleModel,
        update=_throttle_update,
        fields=THROTTLE_FIELDS,
    )


def parse_throttle_journal(data: Any) -> LearningJournal | None:
    """Restore a stored throttle journal (None if invalid)."""
    return LearningJournal.parse(
        data, factory=ThrottleModel, update=_throttle_update, fields=THROTTLE_FIELDS
    )


def theil_sen(points: list[tuple[float, float]]) -> float | None:
    """Median of the pairwise slopes; None with fewer than two times."""
    slopes = [
        (y2 - y1) / (x2 - x1)
        for i, (x1, y1) in enumerate(points)
        for x2, y2 in points[i + 1 :]
        if x2 - x1 > 1e-6
    ]
    return _median(slopes) if slopes else None


def _slope_se(points: list[tuple[float, float]], fit: tuple[float, float]) -> float:
    """Standard error of a least-squares slope (0 with < 3 points)."""
    n = len(points)
    if n < 3:
        return 0.0
    a, b = fit
    mx = sum(p[0] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    if sxx <= 1e-12:
        return 0.0
    ssr = sum((p[1] - (a + b * p[0])) ** 2 for p in points)
    return math.sqrt(ssr / (n - 2) / sxx)


def linear_fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least-squares line (intercept, slope); None if degenerate."""
    n = len(points)
    if n < 2:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    den = sum((p[0] - mx) ** 2 for p in points)
    if den <= 1e-9:
        return None
    slope = sum((p[0] - mx) * (p[1] - my) for p in points) / den
    return my - slope * mx, slope


def target_weight(
    model: StopModel, target: float, flow: float, lead: float
) -> float:
    """Weight at which the auto-stop cuts at ``flow`` (see should_cut)."""
    flow = max(0.0, flow)
    return target - model.overshoot(flow) - flow * lead


def remaining_pow(
    t: float,
    weight: float,
    drop: float,
    w_star: float,
    exponent: float = PREDICT_EXPONENT,
) -> float | None:
    """Remaining time by the power law anchored at the first drop."""
    if weight >= w_star:
        return 0.0
    s = t - (drop - POW_ANCHOR)
    if weight <= POW_MIN_WEIGHT or s <= 0:
        return None
    return max(0.0, s * (w_star / weight) ** (1.0 / exponent) - s)


def fit_exponent(
    profile: list, first_drop: float | None, end: float | None
) -> float | None:
    """The power-law exponent of one stored brew, None if not fittable.

    Least-squares slope of log w against log s, s = t - (first drop -
    POW_ANCHOR), over the profile points with EXPONENT_FIT_LOW < w < the
    weight at ``end`` (the end of the brew time); the weight made monotone
    first (pump vibration lets it fall back). Not bounded here.
    """
    if first_drop is None or end is None or not profile:
        return None
    anchor = first_drop - POW_ANCHOR
    pts: list[tuple[float, float]] = []
    top = -math.inf
    for point in profile:
        t, w = float(point[0]), float(point[1])
        if t > end:
            break
        top = max(top, w)
        pts.append((t, top))
    if not pts:
        return None
    w_end = pts[-1][1]
    logs = [
        (math.log(t - anchor), math.log(w))
        for t, w in pts
        if EXPONENT_FIT_LOW < w < w_end and t - anchor > 0
    ]
    if len(logs) < EXPONENT_MIN_POINTS:
        return None
    fit = linear_fit(logs)
    if fit is None or not math.isfinite(fit[1]):
        return None
    return fit[1]


class ExponentModel:
    """The installation's power-law exponent: a forgetting mean of the
    exponents fitted to its brews, starting from PREDICT_EXPONENT.

    ``p = (sum_k L^k p_k + L^n p0) / (sum_k L^k + L^n)`` with the newest
    sample k = 0, L = FORGETTING (0.93, like the other models: ~14 shots),
    the start value weighing like one sample older than all. The samples are
    kept with their brew (``brew_id``/``start``) so a deleted brew can be
    taken out; the value is recomputed from them. Bounded to
    EXPONENT_MIN..EXPONENT_MAX; brews outside the bounds are not used.
    """

    def __init__(self, samples: list[dict[str, Any]] | None = None) -> None:
        """Start (optionally from stored samples, oldest first)."""
        self.samples: list[dict[str, Any]] = list(samples or [])[-EXPONENT_SAMPLES_MAX:]

    @property
    def value(self) -> float:
        """The exponent for predictions."""
        num = den = 0.0
        weight = 1.0
        for sample in reversed(self.samples):
            num += weight * sample["p"]
            den += weight
            weight *= FORGETTING
        num += weight * PREDICT_EXPONENT
        den += weight
        return _clamp(num / den, EXPONENT_MIN, EXPONENT_MAX)

    def add(self, p: float | None, brew_id: str | None, start: str | None) -> bool:
        """One brew's exponent; False if missing or implausible. A brew
        already in the samples is replaced."""
        if p is None or not math.isfinite(p) or not EXPONENT_MIN <= p <= EXPONENT_MAX:
            return False
        self.remove(brew_id, start)
        self.samples.append({"brew_id": brew_id, "start": start, "p": round(p, 4)})
        del self.samples[:-EXPONENT_SAMPLES_MAX]
        return True

    def remove(self, brew_id: str | None, start: str | None) -> bool:
        """Take a brew's sample out; True if there was one."""
        before = len(self.samples)
        self.samples = [
            s for s in self.samples if not LearningJournal.matches(s, brew_id, start)
        ]
        return len(self.samples) != before

    def to_list(self) -> list[dict[str, Any]]:
        """Plain data for the store."""
        return [dict(s) for s in self.samples]

    @classmethod
    def parse(cls, data: Any) -> ExponentModel:
        """Restore; invalid entries are skipped."""
        samples = []
        for raw in data if isinstance(data, list) else ():
            if not isinstance(raw, dict):
                continue
            p = raw.get("p")
            if not isinstance(p, (int, float)) or isinstance(p, bool) or not math.isfinite(p):
                continue
            if not EXPONENT_MIN <= p <= EXPONENT_MAX:
                continue
            brew_id, start = raw.get("brew_id"), raw.get("start")
            samples.append(
                {
                    "brew_id": brew_id if isinstance(brew_id, str) else None,
                    "start": start if isinstance(start, str) else None,
                    "p": float(p),
                }
            )
        return cls(samples)


def remaining_trend(weight: float, flow: float, slope: float, w_star: float) -> float:
    """Remaining time with the flow rising linearly (inf without flow)."""
    rest = w_star - weight
    if rest <= 0:
        return 0.0
    a = _clamp(slope, 0.0, TREND_SLOPE_MAX)
    if a > 1e-9:
        return (-flow + math.sqrt(flow * flow + 2.0 * a * rest)) / a
    if flow > 1e-9:
        return rest / flow
    return math.inf


class PowerGrid:
    """A pump power entity's range in % of its maximum.

    ``percent = value / max x 100`` (0 % = no power whatever the entity's
    minimum); the entity's ``min`` is a lower bound of its own. Values lie
    on the entity's grid ``min + n x step`` (no step: continuous). The
    floor (in %) is rounded UP to the next grid value, never below it.
    """

    def __init__(self, low: float = 0.0, high: float = 100.0, step: float = 1.0) -> None:
        """``low``/``high``/``step``: the entity's min, max and step."""
        self.high = high if math.isfinite(high) and high > 0 else 100.0
        self.low = low if math.isfinite(low) and 0 <= low < self.high else 0.0
        self.step = step if math.isfinite(step) and step > 0 else 0.0

    def percent(self, value: float) -> float:
        """Entity value -> % of the maximum."""
        return round(value / self.high * 100.0, 6)

    def _snap(self, value: float, up: bool = False) -> float:
        value = _clamp(value, self.low, self.high)
        if not self.step:
            return round(value, 6)
        n = (value - self.low) / self.step
        n = math.ceil(n - 1e-9) if up else round(n)
        snapped = self.low + n * self.step
        if snapped > self.high + 1e-9:
            snapped -= self.step
        return round(_clamp(snapped, self.low, self.high), 6)

    def value(self, percent: float) -> float:
        """% -> the nearest entity value on the grid."""
        return self._snap(percent / 100.0 * self.high)

    def floor_value(self, floor: float) -> float:
        """The lowest entity value the throttle may set for ``floor`` %."""
        return self._snap(max(self.low, floor / 100.0 * self.high), up=True)

    def quantize(self, percent: float, floor: float) -> float:
        """% -> the % of the entity value that will be set (>= floor)."""
        return self.percent(max(self.floor_value(floor), self.value(percent)))


class ShotThrottle:
    """Prediction, trigger, control and the learning step of one shot.

    Fed with every accepted frame of the brew (after the outlier filter).
    ``feed`` returns the power (% of the pump's maximum) to set now (None:
    no change). In ``apply`` mode the caller sets it and passes the power
    in effect on the next frames (``power``: the last command until the
    entity confirms it). Without ``apply`` (observe) only the trigger step
    is computed: ``would_set`` and ``would_need_floor``; nothing after it.
    """

    def __init__(
        self,
        *,
        target_time: float,
        floor: float,
        model: ThrottleModel,
        stop_model: StopModel,
        grid: PowerGrid | None = None,
        step: float = POWER_STEP_DEFAULT,
        apply: bool = True,
        exponent: float = PREDICT_EXPONENT,
        learn: bool = True,
    ) -> None:
        """One shot. ``grid``: the pump entity's range (default 0-100 in
        ``step``); ``floor`` in %; ``exponent`` the power-law exponent."""
        self.target_time = target_time
        self.grid = grid if grid is not None else PowerGrid(0.0, 100.0, step)
        self.floor_setting = floor
        self.floor = self.grid.percent(self.grid.floor_value(floor))
        self.power_max = POWER_MAX
        self.model = model
        self.stop_model = stop_model
        self.apply = apply
        self.exponent = exponent  # the installation's (learned) exponent
        # Learning on: the first large step is held (no correction) until
        # it is measured, at most STEP_BLOCK_MAX. Off: control never waits.
        self.learn = learn
        self.local_fit = True
        self._shot_pts: list[tuple[float, float]] = []  # (log s, log w)
        self._top = -math.inf
        self.exponent_used = exponent
        self.exponent_cautious = exponent
        self.prediction_cautious: float | None = None
        self.drop: float | None = None
        self._flows: deque[tuple[float, float]] = deque()
        self._weights: deque[tuple[float, float]] = deque()
        self._predictions: deque[tuple[float, float, float]] = deque()
        self.prediction: float | None = None  # predicted end, shot time
        self.remaining: float | None = None
        self.method: str | None = None
        self.triggered = False
        self.too_late = False  # predicted fast, but too late to act
        self.trigger_shot_time: float | None = None
        self.predicted_at_trigger: float | None = None
        # Throttle: the floor limits the command right now / did at some
        # point; the final verdict (``finish``) needs the end time.
        self.unreachable = False
        self.floor_hit = False
        self.limit_hit = False
        # Why it was unreachable (``finish``): UNREACHABLE_FLOOR (the floor
        # held it; a lower floor may help), UNREACHABLE_RATE (the
        # per-correction limit held it), UNREACHABLE_LATE (neither: too
        # little shot left after the trigger for the pump to act).
        self.unreachable_reason: str | None = None
        # The trigger step was held at TRIGGER_MIN_SHARE (pump not learned).
        self.trigger_capped = False
        # Observe: the first command it would have set, and whether that
        # needed the floor.
        self.would_set: float | None = None
        self.would_need_floor = False
        self.end_shot_time: float | None = None
        self.command: float | None = None  # last command (% of max)
        self._power_now: float = POWER_MAX
        self.power_trace: list[list[float]] = []
        self.corrections = 0
        self.end_boost = False
        self._behind_run = 0
        self._s0 = 0.0
        self._w0 = 0.0
        self._w_star = 0.0
        self._last_change_t: float | None = None
        self._last_correction_t: float | None = None
        self._integral = 0.0
        self.target_flow: float | None = None
        self.target_now: float | None = None  # weight the curve wants now
        # Learning: the first large step and its response.
        self._step: dict[str, Any] | None = None
        self._step_done = False
        self.dead_time = 0.0  # measured at the first step (s)
        self.dead_time_known = False
        self.sample: dict[str, float] | None = None
        self.sample_reason: str | None = None

    # -------------------------------------------------------- prediction

    def _w_star_at(self, flow: float, target: float, lead: float) -> float:
        return target_weight(self.stop_model, target, flow, lead)

    def predict(
        self,
        t: float,
        weight: float,
        flow: float,
        w_star: float,
        exponent: float | None = None,
    ) -> float | None:
        """Remaining time to the cut (s), None without an estimate."""
        assert self.drop is not None
        exponent = self.exponent_used if exponent is None else exponent
        pow_rem = remaining_pow(t, weight, self.drop, w_star, exponent)
        method = "pow"
        rem = pow_rem
        if t >= self.drop + TREND_DELAY:
            window = [p for p in self._flows if p[0] >= t - TREND_WINDOW]
            slope = theil_sen(window) or 0.0
            trend_rem = remaining_trend(weight, flow, slope, w_star)
            if rem is None or trend_rem > rem:
                rem, method = trend_rem, "trend"
        self.method = method if rem is not None else None
        return rem

    def _fit_local(self, t: float, weight: float) -> None:
        """The running shot's exponent, shrunk to the learned one."""
        assert self.drop is not None
        self._top = max(self._top, weight)
        s = t - (self.drop - POW_ANCHOR)
        if not self.local_fit or s <= 0 or self._top <= LOCAL_FIT_LOW:
            return
        self._shot_pts.append((math.log(s), math.log(self._top)))
        n = len(self._shot_pts)
        if n < LOCAL_FIT_MIN_FRAMES:
            return
        fit = linear_fit(self._shot_pts)
        if fit is None or not math.isfinite(fit[1]):
            return
        local = _clamp(fit[1], EXPONENT_MIN, EXPONENT_MAX)
        share = n / (n + LOCAL_PRIOR_FRAMES)
        self.exponent_used = (1.0 - share) * self.exponent + share * local
        # Cautious exponent for the trigger: the local fit's slope at the
        # lower edge of its uncertainty (a lower exponent = a later end).
        # Early fits over a short log range are uncertain, and an
        # overestimated exponent was what made flat (p 1.3) shots trigger.
        se = _slope_se(self._shot_pts, fit)
        low = _clamp(fit[1] - CAUTION_SE * se, EXPONENT_MIN, EXPONENT_MAX)
        self.exponent_cautious = min(
            self.exponent_used, (1.0 - share) * self.exponent + share * low
        )

    # ------------------------------------------------------------ frames

    def feed(
        self,
        t: float,
        weight: float,
        flow: float,
        shot_time: float,
        *,
        drop: float | None,
        target: float,
        lead: float,
        power: float,
        disturbed_t: float | None = None,
        flow_count: int = FLOW_MIN_FRAMES,
    ) -> float | None:
        """One accepted frame. ``drop``: first drop time (None: not yet),
        ``target``: auto-stop target (g), ``lead``: its look-ahead,
        ``power``: power in effect now (%), ``disturbed_t``: last dropped
        frame or level change (same clock as ``t``). Returns the power to
        set now, or None."""
        self._flows.append((t, flow))
        self._weights.append((t, weight))
        while self._weights and self._weights[0][0] < t - LEARN_FIT_WINDOW - 1.0:
            self._weights.popleft()
        keep = max(TREND_WINDOW, LEARN_TREND_WINDOW) + LEARN_RESPONSE_TIMEOUT + FLOW_WINDOW
        while self._flows and self._flows[0][0] < t - keep:
            self._flows.popleft()
        self._power_now = power
        if self.command is None:
            self.command = power  # the shot starts at the power in effect
        if not self.power_trace:
            self.power_trace.append([round(t, 2), round(power, 2)])
        if drop is None or target <= 0:
            return None
        self.drop = drop
        if not self.triggered:
            self._fit_local(t, weight)
        # Learning measurement first (before a correction on this frame).
        self._watch_step(t, flow, disturbed_t, flow_count)
        w_star = self._w_star_at(flow, target, lead)
        rem = self.predict(t, weight, flow, w_star)
        if rem is not None:
            self.remaining = rem
            self.prediction = shot_time + rem if math.isfinite(rem) else math.inf
        if not self.triggered and t < drop + TREND_DELAY:
            # Only while the power law alone predicts: from TREND_DELAY on
            # the trend estimate checks it (and the cautious one lost a
            # real 25 s shot on the stored profiles).
            cautious = self.predict(t, weight, flow, w_star, self.exponent_cautious)
            self.prediction_cautious = (
                None
                if cautious is None
                else shot_time + cautious
                if math.isfinite(cautious)
                else math.inf
            )
        else:
            self.prediction_cautious = None
        if not self.triggered:
            return self._maybe_trigger(t, weight, flow, shot_time, target, lead)
        if not self.apply:
            return None  # observe: only the trigger step
        return self._correct(t, weight, flow, shot_time)

    def _maybe_trigger(
        self,
        t: float,
        weight: float,
        flow: float,
        shot_time: float,
        target: float,
        lead: float,
    ) -> float | None:
        assert self.drop is not None
        if self.prediction is None:
            return None
        # Collected before the check time too: at first drop + 3 s the
        # filter already has its frames (a filter, not an extra wait).
        preds = self._predictions
        cautious = (
            self.prediction_cautious
            if self.prediction_cautious is not None
            else self.prediction
        )
        preds.append((t, self.prediction, self.remaining or 0.0, cautious))
        while preds and preds[0][0] < t - TRIGGER_WINDOW:
            preds.popleft()
        if t < self.drop + CHECK_DELAY or len(preds) < TRIGGER_FRAMES:
            return None
        limit = self.target_time - TRIGGER_MARGIN
        # Also the cautious prediction (exponent at the lower edge of the
        # running shot's fit) must be fast enough.
        if any(p[1] > limit or p[3] > limit for p in preds):
            return None
        remaining = _median([p[2] for p in preds])
        if remaining < MIN_REMAINING:
            self.too_late = True
            return None
        if self.target_time - shot_time <= END_GUARD or flow < FLOW_MIN:
            self.too_late = True
            return None
        self.triggered = True
        self.trigger_shot_time = shot_time
        self.predicted_at_trigger = _median([p[1] for p in preds])
        self._s0 = shot_time
        self._w0 = weight
        # W* at the flow the curve will ask for (fixed point, converges in
        # a few steps: the overshoot changes W* by tau x the flow change).
        q_ref = flow
        w_star = self._w_star_at(q_ref, target, lead)
        for _ in range(5):
            q_ref = max(FLOW_WANTED_MIN, (w_star - weight) / (self.target_time - shot_time))
            w_star = self._w_star_at(q_ref, target, lead)
        self._w_star = w_star
        self.target_flow = q_ref
        self.target_now = weight
        self._last_correction_t = t
        # The first step compares with the flow the shot would have on
        # average until its predicted end, not with the flow now: the flow
        # keeps rising (puck), so the current flow alone asks for too
        # little throttle (simulation: 100 -> 76 % where ~60 % was needed).
        rest = w_star - weight
        # The shorter of the two remaining times (installation's exponent,
        # running shot's): a first step that is too weak cannot be caught
        # up with a slow pump (corrections limited, several seconds apart);
        # one that is too strong is corrected upwards.
        remaining = self.remaining or 0.0
        inst = remaining_pow(t, weight, self.drop, w_star, self.exponent)
        if inst is not None and inst > 0:
            remaining = min(remaining, inst) if remaining > 0 else inst
        average = rest / remaining if remaining > 0 else 0.0
        return self._command(t, max(flow, average), q_ref, trigger=True, current=flow)

    def _curve(self, shot_time: float) -> tuple[float, float]:
        """Target weight now and the curve's slope."""
        span = self.target_time - self._s0
        if span <= 0:
            return self._w_star, 0.0
        frac = _clamp((shot_time - self._s0) / span, 0.0, 1.0)
        slope = (self._w_star - self._w0) / span if frac < 1.0 else 0.0
        return self._w0 + (self._w_star - self._w0) * frac, slope

    def _correct(
        self, t: float, weight: float, flow: float, shot_time: float
    ) -> float | None:
        w_ref, q_ref = self._curve(shot_time)
        # Boost window: the last END_GUARD s plus END_GUARD_DEAD_SHARE (a
        # quarter) of the dead time beyond the flow lag (a slow pump reacts
        # late; the whole dead time boosted too early in the simulation).
        extra = max(0.0, self._dead_capped() - FLOW_WINDOW / 2) * END_GUARD_DEAD_SHARE
        if self.end_boost:
            return None
        if self.target_time - shot_time <= END_GUARD + extra:
            behind = weight < w_ref or (
                self.prediction is not None and self.prediction > self.target_time
            )
            self._behind_run = self._behind_run + 1 if behind else 0
            late = weight < w_ref - END_BEHIND_GRAMS or (
                self._behind_run >= END_BEHIND_FRAMES
            )
            if late and self.command is not None and (
                self.command < self.power_max - 1e-9
            ):
                # Behind the curve (or predicted after the target): full
                # power at once, once.
                self.end_boost = True
                return self._set(t, self.power_max)
        if self.target_time - shot_time <= END_GUARD:
            return None  # a measured correction would act after the end
        step = self._step
        if step is not None and self.learn and t < step["t"] + self._hold_max():
            return None  # learning: the step is measured first (bounded)
        # The onset is seen in the flow estimate, which trails by about half
        # its window: only the dead time beyond that adds to the wait. While
        # the first step's response is not seen yet, the longest dead time
        # that counts is assumed.
        dead = DEAD_TIME_MAX if step is not None and step["onset"] is None else (
            self._dead_capped()
        )
        wait = CORRECTION_WAIT + max(0.0, dead - FLOW_WINDOW / 2)
        if self._last_change_t is not None and t < self._last_change_t + wait:
            return None
        error = w_ref - weight  # > 0: behind the curve, more flow
        last = self._last_correction_t if self._last_correction_t is not None else t
        dt = t - last
        self._last_correction_t = t
        command = self.command if self.command is not None else self._power_now
        saturated_low = command <= self.floor + 1e-9 and error < 0
        saturated_high = command >= self.power_max - 1e-9 and error > 0
        if not (saturated_low or saturated_high):
            self._integral = _clamp(
                self._integral + error * dt, -INTEGRAL_MAX, INTEGRAL_MAX
            )
        wanted = q_ref + KP * error + KI * self._integral
        self.target_flow = max(FLOW_WANTED_MIN, wanted)
        self.target_now = w_ref
        return self._command(t, flow, self.target_flow)

    def _hold_max(self) -> float:
        """How long the learning step may hold corrections."""
        if self.model.count < LEARN_HOLD_COUNT:
            return STEP_BLOCK_MAX_EARLY
        return STEP_BLOCK_MAX

    def _dead_capped(self) -> float:
        """The measured dead time, at most DEAD_TIME_MAX: a slowly rising
        shot can show its response late (puck 1.8: 4.2 s measured), and
        waiting that long left room for two corrections only."""
        if not self.dead_time_known:
            # Not measured (the first step's response was not seen before
            # the next change): assume the longest that counts, not none -
            # a slow pump corrected every 3 s swung (+4.2 s).
            return DEAD_TIME_MAX
        return min(self.dead_time, DEAD_TIME_MAX)

    def _flow_now(self, t: float, flow: float) -> float:
        """The flow now: the Theil-Sen flow is the slope of the last
        FLOW_WINDOW s, i.e. the flow about half a window ago; with a rising
        flow it is moved forward by that much along the flow's slope."""
        window = [p for p in self._flows if p[0] >= t - TREND_WINDOW]
        slope = _clamp(theil_sen(window) or 0.0, 0.0, TREND_SLOPE_MAX)
        return flow + slope * FLOW_WINDOW / 2

    def _command(
        self,
        t: float,
        flow: float,
        wanted: float,
        trigger: bool = False,
        current: float | None = None,
    ) -> float | None:
        """Power for ``wanted`` flow relative to the current power and
        ``flow`` (the flow at that power; ``current``: the measured flow
        now, when ``flow`` is a predicted average)."""
        if (current if current is not None else flow) < FLOW_MIN:
            return None
        base = self._power_now
        have = flow if current is not None else self._flow_now(t, flow)
        raw = self.model.power_for(base, wanted / have)
        limited = False
        # What the model asks for, before any limit (observe reports it).
        wanted_floor = raw < self.floor - 1e-9
        if trigger and self.model.count < TRIGGER_FREE_COUNT:
            # Pump model not learned yet: the first step at most to
            # TRIGGER_MIN_SHARE of the current power (see there).
            limited = raw < base * TRIGGER_MIN_SHARE - 1e-9
            self.trigger_capped = limited
            raw = max(raw, base * TRIGGER_MIN_SHARE)
        if not trigger:
            limited = raw < base * LOWER_MAX - 1e-9
            raw = _clamp(raw, base * LOWER_MAX, base * RAISE_MAX)
        need_floor = raw < self.floor - 1e-9
        new = _clamp(self.grid.quantize(raw, self.floor_setting), self.floor, self.power_max)
        if not self.apply:
            # Observe: only the trigger step, recorded.
            self.would_set = new
            self.would_need_floor = wanted_floor
            self.corrections += 1
            self.power_trace.append([round(t, 2), round(new, 2)])
            return new
        self.unreachable = need_floor
        self.floor_hit = self.floor_hit or need_floor
        # Braked by the floor or by the rate limit: the end decides whether
        # that made the target unreachable (``finish``).
        self.limit_hit = self.limit_hit or need_floor or limited
        return self._set(t, new)

    def _set(self, t: float, new: float) -> float | None:
        old = self.command if self.command is not None else self._power_now
        if abs(new - old) < 1e-9:
            return None
        self.corrections += 1
        self.command = new
        self._last_change_t = t
        self.power_trace.append([round(t, 2), round(new, 2)])
        if not self._step_done and self._step is None and abs(new - old) >= LEARN_STEP_MIN:
            window = [p for p in self._flows if t - LEARN_TREND_WINDOW <= p[0] <= t]
            fit = self._trend_fit(window)
            # The measured ratio is taken against the trend with the running
            # shot's own exponent (frozen at the trigger): with the
            # installation's mean, steep or flat pucks read gamma 20-30 %
            # off (puck 1.8: 1.0 -> 0.77). The response onset keeps the
            # installation's trend (changing it moved the hold timing and
            # cost control accuracy in the simulation).
            fit_ratio = self._trend_fit(window, self.exponent_used) or fit
            if fit is None or len(window) < LEARN_TREND_MIN_POINTS:
                self._step_done = True
                self.sample_reason = "no_trend"
            else:
                self._step = {
                    "t": t, "from": old, "to": new, "fit": fit, "fit_ratio": fit_ratio,
                    "expected": self.model.flow_ratio(old, new) - 1.0,
                    "onset": None, "run": 0, "run_t": None,
                }
        elif self._step is not None:
            # Changed again before the measurement: not clean.
            self._step = None
            self._step_done = True
            self.sample_reason = "changed_again"
        return new

    def _trend_fit(
        self, window: list[tuple[float, float]], exponent: float | None = None
    ) -> Any:
        """The trend before the step, to extrapolate the flow past it.

        The shot's own shape, not a straight line: the weight follows the
        power law of the prediction, ``w = (m t + c) ** p`` with
        ``exponent`` (default: the installation's; for the measured ratio
        the running shot's, see ``_set``), so ``w ** (1 / p)`` is a line in t;
        it is fitted to the weights of the last LEARN_FIT_WINDOW s before
        the step (weights are far less noisy than flow estimates, and the
        line also finds where the power law starts, which the first drop
        rule only estimates). The flow the Theil-Sen estimator would show
        is then the mean slope of that curve over the flow window. A line
        through the flow estimates overestimated the flow a few seconds
        later (simulation: gamma 1.5 read as ~2.0); the first drop as the
        anchor moved with the scale noise (+30 %). Falls back to the line
        through the flow estimates with too few weights.
        """
        if exponent is None:
            exponent = self.exponent
        t_step = window[-1][0] if window else 0.0
        pts = [
            (pt, w ** (1.0 / exponent))
            for pt, w in self._weights
            if t_step - LEARN_FIT_WINDOW <= pt <= t_step and w > LEARN_FIT_LOW
        ]
        if len(pts) >= LEARN_TREND_MIN_POINTS:
            line = linear_fit(pts)
            if line is not None and line[1] > 0:
                return ("pow", (line[1], line[0], exponent))
        line = linear_fit(window)
        return None if line is None else ("line", line)

    @staticmethod
    def _trend_at(fit: Any, t: float) -> float:
        kind, params = fit
        if kind == "line":
            a, b = params
            return a + b * t
        m, c, p = params

        def weight(x: float) -> float:
            return max(m * x + c, 0.0) ** p

        return (weight(t) - weight(t - FLOW_WINDOW)) / FLOW_WINDOW

    def _watch_step(
        self, t: float, flow: float, disturbed_t: float | None, flow_count: int
    ) -> None:
        """Follow the first large step: find the flow's response, then
        measure it against the trend before the step."""
        step = self._step
        if step is None:
            return
        predicted = self._trend_at(step["fit"], t)
        expected = predicted * step["expected"]
        if step["onset"] is None:
            moved = (flow - predicted) / expected if abs(expected) > 1e-9 else 0.0
            if moved >= LEARN_ONSET_SHARE:
                if step["run"] == 0:
                    step["run_t"] = t
                step["run"] += 1
                if step["run"] >= LEARN_ONSET_FRAMES:
                    step["onset"] = step["run_t"]
                    self.dead_time = max(0.0, step["onset"] - step["t"])
                    self.dead_time_known = True
            else:
                step["run"] = 0
            if step["onset"] is None and t > step["t"] + LEARN_RESPONSE_TIMEOUT:
                self._step = None
                self._step_done = True
                self.sample_reason = "no_response"
            return
        due = step["onset"] + LEARN_SETTLE + FLOW_WINDOW
        if self.learn:
            # Normally after LEARN_SETTLE + FLOW_WINDOW past the onset; if
            # the hold ends earlier, then already at its end - provided the
            # flow window lies past the onset by then (else, as without
            # learning, the next correction drops the sample).
            due = min(due, max(step["t"] + self._hold_max(), step["onset"] + FLOW_WINDOW))
        if t < due:
            return
        self._step = None
        self._step_done = True
        if disturbed_t is not None and disturbed_t >= step["t"] - LEARN_TREND_WINDOW:
            self.sample_reason = "disturbed"
            return
        if flow_count < FLOW_MIN_FRAMES:
            self.sample_reason = "gap"
            return
        # Checked once, on the trend the ratio is taken against.
        predicted = self._trend_at(step.get("fit_ratio") or step["fit"], t)
        if predicted < LEARN_FLOW_MIN:
            self.sample_reason = "no_flow"
            return
        ratio = flow / predicted
        if not ThrottleModel.plausible(step["from"], step["to"], ratio):
            self.sample_reason = "outlier"
            return
        self.sample = {"d_from": step["from"], "d_to": step["to"], "ratio": ratio}
        self.sample_reason = None

    # ------------------------------------------------------------- end

    def finish(self, end_shot_time: float | None) -> None:
        """The shot ended (cut, relay off, brew end) at ``end_shot_time``
        (None: unknown). Unreachable (throttle only), decided at the real
        end: it ended UNREACHABLE_SLACK before the target time; the reason
        says what held it back (floor, rate limit, or neither: triggered
        too late). Without an end time: the floor limited the last
        correction."""
        self.end_shot_time = end_shot_time
        if not (self.triggered and self.apply):
            self.unreachable = False
            self.unreachable_reason = None
        elif end_shot_time is not None:
            self.unreachable = end_shot_time < self.target_time - UNREACHABLE_SLACK
            self.unreachable_reason = (
                None
                if not self.unreachable
                else UNREACHABLE_FLOOR
                if self.floor_hit
                else UNREACHABLE_TRIGGER_CAP
                if self.trigger_capped
                else UNREACHABLE_RATE
                if self.limit_hit
                else UNREACHABLE_LATE
            )
        elif self.unreachable:
            self.unreachable_reason = UNREACHABLE_FLOOR

    @property
    def grind_finer(self) -> bool:
        """The shot ran (or would have run) too fast: grind finer."""
        return self.triggered

    @property
    def changed(self) -> bool:
        """The power was changed (or would have been, in observe)."""
        return self.corrections > 0
