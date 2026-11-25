"""0.7.0b4: the number *Standard pump power* (the power the profile
Standard runs at) and the profile preview's websocket fields for it.

Same harness as ``test_profiles_ha``.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402

from test_integration import OPTIONS, Scale, mock_plug  # noqa: E402
from test_profile_preview import _ws  # noqa: E402
from test_profiles_ha import SELECT, _setup_profiles, _tare, tclock  # noqa: E402,F401
from test_throttle_ha import PLUG, PUMP, Pump, _shot  # noqa: E402

from custom_components.difluid_scale.const import DOMAIN, STOP_STORAGE_KEY  # noqa: E402

NUMBER = "number.microbalance_standard_pump_power"
FLOOR = "number.microbalance_pump_throttle_floor"


async def _set(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    await hass.async_block_till_done()


async def test_entity_range_default_and_persistence(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="throttle")
    reg = er.async_get(hass).async_get(NUMBER)
    assert reg is not None and reg.entity_category == "config"
    assert reg.translation_key == "standard_pump_power"
    state = hass.states.get(NUMBER)
    assert float(state.state) == 100.0  # default: full power, as before
    assert state.attributes["min"] == 40.0  # the floor (default)
    assert state.attributes["max"] == 100.0 and state.attributes["step"] == 1.0
    assert state.attributes["unit_of_measurement"] == "%"
    await _set(hass, NUMBER, 70)
    assert float(hass.states.get(NUMBER).state) == 70.0
    # The range follows the floor; below it Standard runs at the floor.
    await _set(hass, FLOOR, 75)
    state = hass.states.get(NUMBER)
    assert state.attributes["min"] == 75.0 and float(state.state) == 75.0
    await _set(hass, FLOOR, 40)
    assert float(hass.states.get(NUMBER).state) == 70.0  # the stored value
    # Kept across a reload (stop store, additive key).
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get(NUMBER).state) == 70.0
    stored = hass_storage[STOP_STORAGE_KEY.format(entry.entry_id)]
    assert stored["data"]["throttle"]["settings"]["standard_power"] == 70.0
    assert stored["minor_version"] == 4


async def test_old_store_without_the_setting_is_full_power(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """A store from before 0.7.0b4 (no key): full power, as before."""
    await _setup_profiles(hass, entry, mode="throttle")
    throttle = entry.runtime_data.autostop.throttle
    data = throttle.to_store()
    del data["settings"]["standard_power"]
    throttle.standard_power = 55.0
    throttle.load(data)
    assert throttle.standard_power == 100.0
    data["settings"]["standard_power"] = 5  # below the lowest floor
    throttle.load(data)
    assert throttle.standard_power == 10.0


async def _standard_shot(
    hass: HomeAssistant, entry, tclock, *, mode: str, pump: bool, power: float,
    seconds: float = 60.0,
) -> tuple[Any, Pump | None]:
    await _setup_profiles(hass, entry, mode=mode, profile="standard", pump=pump)
    await _set(hass, NUMBER, power)
    mock_plug(hass)
    fake = Pump(hass, step=1.0) if pump else None
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    _tare(scale)
    await _shot(hass, scale, tclock, pump=fake if mode == "throttle" else None, seconds=seconds)
    return coord, fake


async def test_standard_runs_at_the_setting_in_throttle_mode(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    coord, pump = await _standard_shot(hass, entry, tclock, mode="throttle", pump=True, power=70)
    assert pump is not None and pump.calls[0] == 70.0
    assert pump.calls[-1] == 100.0  # set back at the end, as every profile
    record = coord.brews.last["throttle"]
    assert record["profile"] == "standard"
    assert record["power"][0][1] == 70.0
    assert record["applied"] is True and record["controlled"] is True


async def test_standard_at_100_writes_nothing(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """100 % = the behaviour before 0.7.0b4: the pump is never lowered."""
    coord, pump = await _standard_shot(hass, entry, tclock, mode="throttle", pump=True, power=100)
    assert pump is not None and all(v == 100.0 for v in pump.calls)
    assert coord.brews.last["throttle"]["power"][0][1] == 100.0


async def test_standard_observe_only_records(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    coord, pump = await _standard_shot(hass, entry, tclock, mode="observe", pump=True, power=60)
    assert pump is not None and pump.calls == []
    record = coord.brews.last["throttle"]
    assert record["power"][0][1] == 60.0 and record["controlled"] is False


async def test_standard_without_pump_has_no_effect(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    coord, _ = await _standard_shot(hass, entry, tclock, mode="throttle", pump=False, power=60)
    record = coord.brews.last["throttle"]
    assert record["controlled"] is False and record["pump"] is False
    assert record["reason"] == "no_pump"


async def test_change_during_a_shot_takes_effect_next_shot(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="throttle", profile="standard")
    await _set(hass, NUMBER, 70)
    mock_plug(hass)
    pump = Pump(hass, step=1.0)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    _tare(scale)
    await _shot(hass, scale, tclock, pump=pump, seconds=5.0)
    await _set(hass, NUMBER, 50)  # mid-shot
    run = coord.autostop.throttle.profiles.current
    assert run is not None and run.profile.phases[0].power == 70.0
    assert pump.calls == [70.0]


async def test_ws_fields(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="throttle", profile="standard")
    Pump(hass)
    registry = er.async_get(hass)
    address = entry.runtime_data.address
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT})
    result = res["result"]
    assert result["profile"]["standard"] is True
    assert result["standard_power_entity"] == NUMBER
    assert result["ratio_entity"] == registry.async_get_entity_id(
        "number", DOMAIN, f"{address}_target_ratio"
    )
    assert result["pump_controllable"] is True
    assert result["auto_stop_target"] is True
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT, "label": "blooming"})
    assert res["result"]["profile"]["standard"] is False


def _attr(hass: HomeAssistant, key: str) -> Any:
    return hass.states.get(SELECT).attributes[key]


async def test_pump_controllable_follows_mode_options_and_pump_state(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """``pump_controllable`` (select attribute) without a reload: mode
    throttle and a configured pump entity that shows a number."""
    await _setup_profiles(hass, entry, mode="throttle", profile="standard")
    pump = Pump(hass)
    await hass.async_block_till_done()
    assert _attr(hass, "pump_controllable") is True
    # Mode observe / off: not controlled.
    entry.runtime_data.autostop.throttle.set_mode("observe")
    await hass.async_block_till_done()
    assert _attr(hass, "pump_controllable") is False
    entry.runtime_data.autostop.throttle.set_mode("off")
    await hass.async_block_till_done()
    assert _attr(hass, "pump_controllable") is False
    entry.runtime_data.autostop.throttle.set_mode("throttle")
    await hass.async_block_till_done()
    assert _attr(hass, "pump_controllable") is True
    # The pump goes unavailable, comes back, is removed.
    hass.states.async_set(PUMP, "unavailable", pump.attrs)
    await hass.async_block_till_done()
    assert _attr(hass, "pump_controllable") is False
    hass.states.async_set(PUMP, "100", pump.attrs)
    await hass.async_block_till_done()
    assert _attr(hass, "pump_controllable") is True
    hass.states.async_remove(PUMP)
    await hass.async_block_till_done()
    assert _attr(hass, "pump_controllable") is False
    hass.states.async_set(PUMP, "100", pump.attrs)
    await hass.async_block_till_done()
    assert _attr(hass, "pump_controllable") is True
    # The pump entity removed in the options.
    hass.config_entries.async_update_entry(entry, options=dict(OPTIONS, stop_switch=PLUG))
    await hass.async_block_till_done()
    assert _attr(hass, "pump_controllable") is False
    assert PUMP not in entry.options.values()


async def test_auto_stop_target_attribute(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """``auto_stop_target``: a stop switch and a target ratio or yield."""
    await _setup_profiles(hass, entry, mode="observe")
    autostop = entry.runtime_data.autostop
    assert _attr(hass, "auto_stop_target") is True  # target 36 g (setup)
    autostop.set_target(0)
    autostop.set_ratio(0)
    await hass.async_block_till_done()
    assert _attr(hass, "auto_stop_target") is False
    autostop.set_ratio(2.5)
    await hass.async_block_till_done()
    assert _attr(hass, "auto_stop_target") is True


async def test_settings_reject_nan(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="throttle")
    throttle = entry.runtime_data.autostop.throttle
    for setter in (throttle.set_standard_power, throttle.set_floor, throttle.set_target_time):
        with pytest.raises(ValueError):
            setter(float("nan"))
    assert throttle.standard_power == 100.0 and throttle.floor == 40.0
