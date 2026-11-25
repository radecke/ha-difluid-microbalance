"""Simulated espresso shot with a throttled pump (no Home Assistant).

Puck: no flow until the first drop, then the flow at 100 % rises linearly
(puck washout) up to a cap. Pump: ``g(d) = (d / 100) ** gamma_true`` (or a
pump with a dead zone), applied after a dead time (API call + PSM) and a
first-order lag, set in steps of the power entity (2 %). Scale: weight
delayed, rounded to 0.1 g, with noise, vibration fall-backs and single
outlier frames, frames every 0.2-0.35 s. The scale timer (brew t = 0)
starts some time after the relay (shot time 0).

The pipeline is the integration's: SampleFilter -> FlowEstimator ->
FirstDropDetector -> stop rule (StopModel.should_cut, IntervalEstimator
lead) -> ShotThrottle. ``run_profile`` (0.7.0) runs a pump profile
(``ProfileRun``) on the same machine instead; Zielzeit runs ``run``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
from pathlib import Path
import random
import sys
import types

_DIR = Path(__file__).resolve().parents[1] / "custom_components/difluid_scale"


def _package():
    """The integration's pure modules as a package without its __init__."""
    name = "difluid_pure"
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(_DIR)]  # type: ignore[attr-defined]
        sys.modules[name] = pkg
    return (
        importlib.import_module(f"{name}.stop_model"),
        importlib.import_module(f"{name}.throttle_model"),
        importlib.import_module(f"{name}.brew"),
        importlib.import_module(f"{name}.profiles"),
    )


sm, tm, brew, pf = _package()


@dataclass
class Shot:
    """One simulated shot (all times in shot time = since relay on)."""

    # Puck at 100 % (fixed review round 1): the shown weight follows the
    # power law the prediction assumes, w = A (S - (first_drop - 2)) ** p,
    # with A such that it shows 1.5 g (the first drop rule) at first_drop;
    # the true weight leads the shown one by the scale delay. Before, the
    # flow started 2 s before ``first_drop`` in true time, so slow shots
    # crossed 1.5 g (and were detected) late and every prediction came out
    # early - unlike the stored shots, whose backtest was late (+1.9 s).
    # Shot speed varies with ``puck_p`` (stored shots 1.2-1.95) and
    # ``first_drop``; ``rise`` adds washout beyond the power law.
    first_drop: float = 5.0  # s, shot time at which the cup shows 1.5 g
    puck_p: float = 1.55
    w_detect: float = 1.5  # g
    rise: float = 0.0  # g/s^2
    q_max: float = 6.0
    gamma_true: float = 1.0
    dead_zone: float = 0.0  # % below which the pump delivers nothing
    timer_offset: float = 0.5  # s, scale timer start after the relay
    scale_delay: float = 0.4  # s
    noise: float = 0.08  # g
    pump_dead_time: float = 0.3  # s, call + PSM
    pump_lag: float = 0.5  # s, first-order
    target: float = 40.0  # g, auto-stop target
    target_time: float = 30.0
    floor: float = 40.0
    step: float = 2.0  # pump entity 0-100 in this step
    grid: tuple[float, float, float] | None = None  # (min, max, step) instead
    apply: bool = True
    outliers: bool = True
    seed: int = 1
    max_time: float = 60.0
    gamma_model: float = 1.0
    exponent: float | None = None  # prediction exponent (None: start value)
    learn: bool = True  # the learning switch (holds the first step)


@dataclass
class Result:
    end: float | None  # shot time of the cut
    triggered: bool
    unreachable: bool
    too_late: bool
    trigger_at: float | None
    predicted_at_trigger: float | None
    powers: list[float] = field(default_factory=list)  # powers really applied
    trace: list[list[float]] = field(default_factory=list)
    natural_end: float | None = None
    sample: dict | None = None
    sample_reason: str | None = None
    throttle: object = None


def _g(shot: Shot, d: float) -> float:
    if d <= shot.dead_zone:
        return 0.0
    if shot.dead_zone > 0:
        d = (d - shot.dead_zone) / (100.0 - shot.dead_zone) * 100.0
    return (d / 100.0) ** shot.gamma_true


