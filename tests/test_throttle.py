"""Pump throttle (0.6.0): model, prediction, control in simulated shots.

No Home Assistant needed. Run with plain ``python3 tests/test_throttle.py``
or via pytest. The simulation is in ``throttle_sim.py``.
"""

from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from throttle_sim import Shot, brew, natural_end, run, sm, tm  # noqa: E402

TARGET_TIME = 30.0


SEEDS = range(1, 21)
SEEDS_B = range(101, 121)  # a second, independent set (bounds hold for both)


def _variants(seeds=SEEDS, **kw):
    for seed in seeds:
        args = {
            "seed": seed,
            # Scale timer 0.3-2.6 s after the relay, spread over the seeds.
            "timer_offset": 0.3 + ((seed * 7) % 20) * 0.12,
            "outliers": seed % 2 == 0,
        }
        args.update(kw)
        yield Shot(**args)


# ------------------------------------------------------------- the model


def test_model_prior_is_proportional() -> None:
    model = tm.ThrottleModel()
    assert model.gamma == 1.0
    assert math.isclose(model.flow_ratio(100, 50), 0.5)
    assert math.isclose(model.power_for(100, 0.5), 50.0)
    # Relative: the same ratio from 60 % gives 30 %.
    assert math.isclose(model.power_for(60, 0.5), 30.0)
    model.gamma = 2.0
    assert math.isclose(model.power_for(100, 0.25), 50.0)
    assert math.isclose(model.flow_ratio(100, 50), 0.25)


def test_model_learns_exponent() -> None:
    model = tm.ThrottleModel()
    for d_from, d_to in [(100, 60), (100, 50), (90, 56), (100, 70), (84, 50)] * 3:
        ratio = (d_to / d_from) ** 1.6
        assert model.update(d_from, d_to, ratio)
    assert 1.45 < model.gamma <= 1.6, model.gamma
    assert model.count == 15


def test_model_rejects_outliers() -> None:
    model = tm.ThrottleModel()
    assert not model.update(100, 90, 0.9)  # step < 15 %
    assert not model.update(100, 50, 1.2)  # flow rose while the power fell
    assert not model.update(100, 50, 0.01)  # below RATIO_MIN
    assert not model.update(100, 50, float("nan"))
    assert not model.update(0, 50, 0.5)
    assert model.count == 0 and model.gamma == 1.0


def test_model_round_trip_and_parse() -> None:
    model = tm.ThrottleModel()
    model.update(100, 50, 0.4)
    again = tm.ThrottleModel.from_dict(model.to_dict())
    assert again.to_dict() == model.to_dict()
    assert tm.ThrottleModel.parse({"gamma": "x"}) is None
    assert tm.ThrottleModel.parse({"gamma": 1, "p": -1, "count": 0}) is None
    assert tm.ThrottleModel.from_dict(None).to_dict() == tm.ThrottleModel().to_dict()
    clamped = tm.ThrottleModel.parse({"gamma": 99, "p": 1, "count": 2})
    assert clamped.gamma == tm.GAMMA_MAX


def test_journal_replay_and_remove() -> None:
    """Same journal class as the stop model: take a brew's sample out and
    replay from the base."""
    journal = tm.throttle_journal()
    model = tm.ThrottleModel()
    samples = [(100, 50, 0.4), (100, 60, 0.5), (90, 50, 0.45)]
    for i, (a, b, r) in enumerate(samples):
        assert model.update(a, b, r)
        journal.add(
            sm.LearningJournal.make(
                {"d_from": a, "d_to": b, "ratio": r}, f"b{i}", f"s{i}", "t"
            )
        )
    assert journal.replay().to_dict() == model.to_dict()
    assert journal.remove("b1", None) == 1
    expected = tm.ThrottleModel()
    expected.update(100, 50, 0.4)
    expected.update(90, 50, 0.45)
    assert journal.replay().to_dict() == expected.to_dict()
    stored = journal.to_dict()
    again = tm.parse_throttle_journal(stored)
    assert again is not None and again.replay().to_dict() == expected.to_dict()
    # A stop journal does not parse as a throttle journal's entries.
    bad = {"base": tm.ThrottleModel().to_dict(), "entries": [{"flow": 2, "overshoot": 1}]}
    assert len(tm.parse_throttle_journal(bad)) == 0
    assert tm.parse_throttle_journal({"base": sm.StopModel().to_dict()}) is None


