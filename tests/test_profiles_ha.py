"""Pump profiles (0.7.0) at the Home Assistant level.

Same harness as ``test_throttle_ha`` (scale frames at 10 Hz on a
controlled clock, a fake pump ``number``). The profile run's clock tick is
driven by hand (``profiles.tick()``) where no frames come.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from collections.abc import Generator  # noqa: E402
from unittest.mock import patch  # noqa: E402

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402

from test_integration import OPTIONS, PLUG_ATTRS, Scale, _setup, _state, mock_plug  # noqa: E402
from test_throttle_ha import (  # noqa: E402
    PLUG,
    PUMP,
    Pump,
    ThrottleClock,
    _events,
    _relay,
    _shot,
)

from conftest import Clock  # noqa: E402
from custom_components.difluid_scale import throttle as throttle_mod  # noqa: E402
from custom_components.difluid_scale.const import DOMAIN, STOP_STORAGE_KEY  # noqa: E402

SELECT = "select.microbalance_pump_profile"
MODE = "select.microbalance_pump_throttle"
MINE = {
    "label": "Mein Profil",
    "type": "pro",
    "temperature": 93,
    "x_mine": {"keep": True},
    "phases": [
        {"name": "Vor", "phase": "preinfusion", "valve": 1, "duration": 6, "pump": 50},
        {"name": "brew", "phase": "brew", "duration": 60, "pump": 100,
         "transition": {"type": "linear", "duration": 2}},
    ],
}


@pytest.fixture
def tclock(clock: Clock) -> Generator[Clock]:
    """The throttle's (and the profile run's) clock = the test clock."""
    with patch.object(throttle_mod, "time", ThrottleClock(clock)):
        yield clock


async def _setup_profiles(
    hass: HomeAssistant, entry, *, mode: str = "throttle", profile: str | None = None,
    pump: bool = True,
) -> None:
    options = dict(OPTIONS, stop_switch=PLUG)
    if pump:
        options["pump_power"] = PUMP
    hass.states.async_set(PLUG, "off", PLUG_ATTRS)
    hass.config_entries.async_update_entry(entry, options=options)
    await _setup(hass, entry)
    throttle = entry.runtime_data.autostop.throttle
    entry.runtime_data.autostop.set_target(36.0)
    throttle.set_mode(mode)
    if profile is not None:
        throttle.select_profile(profile)
    await hass.async_block_till_done()


def _tare(scale: Scale) -> None:
    """A cup placed (150 g) and tared, "Prepare brew" turned on (e.g. by an
    automation at the tare), the relay follows ~1 s later (0.7.0b2: the
    switch gates the profile, the tare alone does not)."""
    scale.run(1.0, 150.0)
    scale.coord.async_set_brew_ready(True)
    scale.run(1.0, 0.0)


async def _call(hass: HomeAssistant, service: str, data: dict, response: bool = True) -> Any:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=response
    )


# ------------------------------------------------------- select, services


async def test_select_options_default_and_persistence(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="observe")
    state = hass.states.get(SELECT)
    assert state is not None and state.state == "standard"  # a new store
    assert state.attributes["options"] == [
        "standard", "soft_preinfusion", "blooming", "allonge", "lever", "target_time",
    ]
    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": "blooming"}, blocking=True
    )
    assert _state(hass, SELECT) == "blooming"
    await _call(hass, "import_profile", {"profile": MINE}, response=False)
    assert hass.states.get(SELECT).attributes["options"][-1] == "Mein Profil"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": "Mein Profil"}, blocking=True
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, SELECT) == "Mein Profil"
    stored = hass_storage[STOP_STORAGE_KEY.format(entry.entry_id)]
    assert stored["minor_version"] == 4  # 0.7.0b5: built-in profile ids
    profiles = stored["data"]["throttle"]["profiles"]
    assert profiles["active"] == "Mein Profil"
    assert profiles["user"] == [MINE]  # unknown fields kept
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": SELECT, "option": "Nope"}, blocking=True
        )


async def test_import_export_delete_services(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="observe")
    throttle = entry.runtime_data.autostop.throttle
    res = await _call(hass, "import_profile", {"profile": json.dumps(MINE), "activate": True})
    assert res == {"label": "Mein Profil", "replaced": False, "active": "Mein Profil", "phases": 2}
    assert _state(hass, SELECT) == "Mein Profil"
    # Collision (case-insensitive) without overwrite, and with it.
    other = {**MINE, "label": "mein profil", "description": "neu"}
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "import_profile", {"profile": other})
    assert err.value.translation_key == "profile_exists"
    res = await _call(hass, "import_profile", {"profile": other, "overwrite": True})
    assert res["replaced"] is True and throttle.profiles.active.label == "mein profil"
    # Built-ins cannot be overwritten.
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "import_profile", {"profile": {**MINE, "label": "blooming"}, "overwrite": True}
        )
    assert err.value.translation_key == "profile_builtin"
    # Pressure needs a sensor: a clear validation error.
    bad = {**MINE, "label": "Druck", "phases": [
        {"name": "x", "duration": 10, "pump": {"target": "pressure", "pressure": 9}}]}
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "import_profile", {"profile": bad})
    assert err.value.translation_key == "profile_invalid"
    assert "pressure sensor" in err.value.translation_placeholders["error"]
    with pytest.raises(ServiceValidationError):
        await _call(hass, "import_profile", {"profile": "{not json"})
    # Export: the active one, a built-in by label.
    res = await _call(hass, "export_profile", {})
    assert res["label"] == "mein profil" and res["builtin"] is False and res["active"]
    assert res["profile"]["x_mine"] == {"keep": True} and res["profile"]["description"] == "neu"
    res = await _call(hass, "export_profile", {"label": "blooming"})
    assert res["label"] == "blooming" and res["builtin"] is True
    assert res["profile"]["phases"][1]["pump"] == 0
    with pytest.raises(ServiceValidationError):
        await _call(hass, "export_profile", {"label": "Nope"})
    # Delete: built-ins never; the active one falls back to Standard.
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "delete_profile", {"label": "standard"})
    assert err.value.translation_key == "profile_builtin"
    res = await _call(hass, "delete_profile", {"label": "MEIN PROFIL"})
    assert res == {"label": "mein profil", "active": "standard"}
    assert _state(hass, SELECT) == "standard"
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "delete_profile", {"label": "mein profil"})
    assert err.value.translation_key == "profile_unknown"


async def test_bean_default_profile(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="observe")
    autostop = entry.runtime_data.autostop
    res = await _call(hass, "add_bean", {"name": "Kenia", "ratio": 2.2, "profile": "blooming"})
    assert res["profile"] == "blooming"
    assert _state(hass, SELECT) == "blooming"  # activated with the bean
    await _call(hass, "add_bean", {"name": "Brasil", "ratio": 2.0, "activate": False})
    # A bean without a profile leaves the active one (0.7.0b6: until a
    # profile is chosen while it is active, which it then remembers).
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.microbalance_bean", "option": "Brasil"},
        blocking=True,
    )
    assert _state(hass, SELECT) == "blooming"
    assert "profile" not in autostop.active_bean
    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": "target_time"}, blocking=True
    )
    assert autostop.active_bean["profile"] == "target_time"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.microbalance_bean", "option": "Kenia"},
        blocking=True,
    )
    assert _state(hass, SELECT) == "blooming"
    assert autostop.active_bean["profile"] == "blooming"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.microbalance_bean", "option": "Brasil"},
        blocking=True,
    )
    assert _state(hass, SELECT) == "target_time"
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "add_bean", {"name": "X", "ratio": 2.0, "profile": "Nope"})
    assert err.value.translation_key == "profile_unknown"


async def test_old_store_keeps_the_throttle(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, tclock
) -> None:
    """A 0.6.0 store (throttle part without profiles): Zielzeit is active,
    so an update keeps its behaviour."""
    await _setup_profiles(hass, entry, mode="throttle")
    stop = entry.runtime_data.autostop
    stop.async_schedule_save()
    await stop.async_flush()
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    data = hass_storage[key]
    del data["data"]["throttle"]["profiles"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, SELECT) == "target_time"


async def test_diagnostics_carry_profiles(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    from custom_components.difluid_scale.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await _setup_profiles(hass, entry, mode="observe", profile="blooming")
    await _call(hass, "import_profile", {"profile": MINE}, response=False)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["throttle"]["profiles"] == {"active": "blooming", "user_profiles": 1}
    assert "Mein Profil" not in json.dumps(diag)  # contents are not dumped


# ------------------------------------------------------------- shots


async def test_profile_shot_applied_and_restored(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Sanfte Vorbrühung in throttle mode: 40 % from the relay start, the
    ramp after 2 g, cut by the auto-stop, pump back to 100 %; the throttle
    never runs (one writer), the brew stores the profile record."""
    await _setup_profiles(hass, entry, profile="soft_preinfusion")
    mock_plug(hass)
    pump = Pump(hass, step=2.0)
    coord = entry.runtime_data
    throttle = coord.autostop.throttle
    events = _events(hass)
    scale = Scale(coord, tclock)
    _tare(scale)
    info = await _shot(hass, scale, tclock, pump=pump)
    assert "cut" in info
    assert pump.calls[0] == 40.0
    assert pump.calls[-1] == 100.0 and pump.percent == 100.0
    assert all(40.0 <= v <= 100.0 for v in pump.calls)
    assert all(a != b for a, b in zip(pump.calls, pump.calls[1:], strict=False)), str(pump.calls)
    # Single writer: no throttle shot, no throttle events.
    assert throttle._brew.shot is None  # noqa: SLF001
    assert events == []
    assert throttle.profiles.current is None  # ended at the cut
    brew = coord.brews.last
    record = brew["throttle"]
    assert record["profile"] == "soft_preinfusion"
    assert record["reason"] == "profile" and record["active"] is False
    assert record["applied"] is True and record["target_time"] is None
    assert [p[2] for p in record["phases"]][:2] == ["preinfusion", "brew"]
    assert record["phases"][1][3] == "volumetric"
    assert record["power"][0][1] == 40.0 and record["power"][-1][1] == 100.0
    # Profile times are brew times (relay offset 0.6 s before the timer).
    assert record["power"][0][0] == pytest.approx(-0.6, abs=0.05)
    assert coord.autostop.last["throttled"] is True  # the pump was lowered
    assert brew["relay_offset"] == pytest.approx(0.6, abs=0.005)


