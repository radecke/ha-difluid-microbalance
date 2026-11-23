"""Adaptive stop-at-target model (pure maths, no HA imports).

The weight still rises after the stop switch is turned off: the signal
needs time to travel (scale -> BLE -> HA -> switch -> pump stop) and the
puck drips afterwards. The overshoot is modelled as

    overshoot = c + tau * flow_at_cut

- ``tau`` (s): effective latency, including any constant lag of the flow
  estimate and the *nominal* stop switch latency (the measured deviation of
  each cut's switch latency from it is taken out of the learning sample,
  see ``adjusted_overshoot``);
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

# Stop switch latency: turn_off call -> the switch reports ``off``. Measured
# for a Zigbee2MQTT plug (2026-11-23, 109 switch-offs): median 68 ms, p95
# 306 ms, rare outliers of 0.3-3.1 s that cannot be predicted. ``tau``
# keeps meaning the total effective latency including a *nominal* switch
# latency: the running median of the last SWITCH_LATENCY_COUNT measured
# cuts (SWITCH_LATENCY_DEFAULT until SWITCH_LATENCY_MIN_COUNT are known).
# A learning sample is corrected by the part of its overshoot that the
# switch's deviation from that nominal caused (see adjusted_overshoot).
SWITCH_LATENCY_COUNT = 15
SWITCH_LATENCY_MIN_COUNT = 3
SWITCH_LATENCY_DEFAULT = 0.07  # s
# No ``off`` report within this after the call: latency unknown.
SWITCH_LATENCY_TIMEOUT = 5.0  # s


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
        model = cls.parse(data)
        return model if model is not None else cls()

    @classmethod
    def parse(cls, data: Any) -> StopModel | None:
        """Restore; None for missing or invalid data."""
        model = cls()
        try:
            tau = float(data["tau"])
            c = float(data["c"])
            p = [[float(v) for v in row] for row in data["p"]]
            count = int(data["count"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            len(p) != 2
            or any(len(row) != 2 for row in p)
            or not all(math.isfinite(v) for v in (tau, c, *p[0], *p[1]))
            or count < 0
        ):
            return None
        model.tau = _clamp(tau, TAU_MIN, TAU_MAX)
        model.c = _clamp(c, C_MIN, C_MAX)
        model.p = p
        model.count = count
        return model

    def copy(self) -> StopModel:
        """An independent copy (same floats)."""
        return StopModel.from_dict(self.to_dict())


# Accepted learning samples kept for un-learning (older ones are folded
# into the journal's base).
JOURNAL_MAX = 50


class LearningJournal:
    """Every accepted learning sample, so one can be taken out again.

    ``entries`` (oldest first) hold exactly what was fed into
    ``StopModel.update``: the regressor ``flow`` and the ``overshoot``
    (latency-adjusted where the cut had a measured switch latency), plus
    ``brew_id`` / ``start`` (ISO, as stored with the brew) of the brew and
    the time ``t`` it was learned. ``base`` is the model before the first
    entry: the prior for a fresh model, the stored model for one learned
    before the journal existed (0.5.1), so nothing learned earlier is
    lost by a replay.

    ``replay()`` starts from ``base`` and feeds the entries in order through
    the same ``update``; with an unchanged journal it reproduces the
    incrementally learned model exactly (same operations on the same
    floats). Beyond ``JOURNAL_MAX`` entries the oldest is dropped from the
    journal and folded into ``base`` (``base.update`` with it): the model
    stays identical, but that sample can no longer be taken out. Its weight
    in the current model is at most FORGETTING ** JOURNAL_MAX (0.93 ** 50 ~
    2.6 %), so not being able to remove it is negligible.
    """

    def __init__(
        self,
        base: StopModel | None = None,
        entries: list[dict[str, Any]] | None = None,
    ) -> None:
        """Start on ``base`` (default: the prior) with ``entries``."""
        self.base = base.copy() if base is not None else StopModel()
        self.entries: list[dict[str, Any]] = []
        for entry in entries or ():
            self.add(entry)

    def __len__(self) -> int:
        """Number of entries (samples that can still be taken out)."""
        return len(self.entries)

    @staticmethod
    def entry(
        flow: float,
        overshoot: float,
        brew_id: str | None = None,
        start: str | None = None,
        t: str | None = None,
    ) -> dict[str, Any]:
        """One journal entry (plain data for the store)."""
        return {
            "brew_id": brew_id,
            "start": start,
            "flow": flow,
            "overshoot": overshoot,
            "t": t,
        }

    def add(self, entry: dict[str, Any]) -> None:
        """Append an accepted sample; fold the oldest beyond the cap."""
        self.entries.append(entry)
        while len(self.entries) > JOURNAL_MAX:
            old = self.entries.pop(0)
            self.base.update(old["flow"], old["overshoot"])

    @staticmethod
    def matches(
        entry: dict[str, Any], brew_id: str | None, start: str | None
    ) -> bool:
        """Entry belongs to the brew: by id, or by start (an entry learned
        before its brew was stored, see ``set_brew_id``)."""
        return (brew_id is not None and entry.get("brew_id") == brew_id) or (
            start is not None and entry.get("start") == start
        )

    def index_of(self, brew_id: str | None, start: str | None) -> int | None:
        """Position of the brew's first entry, None if not journalled."""
        for i, entry in enumerate(self.entries):
            if self.matches(entry, brew_id, start):
                return i
        return None

    def set_brew_id(self, start: str, brew_id: str) -> bool:
        """Fill in the id of entries learned for the brew starting at
        ``start`` before it was stored; True if one changed."""
        changed = False
        for entry in self.entries:
            if entry.get("brew_id") is None and entry.get("start") == start:
                entry["brew_id"] = brew_id
                changed = True
        return changed

    def remove(self, brew_id: str | None, start: str | None) -> int:
        """Drop the brew's entries; how many were removed."""
        before = len(self.entries)
        self.entries = [
            e for e in self.entries if not self.matches(e, brew_id, start)
        ]
        return before - len(self.entries)

    def replay(self) -> StopModel:
        """The model learned from ``base`` and the entries, in order."""
        model = self.base.copy()
        for entry in self.entries:
            model.update(entry["flow"], entry["overshoot"])
        return model

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the store."""
        return {
            "base": self.base.to_dict(),
            "entries": [dict(e) for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: Any, model: StopModel) -> LearningJournal:
        """Restore. Missing or invalid data gives an empty journal on
        ``model``: a store from before the journal keeps its model, and
        replaying the empty journal gives that model back."""
        journal = cls.parse(data)
        return journal if journal is not None else cls(model)

    @classmethod
    def parse(cls, data: Any) -> LearningJournal | None:
        """Restore; None without a valid ``base``. Entries that are not a
        dict or whose flow/overshoot is not a finite number are skipped
        (the sum base count + entries then no longer equals the model's
        count)."""
        if not isinstance(data, dict):
            return None
        base = StopModel.parse(data.get("base"))
        if base is None:
            return None
        raw_entries = data.get("entries")
        entries: list[dict[str, Any]] = []
        for raw in raw_entries if isinstance(raw_entries, list) else ():
            if not isinstance(raw, dict):
                continue
            flow, overshoot = raw.get("flow"), raw.get("overshoot")
            if not all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in (flow, overshoot)
            ):
                continue
            flow, overshoot = float(flow), float(overshoot)
            if not (math.isfinite(flow) and math.isfinite(overshoot)):
                continue
            brew_id, start, t = raw.get("brew_id"), raw.get("start"), raw.get("t")
            entries.append(
                cls.entry(
                    flow,
                    overshoot,
                    brew_id if isinstance(brew_id, str) else None,
                    start if isinstance(start, str) else None,
                    t if isinstance(t, str) else None,
                )
            )
        return cls(base, entries)


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


class SwitchLatencies:
    """Measured stop switch latencies (s) of the last cuts, newest last.

    Bound to the switch entity they were measured on: another switch
    starts empty (its latency is a property of that device).
    ``unknown_streak`` counts the cuts in a row whose measurement ran but
    ended without a value (no ``off`` report in time, failed call).
    """

    def __init__(
        self, entity_id: str | None = None, values: list[float] | None = None
    ) -> None:
        """Start empty (or from stored values)."""
        self.entity_id = entity_id
        self.values: deque[float] = deque(values or (), maxlen=SWITCH_LATENCY_COUNT)
        self.unknown_streak = 0

    def nominal(self, entity_id: str | None) -> float:
        """Nominal latency for ``entity_id``: median of the stored values,
        the default with fewer than SWITCH_LATENCY_MIN_COUNT (or for another
        switch)."""
        if entity_id != self.entity_id or len(self.values) < SWITCH_LATENCY_MIN_COUNT:
            return SWITCH_LATENCY_DEFAULT
        return _median(list(self.values))

    def add(self, entity_id: str, latency: float | None) -> None:
        """One measured cut on ``entity_id`` (None: measured, no value)."""
        if entity_id != self.entity_id:
            self.entity_id = entity_id
            self.values.clear()
            self.unknown_streak = 0
        if latency is None:
            self.unknown_streak += 1
            return
        self.unknown_streak = 0
        self.values.append(latency)

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the store."""
        return {
            "entity_id": self.entity_id,
            "values": list(self.values),
            "unknown_streak": self.unknown_streak,
        }

    @classmethod
    def from_dict(cls, data: Any) -> SwitchLatencies:
        """Restore; invalid or missing data gives an empty history."""
        if not isinstance(data, dict):
            return cls()
        entity_id = data.get("entity_id")
        values: list[float] = []
        for value in data.get("values") or ():
            try:
                latency = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(latency) and 0.0 <= latency <= SWITCH_LATENCY_TIMEOUT:
                values.append(latency)
        result = cls(entity_id if isinstance(entity_id, str) else None, values)
        streak = data.get("unknown_streak")
        if isinstance(streak, int) and not isinstance(streak, bool) and streak > 0:
            result.unknown_streak = streak
        return result


def adjusted_overshoot(
    overshoot: float, flow: float, latency: float, nominal: float
) -> float:
    """Overshoot as if the switch had taken its nominal latency.

    The pump ran ``latency - nominal`` seconds longer (or shorter) than
    usual at about the flow at the cut, so ``y_adj = y - flow * (latency -
    nominal)`` (flow clamped at 0 like the model's regressor). The plausible
    overshoot window applies to this adjusted value.
    """
    return overshoot - max(0.0, flow) * (latency - nominal)


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
    # Nominal switch latency (s) when the cut was made (used to correct
    # this cut's learning sample) and the measured one (None: not measured
    # yet, timed out, switch already off/unavailable, or the call failed).
    nominal_latency: float = SWITCH_LATENCY_DEFAULT
    switch_latency: float | None = None
    # True until the measurement ended (learning waits for it, at most
    # SWITCH_LATENCY_TIMEOUT after the call).
    latency_pending: bool = False