# ------------------------------------------------------------ prediction


def test_remaining_pow_and_trend() -> None:
    # w = s^1.55 exactly: from s = 4 (w = 8.6) to W* = 40.
    s = 4.0
    w = s**tm.PREDICT_EXPONENT
    rem = tm.remaining_pow(t=s + 3.0, weight=w, drop=5.0, w_star=40.0)
    end_s = 40.0 ** (1 / tm.PREDICT_EXPONENT)
    assert math.isclose(rem, end_s - s, rel_tol=1e-9)
    assert tm.remaining_pow(10.0, 0.3, 5.0, 40.0) is None  # below 0.5 g
    assert tm.remaining_pow(10.0, 41.0, 5.0, 40.0) == 0.0
    # Constant flow 2 g/s: 20 g in 10 s; rising flow: sooner.
    assert math.isclose(tm.remaining_trend(20.0, 2.0, 0.0, 40.0), 10.0)
    rising = tm.remaining_trend(20.0, 2.0, 0.2, 40.0)
    assert math.isclose(20.0 + 2.0 * rising + 0.1 * rising**2, 40.0)
    # Slope clamped to 0.25 g/s^2.
    assert math.isclose(
        tm.remaining_trend(20.0, 2.0, 5.0, 40.0), tm.remaining_trend(20.0, 2.0, 0.25, 40.0)
    )
    assert tm.remaining_trend(20.0, 0.0, 0.0, 40.0) == math.inf


def test_target_weight_follows_cut_rule() -> None:
    model = sm.StopModel()
    w_star = tm.target_weight(model, 40.0, 2.0, 0.15)
    assert math.isclose(w_star, 40.0 - (model.c + model.tau * 2.0) - 2.0 * 0.15)
    assert model.should_cut(w_star, 2.0, 40.0, 0.15)
    assert not model.should_cut(w_star - 0.1, 2.0, 40.0, 0.15)


def test_live_first_drop_matches_profile_metric() -> None:
    pts = [[round(i * 0.25, 2), 0.0 if i < 30 else 0.2 * (i - 30), 0.0] for i in range(120)]
    pts[35][1] = 0.1  # a dip before the sustained rise
    det = brew.FirstDropDetector()
    for p in pts:
        det.feed(p[0], p[1])
    assert det.time == brew.first_drop_time(pts)
    untared = brew.FirstDropDetector()
    for p in pts:
        untared.feed(p[0], p[1] + 20.0)
    assert untared.time is None and untared.untared


def test_trigger_rules() -> None:
    """No check before first drop + 3 s; a fast shot whose remaining time is
    below MIN_REMAINING is only marked too late."""
    th = tm.ShotThrottle(
        target_time=30.0, floor=40.0, model=tm.ThrottleModel(), stop_model=sm.StopModel()
    )
    kw = {"target": 40.0, "lead": 0.15, "power": 100.0}
    # Drop at 2 s; at 4.9 s already 30 g (very fast) -> not checked yet.
    assert th.feed(4.9, 30.0, 5.0, 5.5, drop=2.0, **kw) is None
    assert not th.triggered
    # From 5.1 s: remaining ~1.4 s < MIN_REMAINING on three frames in a
    # row -> too late, no change.
    for i, w in enumerate((32.0, 32.5, 33.0)):
        assert th.feed(5.1 + i * 0.2, w, 5.0, 5.7 + i * 0.2, drop=2.0, **kw) is None
    assert not th.triggered and th.too_late
    # Without a first drop: nothing at all.
    th2 = tm.ShotThrottle(
        target_time=30.0, floor=40.0, model=tm.ThrottleModel(), stop_model=sm.StopModel()
    )
    for i in range(100):
        assert th2.feed(i * 0.25, i * 0.3, 2.0, i * 0.25 + 0.5, drop=None, **kw) is None
    assert th2.prediction is None and not th2.triggered


