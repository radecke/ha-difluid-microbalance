"""Standalone tests for the adaptive stop model (no Home Assistant needed).

Run with plain ``python3 tests/test_stop_model.py`` or via pytest.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import random
import sys

_DIR = Path(__file__).resolve().parents[1] / "custom_components/difluid_scale"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _DIR / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sm = _load("difluid_stop_model", "stop_model.py")
brew = _load("difluid_brew_for_stop", "brew.py")

RATE = 0.1  # 10 Hz frames


def _shots(model, tau: float, c: float, n: int, noise: float, seed: int = 1):
    rng = random.Random(seed)
    for _ in range(n):
        flow = rng.uniform(1.0, 3.0)
        y = c + tau * flow + rng.gauss(0.0, noise)
        assert model.update(flow, y)


def test_converges_on_synthetic_shots() -> None:
    model = sm.StopModel()
    _shots(model, tau=1.4, c=0.6, n=40, noise=0.1)
    assert model.count == 40
    assert abs(model.tau - 1.4) < 0.15, model.tau
    assert abs(model.c - 0.6) < 0.25, model.c
    for flow in (1.0, 2.0, 3.0):
        assert abs(model.overshoot(flow) - (0.6 + 1.4 * flow)) < 0.2, flow


def test_follows_a_change() -> None:
    """Forgetting: after the machine changes the old shots fade out."""
    model = sm.StopModel()
    _shots(model, tau=1.4, c=0.6, n=30, noise=0.05, seed=2)
    _shots(model, tau=0.8, c=1.0, n=40, noise=0.05, seed=3)
    assert abs(model.overshoot(2.0) - 2.6) < 0.15, model.overshoot(2.0)
    assert abs(model.tau - 0.8) < 0.2, model.tau


def test_prior_phase_updates_only_tau() -> None:
    model = sm.StopModel()
    assert (model.tau, model.c) == (sm.TAU_PRIOR, sm.C_PRIOR)
    for i in range(sm.MIN_FULL_SAMPLES):
        assert model.update(2.0, 3.5)
        assert model.c == sm.C_PRIOR, (i, model.c)
    assert model.tau > sm.TAU_PRIOR
    c_before = model.c
    assert model.update(2.0, 3.5)
    assert model.count == sm.MIN_FULL_SAMPLES + 1
    assert c_before != model.c  # full RLS from here on


def test_prior_phase_real_shots() -> None:
    """2026-11-21: tau prior 1 s predicted ~3.5 g, real overshoot ~1.6 g.

    Learning only c ran into C_MIN after one shot and stalled; learning tau
    lands the prediction near the measurement after two shots.
    """
    model = sm.StopModel()
    assert model.update(3.37, 1.8)
    assert model.update(3.294, 1.5)
    assert model.c == sm.C_PRIOR
    assert abs(model.overshoot(3.3) - 1.65) < 0.15, model.overshoot(3.3)


def test_prior_phase_is_robust() -> None:
    """One large (but plausible) residual is limited to PRIOR_STEP_CLIP."""
    model = sm.StopModel()
    assert model.update(2.0, 4.9)
    # Unclipped residual would be 4.9 - 2.3 = 2.6 g; only 2.0 g is applied.
    gain = 2.0 / (sm.FORGETTING + 4.0)
    assert abs(model.tau - (sm.TAU_PRIOR + gain * sm.PRIOR_STEP_CLIP)) < 1e-9


def test_outliers_rejected() -> None:
    model = sm.StopModel()
    before = model.to_dict()
    for overshoot in (-0.1, 5.01, 40.0, float("nan"), float("inf")):
        assert not model.update(2.0, overshoot), overshoot
    assert not model.update(float("nan"), 2.0)
    assert model.to_dict() == before and model.count == 0
    assert model.update(2.0, 0.0) and model.update(2.0, 5.0)  # bounds inclusive


def test_clamping() -> None:
    high = sm.StopModel()
    for _ in range(60):
        high.update(0.2, 5.0)  # huge drip at no flow
    assert sm.C_MIN <= high.c <= sm.C_MAX and high.c == sm.C_MAX
    assert sm.TAU_MIN <= high.tau <= sm.TAU_MAX
    for _ in range(60):
        high.update(4.0, 0.0)  # nothing after a cut at high flow
    assert sm.TAU_MIN <= high.tau <= sm.TAU_MAX
    assert sm.C_MIN <= high.c <= sm.C_MAX
    low = sm.StopModel()
    low.tau, low.c = 0.0, sm.C_MIN
    assert low.overshoot(0.0) == 0.0  # predicted overshoot never negative
    assert low.overshoot(-3.0) == 0.0
    big = sm.StopModel()
    big.tau, big.c = sm.TAU_MAX, sm.C_MAX
    assert big.should_cut(30.0, 2.0, 38.0)  # 30 + 3 + 6 >= 38
    assert not sm.StopModel().should_cut(30.0, 2.0, 38.0)  # 30 + 0.3 + 2


def test_covariance_windup_bounded() -> None:
    """Same flow at every cut: no excitation, the covariance stays bounded."""
    model = sm.StopModel()
    for _ in range(300):
        model.update(2.0, 2.7)
    trace = model.p[0][0] + model.p[1][1]
    assert trace <= sm.P_MAX_TRACE + 1e-9, trace
    assert all(math.isfinite(v) for row in model.p for v in row)
    assert abs(model.overshoot(2.0) - 2.7) < 0.01
    # A different flow afterwards does not throw the estimate around.
    model.update(1.0, 1.7)
    assert abs(model.overshoot(2.0) - 2.7) < 0.3


def test_persistence_round_trip() -> None:
    model = sm.StopModel()
    _shots(model, tau=1.2, c=0.4, n=10, noise=0.05)
    data = json.loads(json.dumps(model.to_dict()))
    restored = sm.StopModel.from_dict(data)
    assert restored.to_dict() == model.to_dict()
    assert restored.overshoot(2.0) == model.overshoot(2.0)
    # Broken or missing data: the prior.
    for bad in (None, {}, {"tau": "x", "c": 0, "p": [], "count": 1},
                {"tau": 1, "c": 1, "p": [[1, 0]], "count": 1},
                {"tau": float("nan"), "c": 1, "p": [[1, 0], [0, 1]], "count": 1}):
        prior = sm.StopModel.from_dict(bad)
        assert (prior.tau, prior.c, prior.count) == (sm.TAU_PRIOR, sm.C_PRIOR, 0), bad
    # Out-of-range values are clamped on load.
    clamped = sm.StopModel.from_dict(
        {"tau": 9, "c": -4, "p": [[1, 0], [0, 1]], "count": 7}
    )
    assert (clamped.tau, clamped.c, clamped.count) == (sm.TAU_MAX, sm.C_MIN, 7)
    model.reset()
    assert (model.tau, model.c, model.count) == (sm.TAU_PRIOR, sm.C_PRIOR, 0)


def _secant(points, window=1.0):
    """The pre-0.5.0b6 flow: secant over the last second (for comparison)."""
    t, w = points[-1]
    t0, w0 = next(p for p in points if p[0] >= t - window)
    return (w - w0) / (t - t0)


def test_flow_is_exact_on_a_clean_ramp() -> None:
    """Theil-Sen over FLOW_WINDOW: a straight line gives its slope."""
    est = sm.FlowEstimator()
    flows = []
    w = 0.0
    for i in range(120):
        t = round(i * RATE, 3)
        w = w + (0.0 if i < 30 else 0.25 if i < 80 else 0.1)
        flows.append(est.feed(t, w))
    assert sm.FLOW_WINDOW == 2.0
    assert abs(flows[75] - 2.5) < 1e-6 and abs(flows[-1] - 1.0) < 1e-6
    assert est.feed(12.1, w - 3) >= 0.0  # never negative
    falling = sm.FlowEstimator()
    for i in range(10):
        assert falling.feed(i * 0.3, 40.0 - i) == 0.0


def _vibrating_shot(seed: int, flow: float = 2.5):
    """Frames ~0.3 s apart (0.2-0.4 s); after a jump the weight falls back
    by 0.3-3.2 g for one frame, like the real scale during a brew."""
    rng = random.Random(seed)
    t = 0.0
    frames = []
    while t < 25.0:
        t += rng.uniform(0.2, 0.4)
        w = flow * t
        if rng.random() < 0.25:
            w -= rng.uniform(0.3, 3.2)  # fallback frame
        frames.append((round(t, 3), round(w, 1)))
    return frames


def test_flow_robust_to_fallbacks() -> None:
    """Fallen-back frames barely move the Theil-Sen flow; the 1 s secant
    swings by several g/s (either end can be a fallback)."""
    errors: dict[str, list[float]] = {"theil_sen": [], "secant": []}
    for seed in range(20):
        frames = _vibrating_shot(seed)
        est = sm.FlowEstimator()
        for i, (t, w) in enumerate(frames):
            flow = est.feed(t, w)
            if t < 10:
                continue
            errors["theil_sen"].append(abs(flow - 2.5))
            errors["secant"].append(abs(_secant(frames[: i + 1]) - 2.5))
    stats = {}
    for name, errs in errors.items():
        errs.sort()
        stats[name] = (sum(errs) / len(errs), errs[int(0.95 * len(errs))])
    print("fallbacks (mean, p95 error g/s):", stats)
    assert stats["secant"][0] > 0.6 and stats["secant"][1] > 2.5
    assert stats["theil_sen"][0] < 0.25 and stats["theil_sen"][1] < 1.0


# Frames of the brew 2026-11-22 07:52 (stored profile, s since the start,
# cut at 17.30 s with 0.5.0b5): one frame -197.4 g between 12.8 and 14.9 g.
BREW_0752 = [
    (13.10, 6.1), (13.41, 7.8), (13.72, 7.1), (14.02, 7.7), (14.33, 9.2),
    (14.57, 9.7), (14.85, 9.7), (15.25, 10.4), (15.46, 10.2), (15.76, 12.2),
    (16.07, 12.8), (16.37, -197.4), (16.63, 14.9), (17.00, 15.0),
    (17.30, 15.8), (17.61, 15.7), (17.81, 17.3), (18.04, 18.0),
]


def _filtered(frames, filt=None, est=None):
    """(t, weight, flow) of the frames the auto-stop would use."""
    filt = filt or sm.SampleFilter()
    est = est or sm.FlowEstimator()
    out = []
    for t, w in frames:
        for ft, fw, shift in filt.feed(t, w):
            out.append((ft, fw, est.feed(ft, fw, shift)))
    return out


def test_brew_0752_outlier_frame_never_cuts() -> None:
    """2026-11-22 07:52: target 41.3 g, model tau 0.51 s / c 0.071 g. 0.5.0b5
    read 229.85 g/s at 15.8 g and cut; now the -197.4 g frame is dropped and
    nothing up to 18 g comes near the target."""
    model = sm.StopModel.from_dict(
        {"tau": 0.51, "c": 0.071, "p": [[0.05, 0.0], [0.0, 0.3]], "count": 6}
    )
    # The old secant at 17.30 s indeed spans the bad frame.
    assert _secant(BREW_0752[:15]) > 200
    filt = sm.SampleFilter()
    used = _filtered(BREW_0752, filt)
    assert all(w > 0 for _, w, _ in used)
    assert [t for t, _, _ in used] == [t for t, w in BREW_0752 if w > 0]
    assert filt.dropped == 1 and filt.disturbed_t == 16.37
    for t, w, flow in used:
        if t >= 14.0:  # enough frames in the window
            assert 1.0 < flow < 3.5, (t, flow)
        assert not model.should_cut(w, flow, 41.3, lead=0.153), (t, w, flow)
    # The b5 cut replayed: the dropped frame is left out, the flow is
    # plausible, the cut is clean (only a level change spoils it, 0.5.0b7).
    profile = [[t, w, 0.0] for t, w in BREW_0752]
    t, w, flow, clean = sm.profile_cut(profile, 17.36)
    assert (t, w) == (17.30, 15.8) and 1.0 < flow < 3.5 and clean


def test_sample_filter_cases() -> None:
    # Single bad frame upwards (+200 g): dropped, the next frame is used.
    filt = sm.SampleFilter()
    out = [filt.feed(t, w) for t, w in ((0.0, 20.0), (0.3, 220.4), (0.6, 21.3))]
    assert out == [[(0.0, 20.0, False)], [], [(0.6, 21.3, False)]]
    assert filt.dropped == 1
    # Two bad frames in a row (same level): read as a level change down and
    # back up; each change is confirmed by a second frame on the new level.
    filt = sm.SampleFilter()
    frames = [(0.0, 12.8), (0.3, -197.4), (0.6, -197.0), (0.9, 14.9), (1.2, 15.0)]
    out = [filt.feed(t, w) for t, w in frames]
    assert out == [
        [(0.0, 12.8, False)], [],
        [(0.3, -197.4, True), (0.6, -197.0, False)], [],
        [(0.9, 14.9, True), (1.2, 15.0, False)],
    ]
    # ... and the flow survives both steps (the stored frames are shifted).
    filt = sm.SampleFilter()
    frames = [(i * 0.3, round(2.0 * i * 0.3, 2)) for i in range(8)]
    frames += [(2.4, -190.0), (2.7, -189.4), (3.0, 6.0), (3.3, 6.6), (3.6, 7.2)]
    flows = [f for _, _, f in _filtered(frames, filt)]
    assert all(abs(f - 2.0) < 0.3 for f in flows[3:]), flows
    # Two different bad frames in a row: the older one goes, the newer is
    # held (third level).
    filt = sm.SampleFilter()
    out = [filt.feed(t, w) for t, w in ((0.0, 30.0), (0.3, -50.0), (0.6, 150.0), (0.9, 30.6))]
    assert out == [[(0.0, 30.0, False)], [], [], [(0.9, 30.6, False)]]
    assert filt.dropped == 2
    # Real rises and the observed fallbacks pass at once.
    filt = sm.SampleFilter()
    for t, w in ((0.0, 38.0), (0.3, 37.9), (0.6, 39.3), (0.9, 36.1), (1.2, 40.9),
                 (1.5, 42.3), (1.8, 39.1)):
        assert filt.feed(t, w) == [(t, w, False)]
    # A lifted cup stays down: confirmed by the second frame.
    assert filt.feed(2.1, -160.0) == []
    assert filt.feed(2.4, -160.1) == [(2.1, -160.0, True), (2.4, -160.1, False)]
    # Clock going back: no time passed, the last weight stays the reference.
    assert filt.feed(1.0, -159.8) == [(1.0, -159.8, False)]
    assert filt.feed(0.5, 40.0) == []
    # After a BLE gap the upward allowance is capped (5 g + 8 g/s x 1 s).
    filt = sm.SampleFilter()
    assert filt.feed(0.0, 20.0) == [(0.0, 20.0, False)]
    assert filt.feed(3.0, 40.0) == []  # +20 g after 3 s: held
    assert filt.feed(3.3, 26.5) == [(3.3, 26.5, False)]


def test_spike_above_the_line_and_gap() -> None:
    """A frame far above the robust line does not cut on its own (residual);
    a window with too few frames is not clean for learning."""
    est = sm.FlowEstimator()
    for i in range(9):
        est.feed(i * 0.25, 2.3 * i * 0.25)
    assert abs(est.residual()) < 1e-9
    est.feed(2.25, 2.3 * 2.25 + 6.0)  # +6 g: passes the frame filter
    assert est.residual() > sm.CUT_RESIDUAL_MAX
    est.feed(2.5, 2.3 * 2.5)
    assert abs(est.residual()) < 1.0
    # Gap: frames missing for 3 s, the window holds the new frame only.
    est.feed(5.5, 2.3 * 5.5)
    assert est.count < sm.FLOW_MIN_FRAMES
    profile = [[i * 0.25, 2.3 * i * 0.25, 0.0] for i in range(12)]
    profile += [[6.0, 13.8, 0.0]]
    assert sm.profile_cut(profile, 6.0)[3] is False
    assert sm.profile_cut(profile, 2.75)[3] is True


def test_implausible_flow_is_not_learned() -> None:
    model = sm.StopModel()
    assert not model.update(229.85, 2.5)  # the 07:52 cut
    assert not model.update(sm.FLOW_MAX + 0.1, 2.0)
    assert not model.update(float("nan"), 2.0)
    assert model.count == 0 and (model.tau, model.c) == (sm.TAU_PRIOR, sm.C_PRIOR)
    assert model.update(sm.FLOW_MAX, 4.0) and model.count == 1
    assert sm.plausible_flow(4.7) and not sm.plausible_flow(-0.1)


def test_settle_detector() -> None:
    det = sm.SettleDetector()
    t, w = 0.0, 36.0
    settled = None
    drip = 0.8  # g/s, decaying
    while settled is None and t < 20:
        t = round(t + RATE, 3)
        w += drip * RATE
        drip *= 0.93
        settled = det.feed(t, w)
    assert settled is not None
    # Settled only when the drip is below ~0.1 g/s.
    assert drip < 0.12, drip
    assert 36.0 < settled < 36.0 + 0.8 / (1 - 0.93) * RATE + 1e-6
    # A constant weight is settled once the window is covered, not before.
    det = sm.SettleDetector()
    assert det.feed(0.0, 38.0) is None
    assert det.feed(1.0, 38.0) is None
    assert det.feed(2.0, 38.0) == 38.0


def _simulate_shot(model, target, pump_flow, latency, drip, seed):
    """A shot at 10 Hz: the switch acts ``latency`` s after the cut sample,
    then ``drip`` g follow. Returns (cut weight, flow at cut, final)."""
    rng = random.Random(seed)
    est = sm.FlowEstimator()
    t, w = 0.0, 0.0
    while True:
        t = round(t + RATE, 3)
        ramp = min(1.0, t / 4.0)  # flow builds up over the first seconds
        w += pump_flow * ramp * RATE
        measured = round(w + rng.gauss(0, 0.03), 1)
        flow = est.feed(t, measured)
        if model.should_cut(measured, flow, target):
            break
    final = w + pump_flow * latency + drip
    return measured, flow, final


def test_closed_loop_lands_on_target() -> None:
    """Cut, settle, learn: shots with varying flow end up on the target."""
    model = sm.StopModel()
    rng = random.Random(7)
    errors = []
    for shot in range(40):
        pump_flow = rng.uniform(1.2, 3.0)
        cut_w, cut_f, final = _simulate_shot(
            model, 38.0, pump_flow, latency=1.3, drip=0.5, seed=shot
        )
        assert model.update(cut_f, final - cut_w)
        errors.append(final - 38.0)
    # The cut happens at the first sample past the threshold: at 3 g/s the
    # weight moves 0.3 g between two frames, so shots land up to one frame
    # of flow late (on average half a frame, ~+0.1 g at 10 Hz).
    late = errors[-15:]
    assert max(abs(e) for e in late) < 0.4, late
    assert -0.05 < sum(late) / len(late) < 0.2, late
    assert abs(errors[0]) > 0.5  # the prior alone was off


def _sparse_shot(model, target, pump_flow, latency, drip, rng, anticipate):
    """A shot sampled like the real scale: ~0.3 s apart with jitter, a
    random phase and dropped frames. Returns (cut weight, flow, final)."""
    est = sm.FlowEstimator()
    intervals = sm.IntervalEstimator()
    t = rng.uniform(0.0, 0.3)  # random sample phase
    while True:
        t += rng.uniform(0.25, 0.35)
        if rng.random() < 0.15:
            continue  # frame dropped
        w = pump_flow * max(0.0, t - 2.0)
        measured = round(w, 1)
        flow = est.feed(t, measured)
        intervals.feed(t)
        lead = intervals.lead if anticipate else 0.0
        if model.should_cut(measured, flow, target, lead):
            break
    final = pump_flow * max(0.0, t + latency - 2.0) + drip
    return measured, flow, final


def test_anticipation_halves_the_sampling_error() -> None:
    """Cutting half a sample interval ahead removes the sampling bias."""
    results = {}
    for anticipate in (False, True):
        late = []
        # Averaged over several runs: one run is chaotic (each cut moves
        # the random sample phase of all later shots).
        for seed in range(10):
            model = sm.StopModel()
            rng = random.Random(seed)
            errors = []
            for _ in range(80):
                pump_flow = rng.uniform(1.5, 3.0)
                cut_w, cut_f, final = _sparse_shot(
                    model, 38.0, pump_flow, 1.2, 0.4, rng, anticipate
                )
                assert model.update(cut_f, final - cut_w)
                errors.append(final - 38.0)
            late += errors[-40:]
        results[anticipate] = (
            sum(abs(e) for e in late) / len(late),
            sum(late) / len(late),
        )
    (mae_plain, bias_plain), (mae_lead, bias_lead) = results[False], results[True]
    print(f"sampling: MAE {mae_plain:.2f} g -> {mae_lead:.2f} g, "
          f"bias {bias_plain:+.2f} g -> {bias_lead:+.2f} g")
    assert bias_plain > 0.2  # cut always lands late without anticipation
    # Not zero: the interval holding the threshold is longer than the median
    # on average (dropped frames, inspection paradox).
    assert abs(bias_lead) < 0.2 and abs(bias_lead) < bias_plain / 2
    assert mae_lead < 0.7 * mae_plain


def test_interval_estimator() -> None:
    est = sm.IntervalEstimator()
    assert est.interval == sm.INTERVAL_FALLBACK  # nothing known yet
    for t in (0.0, 0.3, 0.6):
        est.feed(t)
    assert est.interval == sm.INTERVAL_FALLBACK  # two intervals only
    for t in (0.9, 1.2, 3.0, 3.3):  # one long gap does not move the median
        est.feed(t)
    assert abs(est.interval - 0.3) < 1e-9 and abs(est.lead - 0.15) < 1e-9
    fast = sm.IntervalEstimator()
    for i in range(10):
        fast.feed(i * 0.02)
    assert fast.interval == sm.INTERVAL_MIN  # clamped
    slow = sm.IntervalEstimator()
    for i in range(10):
        slow.feed(i * 2.0)
    assert slow.interval == sm.INTERVAL_MAX
    slow.clear()
    assert slow.interval == sm.INTERVAL_FALLBACK


def test_lead_is_not_learned() -> None:
    """The look-ahead only moves the cut; the model learns the real overshoot."""
    model = sm.StopModel()
    assert not model.should_cut(35.6, 2.0, 38.0)  # 35.6 + 2.3 < 38
    assert model.should_cut(35.6, 2.0, 38.0, lead=0.15)  # + 0.3
    assert model.overshoot(2.0) == sm.C_PRIOR + sm.TAU_PRIOR * 2.0


def test_profile_cut_uses_live_flow() -> None:
    """The service's cut sample: point at or before the offset, live flow."""
    profile = [[26.83, 33.7, 2.9], [27.14, 34.8, 3.1], [27.44, 34.4, 3.0],
               [27.75, 36.8, 3.2], [28.04, 37.4, 3.0]]
    t, w, f, clean = sm.profile_cut(profile, 27.83)
    assert (t, w) == (27.75, 36.8) and clean
    est = sm.FlowEstimator()
    expected = [est.feed(p[0], p[1]) for p in profile[:4]][-1]
    assert f == expected and 2.5 < f < 3.5
    assert sm.profile_cut(profile, 27.75)[0] == 27.75  # "at" counts
    assert sm.profile_cut(profile, 26.0) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"OK: {len(tests)} tests passed")