async def test_observe_never_writes_the_pump(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="observe", profile="blooming")
    mock_plug(hass)
    pump = Pump(hass)
    hass.states.async_set(PUMP, "80", pump.attrs)  # not at 100: still untouched
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    _tare(scale)
    await _shot(hass, scale, tclock, pump=None, seconds=50.0)
    assert pump.calls == []
    assert _state(hass, PUMP) == "80"
    record = coord.brews.last["throttle"]
    assert record["mode"] == "observe" and record["applied"] is False
    assert record["profile"] == "blooming"
    assert [p[1] for p in record["power"]][:2] == [100.0, 0.0]  # would bloom
    assert coord.autostop.last["throttled"] is False


async def test_mode_off_runs_nothing(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="off", profile="blooming")
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    _relay(hass, True)
    await hass.async_block_till_done()
    assert coord.autostop.throttle.profiles.current is None
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump, relay=False, seconds=10.0)
    assert pump.calls == []


async def test_zielzeit_runs_only_the_throttle(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Zielzeit: no profile run at all (the throttle is the only writer)."""
    await _setup_profiles(hass, entry, profile="target_time")
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    throttle = coord.autostop.throttle
    _relay(hass, True)
    await hass.async_block_till_done()
    assert throttle.profiles.current is None
    _relay(hass, False)
    await hass.async_block_till_done()
    info = await _shot(hass, Scale(coord, tclock), tclock, pump=pump)
    assert "cut" in info and pump.calls and min(pump.calls) < 100.0
    record = coord.brews.last["throttle"]
    assert record["profile"] == "target_time" and record["triggered"]
    assert record["target_time"] == 30.0 and "phases" not in record


async def test_profile_without_scale_runs_on_time(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """No scale brew (tared, then the scale sends nothing): durations still
    end phases (clock ticks), weight exits are skipped; relay off ends the
    run and restores 100 %."""
    await _setup_profiles(hass, entry, profile="blooming")
    pump = Pump(hass)
    coord = entry.runtime_data
    profiles = coord.autostop.throttle.profiles
    _tare(Scale(coord, tclock))
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is not None
    seen = []
    for _ in range(int(50 / 0.25)):
        tclock.t = round(tclock.t + 0.25, 3)
        profiles.tick()
        await hass.async_block_till_done(wait_background_tasks=True)
        seen.append(pump.percent)
    phases = profiles.current.run.phases
    assert [p[2] for p in phases] == ["fill", "bloom", "flow"]
    assert phases[1][0] == pytest.approx(12.0, abs=0.3) and phases[1][3] == "duration"
    assert 0.0 in seen  # the bloom
    assert seen[-1] == 100.0  # flow phase without scale: holds its start power
    _relay(hass, False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is None and pump.percent == 100.0


async def test_relay_off_mid_bloom_restores_and_unload(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, profile="blooming")
    pump = Pump(hass)
    profiles = entry.runtime_data.autostop.throttle.profiles
    scale = Scale(entry.runtime_data, tclock)
    _tare(scale)
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    for _ in range(60):  # 15 s: into the bloom
        tclock.t = round(tclock.t + 0.25, 3)
        profiles.tick()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.percent == 0.0
    # Audit B4: options saved mid-shot that do not touch relay or pump
    # (a detection threshold, a bean, the bean's ratio) keep the run.
    autostop = entry.runtime_data.autostop
    hass.config_entries.async_update_entry(entry, options={**entry.options, "tail_seconds": 4.0})
    await hass.async_block_till_done(wait_background_tasks=True)
    await _call(hass, "add_bean", {"name": "Kenya", "ratio": 2.2}, response=False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert autostop.active_bean is not None
    autostop.set_ratio(2.4)  # written back into the bean's options
    await hass.async_block_till_done(wait_background_tasks=True)
    # As its ratio for the active profile (0.7.0b6).
    assert autostop.active_bean["ratios"]["blooming"] == 2.4
    assert profiles.current is not None and not profiles.current.ended
    assert pump.percent == 0.0
    # Another pump entity in the options: the run ends, the pump goes back.
    hass.states.async_set("number.other_pump", "100", {"min": 0, "max": 100, "step": 1})
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "pump_power": "number.other_pump"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is None and profiles.last_run.end_reason == "options"
    assert pump.percent == 100.0
    hass.config_entries.async_update_entry(entry, options={**entry.options, "pump_power": PUMP})
    await hass.async_block_till_done(wait_background_tasks=True)
    # Unload mid-shot (a new run first, after a new tare).
    _relay(hass, False)
    _tare(scale)
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    for _ in range(60):
        tclock.t = round(tclock.t + 0.25, 3)
        profiles.tick()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.percent == 0.0
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert pump.percent == 100.0


async def test_live_carries_profile(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, profile="soft_preinfusion")
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    _tare(scale)
    await _shot(hass, scale, tclock, pump=pump, seconds=5.0)
    live = coord.live_snapshot().get("throttle")
    assert live is not None and live["profile"] == "soft_preinfusion"
    assert live["phase"] == "preinfusion" and live["power"][0][1] == 40.0
    assert live["target_time"] is None and live["applied"] is True


# ------------------------------------------------- review fixes (0.7.0b1)


async def _tick(hass: HomeAssistant, tclock, profiles, seconds: float) -> None:
    for _ in range(round(seconds / 0.25)):
        tclock.t = round(tclock.t + 0.25, 3)
        profiles.tick()
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_prepare_brew_gates_profiles(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """0.7.0b2: "Prepare brew" on at the relay start -> the profile runs;
    relay pulses without it (backflush program 10 x 15 s on / 5 s off)
    never lower the pump, a tare alone (button or weight step) does not
    count any more, and the switch going off mid-shot does not end the run."""
    await _setup_profiles(hass, entry, profile="blooming")
    pump = Pump(hass)
    coord = entry.runtime_data
    profiles = coord.autostop.throttle.profiles
    scale = Scale(coord, tclock)
    # Cleaning: the scale is on (portafilter, no cup), nothing prepared.
    for _ in range(10):
        _relay(hass, True)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert profiles.current is None
        for _ in range(15):
            scale.run(1.0, 0.0)
            profiles.tick()
        _relay(hass, False)
        await hass.async_block_till_done(wait_background_tasks=True)
        scale.run(5.0, 0.0)
    assert pump.calls == [] and pump.percent == 100.0
    assert profiles.last_skip is not None and profiles.last_skip[1] == "not_prepared"
    assert profiles.attributes()["last_outcome"] == "not_prepared"
    # A tare without "Prepare brew" (weight step, and the tare button): no run.
    from custom_components.difluid_scale.protocol import CMD_TARE

    scale.run(1.0, 150.0)
    scale.run(1.0, 0.0)
    await coord.async_send(CMD_TARE)
    scale.run(0.5, 0.0)
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is None and pump.calls == []
    _relay(hass, False)
    await hass.async_block_till_done(wait_background_tasks=True)
    # Prepared (normal flow: the switch on, the relay ~1 s later): runs.
    _tare(scale)
    assert coord.brew_ready
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is not None
    assert profiles.attributes()["last_outcome"] == "applied"
    # "Prepare brew" off mid-shot (e.g. the brew ended): the run goes on.
    coord.async_set_brew_ready(False)
    await _tick(hass, tclock, profiles, 2.0)
    assert profiles.current is not None and not profiles.current.ended
    _relay(hass, False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is None and profiles.last_run.end_reason == "relay_off"


async def test_zielzeit_needs_no_prepare(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Zielzeit keeps its 0.6.0 gating (a relay start matched to a brew)."""
    await _setup_profiles(hass, entry, profile="target_time")
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    info = await _shot(hass, Scale(coord, tclock), tclock, pump=pump)
    assert "cut" in info and min(pump.calls) < 100.0


async def test_pump_unavailable_at_shot_end_is_restored_later(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Bloom (0 %) -> pump entity unavailable (Wi-Fi) -> relay off: the
    restore fails; when the pump comes back at 0.0 it is set to its maximum
    and the pending flag clears only then."""
    await _setup_profiles(hass, entry, profile="blooming")
    pump = Pump(hass)
    coord = entry.runtime_data
    throttle = coord.autostop.throttle
    profiles = throttle.profiles
    _tare(Scale(coord, tclock))
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _tick(hass, tclock, profiles, 15.0)
    assert pump.percent == 0.0 and PUMP in throttle.restore_pending
    pump.fail = True
    hass.states.async_set(PUMP, "unavailable", pump.attrs)
    _relay(hass, False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is None
    assert PUMP in throttle.restore_pending
    pump.fail = False
    calls = len(pump.calls)
    hass.states.async_set(PUMP, "0.0", pump.attrs)  # back, still at 0
    await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.calls[calls:] == [100.0] and pump.percent == 100.0
    assert PUMP not in throttle.restore_pending


async def test_pending_restore_survives_restart(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, tclock
) -> None:
    """Restart mid-bloom with the pump unreachable: the flag is stored; at
    setup, once the pump shows a number, it is restored, in any mode."""
    await _setup_profiles(hass, entry, profile="blooming")
    pump = Pump(hass)
    coord = entry.runtime_data
    throttle = coord.autostop.throttle
    _tare(Scale(coord, tclock))
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _tick(hass, tclock, throttle.profiles, 15.0)
    assert pump.percent == 0.0
    pump.fail = True  # the unload's restore fails
    throttle.set_mode("observe")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    stored = hass_storage[STOP_STORAGE_KEY.format(entry.entry_id)]["data"]["throttle"]
    assert stored["restore_pending"] == [PUMP]
    pump.fail = False
    hass.states.async_set(PLUG, "off", PLUG_ATTRS)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    entry.runtime_data.autostop.async_watch_switch()  # what async_at_started does
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.autostop.throttle.mode == "observe"
    assert pump.percent == 100.0
    assert entry.runtime_data.autostop.throttle.restore_pending == set()


async def test_relay_unavailable_ends_the_run(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, profile="blooming")
    pump = Pump(hass)
    coord = entry.runtime_data
    profiles = coord.autostop.throttle.profiles
    _tare(Scale(coord, tclock))
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _tick(hass, tclock, profiles, 15.0)
    assert pump.percent == 0.0
    hass.states.async_set(PLUG, "unavailable", PLUG_ATTRS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is None and pump.percent == 100.0
    assert profiles.last_run.end_reason == "relay_lost"


async def test_deleted_profile_leaves_beans(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Deleting a profile clears it from beans; the options form offers
    "none" and drops an unknown suggested profile; the newest bean's
    profile applies when it becomes active by a delete."""
    await _setup_profiles(hass, entry, mode="observe")
    await _call(hass, "import_profile", {"profile": MINE}, response=False)
    await _call(hass, "add_bean", {"name": "A", "ratio": 2.0, "profile": "Mein Profil"})
    assert _state(hass, SELECT) == "Mein Profil"
    await _call(hass, "delete_profile", {"label": "Mein Profil"})
    assert "profile" not in entry.options["beans"][0]
    assert _state(hass, SELECT) == "standard"
    # Options flow: "none" offered; a stale label is not suggested.
    beans = [dict(entry.options["beans"][0], profile="Gone")]
    hass.config_entries.async_update_entry(entry, options={**entry.options, "beans": beans})
    await hass.async_block_till_done()
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "edit_bean"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"id": beans[0]["id"]}
    )
    schema = flow["data_schema"].schema
    key = next(k for k in schema if str(k) == "profile")
    assert "none" in schema[key].config["options"]
    assert (key.description or {}).get("suggested_value") is None
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"name": "A", "ratio": 2.0, "profile": "none"}
    )
    assert result["type"] == "create_entry"
    assert "profile" not in entry.options["beans"][0]
    # Newest bean becomes active after deleting the active one: its profile too.
    await _call(
        hass, "add_bean", {"name": "B", "ratio": 2.0, "profile": "allonge", "activate": False}
    )
    await _call(hass, "delete_bean", {"name": "A"})
    await hass.async_block_till_done()
    assert entry.runtime_data.autostop.active_bean["name"] == "B"
    assert _state(hass, SELECT) == "allonge"