def run(shot: Shot, stop_model=None, model=None) -> Result:
    """Simulate; ``end`` is the shot time at which the auto-stop cut.
    ``model``: the throttle model to use (learning across shots)."""
    rng = random.Random(shot.seed)
    stop = stop_model or sm.StopModel.from_dict(
        {"tau": 0.5, "c": 0.63, "p": [[0.04, -0.1], [-0.1, 0.46]], "count": 8}
    )
    if model is None:
        model = tm.ThrottleModel()
        model.gamma = shot.gamma_model
    grid = tm.PowerGrid(*(shot.grid or (0.0, 100.0, shot.step)))
    throttle = tm.ShotThrottle(
        target_time=shot.target_time,
        floor=shot.floor,
        model=model,
        stop_model=stop,
        grid=grid,
        apply=shot.apply,
        exponent=shot.exponent if shot.exponent is not None else tm.PREDICT_EXPONENT,
        learn=shot.learn,
    )
    filt = sm.SampleFilter()
    est = sm.FlowEstimator()
    intervals = sm.IntervalEstimator()
    drop_det = brew.FirstDropDetector()
    dt = 0.01
    s = 0.0
    weight_true = 0.0
    mult = 1.0  # pump flow multiplier (lagged)
    power = 100.0  # power in effect at the pump (after the dead time)
    pending: list[tuple[float, float]] = []  # (apply at, power)
    history: list[tuple[float, float]] = [(0.0, 0.0)]  # (s, true weight)
    next_frame = shot.timer_offset + rng.uniform(0.0, 0.3)
    applied: list[float] = []
    end = None
    since_frame = 0
    while s < shot.max_time:
        s += dt
        while pending and pending[0][0] <= s:
            power = pending.pop(0)[1]
        mult += (_g(shot, power) - mult) * dt / shot.pump_lag
        age = s - (shot.first_drop - 2.0 - shot.scale_delay)
        if age > 0:
            amp = shot.w_detect / 2.0**shot.puck_p
            q100 = min(
                shot.q_max,
                amp * shot.puck_p * age ** (shot.puck_p - 1.0)
                + shot.rise * max(0.0, age - 2.0),
            )
            weight_true += q100 * mult * dt
        history.append((s, weight_true))
        if s < next_frame:
            continue
        next_frame = s + rng.uniform(0.2, 0.35)
        since_frame += 1
        # Scale reading: delayed true weight, noise, rounding.
        seen_at = s - shot.scale_delay
        w = next((hw for hs, hw in reversed(history) if hs <= seen_at), 0.0)
        w += rng.gauss(0.0, shot.noise)
        if shot.outliers and weight_true > 3 and rng.random() < 0.04:
            w -= rng.uniform(0.5, 2.5)  # vibration fall-back
        if shot.outliers and (since_frame + 7 * shot.seed) % 61 == 30:
            w = -197.4  # single bad frame
        w = round(w, 1)
        t = s - shot.timer_offset  # brew (profile) time
        for ft, fw, shift in filt.feed(t, w):
            flow = est.feed(ft, fw, shift)
            intervals.feed(ft)
            drop = drop_det.feed(ft, fw)
            lead = intervals.lead
            cut_flow = flow if sm.plausible_flow(flow) else 0.0
            if est.residual() <= sm.CUT_RESIDUAL_MAX and stop.should_cut(
                fw, cut_flow, shot.target, lead
            ):
                end = ft + shot.timer_offset
                break
            new = throttle.feed(
                ft,
                fw,
                flow,
                ft + shot.timer_offset,
                drop=drop,
                target=shot.target,
                lead=lead,
                power=power if shot.apply else 100.0,
                disturbed_t=filt.disturbed_t,
                flow_count=est.count,
            )
            if new is not None and shot.apply:
                pending.append((s + shot.pump_dead_time, new))
                applied.append(new)
        if end is not None:
            break
    throttle.finish(end)
    return Result(
        end=end,
        triggered=throttle.triggered,
        unreachable=throttle.unreachable,
        too_late=throttle.too_late,
        trigger_at=throttle.trigger_shot_time,
        predicted_at_trigger=throttle.predicted_at_trigger,
        powers=applied,
        trace=throttle.power_trace,
        sample=throttle.sample,
        sample_reason=throttle.sample_reason,
        throttle=throttle,
    )


def natural_end(shot: Shot) -> float | None:
    """End of the same shot without throttling (mode off)."""
    from dataclasses import replace

    return run(replace(shot, apply=False, target_time=-100.0)).end


# ----------------------------------------------------------- profiles (0.7.0)

TICK = 0.25  # s, the HA side's clock tick without frames (BREW_TICK_INTERVAL)


@dataclass
class ProfileResult:
    end: float | None  # shot time of the cut
    trace: list[list[float]] = field(default_factory=list)  # [t, %] commands
    phases: list[list] = field(default_factory=list)
    applied: list[tuple[float, float]] = field(default_factory=list)  # (t, %)
    flows: list[tuple[float, float]] = field(default_factory=list)  # (t, g/s)
    weights: list[tuple[float, float]] = field(default_factory=list)
    run: object = None
    throttle: Result | None = None  # Zielzeit: the 0.6.0 throttle's result


