"""Pump profiles (0.7.0): format, built-ins, executor, simulated shots.

No Home Assistant needed. Run with plain ``python3 tests/test_profiles.py``
or via pytest. The simulation is ``throttle_sim.run_profile``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from throttle_sim import Shot, pf, run, run_profile, tm  # noqa: E402

# Two simulated machines (as in test_throttle) and two grinds each.
MACHINE_1: dict = {}
MACHINE_2 = dict(
    puck_p=1.5, rise=0.03, gamma_true=1.3, grid=(0.0, 255.0, 1.0), floor=30.0
)
FINE = dict(first_drop=8.0, puck_p=1.3)
COARSE = dict(first_drop=3.0, puck_p=1.8, q_max=8.0)
SEEDS = range(1, 7)


def _raises(data, fragment: str, **kw) -> None:
    try:
        pf.parse_profile(data, **kw)
    except pf.ProfileError as err:
        assert fragment in str(err), (fragment, str(err))
        return
    raise AssertionError(f"accepted: {data}")


def _profile(*phases, label="Test", **extra) -> dict:
    return {"label": label, "type": "pro", "phases": list(phases), **extra}


def _phase(pump=100, duration=10, **kw) -> dict:
    return {"name": kw.pop("name", "P"), "phase": "brew", "duration": duration, "pump": pump, **kw}


# ------------------------------------------------------------- format


def test_builtins() -> None:
    labels = [p.label for p in pf.BUILTINS]
    assert labels == [
        "standard", "soft_preinfusion", "blooming", "allonge",
        "lever", "target_time",
    ]
    assert all(p.builtin for p in pf.BUILTINS)
    std = pf.builtin("standard")
    assert len(std.phases) == 1 and std.phases[0].power == 100 and not std.phases[0].targets
    assert std.throttle is None
    zt = pf.builtin("ZIELZEIT")
    assert zt.throttle == pf.THROTTLE_TARGET_TIME
    assert [p.power for p in zt.phases] == [100]
    # Only the Zielzeit profile runs the throttle.
    assert [p.label for p in pf.BUILTINS if p.throttle] == ["target_time"]
    bloom = pf.builtin("blooming")
    assert [p.power for p in bloom.phases] == [100, 0, None]
    assert bloom.phases[2].flow == 2.2 and bloom.phases[2].transition_duration == 5
    # Every built-in exports and parses back to itself.
    for p in pf.BUILTINS:
        again = pf.parse_profile(json.loads(json.dumps(p.to_json())), builtin=True)
        assert again == p


def test_parse_subset_and_round_trip() -> None:
    raw = _profile(
        {
            "name": "Vor", "phase": "preinfusion", "valve": 1, "duration": 8,
            "temperature": 93, "pump": 50,
            "transition": {"type": "ease-in", "duration": 2, "adaptive": False},
            "targets": [{"type": "volumetric", "operator": "gte", "value": 3},
                        {"type": "flow", "operator": "lte", "value": 0.4},
                        {"type": "ratio", "value": 0.5}],
            "x_note": "kept",
        },
        _phase(pump={"target": "flow", "flow": 2.0}, duration=40),
        temperature=93, description="d", id="abc", x_custom={"a": 1},
    )
    p = pf.parse_profile(json.dumps(raw))  # JSON text works too
    assert p.label == "Test" and p.type == "pro" and not p.builtin
    first = p.phases[0]
    assert first.kind == "preinfusion" and first.power == 50 and first.transition == "ease-in"
    assert [t.type for t in first.targets] == ["volumetric", "flow", "ratio"]
    assert first.targets[2].operator == "gte"  # default
    assert p.phases[1].flow == 2.0 and p.phases[1].power is None
    out = p.to_json()
    assert out == raw  # unknown fields kept, nothing added
    out["phases"][0]["pump"] = 1  # a copy
    assert p.to_json()["phases"][0]["pump"] == 50
    # Defaults: type pro, phase brew, no valve, no transition, no targets.
    q = pf.parse_profile({"label": "x", "phases": [{"duration": 5, "pump": 70}]})
    assert q.type == "pro" and q.phases[0].kind == "brew" and q.phases[0].transition == "instant"


def test_rejections() -> None:
    _raises("{", "not valid JSON")
    _raises([], "JSON object")
    _raises({"phases": [_phase()]}, "label is required")
    _raises(_profile(label="unknown"), "reserved")
    _raises({"label": "a" * 61, "phases": [_phase()]}, "at most 60")
    _raises(_profile(), "non-empty list")
    _raises(_profile(_phase(), type="fancy"), "type must be")
    _raises(_profile(_phase(pump={"target": "pressure", "pressure": 9})), "needs a pressure sensor")
    _raises(
        _profile(_phase(targets=[{"type": "pressure", "operator": "gte", "value": 4}])),
        "needs a pressure sensor",
    )
    _raises(_profile(_phase(targets=[{"type": "pumped", "value": 30}])), "needs a pressure sensor")
    _raises(_profile(_phase(targets=[{"type": "weight", "value": 3}])), "unknown target type")
    _raises(_profile(_phase(targets=[{"type": "flow", "operator": "eq", "value": 1}])), "operator")
    _raises(_profile(_phase(valve=0)), "valve 0 is not supported")
    _raises(_profile(_phase(valve=2)), "valve must be 1")
    _raises(_profile(_phase(duration=0)), "duration must be > 0")
    _raises(_profile({"name": "x", "pump": 100}), "duration must be a number")
    _raises(_profile(_phase(pump=120)), "0-100")
    _raises(_profile(_phase(pump=None)), "pump is required")
    _raises(_profile(_phase(pump="100")), "must be a number")
    _raises(_profile(_phase(pump={"target": "flow", "flow": 0})), "pump flow")
    _raises(_profile(_phase(transition={"type": "bounce", "duration": 1})), "transition type")
    _raises(
        _profile(_phase(transition={"type": "linear", "duration": 3, "target": "volumetric"})),
        "time only",
    )
    _raises(_profile(_phase(phase="steam")), "preinfusion")
    _raises(_profile(_phase(), x_throttle="target_time"), "reserved for the built-in")
    _raises(_profile(_phase(), x_throttle="other"), "must be 'target_time'", builtin=True)
    _raises(_profile(*[_phase()] * 21), "at most 20 phases")


def test_library() -> None:
    lib = pf.ProfileLibrary()
    assert lib.active.label == "standard"
    mine = pf.parse_profile(_profile(_phase(pump=80), label="Mein Profil"))
    assert lib.add(mine) is False
    assert lib.labels[-1] == "Mein Profil"
    try:
        lib.add(pf.parse_profile(_profile(_phase(), label="mein profil")))
    except pf.LibraryError as err:
        assert err.key == pf.ERR_EXISTS
    else:
        raise AssertionError("collision accepted")
    for label in ("blooming", "blooming"):
        try:
            lib.add(pf.parse_profile(_profile(_phase(), label=label)), overwrite=True)
        except pf.LibraryError as err:
            assert err.key == pf.ERR_BUILTIN
        else:
            raise AssertionError("built-in overwritten")
    lib.select("MEIN PROFIL")
    assert lib.active.label == "Mein Profil"
    newer = pf.parse_profile(_profile(_phase(pump=60), label="mein Profil"))
    assert lib.add(newer, overwrite=True) is True
    assert lib.active.phases[0].power == 60 and len(lib.user) == 1
    data = json.loads(json.dumps(lib.to_store()))
    data["user"].append({"label": "kaputt", "phases": [{"pump": {"target": "pressure"}}]})
    again = pf.ProfileLibrary()
    assert again.load(data) == 1  # the invalid one is skipped
    assert again.active.label == "mein Profil" and [p.label for p in again.user] == ["mein Profil"]
    try:
        lib.remove("standard")
    except pf.LibraryError as err:
        assert err.key == pf.ERR_BUILTIN
    else:
        raise AssertionError("built-in deleted")
    lib.remove("mein profil")
    assert lib.active.label == "standard" and lib.user == []
    try:
        lib.select("nope")
    except pf.LibraryError as err:
        assert err.key == pf.ERR_UNKNOWN
    else:
        raise AssertionError
    # Unknown stored active profile -> the given default.
    fresh = pf.ProfileLibrary()
    fresh.load({"active": "gone", "user": []}, default_active="target_time")
    assert fresh.active.label == "target_time"
    fresh.load(None, default_active="target_time")
    assert fresh.active.label == "target_time"


# ------------------------------------------------------------ executor


def _times(run_: pf.ProfileRun, until: float, step: float = 0.25, **kw) -> list:
    out = []
    t = 0.0
    while t <= until + 1e-9:
        new = run_.update(t, **kw)
        if new is not None:
            out.append((round(t, 2), new))
        t += step
    return out


def test_power_floor_and_explicit_zero() -> None:
    prof = pf.parse_profile(_profile(
        _phase(pump=20, duration=2, name="low"),
        _phase(pump=0, duration=2, name="off"),
        _phase(pump=100, duration=4, name="ramp", transition={"type": "linear", "duration": 4}),
    ))
    r = pf.ProfileRun(prof, floor=45.0, grid=tm.PowerGrid(0.0, 100.0, 2.0))
    cmds = _times(r, 10.0, step=0.5)
    assert cmds[0] == (0.0, 46.0)  # 20 % -> the floor, rounded UP on the grid
    assert (2.0, 0.0) in cmds  # an explicit 0 % (bloom) goes below the floor
    ramp = [p for t, p in cmds if 4.0 <= t < 8.0]
    # From 0 the ramp's values below the floor are raised to it.
    assert ramp[0] == 46.0 and ramp == sorted(ramp) and all(p >= 46.0 for p in ramp)
    assert cmds[-1][1] == 100.0
    assert [p[2] for p in r.phases] == ["low", "off", "ramp", None]
    assert [p[3] for p in r.phases] == [None, "duration", "duration", "duration"]
    assert r.done and r.command == 100.0


def test_transition_curves() -> None:
    curves = (("linear", 70.0), ("ease-in", 55.0), ("ease-out", 85.0), ("ease-in-out", 70.0))
    for kind, mid in curves:
        prof = pf.parse_profile(_profile(
            _phase(pump=40, duration=1),
            _phase(pump=100, duration=20, transition={"type": kind, "duration": 4}),
        ))
        r = pf.ProfileRun(prof, floor=0.0)
        r.update(0.0)
        r.update(1.0)
        r.update(3.0)
        assert math.isclose(r.command, mid, abs_tol=0.51), (kind, r.command)
        r.update(5.0)
        assert r.command == 100.0
    assert pf.ease("instant", 0.1) == 1.0


def test_exits_weight_flow_ratio_and_no_scale() -> None:
    prof = pf.builtin("soft_preinfusion")
    r = pf.ProfileRun(prof, floor=40.0)
    assert r.update(0.0, 0.0, 0.0) == 40.0
    assert r.update(3.0, 1.9, 0.6) is None
    r.update(3.5, 2.0, 0.7)  # weight >= 2 g: met, but must hold TARGET_HOLD
    assert r.index == 0
    r.update(4.0, 1.8, 0.7)  # vibration dip: not held, starts again
    r.update(4.2, 2.1, 0.7)
    r.update(5.0, 2.3, 0.7)
    assert r.index == 0
    r.update(5.2, 2.4, 0.7)  # held 1 s: the ramp starts (at 40 %)
    assert r.index == 1 and r.phases[-1][2:] == ["brew", "volumetric"]
    r.update(7.2, 5.0, 1.5)
    assert math.isclose(r.command, 70.0, abs_tol=0.51)
    # A single vibration frame of 2.5 g never ends the fill.
    r = pf.ProfileRun(prof, floor=40.0)
    for i in range(40):
        t = i * 0.25
        r.update(t, 2.5 if i % 4 == 0 else 0.4, 0.0)
    assert r.index == 0
    # Without scale data the weight exit is skipped: the duration ends it.
    r = pf.ProfileRun(prof, floor=40.0)
    _times(r, 14.75)
    assert r.index == 0 and r.no_scale_skips > 0
    r.update(15.0)
    assert r.index == 1 and r.phases[-1][3] == "duration"
    # Flow target (lte) and ratio target (needs the dose).
    flow_prof = pf.parse_profile(_profile(
        _phase(duration=30, targets=[{"type": "flow", "operator": "lte", "value": 0.5}]),
        _phase(duration=30, targets=[{"type": "ratio", "value": 1.0}]),
        _phase(duration=30),
    ))
    r = pf.ProfileRun(flow_prof, dose=None)
    r.update(0.0, 0.0, 1.0)
    # Before the first drops the scale flow is ~0: no flow exit yet.
    _times_from(r, 0.25, 3.0, weight=0.2, flow=0.0)
    assert r.index == 0 and r.drop_t is None
    _times_from(r, 3.25, 4.25, weight=3.0, flow=2.0)  # first drops: 1.5 g held 1 s
    assert r.drop_t == 3.25 and r.index == 0
    _times_from(r, 4.5, 5.75, weight=4.0, flow=0.4)
    assert r.index == 1 and r.phases[-1][3] == "flow"
    _times_from(r, 6.0, 8.0, weight=30.0, flow=2.0)
    assert r.index == 1 and r.ignored == [(1, 1.0)]  # no dose: ignored
    r.dose = 18.0
    r.update(8.5, 17.9, 2.0)
    _times_from(r, 9.0, 9.75, weight=18.0, flow=2.0)
    assert r.index == 1
    r.update(10.0, 18.2, 2.0)
    assert r.index == 2 and r.phases[-1][3] == "ratio"


def _times_from(run_: pf.ProfileRun, start: float, end: float, step: float = 0.25, **kw) -> None:
    t = start
    while t <= end + 1e-9:
        run_.update(round(t, 2), **kw)
        t += step


def test_first_drop_deadline() -> None:
    """End of the first weight-exit phase + next transition + following 0 %
    phases (and the ramp out of them) + DEADLINE_BUFFER; None when nothing
    delays the first drops against a shot without profile."""
    buf = pf.DEADLINE_BUFFER
    assert buf == 3.0
    assert pf.builtin("standard").first_drop_deadline is None
    assert pf.builtin("target_time").first_drop_deadline is None
    # 100 % until the drops, no pause after: no later than without profile.
    assert pf.builtin("allonge").first_drop_deadline is None
    assert pf.builtin("lever").first_drop_deadline is None
    assert pf.builtin("soft_preinfusion").first_drop_deadline == 15 + 4 + buf  # 22
    assert pf.builtin("blooming").first_drop_deadline == 12 + 30 + 5 + buf  # 50
    # Reduced power before the weight phase: the time before it counts.
    p = pf.parse_profile(_profile(
        _phase(pump=60, duration=5),
        _phase(pump=80, duration=10, targets=[{"type": "volumetric", "value": 2}]),
        _phase(pump=100, duration=30, transition={"type": "ease-in", "duration": 3}),
    ))
    assert p.first_drop_deadline == 5 + 10 + 3 + buf
    # A pause after a 100 % fill makes it meaningful; two pauses add up.
    p = pf.parse_profile(_profile(
        _phase(pump=100, duration=8, targets=[{"type": "volumetric", "value": 1.5}]),
        _phase(pump=0, duration=10),
        _phase(pump=0, duration=5, transition={"type": "linear", "duration": 1}),
        _phase(pump=90, duration=30),
    ))
    assert p.first_drop_deadline == 8 + 10 + 1 + 5 + buf
    # lte weight targets are no first-drop exits.
    p = pf.parse_profile(_profile(
        _phase(
            pump=50, duration=8,
            targets=[{"type": "volumetric", "operator": "lte", "value": 1}],
        ),
    ))
    assert p.first_drop_deadline is None
    over = pf.parse_profile(_profile(_phase(), x_first_drop_deadline=35))
    assert over.first_drop_deadline == 35.0
    assert over.to_json()["x_first_drop_deadline"] == 35
    _raises(_profile(_phase(), x_first_drop_deadline=0), "x_first_drop_deadline")
    _raises(_profile(_phase(), x_first_drop_deadline="20"), "must be a number")
    assert pf.builtin("blooming").has_bloom and not pf.builtin("standard").has_bloom


def test_flow_phase_start_power() -> None:
    flow_prof = pf.parse_profile(_profile(
        _phase(pump=80, duration=2),
        _phase(pump={"target": "flow", "flow": 2.0}, duration=30),
    ))
    # Not learned: the power before (80 %).
    r = pf.ProfileRun(flow_prof, model=tm.ThrottleModel(), floor=30.0)
    r.update(0.0, 1.0, 3.0, 80.0)
    r.update(2.0, 5.0, 3.0, 80.0)
    assert r.index == 1 and r.command == 80.0
    # Learned (gamma 2, one step): g^-1(g(80) * 2 / 3) = 80 * sqrt(2/3).
    model = tm.ThrottleModel()
    model.gamma, model.count = 2.0, 1
    r = pf.ProfileRun(flow_prof, model=model, floor=30.0)
    r.update(0.0, 1.0, 3.0, 80.0)
    assert r.update(2.0, 5.0, 3.0, 80.0) == round(80 * math.sqrt(2 / 3))
    # After a 0 % bloom (no model): 100 %, reached over the transition.
    bloom = pf.builtin("blooming")
    r = pf.ProfileRun(bloom, floor=40.0, model=tm.ThrottleModel())
    r.update(0.0, 0.0, 0.0)
    _times_from(r, 3.0, 4.0, weight=1.6, flow=0.3)  # first drops held 1 s
    assert r.command == 0.0 and r.index == 1
    r.update(34.0, 2.0, 0.1)
    assert r.index == 2 and r.command == 0.0  # the ramp starts at 0 ...
    r.update(34.25, 2.0, 0.1)
    assert r.command == 40.0  # ... and is raised to the floor at once
    r.update(36.5, 3.0, 0.5)
    assert math.isclose(r.command, 50.0, abs_tol=0.51)
    r.update(39.0, 4.0, 1.0)
    assert r.command == 100.0
    # The controller waits FLOW_WAIT after that change, then corrects.
    assert r.update(41.0, 8.0, 3.2) is None
    new = r.update(39.0 + pf.FLOW_WAIT, 9.0, 3.2, 100.0)
    assert new is not None and 100.0 - pf.FLOW_STEP_MAX <= new < 100.0


def test_flow_controller_holds_before_drops_and_bounds() -> None:
    prof = pf.parse_profile(_profile(_phase(pump={"target": "flow", "flow": 2.0}, duration=600)))
    r = pf.ProfileRun(prof, floor=40.0, model=tm.ThrottleModel())
    assert r.update(0.0, 0.0, 0.0, 100.0) == 100.0
    # Vibration frames above 1.5 g that do not hold are no first drops.
    for i in range(20):
        assert r.update(5.0 + i * 0.25, 1.6 if i % 3 == 0 else 0.3, 5.0, 100.0) is None
    assert r.drop_t is None
    assert r.update(10.0, None, None, 100.0) is None  # no scale data
    assert r.update(10.5, 5.0, 8.0, 100.0) is None  # drops seen, not held yet
    new = r.update(11.5, 5.0, 8.0, 100.0)
    assert r.drop_t == 10.5
    assert new == 100.0 - pf.FLOW_STEP_MAX  # bounded step
    assert r.update(11.6, 6.0, 8.0, 85.0) is None  # waits for the flow window
    # Never below the floor, never above 100.
    t = 11.5
    for _ in range(20):
        t += pf.FLOW_WAIT
        r.update(t, 10.0, 8.0, r.command)
    assert r.command == 40.0
    for _ in range(20):
        t += pf.FLOW_WAIT
        r.update(t, 10.0, 0.1, r.command)
    assert r.command == 100.0
    # Within the tolerance: no correction.
    r2 = pf.ProfileRun(prof, floor=40.0)
    r2.update(0.0, 0.0, 0.0)
    r2.update(4.0, 3.0, 2.0, 100.0)
    assert r2.update(5.0, 3.0, 2.0 + pf.FLOW_TOLERANCE * 0.9, 100.0) is None


def test_after_last_phase_full_power_and_finish() -> None:
    prof = pf.parse_profile(_profile(_phase(pump=60, duration=3)))
    r = pf.ProfileRun(prof, floor=40.0)
    assert r.update(0.0) == 60.0
    assert r.update(3.0) == 100.0 and r.done
    r2 = pf.ProfileRun(prof, floor=40.0)
    r2.update(0.0)
    r2.finish(1.2)
    assert r2.done and r2.phases[-1] == [1.2, 0, None, "end"]


def test_trace_thinned() -> None:
    prof = pf.parse_profile(_profile(
        _phase(pump=40, duration=1),
        _phase(pump=100, duration=600, transition={"type": "linear", "duration": 600}),
    ))
    r = pf.ProfileRun(prof, floor=0.0, grid=tm.PowerGrid(0.0, 1000.0, 1.0))
    _times(r, 600.0, step=0.5)
    assert len(r.trace) <= pf.ProfileRun.TRACE_MAX
    assert r.trace[0][1] == 40.0 and r.trace[-1][1] >= 99.9
    assert [p[0] for p in r.trace] == sorted(p[0] for p in r.trace)


# ---------------------------------------------------- simulated shots


def _machines():
    for mname, machine in (("M1", MACHINE_1), ("M2", MACHINE_2)):
        for gname, grind in (("fine", FINE), ("coarse", COARSE)):
            yield f"{mname}/{gname}", {**machine, **grind}


def _phase_names(res) -> list:
    return [p[2] for p in res.phases]


def test_builtins_in_simulation() -> None:
    """Every built-in on both machines, fine and coarse: one writer, the
    floor holds (only an explicit 0 % below), the pump ends at 100 % (or
    the cut comes first), the phases come in their order."""
    for name, kw in _machines():
        floor = kw.get("floor", 40.0)
        grid = tm.PowerGrid(*(kw.get("grid") or (0.0, 100.0, 2.0)))
        floor_q = grid.percent(grid.floor_value(floor))
        for prof in pf.BUILTINS:
            target = 90.0 if prof.label == "allonge" else 40.0
            for seed in SEEDS:
                res = run_profile(Shot(seed=seed, target=target, **kw), prof)
                assert res.end is not None, (name, prof.label, seed)
                if prof.throttle:
                    assert res.throttle is not None and res.run is None
                    continue
                powers = [p for _, p in res.applied]
                ok = all(p == 0.0 or p >= floor_q - 1e-6 for p in powers)
                assert ok, (name, prof.label, powers)
                assert all(p <= 100.0 for p in powers)
                values = [p * grid.high / 100 for p in powers]
                assert all(abs(v - round(v / grid.step) * grid.step) < 1e-4 for v in values)
                names = [n for n in _phase_names(res) if n is not None]
                assert names == [ph.name for ph in prof.phases][: len(names)], names
                if prof.label == "standard":
                    assert powers == [100.0]
                if 0.0 in powers:
                    assert prof.label == "blooming"


def test_soft_preinfusion_simulated() -> None:
    prof = pf.builtin("soft_preinfusion")
    for name, kw in _machines():
        floor = kw.get("floor", 40.0)
        for seed in SEEDS:
            res = run_profile(Shot(seed=seed, **kw), prof)
            first = res.applied[0][1]
            assert first == max(40.0, floor) or abs(first - 40.0) < 0.4, (name, first)
            ends = [p for p in res.phases if p[2] == "brew"]
            assert ends and ends[0][3] == "volumetric", (name, res.phases)
            assert ends[0][0] < 15.0
            # The ramp reaches 100 % 4 s after it starts (or the cut came).
            t_ramp = ends[0][0]
            assert any(p == 100.0 and t <= t_ramp + 4.3 for t, p in res.applied), res.applied
            # Slower than the same shot at full power.
            full = run_profile(Shot(seed=seed, **kw), pf.builtin("standard"))
            assert res.end > full.end


def test_blooming_simulated() -> None:
    """Fine grind (60 g, so the controller has time): fill until the first
    drops, 30 s at 0 %, ramp, then the flow settles near 2.2 g/s. Coarse:
    the flow at the floor is above the target (the floor wins: the trace
    ends at the floor)."""
    prof = pf.builtin("blooming")
    for name, kw in _machines():
        for seed in SEEDS:
            # 60 g: long enough for the controller to settle after the ramp.
            res = run_profile(Shot(seed=seed, target=60.0, max_time=120.0, **kw), prof)
            phases = res.phases
            assert phases[1][2] == "bloom" and phases[1][3] == "volumetric", phases
            assert phases[2][2] == "flow" and phases[2][3] == "duration"
            assert math.isclose(phases[2][0] - phases[1][0], 30.0, abs_tol=0.3)
            bloom = [p for t, p in res.applied if phases[1][0] <= t < phases[2][0]]
            assert bloom == [0.0], bloom
            if "fine" in name:
                # Last 4 s before the cut (the second machine's puck keeps
                # washing out: the slow controller trails it a little).
                late = [f for t, f in res.flows if t > res.end - 4.0]
                mean = statistics.mean(late)
                assert abs(mean - 2.2) < 0.4, (name, seed, mean)
            else:
                floor = kw.get("floor", 40.0)
                assert min(p for t, p in res.applied if t > phases[2][0]) <= floor + 1.0


def test_allonge_simulated() -> None:
    """Coarse: the flow is pulled down towards 4.5 g/s; fine: the target is
    above what 100 % gives, the pump stays at 100 %."""
    prof = pf.builtin("allonge")
    for name, kw in _machines():
        for seed in SEEDS:
            res = run_profile(Shot(seed=seed, target=90.0, **kw), prof)
            start = res.phases[1][0]
            if "coarse" in name:
                assert min(p for _, p in res.applied) < 90.0, res.applied
                full = run_profile(Shot(seed=seed, target=90.0, **kw), pf.builtin("standard"))
                # The flow only exceeds 4.5 g/s late in this short shot: it
                # ends later, but not by much (an honest limit of a slow
                # controller on a 20 s shot).
                assert res.end > full.end
                powers = [p for t, p in res.applied if t > start]
                assert powers == sorted(powers, reverse=True)  # no overshoot back up
            else:
                assert [p for _, p in res.applied] == [100.0]


def test_lever_simulated() -> None:
    prof = pf.builtin("lever")
    for name, kw in _machines():
        for seed in SEEDS:
            res = run_profile(Shot(seed=seed, target=40.0, max_time=90.0, **kw), prof)
            phases = res.phases
            assert phases[1][2] == "peak" and phases[1][3] == "volumetric"
            if "fine" in name:
                decline = [p for t, p in res.applied if t > phases[2][0]]
                assert decline == sorted(decline, reverse=True)
                assert min(decline) < 90.0


def test_flow_controller_stable_with_lag() -> None:
    """Puck at a steady flow, 1.0 and 1.5 s total lag (call + PSM + pump),
    pump exponents 1-2.2: the flow settles at the target (mean within
    0.15 g/s) and the power only dithers by at most four 2 % grid steps
    (scale noise; the steep gamma-2.2 pump dithers most) and does not grow
    (the last 15 s swing no more than the 15 s before)."""
    prof = pf.parse_profile({"label": "T", "phases": [
        {"name": "fill", "duration": 12, "pump": 100,
         "targets": [{"type": "volumetric", "value": 0.5}]},
        {"name": "flow", "duration": 120, "pump": {"target": "flow", "flow": 3.0}},
    ]})
    for lag in (1.0, 1.5):
        for gamma in (1.0, 1.6, 2.2):
            for seed in (1, 2, 3):
                shot = Shot(seed=seed, q_max=5.0, puck_p=1.55, first_drop=4.0,
                            target=500.0, max_time=60.0, pump_dead_time=lag - 0.5,
                            pump_lag=0.5, gamma_true=gamma)
                res = run_profile(shot, prof)
                late = [f for t, f in res.flows if t > 35.0]
                assert abs(statistics.mean(late) - 3.0) < 0.15, (lag, gamma, seed)
                powers = [p for t, p in res.applied if t > 30.0]
                assert max(powers) - min(powers) <= 8.0 + 1e-6, (lag, gamma, powers)
                early = [p for t, p in res.applied if 30.0 < t <= 45.0]
                later = [p for t, p in res.applied if t > 45.0]
                if early and later:
                    assert max(later) - min(later) <= max(early) - min(early) + 2.0 + 1e-6


def test_zielzeit_is_the_throttle() -> None:
    """Zielzeit runs the 0.6.0 throttle unchanged: same cut, trigger and
    commands as ``run`` (the throttle's own tests are untouched); its phase
    alone never commands anything but 100 %."""
    prof = pf.builtin("target_time")
    for kw in (MACHINE_1, dict(MACHINE_2, first_drop=4.0, target_time=27.0, target=45.0)):
        for seed in SEEDS:
            shot = Shot(seed=seed, **kw)
            a = run_profile(shot, prof)
            b = run(shot)
            assert a.end == b.end and a.throttle.powers == b.powers
            assert a.throttle.trigger_at == b.trigger_at and a.trace == b.trace
    r = pf.ProfileRun(prof)
    assert _times(r, 200.0, step=1.0, weight=10.0, flow=2.0) == [(0.0, 100.0)]


def test_without_scale_time_exits_only() -> None:
    prof = pf.builtin("blooming")
    res = run_profile(Shot(seed=1, max_time=60.0), prof, scale=False)
    assert res.end is None
    phases = res.phases
    assert [p[2] for p in phases[:3]] == ["fill", "bloom", "flow"]
    assert math.isclose(phases[1][0], 12.0, abs_tol=0.3) and phases[1][3] == "duration"
    assert math.isclose(phases[2][0], 42.0, abs_tol=0.3)
    # The flow phase holds its start power (100 % after the bloom).
    assert res.applied[-1][1] == 100.0
    assert all(p in (0.0, 100.0) or 40.0 <= p <= 100.0 for _, p in res.applied)


def test_observe_never_applies() -> None:
    from dataclasses import replace

    prof = pf.builtin("soft_preinfusion")
    obs = run_profile(replace(Shot(seed=2), apply=False), prof)
    full = run_profile(Shot(seed=2), pf.builtin("standard"))
    assert obs.applied == []
    assert obs.end == full.end  # the shot ran at 100 %
    assert obs.trace[0][1] == 40.0  # what it would have set


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"OK: {len(tests)} tests passed")