# ------------------------------------------------- review round 1 (0.7.0b1)

# A pump power ``number`` as ESPHome creates it (attribute names and types
# of ESPHome's number platform; no live state of the target pump exists yet,
# the dimmer is not installed).
ESPHOME_PUMP_ATTRS = {
    "min": 0.0,
    "max": 100.0,
    "step": 1.0,
    "mode": "slider",
    "unit_of_measurement": "%",
    "friendly_name": "Kaffeemaschine Pumpenleistung",
}


def _esphome_pump(hass: HomeAssistant, **kw) -> Pump:
    pump = Pump(hass, step=1.0, **kw)
    pump.attrs.update(ESPHOME_PUMP_ATTRS)
    hass.states.async_set(PUMP, "100.0", pump.attrs)
    return pump


async def _bloom(hass: HomeAssistant, entry, tclock, pump: Pump) -> Any:
    coord = entry.runtime_data
    profiles = coord.autostop.throttle.profiles
    _tare(Scale(coord, tclock))
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _tick(hass, tclock, profiles, 15.0)
    return profiles


async def test_pending_with_late_reporting_pump(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """T1: the entity reports only after the call returned: the flag must
    survive the stale 100 % that is still shown while 0 % is on its way."""
    await _setup_profiles(hass, entry, profile="blooming")
    pump = _esphome_pump(hass, report="delayed")
    throttle = entry.runtime_data.autostop.throttle
    await _bloom(hass, entry, tclock, pump)
    assert _state(hass, PUMP) == "0.0"
    assert throttle.restore_pending == {PUMP}
    _relay(hass, False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _state(hass, PUMP) == "100.0" and throttle.restore_pending == set()
    assert pump.calls == [0.0, 100.0]


async def test_pump_without_low_feedback_no_loop(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """T2: a pump that never reports values below its maximum, and one that
    reports on another grid (99.9 for 100): a few calls, never a loop; the
    flag clears once the maximum was asked for and is shown."""
    for kw in ({"report": "never_low"}, {"report_scale": 0.999}):
        await _setup_profiles(hass, entry, profile="blooming")
        pump = _esphome_pump(hass, **kw)
        throttle = entry.runtime_data.autostop.throttle
        await _bloom(hass, entry, tclock, pump)
        assert throttle.restore_pending == {PUMP}
        _relay(hass, False)
        await hass.async_block_till_done(wait_background_tasks=True)
        for _ in range(20):  # attribute-only updates, more loop turns
            hass.states.async_set(PUMP, hass.states.get(PUMP).state, {**pump.attrs, "x": _})
            await hass.async_block_till_done(wait_background_tasks=True)
        assert pump.calls == [0.0, 100.0], (kw, pump.calls)
        assert throttle.restore_pending == set(), kw
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_pending_pump_low_and_silent_is_retried_bounded(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """T2: a pending pump that shows a low value and never reports: the
    retry comes on 'back from unavailable' only, at most once per
    RESTORE_RETRY_INTERVAL, never in a tight loop."""
    await _setup_profiles(hass, entry, profile="blooming")
    pump = _esphome_pump(hass, feedback=False)
    # Let the pump's own state events settle first (HA 2026.5 dispatches
    # state listeners a loop turn later): its 100 % would clear the flag.
    await hass.async_block_till_done()
    throttle = entry.runtime_data.autostop.throttle
    throttle.restore_pending.add(PUMP)  # as a low command would
    throttle._track_pumps()  # noqa: SLF001
    hass.states.async_set(PUMP, "0.0", pump.attrs)
    for i in range(30):  # unavailable <-> 0.0 flapping, low state events
        hass.states.async_set(PUMP, "unavailable" if i % 2 else "0.0", pump.attrs)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.calls == [100.0]
    tclock.t += throttle_mod.RESTORE_RETRY_INTERVAL + 1
    hass.states.async_set(PUMP, "unavailable", pump.attrs)
    hass.states.async_set(PUMP, "0.0", pump.attrs)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.calls == [100.0, 100.0]
    assert throttle.restore_pending == {PUMP}  # never shown at max


async def test_startup_sequence_unknown_then_value(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, tclock
) -> None:
    """Restart with a pending pump: at setup the pump and the relay are
    still 'unknown'/'unavailable' (startup order), then the pump shows 0.0:
    restored once, in observe mode too; the relay's unknown -> off -> on
    start is a normal shot (unknown -> on is none: its start is unknown)."""
    await _setup_profiles(hass, entry, mode="observe", profile="blooming")
    stop = entry.runtime_data.autostop
    stop.throttle.restore_pending.add(PUMP)
    stop.async_schedule_save()
    await stop.async_flush()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    pump = _esphome_pump(hass, report="delayed")
    hass.states.async_set(PUMP, "unknown", pump.attrs)
    hass.states.async_set(PLUG, "unavailable", PLUG_ATTRS)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    throttle = entry.runtime_data.autostop.throttle
    throttle.async_watch(PLUG)  # what async_at_started does
    await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.calls == [] and throttle.restore_pending == {PUMP}
    hass.states.async_set(PUMP, "0.0", pump.attrs)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.calls == [100.0] and _state(hass, PUMP) == "100.0"
    assert throttle.restore_pending == set()
    # Relay: unavailable -> on (start unknown) runs nothing.
    _tare(Scale(entry.runtime_data, tclock))
    hass.states.async_set(PLUG, "on", PLUG_ATTRS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert throttle.profiles.current is None


async def test_prepare_switch_then_relay_at_once(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """The "Prepare brew" switch entity turned on, the relay 0.1 s later
    (before any scale frame): the profile runs. The switch's own
    10-minute backstop turns it off without a brew: then no run."""
    await _setup_profiles(hass, entry, profile="blooming")
    _esphome_pump(hass)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    scale.run(1.0, 160.0)  # cup on the scale
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.microbalance_prepare_brew"},
        blocking=True,
    )
    tclock.t += 0.1
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    profiles = coord.autostop.throttle.profiles
    assert profiles.current is not None
    _relay(hass, False)
    await hass.async_block_till_done(wait_background_tasks=True)
    # Prepared, but no brew within 10 minutes: the switch turns itself off.
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    coord.async_set_brew_ready(True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=601))
    await hass.async_block_till_done()
    assert not coord.brew_ready
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is None and profiles.last_skip[1] == "not_prepared"


async def test_late_timer_and_bloom_never_end_the_brew(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """D-C: the scale timer starts 10 s late (after the first drops): the
    fill still ends on the weight; the 30 s bloom (no flow) neither cuts
    nor ends the brew; the auto-stop cuts after the bloom."""
    await _setup_profiles(hass, entry, profile="blooming")
    mock_plug(hass)
    pump = _esphome_pump(hass)
    coord = entry.runtime_data
    profiles = coord.autostop.throttle.profiles
    scale = Scale(coord, tclock)
    _tare(scale)
    stored_before = coord.brews.last
    info = await _shot(hass, scale, tclock, pump=pump, timer_delay=10.0, seconds=90.0)
    run = profiles.last_run
    phases = run.run.phases
    assert phases[1][2] == "bloom" and phases[1][3] == "volumetric", phases
    assert phases[1][0] < 10.0  # before the scale timer started
    bloom_end = phases[2][0]
    assert bloom_end == pytest.approx(phases[1][0] + 30.0, abs=0.3)
    assert "cut" in info and info["cut"] > bloom_end, (info.get("cut"), bloom_end)
    assert 0.0 in pump.calls and pump.calls[-1] == 100.0
    brew = coord.brews.last
    assert brew is not stored_before and brew["throttle"]["profile"] == "blooming"
    assert brew["duration"] > bloom_end - 10.0


async def test_bloom_not_possible_with_pump_min(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """D-B: a pump entity with min > 0 cannot be off: warned and recorded."""
    await _setup_profiles(hass, entry, profile="blooming")
    pump = Pump(hass, low=20.0, step=1.0)
    profiles = await _bloom(hass, entry, tclock, pump)
    assert profiles.current.bloom_not_possible is True
    assert pump.percent == 20.0
    sel = hass.states.get(SELECT).attributes
    assert sel["bloom_not_possible"] is True


async def test_skipped_profile_visible_and_deadline(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """D-G / D-F: not prepared -> the brew record and the select show the skip;
    the select carries the active profile's first drop deadline and the
    running shot's (relative and absolute)."""
    await _setup_profiles(hass, entry, profile="soft_preinfusion")
    mock_plug(hass)
    pump = _esphome_pump(hass)
    coord = entry.runtime_data
    attrs = hass.states.get(SELECT).attributes
    assert attrs["first_drop_deadline"] == 22.0 and attrs["running"] is None
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump)  # not prepared
    assert pump.calls == []
    record = coord.brews.last["throttle"]
    assert record["profile"] == "soft_preinfusion" and record["profile_reason"] == "not_prepared"
    attrs = hass.states.get(SELECT).attributes
    assert attrs["last_outcome"] == "not_prepared"
    assert attrs["last_outcome_profile"] == "soft_preinfusion"
    # A prepared shot: running deadline while it runs.
    scale = Scale(coord, tclock)
    scale.frozen = None
    scale.timer_start = None
    scale.run(5, 0.0)
    _tare(scale)
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    attrs = hass.states.get(SELECT).attributes
    assert attrs["running"] == "soft_preinfusion" and attrs["running_phase"] == "preinfusion"
    assert attrs["running_first_drop_deadline"] == 22.0
    assert attrs["running_first_drop_deadline_at"] is not None
    assert attrs["last_outcome"] == "applied"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": "standard"}, blocking=True
    )
    assert hass.states.get(SELECT).attributes["first_drop_deadline"] is None


async def test_error_in_run_ends_it_and_restores(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """T5: an exception in the profile's update ends the run, pump back."""
    await _setup_profiles(hass, entry, profile="blooming")
    pump = Pump(hass)
    profiles = await _bloom(hass, entry, tclock, pump)
    assert pump.percent == 0.0

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    profiles.current.run.update = _boom
    await _tick(hass, tclock, profiles, 0.25)
    assert profiles.current is None and profiles.last_run.end_reason == "error"
    assert pump.percent == 100.0


async def test_zielzeit_relay_lost_restores(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """T6: relay on -> unavailable mid-throttle ends the Zielzeit shot too."""
    await _setup_profiles(hass, entry, profile="target_time")
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump, seconds=14.0)
    assert pump.percent < 100.0
    hass.states.async_set(PLUG, "unavailable", PLUG_ATTRS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.percent == 100.0
    assert coord.autostop.throttle._brew.end_reason == "relay_lost"  # noqa: SLF001


async def test_tare_window_option_migrated_away(
    hass: HomeAssistant, no_ble, lovelace, tclock
) -> None:
    """0.7.0b2: the option ``tare_window`` (0.7.0b1) is dropped by the entry
    migration 1.1 -> 1.2, everything else kept; the detection form no longer
    has it, and a save does not bring it back."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from test_integration import _options_step

    from conftest import ADDR

    hass.config.components.add("bluetooth_adapters")
    hass.config.components.add("bluetooth")
    old = dict(OPTIONS, stop_switch=PLUG, pump_power=PUMP, tare_window=40)
    entry = MockConfigEntry(
        domain=DOMAIN, data={"address": ADDR}, title="Microbalance",
        unique_id=ADDR, options=old, version=1, minor_version=1,
    )
    entry.add_to_hass(hass)
    hass.states.async_set(PLUG, "off", PLUG_ATTRS)
    await _setup(hass, entry)
    assert entry.minor_version == 4
    assert "tare_window" not in entry.options
    assert entry.options == {k: v for k, v in old.items() if k != "tare_window"}
    form = await _options_step(hass, entry, "detection")
    assert "tare_window" not in {str(k) for k in form["data_schema"].schema}


async def test_store_written_by_0_6_0b2(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, tclock
) -> None:
    """T7: a store as 0.6.0b2 wrote it (fixture produced by the 0.6.0b2 code
    from the real host store of 2026-11-23, throttle set to throttle /
    learning on / 28 s / floor 45): Zielzeit active, settings kept,
    nothing pending; written back with the profile keys, still 1.3."""
    from pathlib import Path

    data = json.loads(
        (Path(__file__).parent / "fixtures" / "stop_learning_0.6.0b2_written.json").read_text()
    )
    assert "profiles" not in data["data"]["throttle"]
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    hass_storage[key] = {**data, "key": key}
    hass.states.async_set(PLUG, "off", PLUG_ATTRS)
    hass.config_entries.async_update_entry(
        entry, options=dict(OPTIONS, stop_switch=PLUG, pump_power=PUMP)
    )
    await _setup(hass, entry)  # no setting touched: all from the store
    stop = entry.runtime_data.autostop
    throttle = stop.throttle
    assert _state(hass, SELECT) == "target_time"
    assert throttle.mode == "throttle" and throttle.learning is True
    assert throttle.target_time == 28.0 and throttle.floor == 45.0
    assert throttle.last_relay_off == 1763884800.0
    assert throttle.restore_pending == set()
    assert stop.model.to_dict() == data["data"]["model"]
    stop.async_schedule_save()
    await stop.async_flush()
    saved = hass_storage[key]
    assert saved["minor_version"] == 4
    assert saved["data"]["throttle"]["profiles"]["active"] == "target_time"
    assert saved["data"]["throttle"]["restore_pending"] == []


# ------------------------------------------------- review round 2 (0.7.0b1)


async def _fire_later(hass: HomeAssistant, tclock, seconds: float) -> None:
    """Advance the shot clock and HA's timers together."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    tclock.t += seconds
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_retry_limited_then_deferred(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """TN1: a retry inside the 30 s interval is not lost: one deferred
    retry at the end of the interval (and only one, no loop)."""
    await _setup_profiles(hass, entry, profile="blooming")
    pump = _esphome_pump(hass, feedback=False)
    await hass.async_block_till_done()
    throttle = entry.runtime_data.autostop.throttle
    throttle.restore_pending.add(PUMP)
    throttle._track_pumps()  # noqa: SLF001
    hass.states.async_set(PUMP, "unavailable", pump.attrs)
    hass.states.async_set(PUMP, "0.0", pump.attrs)  # retry 1
    await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.calls == [100.0]
    await _fire_later(hass, tclock, 5.0)
    for _ in range(3):  # back from unavailable again, inside the interval
        hass.states.async_set(PUMP, "unavailable", pump.attrs)
        hass.states.async_set(PUMP, "0.0", pump.attrs)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.calls == [100.0]
    await _fire_later(hass, tclock, 26.0)  # end of the interval: deferred retry
    assert pump.calls == [100.0, 100.0]
    await _fire_later(hass, tclock, 600.0)  # nothing more on its own
    assert pump.calls == [100.0, 100.0]
    # It reports at last: cleared, timer gone.
    hass.states.async_set(PUMP, "100.0", pump.attrs)
    await hass.async_block_till_done()
    assert throttle.restore_pending == set() and not throttle._retry_timers  # noqa: SLF001


async def test_failed_retry_does_not_use_up_the_interval(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """TN1: a retry whose call fails; the pump comes back 5 s later: retried
    at once, not 30 s later."""
    await _setup_profiles(hass, entry, profile="blooming")
    pump = _esphome_pump(hass, feedback=False, fail=True)
    await hass.async_block_till_done()
    throttle = entry.runtime_data.autostop.throttle
    throttle.restore_pending.add(PUMP)
    throttle._track_pumps()  # noqa: SLF001
    hass.states.async_set(PUMP, "unavailable", pump.attrs)
    hass.states.async_set(PUMP, "0.0", pump.attrs)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.calls == [100.0]  # failed
    pump.fail = False
    await _fire_later(hass, tclock, 5.0)
    hass.states.async_set(PUMP, "unavailable", pump.attrs)
    hass.states.async_set(PUMP, "0.0", pump.attrs)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.calls == [100.0, 100.0] and pump.commanded == 100.0


async def test_at_max_tolerance_never_counts_a_lower_grid_value(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """TN4: step 10: 90 is not the maximum; 99.9 of a step-1 grid is."""
    await _setup_profiles(hass, entry, profile="blooming")
    throttle = entry.runtime_data.autostop.throttle
    hass.states.async_set(PUMP, "90", {"min": 0, "max": 100, "step": 10})
    assert not throttle._at_max(PUMP, None)  # noqa: SLF001
    assert throttle._at_max(PUMP, 100.0)  # noqa: SLF001
    hass.states.async_set(PUMP, "99.9", {"min": 0, "max": 100, "step": 1})
    assert throttle._at_max(PUMP, None)  # noqa: SLF001
    assert not throttle._at_max(PUMP, 99.0)  # noqa: SLF001
    hass.states.async_set(PUMP, "254", {"min": 0, "max": 255, "step": 1})
    assert not throttle._at_max(PUMP, None)  # noqa: SLF001


async def test_profile_sample_after_the_cut_check(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """TN2: on a brew frame the cut is checked before the profile runs."""
    await _setup_profiles(hass, entry, profile="blooming")
    mock_plug(hass)
    _esphome_pump(hass)
    coord = entry.runtime_data
    autostop = coord.autostop
    order: list[str] = []
    cut, sample = autostop._check_cut, autostop.throttle.profiles.sample  # noqa: SLF001

    def _cut(*a: Any) -> None:
        order.append("cut")
        cut(*a)

    def _sample(*a: Any) -> None:
        order.append("sample")
        sample(*a)

    autostop._check_cut = _cut  # noqa: SLF001
    autostop.throttle.profiles.sample = _sample
    scale = Scale(coord, tclock)
    _tare(scale)
    await _shot(hass, scale, tclock, pump=None, seconds=3.0)
    i = order.index("cut")
    assert order[i : i + 2] == ["cut", "sample"]
    assert "cut" not in order[: order.index("sample")] or order[0] == "sample"
    # Every brew frame: cut, then sample.
    pairs = order[i:]
    assert all(pairs[k] == "cut" and pairs[k + 1] == "sample" for k in range(0, len(pairs) - 1, 2))


async def test_profile_repair_issues(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """DN2: a 0 % phase with the scale's auto stop timing on / a pump min
    above 0 raises a repair issue; cleared when no longer true."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.difluid_scale.protocol import CMD_SETTINGS_AUTO_STOP_TIMING

    await _setup_profiles(hass, entry, profile="standard")
    coord = entry.runtime_data
    throttle = coord.autostop.throttle
    reg = ir.async_get(hass)
    scale_id = f"profile_bloom_scale_auto_stop_{entry.entry_id}"
    pump_id = f"profile_bloom_pump_min_{entry.entry_id}"
    Pump(hass, low=20.0)
    coord.data.settings[CMD_SETTINGS_AUTO_STOP_TIMING] = 1
    throttle.profiles.check_issues()
    assert reg.async_get_issue(DOMAIN, scale_id) is None  # Standard: no bloom
    assert reg.async_get_issue(DOMAIN, pump_id) is None
    throttle.select_profile("blooming")
    assert reg.async_get_issue(DOMAIN, scale_id) is not None
    issue = reg.async_get_issue(DOMAIN, pump_id)
    assert issue is not None and issue.translation_placeholders["min"] == "20"
    assert issue.translation_placeholders["profile"] == "Blooming"  # the name, not the id
    coord.data.settings[CMD_SETTINGS_AUTO_STOP_TIMING] = 0
    coord.async_update_listeners()  # a settings frame
    assert reg.async_get_issue(DOMAIN, scale_id) is None
    hass.states.async_set(PUMP, "100", {"min": 0, "max": 100, "step": 1})
    await hass.async_block_till_done()
    assert reg.async_get_issue(DOMAIN, pump_id) is None
    hass.states.async_set(PUMP, "100", {"min": 10, "max": 100, "step": 1})
    await hass.async_block_till_done()
    assert reg.async_get_issue(DOMAIN, pump_id) is not None
    throttle.set_mode("off")
    assert reg.async_get_issue(DOMAIN, pump_id) is None


async def test_last_brew_outcome_survives_flushes(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """DN3: relay pulses without a brew (rinsing) update last_outcome only;
    last_brew_outcome stays at the last real brew."""
    await _setup_profiles(hass, entry, profile="soft_preinfusion")
    mock_plug(hass)
    pump = _esphome_pump(hass)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    _tare(scale)
    await _shot(hass, scale, tclock, pump=pump)
    attrs = hass.states.get(SELECT).attributes
    assert attrs["last_brew_outcome"] == "applied"
    assert attrs["last_brew_profile"] == "soft_preinfusion"
    for _ in range(3):  # rinse: relay pulses, not prepared, no scale timer
        _relay(hass, True)
        await hass.async_block_till_done(wait_background_tasks=True)
        scale.gap(2.0)
        _relay(hass, False)
        await hass.async_block_till_done(wait_background_tasks=True)
    attrs = hass.states.get(SELECT).attributes
    assert attrs["last_outcome"] == "not_prepared"
    assert attrs["last_brew_outcome"] == "applied"