# ------------------------------------------------------------ simulation

FAST = {"puck_p": 1.45, "first_drop": 4.0}  # ends at ~20.6 s unthrottled
REAL_DEAD = {"pump_dead_time": 1.5, "pump_lag": 1.0, "scale_delay": 1.0}


def _ends(seeds_sets=(SEEDS, SEEDS_B), model_count=None, **kw):
    """(end - target time, result) over both seed sets."""
    out = []
    for seeds in seeds_sets:
        for shot in _variants(seeds, **kw):
            model = None
            if model_count is not None:
                model = tm.ThrottleModel()
                model.count = model_count
            res = run(shot, model=model)
            assert res.triggered and res.end is not None, shot.seed
            out.append((res.end - shot.target_time, res))
    return out


def test_fast_shot_ends_at_target_time() -> None:
    """Zu schnell: natural end <= 25 s, pump model at its start values
    (gamma 1, so the first step goes at most to half the power), learning
    on, on pumps with exponent 1.0 and 1.4 and with 20 % / 35 % dead
    zones; outliers, noise, scale delay, PSM in 2 % steps, timer start
    0.3-2.6 s after the relay; two seed sets of 20. Measured: 1.52 s early
    to 0.68 s late - NOT the +-1 s of the spec on the early side (the
    early ends are marked unreachable: floor or rate limit). Bound
    -1.85 .. +1.0 s (0.3 s margin). The dead zone 35 % case is always
    reachable."""
    for extra in ({}, {"gamma_true": 1.4}, {"dead_zone": 20.0}, {"dead_zone": 35.0}):
        for shot in _variants(**FAST, **extra):
            natural = natural_end(shot)
            assert natural is not None and natural <= TARGET_TIME - 5.0, natural
        for dev, res in _ends(**FAST, **extra):
            assert -1.85 <= dev <= 1.0, (extra, dev)
            assert res.powers and all(40.0 <= p <= 100.0 for p in res.powers)
            assert all(abs(p / 2 - round(p / 2)) < 1e-9 for p in res.powers), res.powers
            assert res.trigger_at is not None and res.trigger_at >= 3.0
            assert res.unreachable == (dev < -1.0)
            if extra.get("dead_zone") == 35.0:
                assert not res.unreachable


def test_strongly_nonlinear_pump_without_learning() -> None:
    """Pump exponent 2.5 with the start model 1.0: 0.43 s early to 0.99 s
    late over both seed sets (end guard; each correction limited to x0.7
    .. x1.2; first step at most to half the power: without that +2.1 s);
    before the end guard 33-35 s. Never unreachable. Bound -0.75 .. +1.3 s
    (above the spec's +1 s)."""
    for dev, res in _ends(**FAST, gamma_true=2.5):
        assert -0.75 <= dev <= 1.3, dev
        assert not res.unreachable


def test_realistic_dead_time_control() -> None:
    """1.5 s call/PSM dead time, 1.0 s pump lag, 1.0 s scale delay, pump
    model at its start value, both seed sets. Not the +-1 s of the spec:
    learning on (fresh model, hold up to 8 s): 30 % floor 1.55 s early to
    0.75 s late; pump exponent 2.0 1.16 s early to 0.73 s late. Learning
    OFF with pump exponent 2.0: 2.1 s early to 4.2 s late (one seed: the
    second step goes down before the slow pump has answered the first,
    then rising at x1.2 per ~4.5 s is too slow) - this is the case for
    turning learning on. Bounds with 0.3 s margin."""
    for dev, _ in _ends(**FAST, **REAL_DEAD, floor=30.0):
        assert -1.9 <= dev <= 1.1, dev
    for dev, _ in _ends(**FAST, **REAL_DEAD, gamma_true=2.0):
        assert -1.5 <= dev <= 1.05, dev
    for dev, _ in _ends(**FAST, **REAL_DEAD, gamma_true=2.0, learn=False):
        assert -2.45 <= dev <= 4.55, dev