def run_profile(
    shot: Shot,
    profile,
    model=None,
    stop_model=None,
    dose: float | None = 18.0,
    scale: bool = True,
) -> ProfileResult:
    """One shot of ``profile`` on the simulated machine ``shot``.

    The Zielzeit profile is the 0.6.0 throttle: it runs ``run`` unchanged.
    Every other profile runs ``ProfileRun`` from the relay start (shot time
    0): updated with every accepted frame (weight, flow) and every TICK
    without one, its commands applied after the pump's dead time and lag
    like the throttle's. ``scale=False``: no scale data (time exits only;
    the auto-stop cannot cut, the shot runs ``max_time``)."""
    if profile.throttle == pf.THROTTLE_TARGET_TIME:
        res = run(shot, stop_model=stop_model, model=model)
        return ProfileResult(end=res.end, trace=res.trace, throttle=res)
    rng = random.Random(shot.seed)
    stop = stop_model or sm.StopModel.from_dict(
        {"tau": 0.5, "c": 0.63, "p": [[0.04, -0.1], [-0.1, 0.46]], "count": 8}
    )
    if model is None:
        model = tm.ThrottleModel()
        model.gamma = shot.gamma_model
    grid = tm.PowerGrid(*(shot.grid or (0.0, 100.0, shot.step)))
    prun = pf.ProfileRun(profile, floor=shot.floor, grid=grid, model=model, dose=dose)
    filt = sm.SampleFilter()
    est = sm.FlowEstimator()
    intervals = sm.IntervalEstimator()
    dt = 0.01
    s = 0.0
    weight_true = 0.0
    mult = 1.0
    power = 100.0
    pending: list[tuple[float, float]] = []
    history: list[tuple[float, float]] = [(0.0, 0.0)]
    next_frame = shot.timer_offset + rng.uniform(0.0, 0.3)
    next_tick = 0.0
    out = ProfileResult(end=None, run=prun)
    since_frame = 0
    last_scale: tuple[float, float] | None = None

    def command(at: float, new: float | None) -> None:
        if new is not None and shot.apply:
            pending.append((at + shot.pump_dead_time, new))
            out.applied.append((round(at, 2), new))

    while s < shot.max_time:
        s += dt
        while pending and pending[0][0] <= s:
            power = pending.pop(0)[1]
        mult += (_g(shot, power) - mult) * dt / shot.pump_lag
        age = s - (shot.first_drop - 2.0 - shot.scale_delay)
        if age > 0:
            amp = shot.w_detect / 2.0**shot.puck_p
            q100 = min(
                shot.q_max,
                amp * shot.puck_p * age ** (shot.puck_p - 1.0)
                + shot.rise * max(0.0, age - 2.0),
            )
            weight_true += q100 * mult * dt
        history.append((s, weight_true))
        in_effect = power if shot.apply else 100.0
        if s >= next_tick:
            next_tick = s + TICK
            command(s, prun.update(s, None, None, in_effect) if not scale or last_scale is None
                    else prun.update(s, last_scale[0], last_scale[1], in_effect))
        if not scale or s < next_frame:
            continue
        next_frame = s + rng.uniform(0.2, 0.35)
        since_frame += 1
        seen_at = s - shot.scale_delay
        w = next((hw for hs, hw in reversed(history) if hs <= seen_at), 0.0)
        w += rng.gauss(0.0, shot.noise)
        if shot.outliers and weight_true > 3 and rng.random() < 0.04:
            w -= rng.uniform(0.5, 2.5)
        if shot.outliers and (since_frame + 7 * shot.seed) % 61 == 30:
            w = -197.4
        w = round(w, 1)
        t = s - shot.timer_offset
        for ft, fw, shift in filt.feed(t, w):
            flow = est.feed(ft, fw, shift)
            intervals.feed(ft)
            cut_flow = flow if sm.plausible_flow(flow) else 0.0
            if est.residual() <= sm.CUT_RESIDUAL_MAX and stop.should_cut(
                fw, cut_flow, shot.target, intervals.lead
            ):
                out.end = ft + shot.timer_offset
                break
            last_scale = (fw, flow)
            out.flows.append((round(s, 2), flow))
            out.weights.append((round(s, 2), fw))
            command(s, prun.update(s, fw, flow, in_effect))
        if out.end is not None:
            break
    prun.finish(out.end if out.end is not None else s)
    out.trace = prun.trace
    out.phases = prun.phases
    return out