def test_late_slow_shot_dead_time_capped_and_marked() -> None:
    """Puck 1.8 with the first drop at 12 s on the slow pump: the response
    is measured at ~5 s but counts as 2.5 s; still only 2 corrections fit,
    the shot ends ~5 s early - and is marked unreachable, not hidden."""
    for shot in _variants(puck_p=1.8, first_drop=12.0, floor=20.0, **REAL_DEAD):
        res = run(shot)
        th = res.throttle
        assert th.dead_time > 4.0 and th._dead_capped() == 2.5  # noqa: SLF001
        assert res.triggered and res.end is not None
        assert res.end < TARGET_TIME - 1.0 and res.unreachable, (shot.seed, res.end)


def test_unreachable_by_rate_limit() -> None:
    """Floor 20 % never reached; the shot ended > 1 s early: unreachable,
    with the reason in this order: floor, trigger_cap (first step held at
    half the power, pump not learned), rate_limit, late_trigger. Learning
    off (the default), so the first step is always capped."""
    reasons = set()
    for shot in _variants(puck_p=1.8, first_drop=12.0, floor=20.0, learn=False):
        res = run(shot)
        th = res.throttle
        assert not th.floor_hit and min(res.powers) > 20.0
        early = res.end < TARGET_TIME - 1.0
        assert res.unreachable == early, shot.seed
        if early:
            expected = (
                "trigger_cap" if th.trigger_capped
                else "rate_limit" if th.limit_hit else "late_trigger"
            )
            assert th.unreachable_reason == expected, (shot.seed, th.unreachable_reason)
            reasons.add(th.unreachable_reason)
        else:
            assert th.unreachable_reason is None
    assert "trigger_cap" in reasons
    # With a learned pump model (no cap) the same shots: rate_limit.
    for _, res in _ends((SEEDS,), model_count=4, puck_p=1.8, first_drop=12.0, floor=20.0):
        assert res.throttle.unreachable_reason in (None, "rate_limit", "late_trigger")
    # Floor reached: its own reason.
    res = run(Shot(puck_p=1.9, first_drop=3.5, seed=2))
    assert res.unreachable and res.throttle.unreachable_reason == "floor"


def test_borderline_shots_are_untouched() -> None:
    """Knapp schnell: shots that end 26.5-29.5 s unthrottled (exponent of
    the installation learned) are not triggered; noise and outliers on."""
    checked = false = 0
    for i in range(8, 40):
        for seed in range(1, 11):
            shot = Shot(
                puck_p=1.55,
                first_drop=i * 0.5,
                seed=seed,
                timer_offset=0.2 + seed * 0.25,
                apply=False,
            )
            res = run(shot)
            if res.end is None or not 26.5 <= res.end <= 29.5:
                continue
            checked += 1
            false += res.triggered
    assert checked >= 40, checked
    assert false <= 1, (false, checked)


def _false_triggers(puck_p: float, exponent: float, drops: list[float]) -> tuple[int, int]:
    checked = false = 0
    for drop in drops:
        for seed in range(1, 7):
            res = run(
                Shot(
                    puck_p=puck_p,
                    first_drop=drop,
                    seed=seed,
                    timer_offset=0.2 + seed * 0.3,
                    apply=False,
                    exponent=exponent,
                )
            )
            if res.end is not None and 26.5 <= res.end <= 29.5:
                checked += 1
                false += res.triggered
    return false, checked


def test_borderline_with_other_puck_exponents() -> None:
    """Shots whose own exponent differs from the installation's: the
    running shot's exponent (local fit, blended with the learned one) and
    the cautious trigger (until the trend estimate joins, the local slope
    2 standard errors lower must also be fast enough) keep false triggers
    low. Measured (26.5-29.5 s): puck 1.25
    with the start value 1.55: 1/43 (local fit alone 8/43, neither 37/43);
    with 1.25 learned: 0-3/43; puck 1.8: 0 either way."""
    slow = [x * 0.5 for x in range(2, 11)]
    false, checked = _false_triggers(1.25, 1.55, slow)
    assert checked >= 30 and false <= 2, (false, checked)
    false, checked = _false_triggers(1.25, 1.25, slow)
    assert false <= 0.1 * checked, (false, checked)
    # Puck 1.3 with more scale noise (0.15 g), start value 1.55: measured
    # 0/35 false triggers (4/35 without the cautious trigger).
    false, checked = 0, 0
    for drop in [x * 0.5 for x in range(8, 16)]:
        for seed in range(1, 7):
            res = run(Shot(puck_p=1.3, first_drop=drop, noise=0.15, seed=seed,
                           timer_offset=0.2 + seed * 0.3, apply=False))
            if res.end is not None and 26.5 <= res.end <= 29.5:
                checked += 1
                false += res.triggered
    assert checked >= 25 and false <= 1, (false, checked)
    quick = [x * 0.5 for x in range(34, 42)]
    for exponent in (1.55, 1.8):
        false, checked = _false_triggers(1.8, exponent, quick)
        assert checked >= 20 and false == 0, (exponent, false, checked)


def test_model_rejects_implausible_implied_exponent() -> None:
    """One outlier must not swing gamma: 100 -> 84 % with ratio 0.06
    implies 16 (outside 0.3-3): rejected."""
    model = tm.ThrottleModel()
    assert not model.update(100, 84, 0.06)
    assert model.gamma == 1.0 and model.count == 0
    assert model.update(100, 84, 0.84 ** 2.0)
    assert model.gamma > 1.0


def test_end_boost_hysteresis() -> None:
    """In the last second a single frame just behind the curve does not
    boost; 0.5 g behind or two frames in a row do."""
    def throttle() -> object:
        th = tm.ShotThrottle(
            target_time=30.0, floor=40.0, model=tm.ThrottleModel(), stop_model=sm.StopModel()
        )
        th.triggered = True
        th.command = 60.0
        th._power_now = 60.0  # noqa: SLF001
        th._s0, th._w0, th._w_star = 10.0, 10.0, 40.0  # noqa: SLF001
        return th

    th = throttle()
    assert th._correct(29.2, 38.7, 2.0, 29.2) is None  # noqa: SLF001 (curve 38.8)
    assert th._correct(29.4, 39.2, 2.0, 29.4) is None  # noqa: SLF001 back on it (39.1)
    assert not th.end_boost
    th = throttle()
    assert th._correct(29.2, 38.2, 2.0, 29.2) == 100.0  # noqa: SLF001 0.6 g behind
    th = throttle()
    th._correct(29.2, 38.7, 2.0, 29.2)  # noqa: SLF001
    assert th._correct(29.4, 39.0, 2.0, 29.4) == 100.0  # noqa: SLF001 twice


def test_trigger_needs_three_frames() -> None:
    """A single frame with a fast prediction does not trigger."""
    th = tm.ShotThrottle(
        target_time=30.0, floor=40.0, model=tm.ThrottleModel(), stop_model=sm.StopModel()
    )
    kw = {"target": 40.0, "lead": 0.15, "power": 100.0, "drop": 2.0}
    # Slow shot: ~2 g at 8 s.
    th.feed(8.0, 6.0, 1.0, 8.5, **kw)
    th.feed(8.3, 6.3, 1.0, 8.8, **kw)
    assert th.feed(8.6, 14.0, 1.0, 9.1, **kw) is None  # one fast frame
    assert not th.triggered
    assert th.feed(8.9, 6.9, 1.0, 9.4, **kw) is None
    assert not th.triggered


def test_unreachable_goes_to_floor_and_is_marked() -> None:
    for shot in _variants(puck_p=1.9, first_drop=3.5, timer_offset=0.5):
        res = run(shot)
        assert res.triggered and res.unreachable, shot.seed
        assert res.powers[-1] == 40.0 and min(res.powers) == 40.0
        assert res.end is not None and res.end < TARGET_TIME - 1.0
        assert res.throttle.grind_finer


def test_floor_respected_with_other_floor() -> None:
    res = run(Shot(puck_p=1.9, first_drop=3.5, floor=60.0, seed=3))
    assert res.triggered and min(res.powers) == 60.0 and res.unreachable


def test_observe_only_the_trigger_step() -> None:
    """Beobachten: the trigger and the first command it would have set
    (``would_set``, ``would_need_floor``); no further would-be corrections,
    never ``unreachable``; nothing applied; the shot ends as without."""
    for shot in _variants(**FAST, apply=False):
        res = run(shot)
        th = res.throttle
        assert res.powers == []
        assert res.triggered and len(res.trace) == 2
        assert th.would_set is not None and 40.0 <= th.would_set < 100.0
        assert res.trace[1][1] == th.would_set
        assert not res.unreachable and th.would_need_floor is False
        assert res.end == natural_end(shot)
    fast = run(Shot(puck_p=1.9, first_drop=3.5, apply=False, seed=2)).throttle
    # The model wants the floor; the first step (pump model not learned)
    # would go to half the power.
    assert fast.triggered and fast.would_need_floor and fast.would_set == 50.0
    assert not fast.unreachable


def test_learning_with_dead_time() -> None:
    """The first large step is measured once the flow has responded (onset
    found in the flow, then the window past it), against the trend of the
    weight before the step with the running shot's own exponent. With
    learning on, corrections wait for it at most 8 s while the model has
    fewer than 4 samples, then 5.5 s. 20 shots each, pump exponents 0.7 /
    1.5 / 2.5; bound +-20 %. Measured: moderate pump (1 s dead time, 0.8 s
    lag, 0.6 s scale) 0.73 / 1.48 / 2.29; slow pump (REAL_DEAD) 0.69 /
    1.48 / 2.20; steep late puck (1.8, first drop 12 s) with outliers
    0.63 / 1.39 / 2.22 (with the installation's exponent instead of the
    shot's it read 1.0 as 0.77)."""
    moderate = {"pump_dead_time": 1.0, "pump_lag": 0.8, "scale_delay": 0.6}
    cases = (
        dict(puck_p=1.5, first_drop=5.0, outliers=False, **moderate),
        dict(puck_p=1.5, first_drop=5.0, outliers=False, **REAL_DEAD),
        dict(puck_p=1.8, first_drop=12.0, outliers=True),
    )
    for case in cases:
        for gamma_true in (0.7, 1.5, 2.5):
            model = tm.ThrottleModel()
            learned = 0
            for seed in range(1, 21):
                shot = Shot(gamma_true=gamma_true, seed=seed,
                            timer_offset=0.3 + seed * 0.12, **case)
                res = run(shot, model=model)
                if res.sample is not None:
                    assert model.update(**res.sample)
                    learned += 1
            assert learned >= 3, (case, gamma_true, learned)
            assert abs(model.gamma / gamma_true - 1.0) <= 0.2, (case, gamma_true, model.gamma)


def test_control_never_waits_for_learning() -> None:
    """Puck 1.8, first drop 12 s, floor 20 %, both seed sets. Learning off:
    no hold (3.8 s early to 0.1 s late: the first step capped at half the
    power, pump not learned - marked trigger_cap). Learning on with a
    learned model (>= 4 samples): hold <= 5.5 s, 1.7 s early to 0.2 s
    late. Learning on with a fresh model: hold up to 8 s to learn, 4.4 s
    early to 0.2 s early. Before the bounded hold: 24.6 s. Not the spec's
    +-1 s; bounds with 0.3 s margin."""
    kw = {"puck_p": 1.8, "first_drop": 12.0, "floor": 20.0}
    for dev, _ in _ends(learn=False, **kw):
        assert -4.1 <= dev <= 0.4, dev
    for dev, _ in _ends(model_count=tm.LEARN_HOLD_COUNT, **kw):
        assert -2.0 <= dev <= 0.5, dev
    for dev, _ in _ends(**kw):
        assert -4.7 <= dev <= 0.3, dev


def test_no_learning_sample_without_response_or_when_disturbed() -> None:
    th = tm.ShotThrottle(
        target_time=30.0, floor=40.0, model=tm.ThrottleModel(), stop_model=sm.StopModel()
    )
    step = {"t": 10.0, "from": 100.0, "to": 60.0, "fit": ("line", (2.0, 0.0)),
            "expected": -0.4, "onset": None, "run": 0, "run_t": None}
    th._step = dict(step)  # noqa: SLF001
    for i in range(30):  # flow never moves
        th._watch_step(10.0 + i * 0.25, 2.0, None, 8)  # noqa: SLF001
    assert th.sample is None and th.sample_reason == "no_response"
    th = tm.ShotThrottle(
        target_time=30.0, floor=40.0, model=tm.ThrottleModel(), stop_model=sm.StopModel()
    )
    th._step = dict(step)  # noqa: SLF001
    for i in range(30):
        th._watch_step(11.0 + i * 0.25, 1.2, 9.0, 8)  # noqa: SLF001
    assert th.sample is None and th.sample_reason == "disturbed"
    assert th.dead_time == 1.0
    th = tm.ShotThrottle(
        target_time=30.0, floor=40.0, model=tm.ThrottleModel(), stop_model=sm.StopModel()
    )
    th._step = dict(step)  # noqa: SLF001
    for i in range(30):
        th._watch_step(11.0 + i * 0.25, 1.2, None, 8)  # noqa: SLF001
    assert th.sample == {"d_from": 100.0, "d_to": 60.0, "ratio": 0.6}


def test_no_flow_checked_on_the_ratio_trend_only() -> None:
    """A valid sample is not dropped as ``no_flow`` because the onset trend
    (installation's exponent) extrapolates low: only the trend the ratio is
    taken against counts."""
    th = tm.ShotThrottle(
        target_time=30.0, floor=40.0, model=tm.ThrottleModel(), stop_model=sm.StopModel()
    )
    step = {"t": 10.0, "from": 100.0, "to": 60.0,
            "fit": ("line", (0.3, 0.0)),  # onset trend: 0.3 g/s (< LEARN_FLOW_MIN)
            "fit_ratio": ("line", (2.0, 0.0)),  # the shot's own trend: 2 g/s
            "expected": -0.4, "onset": 11.0, "run": 3, "run_t": 11.0}
    th._step = step  # noqa: SLF001
    th._watch_step(14.1, 1.2, None, 8)  # noqa: SLF001
    assert th.sample_reason is None
    assert th.sample == {"d_from": 100.0, "d_to": 60.0, "ratio": 0.6}


def test_simulation_deterministic() -> None:
    shot = Shot(**FAST, seed=4)
    assert run(shot).end == run(replace(shot)).end


def test_second_machine_other_pump_and_puck() -> None:
    """Another machine: other puck (exponent 1.5, washout), pump exponent
    1.3 (model starts at 1.0), a 0-255 power entity in steps of 1, floor
    30 %, target 45 g and 27 s; both seed sets: 1.2 s early to 0.1 s late
    (spec +-1 s exceeded by 0.2 s on the early side). Bound -1.5 .. +0.4."""
    kw = dict(puck_p=1.5, first_drop=4.0, rise=0.03, gamma_true=1.3,
              grid=(0.0, 255.0, 1.0), floor=30.0, target_time=27.0, target=45.0)
    for shot in _variants(**kw):
        assert natural_end(shot) <= 22.0
    for dev, res in _ends(**kw):
        assert -1.5 <= dev <= 0.4, dev
        values = [p * 255 / 100 for p in res.powers]
        assert all(abs(v - round(v)) < 1e-4 for v in values), values
        assert min(values) >= 77  # 30 % of 255 = 76.5, rounded UP


def test_power_grid() -> None:
    """% of the maximum, grid min + n x step, floor rounded up."""
    g = tm.PowerGrid(0.0, 100.0, 2.0)
    assert g.floor_value(45.0) == 46.0 and g.quantize(10.0, 45.0) == 46.0
    assert g.quantize(51.0, 40.0) == 52.0 and g.value(100.0) == 100.0
    g = tm.PowerGrid(10.0, 100.0, 5.0)  # grid 10, 15, ..., min 10
    assert g.percent(100.0) == 100.0 and g.percent(10.0) == 10.0
    assert g.value(52.0) == 50.0 and g.value(53.0) == 55.0 and g.value(3.0) == 10.0
    assert g.floor_value(41.0) == 45.0 and g.floor_value(5.0) == 10.0
    g = tm.PowerGrid(0.0, 255.0, 1.0)
    assert g.value(50.0) == 128.0 and g.floor_value(40.0) == 102.0
    assert g.percent(255.0) == 100.0
    assert tm.PowerGrid(0.0, 100.0, 0.0).value(41.3) == 41.3
    assert tm.PowerGrid(0.0, 0.0, 1.0).high == 100.0  # broken max: 100


def test_exponent_fit_and_model() -> None:
    """The prediction exponent is learned per installation from stored
    brews (log w against log s on the main phase)."""
    for p_true in (1.3, 1.8, 2.2):
        drop = 6.0
        profile = []
        for i in range(160):
            t = i * 0.25
            s = t - (drop - tm.POW_ANCHOR)
            w = 0.4 * s**p_true if s > 0 else 0.0
            profile.append([t, round(w, 1), 0.0])
        end = next(p[0] for p in profile if p[1] >= 38.0)
        fitted = tm.fit_exponent(profile, drop, end)
        assert abs(fitted - p_true) < 0.06, (p_true, fitted)
    assert tm.fit_exponent([], 5.0, 20.0) is None
    assert tm.fit_exponent(profile, None, 20.0) is None
    model = tm.ExponentModel()
    assert model.value == tm.PREDICT_EXPONENT
    for i in range(30):
        assert model.add(1.9, f"b{i}", None)
    assert abs(model.value - 1.9) < 0.07  # the start value has faded
    assert not model.add(3.5, "x", None) and not model.add(None, "y", None)
    assert model.remove("b29", None) and not model.remove("nope", None)
    again = tm.ExponentModel.parse(model.to_list() + [{"p": "bad"}, 5])
    assert again.value == model.value
    one = tm.ExponentModel()
    one.add(2.5, "a", None)
    assert tm.PREDICT_EXPONENT < one.value < 2.5
    one.add(1.0, "a", None)  # same brew: replaced
    assert len(one.samples) == 1


def test_learned_exponent_is_used() -> None:
    th = tm.ShotThrottle(
        target_time=30.0,
        floor=40.0,
        model=tm.ThrottleModel(),
        stop_model=sm.StopModel(),
        exponent=2.0,
    )
    th.drop = 5.0
    rem = th.predict(9.0, 8.0, 2.0, 40.0)
    s = 9.0 - 3.0
    assert math.isclose(rem, s * (40.0 / 8.0) ** 0.5 - s)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"OK: {len(tests)} tests passed")
