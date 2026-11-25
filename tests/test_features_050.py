"""0.5.0: robustness, brew metrics, auto-stop event, bean profiles.

Fixtures come from the live installation (tests/fixtures, read-only copies
of the stores and restore state of 2026-11-21).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from conftest import ADDR
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, State, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
    mock_restore_cache_with_extra_data,
)
from test_integration import (
    OPTIONS,
    PLUG,
    PLUG_ATTRS,
    RATIO,
    TARGET,
    FakeClient,
    Scale,
    _auto_shot,
    _options_step,
    _setup,
    _setup_autostop,
    _state,
    sensor_frame,
    status_frame,
)

from custom_components.difluid_scale import coordinator as coord_mod
from custom_components.difluid_scale.autostop import StopStore
from custom_components.difluid_scale.brew import BREWING, brew_metrics
from custom_components.difluid_scale.brews import BrewStore
from custom_components.difluid_scale.const import (
    DOMAIN,
    EVENT_BREW_COMPLETED,
    SIGNAL_LIVE,
    STOP_STORAGE_KEY,
    STORAGE_KEY,
)
from custom_components.difluid_scale.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.difluid_scale.protocol import (
    CMD_DEVICE_SINGLE_CLICK,
    CMD_TIMER_STOP,
    FUNC_DEVICE,
    DeviceState,
    build_frame,
)
from custom_components.difluid_scale.stop_model import Cut

LIVE = json.loads(
    (Path(__file__).parent / "fixtures/live_2026-11-21.json").read_text()
)
BEAN_SELECT = "select.microbalance_bean"
AUTO_STOP_EVENT = "event.microbalance_auto_stop"
ISSUE_ID_PREFIX = "stop_switch_unavailable_"
COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN


def _live_stores(hass_storage: dict[str, Any], entry_id: str) -> tuple[str, str]:
    brews_key = STORAGE_KEY.format(entry_id)
    stop_key = STOP_STORAGE_KEY.format(entry_id)
    hass_storage[brews_key] = {
        **json.loads(json.dumps(LIVE["brews_store"])),
        "key": brews_key,
    }
    hass_storage[stop_key] = {
        **json.loads(json.dumps(LIVE["stop_store"])),
        "key": stop_key,
    }
    return brews_key, stop_key


def _restore_targets(hass: HomeAssistant, entry, disable_ratio: bool = False) -> None:
    """Number entities as registered and restored by 0.4.0 (live values)."""
    registry = er.async_get(hass)
    for key in ("target_ratio", "target_yield"):
        registry.async_get_or_create(
            "number",
            DOMAIN,
            f"{ADDR}_{key}",
            suggested_object_id=f"microbalance_{key}",
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER
            if disable_ratio and key == "target_ratio"
            else None,
        )
    ratio = LIVE["restore"]["target_ratio"]
    target = LIVE["restore"]["target_yield"]
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(RATIO, str(ratio)),
                {
                    "native_max_value": 5.0,
                    "native_min_value": 0.0,
                    "native_step": 0.1,
                    "native_unit_of_measurement": None,
                    "native_value": ratio,
                },
            ),
            (
                State(TARGET, str(target)),
                {
                    "native_max_value": 100.0,
                    "native_min_value": 0.0,
                    "native_step": 0.1,
                    "native_unit_of_measurement": "g",
                    "native_value": target,
                },
            ),
        ],
    )


def _issue(hass: HomeAssistant, entry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID_PREFIX + entry.entry_id)


def _events(hass: HomeAssistant, entity_id: str) -> list[State]:
    """Every new state of ``entity_id`` (event entities: one per event)."""
    states: list[State] = []

    def _listener(event) -> None:
        if event.data["entity_id"] == entity_id and event.data["new_state"]:
            states.append(event.data["new_state"])

    hass.bus.async_listen("state_changed", _listener)
    return states


async def _add_bean(hass, entry, name: str, ratio: float, note: str | None = None):
    result = await _options_step(hass, entry, "add_bean")
    data: dict[str, Any] = {"name": name, "ratio": ratio}
    if note is not None:
        data["note"] = note
    return await hass.config_entries.options.async_configure(result["flow_id"], data)


# ------------------------------------------------------------ 1a unload race


class ConnectClient(FakeClient):
    """A freshly connected client; ``hook`` runs inside start_notify."""

    def __init__(self) -> None:
        super().__init__()
        self.hook: Any = None
        self.notify: Any = None

    async def start_notify(self, _char: str, callback) -> None:
        self.notify = callback
        if self.hook is not None:
            await self.hook()


@pytest.mark.parametrize("stage", ["establish", "start_notify"])
async def test_unload_while_connecting_drops_the_new_client(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, stage
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    client = ConnectClient()

    async def _establish(*_args, **_kwargs):
        if stage == "establish":
            await coord.async_shutdown()
        return client

    if stage == "start_notify":
        client.hook = coord.async_shutdown
    device = type("Dev", (), {"name": "Microbalance", "address": ADDR})()
    with (
        patch.object(coord_mod.bluetooth, "async_ble_device_from_address", return_value=device),
        patch.object(coord_mod, "establish_connection", _establish),
        patch.object(coord, "_async_after_connect") as after,
    ):
        assert await coord._async_connect() is True
    assert not client.is_connected, "fresh client disconnected"
    assert coord._client is None and not coord.connected
    assert coord._cancel_poll is None, "no polling after unload"
    after.assert_not_called()
    assert client.written == []  # nothing sent after the unload


async def test_connect_still_works_without_unload(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    client = ConnectClient()
    device = type("Dev", (), {"name": "Microbalance", "address": ADDR})()
    with (
        patch.object(coord_mod.bluetooth, "async_ble_device_from_address", return_value=device),
        patch.object(coord_mod, "establish_connection", AsyncMock(return_value=client)),
        patch.object(coord, "_async_after_connect", AsyncMock()),
    ):
        assert await coord._async_connect() is True
    assert coord.connected and coord._cancel_poll is not None
    assert len(client.written) == 2  # status request, auto-send on
    coord._stop_poll()


async def test_connect_pauses_before_subscribe_and_between_writes(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """0.5.0b5: wait SETUP_DELAY after the link is up, then space the writes."""
    await _setup(hass, entry)
    coord = entry.runtime_data
    client = ConnectClient()
    events: list[Any] = []

    async def _sleep(seconds: float) -> None:
        events.append(("sleep", seconds))

    async def _hook() -> None:
        events.append("subscribe")

    client.hook = _hook
    device = type("Dev", (), {"name": "Microbalance", "address": ADDR})()
    with (
        patch.object(coord_mod.bluetooth, "async_ble_device_from_address", return_value=device),
        patch.object(coord_mod, "establish_connection", AsyncMock(return_value=client)),
        patch.object(coord, "_async_after_connect", AsyncMock()),
        patch.object(coord_mod.asyncio, "sleep", _sleep),
    ):
        assert await coord._async_connect() is True
    assert events[:2] == [("sleep", coord_mod.SETUP_DELAY), "subscribe"]
    assert events[2:] == [
        ("sleep", coord_mod.SETUP_COMMAND_GAP),
        ("sleep", coord_mod.SETUP_COMMAND_GAP),
    ]
    assert len(client.written) == 2
    coord._stop_poll()


async def test_unload_during_setup_pause_drops_the_client(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    client = ConnectClient()

    async def _sleep(_seconds: float) -> None:
        await coord.async_shutdown()

    device = type("Dev", (), {"name": "Microbalance", "address": ADDR})()
    with (
        patch.object(coord_mod.bluetooth, "async_ble_device_from_address", return_value=device),
        patch.object(coord_mod, "establish_connection", AsyncMock(return_value=client)),
        patch.object(coord, "_async_after_connect") as after,
        patch.object(coord_mod.asyncio, "sleep", _sleep),
    ):
        assert await coord._async_connect() is True
    assert client.notify is None, "not subscribed after the unload"
    assert not client.is_connected and coord._client is None
    after.assert_not_called()
    assert client.written == []


# ------------------------------------------------- 1b notification isolation


async def test_one_failing_consumer_does_not_drop_the_packet(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, caplog
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    Scale(coord, clock)
    seen: list[str] = []

    def _bad_listener(_event: str) -> None:
        raise RuntimeError("listener boom")

    coord.async_add_key_listener(_bad_listener)
    coord.async_add_key_listener(seen.append)
    packet = (
        status_frame(DeviceState.IDLE)
        + build_frame(FUNC_DEVICE, CMD_DEVICE_SINGLE_CLICK, bytes((2,)))
        + sensor_frame(12.3, 0.0, 0)
    )
    parse_status = coord_mod.parse_status

    def _status_boom(frame):
        if parse_status(frame) is not None:
            raise RuntimeError("frame boom")

    with (
        caplog.at_level(logging.ERROR),
        patch.object(
            coord.autostop, "feed_sample", side_effect=RuntimeError("autostop boom")
        ),
        patch.object(coord_mod, "parse_status", _status_boom),
    ):
        coord._on_notification(None, bytearray(packet))
    # The frames after the failing status frame were still handled; the
    # sensor frame was used although the auto-stop raised on it; the second
    # key listener ran although the first raised.
    assert coord.data.status is None
    assert coord.data.sensor is not None and coord.data.sensor.weight_grams == 12.3
    assert _state(hass, "sensor.microbalance_weight") == "12.3"
    assert len(seen) == 1
    assert "autostop boom" in caplog.text and "listener boom" in caplog.text
    assert "frame boom" in caplog.text


# --------------------------------------------- 1c timer stop only while timing


@pytest.mark.parametrize(
    ("answer", "tracker_state", "sent"),
    [
        (DeviceState.TIMING, None, True),
        (DeviceState.TIMER_PAUSE, None, False),  # stopped by hand meanwhile
        (DeviceState.IDLE, None, False),  # reset meanwhile
        (None, BREWING, True),  # no status answer: timer still counting
        (None, "tail", False),
    ],
)
async def test_autostop_stops_timer_only_while_timing(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, answer, tracker_state, sent
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    if tracker_state is not None:
        coord.brew_tracker.state = tracker_state
    with patch.object(coord, "async_request_status", AsyncMock(return_value=answer)):
        await coord.autostop._async_stop_timer()
    assert (CMD_TIMER_STOP in scale.client.written) is sent


async def test_autostop_timer_reset_by_hand_is_left_alone(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Timer reset on the scale right after the cut (weight frames go on,
    state IDLE): no DLink press once the weight has settled, which would
    otherwise start the timer again."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    original = coord.autostop._fire
    events = _events(hass, AUTO_STOP_EVENT)

    def _fire_and_reset_by_hand(*args, **kwargs) -> None:
        original(*args, **kwargs)
        # The user resets the timer: the scale answers IDLE from now on.
        scale.client.on_status = lambda: scale.status(DeviceState.IDLE)

    with patch.object(coord.autostop, "_fire", _fire_and_reset_by_hand):
        assert await _auto_shot(hass, scale, seconds=40) is not None
    assert len(calls) == 1
    # The weight settled and the stop was due, but the scale was not timing.
    assert coord.autostop._timer_stopped
    assert CMD_TIMER_STOP not in scale.client.written
    assert events[0].attributes["event_type"] == "cut"


# --------------------------------------------- 1d/1e storage and targets


async def test_live_stores_load_migrate_and_take_over_targets(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    brews_key, stop_key = _live_stores(hass_storage, entry.entry_id)
    _restore_targets(hass, entry)
    await _setup(hass, entry)
    coord = entry.runtime_data
    live_brews = LIVE["brews_store"]["data"]["brews"]
    # Brews: unchanged, plus bean (None) and metrics from the profile.
    assert coord.brews.brew_count == 5 and coord.brews.coffee_total == 79.9
    for brew, old in zip(coord.brews.brews, live_brews, strict=True):
        assert {k: brew[k] for k in old} == old
        assert brew["bean"] is None
        assert brew["metrics"] == brew_metrics(old["profile"])
    # Migrated store written right away as 1.2.
    assert hass_storage[brews_key]["minor_version"] == 2
    # Stop model and last result unchanged.
    model = LIVE["stop_store"]["data"]["model"]
    assert coord.autostop.model.to_dict() == model
    assert coord.autostop.last == LIVE["stop_store"]["data"]["last"]
    assert _state(hass, "sensor.microbalance_stop_learning_samples") == "5"
    # Targets taken over from the 0.4.0 restore state.
    assert coord.autostop.ratio == 2.6 and coord.autostop.target == 40.0
    assert _state(hass, RATIO) == "2.6" and _state(hass, TARGET) == "40.0"
    async_fire_time_changed(hass, fire_all=True)
    await hass.async_block_till_done()
    stored = hass_storage[stop_key]
    # 0.6.0: stop store 1.3 (``throttle`` added at its defaults).
    assert stored["minor_version"] == 4
    assert stored["data"]["throttle"]["settings"]["mode"] == "observe"
    assert stored["data"]["settings"] == {
        "target_ratio": 2.6, "target_yield": 40.0, "bean_id": None,
    }
    assert stored["data"]["model"] == model

    # From now on the store decides, not the restore state.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": 2.2}, blocking=True
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.autostop.ratio == 2.2
    assert _state(hass, RATIO) == "2.2"


async def test_targets_taken_over_with_ha_2026_11_restore_api(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """HA 2026.11 dropped RestoreStateData.last_states for
    async_get_stored_state(); the one-time take-over must work with both
    (setup failed live on 2026.11.2 with AttributeError)."""
    from homeassistant.helpers.restore_state import async_get as real_get

    from custom_components.difluid_scale import autostop as autostop_mod

    _live_stores(hass_storage, entry.entry_id)
    _restore_targets(hass, entry)

    class NewApi:
        """Only the 2026.11 accessor, no last_states attribute."""

        def __init__(self, data) -> None:
            self._data = data

        def async_get_stored_state(self, entity_id):
            # The real data object of whichever HA version runs the test.
            real = getattr(self._data, "async_get_stored_state", None)
            if real is not None:
                return real(entity_id)
            return self._data.last_states.get(entity_id)

    with patch.object(
        autostop_mod, "async_get_restore", lambda h: NewApi(real_get(h))
    ):
        await _setup(hass, entry)
    autostop = entry.runtime_data.autostop
    assert autostop.ratio == 2.6 and autostop.target == 40.0


async def test_disabled_target_entity_keeps_the_target(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """A disabled ratio entity no longer leaves the ratio at 0 (which
    silently fell back to the yield target / disabled the auto-stop)."""
    _live_stores(hass_storage, entry.entry_id)
    _restore_targets(hass, entry, disable_ratio=True)
    hass.config_entries.async_update_entry(
        entry, options={**OPTIONS, "stop_switch": PLUG}
    )
    hass.states.async_set(PLUG, "on", PLUG_ATTRS)
    await _setup(hass, entry)
    assert hass.states.get(RATIO) is None  # disabled
    autostop = entry.runtime_data.autostop
    assert autostop.ratio == 2.6 and autostop.enabled
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.autostop.ratio == 2.6


async def test_store_migrate_funcs() -> None:
    brews = {"brews": [{"id": "a"}], "brew_count": 1}
    migrated = await BrewStore._async_migrate_func(None, 1, 1, brews)
    assert migrated["brews"][0]["bean"] is None
    with pytest.raises(NotImplementedError):
        await BrewStore._async_migrate_func(None, 2, 1, brews)
    stop = LIVE["stop_store"]["data"]
    # 0.6.0: up to 1.2 the stop part stays as it was, ``throttle`` is added.
    migrated_stop = await StopStore._async_migrate_func(None, 1, 1, stop)
    assert {k: v for k, v in migrated_stop.items() if k != "throttle"} == stop
    assert migrated_stop["throttle"]["model"]["gamma"] == 1.0
    assert migrated_stop["throttle"]["model"]["count"] == 0
    with pytest.raises(NotImplementedError):
        await StopStore._async_migrate_func(None, 2, 1, stop)


# ------------------------------------------------------ 1f/1g entities, icons


async def test_timer_has_no_state_class_and_icons_from_icons_json(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    assert "state_class" not in hass.states.get("sensor.microbalance_timer").attributes
    registry = er.async_get(hass)
    icons = json.loads((COMPONENT / "icons.json").read_text())["entity"]
    for platform, entity_id, key in (
        ("number", TARGET, "target_yield"),
        ("number", RATIO, "target_ratio"),
        ("switch", "switch.microbalance_prepare_brew", "brew_ready"),
        ("button", "button.microbalance_reset_stop_learning", "reset_stop_learning"),
        ("select", BEAN_SELECT, "bean"),
        ("event", AUTO_STOP_EVENT, "auto_stop"),
    ):
        reg = registry.async_get(entity_id)
        assert reg is not None and reg.original_icon is None, entity_id
        assert reg.translation_key == key
        assert icons[platform][key]["default"].startswith("mdi:"), key
        assert "icon" not in hass.states.get(entity_id).attributes


# ------------------------------------------------------------ 1h diagnostics


async def test_diagnostics(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    _live_stores(hass_storage, entry.entry_id)
    _restore_targets(hass, entry)
    await _setup(hass, entry)
    await _add_bean(hass, entry, "Kenya Kiambu", 2.2, "light roast")
    await hass.async_block_till_done()
    entry.runtime_data.device_info.update(
        {"serial": "SN123", "model": "MB01", "firmware": "V326"}
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag)  # serializable
    assert ADDR not in text and "SN123" not in text
    assert diag["entry"]["data"]["address"] == "**REDACTED**"
    assert diag["device"]["info"]["firmware"] == "V326"
    assert diag["device"]["connected"] is False
    stop = diag["auto_stop"]
    assert stop["model"]["count"] == 5 and set(stop["model"]) == {"tau", "c", "p", "count"}
    assert stop["last"]["final_yield"] == 40.4
    # The first bean became active: its ratio is the target.
    assert stop["settings"]["target_ratio"] == 2.2
    assert stop["active_bean"] == "Kenya Kiambu"
    assert [b["name"] for b in diag["beans"]] == ["Kenya Kiambu"]
    assert diag["entry"]["options"]["beans"][0]["note"] == "light roast"
    last = diag["brews"]["last"]
    assert len(last) == 2 and all("profile" not in b for b in last)
    assert last[0]["metrics"]["first_drop"] == 8.8 and last[0]["bean"] is None


# --------------------------------------------------------------- 1i repairs


async def test_repair_issue_for_missing_or_unavailable_stop_switch(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    hass.config_entries.async_update_entry(
        entry, options={**OPTIONS, "stop_switch": PLUG}
    )
    await _setup(hass, entry)
    # Setup: the switch does not exist.
    issue = _issue(hass, entry)
    assert issue is not None and issue.translation_key == "stop_switch_unavailable"
    assert issue.translation_placeholders == {"entity_id": PLUG, "name": "Microbalance"}
    assert issue.severity == ir.IssueSeverity.WARNING and not issue.is_fixable
    # Back: cleared at once.
    hass.states.async_set(PLUG, "off", PLUG_ATTRS)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None
    # Going unavailable later (e.g. a relay powered off with the machine):
    # the state listener never reports it ...
    hass.states.async_set(PLUG, "unavailable", PLUG_ATTRS)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None
    # ... nor a registry placeholder (integration not loaded) ...
    hass.states.async_set(PLUG, "unavailable", {**PLUG_ATTRS, "restored": True})
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None
    # ... but at the cut it counts, and the attempt is reported.
    calls = async_mock_service(hass, "switch", "turn_off")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": TARGET, "value": 38.0}, blocking=True
    )
    events = _events(hass, AUTO_STOP_EVENT)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    assert await _auto_shot(hass, scale) is not None
    assert len(calls) == 1  # still tried
    assert _issue(hass, entry) is not None
    types = [s.attributes["event_type"] for s in events]
    assert types[0] == "switch_failed", types
    # The attempt went on as usual (timer stopped once settled, concluded),
    # but is not learned (0.5.1): the switch was not on at the cut, so its
    # latency (and the overshoot's switch share) is unknown.
    assert scale.client.written.count(CMD_TIMER_STOP) == 1
    assert types[-1] == "switch_latency_unknown", types
    assert coord.autostop.model.count == 0
    assert events[-1].attributes["switch_latency"] is None
    # Option change: re-checked; emptied option clears it.
    result = await _options_step(hass, entry, "detection")
    await hass.config_entries.options.async_configure(result["flow_id"], dict(OPTIONS))
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None
    # Another missing switch chosen: raised again; unload clears it.
    result = await _options_step(hass, entry, "detection")
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, "stop_switch": "switch.nope"}
    )
    await hass.async_block_till_done()
    assert _issue(hass, entry).translation_placeholders["entity_id"] == "switch.nope"
    # A reload keeps it; disabling the entry clears it.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is not None
    assert await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None


@pytest.mark.parametrize(
    ("state", "attrs"),
    [("unavailable", {"restored": True}), ("unavailable", {})],
)
async def test_repair_startup_ignores_placeholder_and_transient_unavailable(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, state, attrs
) -> None:
    """At startup only a missing switch is reported (its integration may
    still be loading)."""
    hass.states.async_set(PLUG, state, {**PLUG_ATTRS, **attrs})
    hass.config_entries.async_update_entry(
        entry, options={**OPTIONS, "stop_switch": PLUG}
    )
    hass.set_state(CoreState.starting)
    await _setup(hass, entry)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None
    # Coming up normally: still nothing.
    hass.states.async_set(PLUG, "off", PLUG_ATTRS)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None


async def test_repair_name_and_diagnostics_title_without_address(
    hass: HomeAssistant, no_ble, lovelace, clock
) -> None:
    """Manual setup without a name: the title is the BLE address."""
    hass.config.components.add("bluetooth_adapters")
    hass.config.components.add("bluetooth")
    entry = MockConfigEntry(
        domain=DOMAIN, data={"address": ADDR}, title=ADDR, unique_id=ADDR,
        options={**OPTIONS, "stop_switch": "switch.nope"},
    )
    entry.add_to_hass(hass)
    await _setup(hass, entry)
    issue = _issue(hass, entry)
    assert issue.translation_placeholders["name"] == "DiFluid Microbalance"
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["title"] == "**REDACTED**"
    assert ADDR not in json.dumps(diag)


async def test_switch_off_after_unload_creates_no_issue_or_event(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    events: list = []
    coord.autostop.async_add_event_listener(lambda t, d: events.append(t))
    cut = Cut(start=0.0, t=0.0, weight=35.0, flow=2.0, target=38.0, predicted=2.0)
    await coord.async_shutdown()
    hass.states.async_remove(PLUG)  # missing: would raise an issue at a cut
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None  # no longer watched after unload
    await coord.autostop._async_switch_off(PLUG, cut)
    assert events == [] and _issue(hass, entry) is None


async def test_repair_strings_exist() -> None:
    for path in (COMPONENT / "strings.json", *COMPONENT.glob("translations/*.json")):
        issue = json.loads(path.read_text())["issues"]["stop_switch_unavailable"]
        assert "{entity_id}" in issue["description"] and "{name}" in issue["title"]


# ------------------------------------------- 2a metrics, 2b auto-stop event


async def test_autostop_event_cut_then_learned_with_bean_and_metrics(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 30.0)
    await _add_bean(hass, entry, "Kenya Kiambu", 2.0)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": BEAN_SELECT, "option": "Kenya Kiambu"}, blocking=True,
    )
    coord = entry.runtime_data
    assert coord.autostop.ratio == 2.0 and _state(hass, RATIO) == "2.0"
    events = _events(hass, AUTO_STOP_EVENT)
    completed: list = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, completed.append)
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale, dose=18.0) is not None
    assert [s.attributes["event_type"] for s in events] == ["cut", "learned"]
    cut, done = (s.attributes for s in events)
    assert cut["target"] == 36.0 and cut["target_source"] == "ratio"
    assert cut["dose"] == 18.0 and cut["bean"] == "Kenya Kiambu"
    assert 33.0 <= cut["cut_weight"] <= 36.0 and cut["cut_flow"] > 1.5
    assert cut["predicted_overshoot"] > 0 and cut["final_yield"] is None
    assert cut["deviation"] is None
    last = coord.autostop.last
    assert done["final_yield"] == last["final_yield"]
    assert done["deviation"] == last["deviation"] and last["bean"] == "Kenya Kiambu"
    # Brew: bean and metrics in the store, the event, the websocket.
    brew = coord.brews.last
    assert brew["bean"] == "Kenya Kiambu"
    metrics = brew["metrics"]
    assert set(metrics) == {"first_drop", "peak_flow", "mean_flow", "flow_cv"}
    assert 2.5 <= metrics["first_drop"] <= 4.5, metrics  # flow from 3 s
    assert 1.7 <= metrics["mean_flow"] <= 2.3 and 1.7 <= metrics["peak_flow"] <= 2.5
    assert completed[-1].data["bean"] == "Kenya Kiambu"
    assert completed[-1].data["metrics"] == metrics
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/brews"})
    summary = (await ws.receive_json())["result"]["brews"][0]
    assert summary["bean"] == "Kenya Kiambu" and summary["metrics"] == metrics
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/brew"})
    full = (await ws.receive_json())["result"]["brew"]
    assert full["metrics"] == metrics and full["bean"] == "Kenya Kiambu"


async def test_autostop_event_switch_failed_when_service_raises(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_autostop(hass, entry, PLUG, 38.0)

    async def _raise(_call: ServiceCall) -> None:
        raise HomeAssistantError("plug offline")

    hass.services.async_register("switch", "turn_off", _raise)
    events = _events(hass, AUTO_STOP_EVENT)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    # Nothing switched off: the pump runs on (stopped by hand 3 s later).
    # A failed call has no switch latency: not learned (0.5.1; before, the
    # overshoot made it an outlier).
    await _auto_shot(hass, scale, latency=3.0, seconds=45)
    types = [s.attributes["event_type"] for s in events]
    assert types == ["switch_failed", "switch_latency_unknown"], types
    assert coord.autostop.model.count == 0
    # The switch exists and is available: a failing call is no repair issue.
    assert _issue(hass, entry) is None


async def test_event_strings_cover_all_event_types() -> None:
    from custom_components.difluid_scale.autostop import AUTOSTOP_EVENT_TYPES

    for path in (COMPONENT / "strings.json", *COMPONENT.glob("translations/*.json")):
        ent = json.loads(path.read_text())["entity"]
        states = ent["event"]["auto_stop"]["state_attributes"]["event_type"]["state"]
        assert set(states) == set(AUTOSTOP_EVENT_TYPES), path
        assert ent["select"]["bean"]["state"]["none"]


async def test_old_brew_metrics_on_load_and_without_profile(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    key = STORAGE_KEY.format(entry.entry_id)
    old = dict(LIVE["brews_store"]["data"]["brews"][0])
    bare = {k: v for k, v in old.items() if k != "profile"} | {"id": "01BARE"}
    hass_storage[key] = {
        "version": 1, "minor_version": 1, "key": key,
        "data": {"brews": [old, bare], "brew_count": 2, "coffee_total": 31.9},
    }
    await _setup(hass, entry)
    first, second = entry.runtime_data.brews.brews
    assert first["metrics"]["first_drop"] == 12.3
    assert "metrics" not in second and second["bean"] is None


# ------------------------------------------------------- 3 bean profiles


async def test_bean_profiles_options_flow_select_and_write_back(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    hass.config_entries.async_update_entry(entry, options=dict(OPTIONS))
    await _setup(hass, entry)
    coord = entry.runtime_data
    assert hass.states.get(BEAN_SELECT).attributes["options"] == ["none"]
    assert _state(hass, BEAN_SELECT) == "none"

    result = await _add_bean(hass, entry, "Kenya Kiambu", 2.2, "  light roast ")
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    # The first bean becomes active at once; "none" is gone.
    assert _state(hass, BEAN_SELECT) == "Kenya Kiambu" and coord.autostop.ratio == 2.2
    assert hass.states.get(BEAN_SELECT).attributes["options"] == ["Kenya Kiambu"]
    for name, error in (("kenya kiambu", "bean_name_taken"), ("None", "bean_name_reserved"),
                        ("   ", "bean_name_empty")):
        result = await _add_bean(hass, entry, name, 2.0)
        assert result["errors"] == {"name": error}, name
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Ethiopia Guji", "ratio": 2.5}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    beans = entry.options["beans"]
    assert [(b["name"], b["ratio"], b["note"]) for b in beans] == [
        ("Kenya Kiambu", 2.2, "light roast"), ("Ethiopia Guji", 2.5, None),
    ]
    # Detection settings keep the beans; the menu now offers edit/delete.
    result = await _options_step(hass, entry, "detection")
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, "tail_seconds": 4}
    )
    await hass.async_block_till_done()
    assert entry.options["beans"] == beans and entry.options["tail_seconds"] == 4
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["detection", "add_bean", "edit_bean", "delete_bean"]
    hass.config_entries.options.async_abort(result["flow_id"])

    # Select: options follow the profiles (no "none"); a second bean does
    # not take over; choosing sets the ratio.
    assert hass.states.get(BEAN_SELECT).attributes["options"] == [
        "Kenya Kiambu", "Ethiopia Guji",
    ]
    assert _state(hass, BEAN_SELECT) == "Kenya Kiambu"
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": BEAN_SELECT, "option": "Ethiopia Guji"}, blocking=True,
    )
    assert _state(hass, RATIO) == "2.5"
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": BEAN_SELECT, "option": "Kenya Kiambu"}, blocking=True,
    )
    assert _state(hass, BEAN_SELECT) == "Kenya Kiambu" and _state(hass, RATIO) == "2.2"
    # Ratio changed while the bean is active: written back to the profile.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": 2.4}, blocking=True
    )
    await hass.async_block_till_done()
    assert entry.options["beans"][0]["ratio"] == 2.4
    assert entry.options["beans"][1]["ratio"] == 2.5
    # Edited in the options flow (rename, new ratio): the active bean follows.
    result = await _options_step(hass, entry, "edit_bean")
    kenya = entry.options["beans"][0]["id"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": kenya}
    )
    assert result["step_id"] == "edit_bean_details"
    assert result["description_placeholders"] == {"name": "Kenya Kiambu"}
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Ethiopia Guji", "ratio": 2.3}
    )
    assert bad["errors"] == {"name": "bean_name_taken"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Kenya AA", "ratio": 2.3}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert _state(hass, BEAN_SELECT) == "Kenya AA" and coord.autostop.ratio == 2.3
    assert entry.options["beans"][0]["note"] is None  # note cleared

    # Active bean survives a reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coord = entry.runtime_data
    assert _state(hass, BEAN_SELECT) == "Kenya AA" and coord.autostop.ratio == 2.3

    # "none" cannot be chosen while beans exist (translated error).
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "select", "select_option", {"entity_id": BEAN_SELECT, "option": "none"},
            blocking=True,
        )
    # HA's select checks the option list first ("none" is not in it); the
    # entity's own check (bean_required) is the backstop behind it.
    assert err.value.translation_key in ("not_valid_option", "bean_required")
    assert _state(hass, BEAN_SELECT) == "Kenya AA"

    # Deleting a non-active bean changes nothing.
    await _add_bean(hass, entry, "Brazil", 1.8)
    await hass.async_block_till_done()
    assert _state(hass, BEAN_SELECT) == "Kenya AA" and coord.autostop.ratio == 2.3
    result = await _options_step(hass, entry, "delete_bean")
    guji = entry.options["beans"][1]["id"]
    await hass.config_entries.options.async_configure(result["flow_id"], {"id": guji})
    await hass.async_block_till_done()
    assert _state(hass, BEAN_SELECT) == "Kenya AA" and coord.autostop.ratio == 2.3
    # Deleting the active bean: the newest remaining (Brazil) takes over.
    result = await _options_step(hass, entry, "delete_bean")
    await hass.config_entries.options.async_configure(result["flow_id"], {"id": kenya})
    await hass.async_block_till_done()
    assert _state(hass, BEAN_SELECT) == "Brazil" and coord.autostop.ratio == 1.8
    assert hass.states.get(BEAN_SELECT).attributes["options"] == ["Brazil"]
    # The last one: none active, "none" again, ratio stays.
    result = await _options_step(hass, entry, "delete_bean")
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": entry.options["beans"][0]["id"]}
    )
    await hass.async_block_till_done()
    assert _state(hass, BEAN_SELECT) == "none" and coord.autostop.ratio == 1.8
    assert hass.states.get(BEAN_SELECT).attributes["options"] == ["none"]
    assert coord.autostop.bean_id is None


async def test_brew_without_bean_and_learning_stays_shared(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """No bean: brews record None; the stop model is one for all beans."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale) is not None
    assert coord.brews.last["bean"] is None and coord.autostop.last["bean"] is None
    await _add_bean(hass, entry, "Kenya", 2.0)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select", "select_option", {"entity_id": BEAN_SELECT, "option": "Kenya"},
        blocking=True,
    )
    clock.t += 120
    scale2 = Scale(coord, clock)
    scale2.calls = calls
    assert await _auto_shot(hass, scale2) is not None
    assert coord.brews.last["bean"] == "Kenya"
    assert coord.autostop.model.count == 2  # same model learned from both


# ------------------------------------------------------- review round 1


async def test_options_save_keeps_a_manual_ratio(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """An unrelated options save does not reset a ratio set by hand (0 =
    off, or an out-of-form value) to the active bean's ratio; a real change
    of that bean's ratio does."""
    hass.config_entries.async_update_entry(entry, options=dict(OPTIONS))
    await _setup(hass, entry)
    await _add_bean(hass, entry, "Kenya", 2.2)
    await _add_bean(hass, entry, "Guji", 2.5)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select", "select_option", {"entity_id": BEAN_SELECT, "option": "Kenya"},
        blocking=True,
    )
    autostop = entry.runtime_data.autostop
    for manual in (0.0, 0.3):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": RATIO, "value": manual}, blocking=True
        )
        result = await _options_step(hass, entry, "detection")
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**OPTIONS, "tail_seconds": 4}
        )
        await hass.async_block_till_done()
        assert autostop.ratio == manual and _state(hass, BEAN_SELECT) == "Kenya"
        # Not written back: outside the bean form's range (0.5-5).
        assert entry.options["beans"][0]["ratio"] == 2.2
    # Editing the other bean does not touch the target either.
    result = await _options_step(hass, entry, "edit_bean")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": entry.options["beans"][1]["id"]}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Guji", "ratio": 2.7}
    )
    await hass.async_block_till_done()
    assert autostop.ratio == 0.3
    # Editing the active bean's ratio does.
    result = await _options_step(hass, entry, "edit_bean")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": entry.options["beans"][0]["id"]}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Kenya", "ratio": 2.1}
    )
    await hass.async_block_till_done()
    assert autostop.ratio == 2.1
    # Survives a reload: the stored manual value is not reset either.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": 0.0}, blocking=True
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    result = await _options_step(hass, entry, "detection")
    await hass.config_entries.options.async_configure(result["flow_id"], dict(OPTIONS))
    await hass.async_block_till_done()
    assert entry.runtime_data.autostop.ratio == 0.0


async def test_bean_name_reserved_translated_none(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    for name in ("Keine", "NONE", " none ", "Unknown", "unavailable"):
        result = await _add_bean(hass, entry, name, 2.0)
        assert result["errors"] == {"name": "bean_name_reserved"}, name
    # At most 60 characters (a 300-character name broke the select state).
    result = await _add_bean(hass, entry, "x" * 61, 2.0)
    assert result["errors"] == {"name": "bean_name_too_long"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "x" * 60, "ratio": 2.0}
    )
    assert result["type"] == "create_entry"
    for path in (COMPONENT / "strings.json", *COMPONENT.glob("translations/*.json")):
        errors = json.loads(path.read_text())["options"]["error"]
        for key in ("bean_name_taken", "bean_name_reserved", "bean_name_too_long"):
            assert errors[key] and "none" not in errors[key].lower(), (path, key)


async def test_corrupt_profile_does_not_fail_setup(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock,
    caplog,
) -> None:
    key = STORAGE_KEY.format(entry.entry_id)
    good = dict(LIVE["brews_store"]["data"]["brews"][1])
    broken = [
        {**good, "id": "01BAD1", "profile": [[0.0, "x", 0.0], [1.0, None, 0.0]]},
        {**good, "id": "01BAD2", "profile": [[0.0], [1.0]]},
        {**good, "id": "01BAD3", "profile": "garbage"},
    ]
    hass_storage[key] = {
        "version": 1, "minor_version": 1, "key": key,
        "data": {"brews": [*broken, good], "brew_count": 4, "coffee_total": 64.0},
    }
    with caplog.at_level(logging.WARNING):
        await _setup(hass, entry)
    assert entry.state.value == "loaded"
    brews = entry.runtime_data.brews.brews
    assert all("metrics" not in b for b in brews[:3])
    assert brews[3]["metrics"]["first_drop"] == 8.8
    assert caplog.text.count("unreadable profile") == 1
    assert "01BAD1" in caplog.text and "01BAD2" in caplog.text


async def test_bean_fixed_at_brew_start(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Changing the bean during a brew: the brew and its auto-stop events
    keep the bean it started with."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    await _add_bean(hass, entry, "Kenya", 2.0)
    await _add_bean(hass, entry, "Guji", 2.0)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select", "select_option", {"entity_id": BEAN_SELECT, "option": "Kenya"},
        blocking=True,
    )
    coord = entry.runtime_data
    events = _events(hass, AUTO_STOP_EVENT)
    original = coord.autostop.feed_sample
    switched = False

    def _feed(t: float, weight: float, placed: float | None = None) -> None:
        nonlocal switched
        if not switched and coord.brew_tracker.state == BREWING and weight > 10:
            switched = True
            coord.autostop.select_bean(coord.autostop.beans[1]["id"])
        original(t, weight)

    scale = Scale(coord, clock)
    scale.calls = calls
    with patch.object(coord.autostop, "feed_sample", _feed):
        assert await _auto_shot(hass, scale) is not None
    assert switched and coord.autostop.active_bean_name == "Guji"
    assert coord.brews.last["bean"] == "Kenya"
    assert {s.attributes["bean"] for s in events} == {"Kenya"}
    assert coord.autostop.last["bean"] == "Kenya"


async def test_guarded_logs_stack_trace_throttled(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, caplog
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    Scale(coord, clock)
    now = [1000.0]
    with (
        caplog.at_level(logging.DEBUG, logger=coord_mod.__name__),
        patch.object(coord_mod, "monotonic_time_coarse", lambda: now[0]),
        patch.object(coord.autostop, "feed_sample", side_effect=RuntimeError("boom")),
    ):
        for _ in range(5):
            coord._on_notification(None, bytearray(sensor_frame(1.0, 0.0, 0)))
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and errors[0].exc_info
        assert sum("again (4 since" in r.getMessage() for r in caplog.records) == 1
        now[0] += 61
        coord._on_notification(None, bytearray(sensor_frame(1.0, 0.0, 0)))
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2 and "4 more since the last report" in errors[1].getMessage()


# ------------------------------------------------------- review round 2


async def test_runtime_issue_survives_option_save_and_reload(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """An unavailable switch is not reported at setup, option saves or a
    reload while HA runs (it may be powered off with the machine); only a
    cut that cannot switch it off reports it. That issue stays through
    option saves (also the ratio write-back) and a reload; a placeholder
    state does not clear it; only a usable switch does."""
    hass.states.async_set(PLUG, "unavailable", PLUG_ATTRS)
    hass.config_entries.async_update_entry(
        entry, options={**OPTIONS, "stop_switch": PLUG}
    )
    await _setup(hass, entry)  # HA running, machine off: not reported
    assert _issue(hass, entry) is None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None
    # The cut cannot switch it off: reported.
    async_mock_service(hass, "switch", "turn_off")
    coord = entry.runtime_data
    cut = Cut(start=0.0, t=0.0, weight=35.0, flow=2.0, target=38.0, predicted=2.0)
    await coord.autostop._async_switch_off(PLUG, cut)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is not None
    await _add_bean(hass, entry, "Kenya", 2.0)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select", "select_option", {"entity_id": BEAN_SELECT, "option": "Kenya"},
        blocking=True,
    )
    await hass.services.async_call(  # write-back = option save
        "number", "set_value", {"entity_id": RATIO, "value": 2.3}, blocking=True
    )
    await hass.async_block_till_done()
    assert entry.options["beans"][0]["ratio"] == 2.3
    assert _issue(hass, entry) is not None
    result = await _options_step(hass, entry, "detection")
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, "stop_switch": PLUG}
    )
    await hass.async_block_till_done()
    assert _issue(hass, entry) is not None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is not None
    hass.states.async_set(PLUG, "unavailable", {**PLUG_ATTRS, "restored": True})
    await hass.async_block_till_done()
    assert _issue(hass, entry) is not None  # placeholder: not cleared
    hass.states.async_set(PLUG, "on", PLUG_ATTRS)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None  # available again: cleared
    # Going unavailable again afterwards (machine off): not raised again.
    hass.states.async_set(PLUG, "unavailable", PLUG_ATTRS)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None
    # Raised by a cut again, then another (also unavailable) switch chosen:
    # the issue naming the old one is dropped, none raised for the new one.
    await entry.runtime_data.autostop._async_switch_off(PLUG, cut)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is not None
    hass.states.async_set("switch.other", "unavailable", PLUG_ATTRS)
    result = await _options_step(hass, entry, "detection")
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, "stop_switch": "switch.other"}
    )
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None


async def test_unload_mid_cut_still_switches_off(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    release = asyncio.Event()
    done: list = []

    async def _slow_off(call: ServiceCall) -> None:
        await release.wait()
        done.append(call.data["entity_id"])

    hass.services.async_register("switch", "turn_off", _slow_off)
    events: list = []
    coord.autostop.async_add_event_listener(lambda t, d: events.append(t))
    cut = Cut(start=0.0, t=0.0, weight=35.0, flow=2.0, target=38.0, predicted=2.0)
    task = hass.async_create_task(coord.autostop._async_switch_off(PLUG, cut))
    await asyncio.sleep(0)
    await coord.async_shutdown()
    task.cancel()  # what the entry unload does with its background tasks
    release.set()
    await hass.async_block_till_done()
    assert task.cancelled()
    assert done == [PLUG], "the plug was still switched off"
    assert events == [] and _issue(hass, entry) is None


# --------------------------------------------- 0.5.0b2 bean services


async def _bean_service(hass, service: str, **data):
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


async def _bean_setup(hass, entry, ratio: float = 2.4) -> Any:
    hass.config_entries.async_update_entry(entry, options=dict(OPTIONS))
    await _setup(hass, entry)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": ratio}, blocking=True
    )
    return entry.runtime_data


async def test_add_bean_service(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    coord = await _bean_setup(hass, entry, ratio=2.4)
    with patch.object(
        hass.config_entries, "async_reload", side_effect=AssertionError("reload")
    ):
        # Without ratio: the current target ratio; activated by default.
        resp = await _bean_service(hass, "add_bean", name="  Kenya Kiambu ", note=" x ")
        # Select options updated at once (no block_till_done in between).
        assert hass.states.get(BEAN_SELECT).attributes["options"] == ["Kenya Kiambu"]
        assert _state(hass, BEAN_SELECT) == "Kenya Kiambu"
        assert resp == {
            "bean_id": entry.options["beans"][0]["id"], "name": "Kenya Kiambu",
            "ratio": 2.4, "active": True,
            "ratios": {"standard": 2.4},  # 0.7.0b6: per pump profile
        }
        assert entry.options["beans"][0]["note"] == "x"
        # With ratio, not activated: the active bean and the target stay.
        resp = await _bean_service(
            hass, "add_bean", name="Guji", ratio=2.8, activate=False
        )
        assert resp["active"] is False and resp["ratio"] == 2.8
        assert _state(hass, BEAN_SELECT) == "Kenya Kiambu"
        assert coord.autostop.ratio == 2.4
        # Activated with its own ratio: the target follows.
        await _bean_service(hass, "add_bean", name="Brazil", ratio=1.8)
        await hass.async_block_till_done()
    assert _state(hass, BEAN_SELECT) == "Brazil" and coord.autostop.ratio == 1.8
    assert _state(hass, RATIO) == "1.8"
    assert entry.runtime_data is coord  # same instance: no reload
    ids = [b["id"] for b in entry.options["beans"]]
    assert len(set(ids)) == 3 and all(len(i) == 26 for i in ids)  # ULIDs
    assert entry.options["tail_seconds"] == OPTIONS["tail_seconds"]


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"name": "kenya"}, "bean_name_taken"),
        ({"name": "KEINE"}, "bean_name_reserved"),
        ({"name": "none"}, "bean_name_reserved"),
        ({"name": "Unknown"}, "bean_name_reserved"),
        ({"name": "unavailable"}, "bean_name_reserved"),
        ({"name": None}, "bean_name_empty"),
        ({"name": "x" * 61}, "bean_name_too_long"),
        ({"name": "   "}, "bean_name_empty"),
        ({"name": "Guji", "ratio": 0.3}, "bean_ratio_range"),
        ({"name": "Guji", "ratio": 5.5}, "bean_ratio_range"),
    ],
)
async def test_add_bean_service_errors(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, data, key
) -> None:
    await _bean_setup(hass, entry)
    await _bean_service(hass, "add_bean", name="Kenya", ratio=2.0)
    with pytest.raises(ServiceValidationError) as err:
        await _bean_service(hass, "add_bean", **data)
    assert err.value.translation_key == key
    assert len(entry.options["beans"]) == 1


async def test_add_bean_without_ratio_needs_a_valid_target_ratio(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _bean_setup(hass, entry, ratio=0.0)
    with pytest.raises(ServiceValidationError) as err:
        await _bean_service(hass, "add_bean", name="Kenya")
    assert err.value.translation_key == "bean_ratio_missing"
    assert "beans" not in entry.options


async def test_bean_services_need_entry_id_with_two_scales(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _bean_setup(hass, entry)
    other_addr = "11:22:33:44:55:66"
    other = MockConfigEntry(
        domain=DOMAIN, data={"address": other_addr}, title="Second",
        unique_id=other_addr, options=dict(OPTIONS),
    )
    other.add_to_hass(hass)
    await _setup(hass, other)
    for service, data in (
        ("add_bean", {"name": "Kenya", "ratio": 2.0}),
        ("delete_bean", {"active": True}),
    ):
        with pytest.raises(ServiceValidationError) as err:
            await _bean_service(hass, service, **data)
        assert err.value.translation_key == "bean_entry_ambiguous"
    with pytest.raises(ServiceValidationError) as err:
        await _bean_service(hass, "add_bean", name="K", ratio=2.0, config_entry_id="nope")
    assert err.value.translation_key == "bean_entry_unknown"
    await _bean_service(
        hass, "add_bean", name="Kenya", ratio=2.0, config_entry_id=other.entry_id
    )
    assert [b["name"] for b in other.options["beans"]] == ["Kenya"]
    assert "beans" not in entry.options
    await _bean_service(hass, "delete_bean", active=True, config_entry_id=other.entry_id)
    assert other.options["beans"] == []


async def test_add_bean_during_brew_does_not_disturb_it(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    coord = await _bean_setup(hass, entry)
    scale = Scale(coord, clock)
    scale.weigh_dose(18.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(10, lambda r: max(0.0, round((r - 3) * 2.0, 1)))
    assert coord.brew_tracker.state == BREWING
    start = coord.brew_tracker.start
    await _bean_service(hass, "add_bean", name="Kenya", ratio=2.0)
    await _bean_service(hass, "delete_bean", name="kenya")
    await hass.async_block_till_done()
    assert entry.runtime_data is coord
    assert coord.brew_tracker.state == BREWING and coord.brew_tracker.start == start
    scale.run(15, lambda r: 14.0 + r * 2.0)
    scale.frozen = scale.timer()
    scale.run(5, 44.0)
    scale.status(DeviceState.TIMER_PAUSE)
    scale.run(1, 44.0)
    await hass.async_block_till_done()
    assert coord.brews.brew_count == 1  # one brew, not split
    assert coord.brews.last["bean"] is None  # bean fixed at the brew start


async def test_delete_bean_service(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    coord = await _bean_setup(hass, entry, ratio=2.4)
    with pytest.raises(ServiceValidationError) as err:
        await _bean_service(hass, "delete_bean", active=True)  # nothing active
    assert err.value.translation_key == "bean_none_active"
    for name, ratio in (("Kenya", 2.0), ("Guji", 2.5), ("Brazil", 1.8)):
        await _bean_service(hass, "add_bean", name=name, ratio=ratio, activate=False)
    ids = {b["name"]: b["id"] for b in entry.options["beans"]}
    for data in ({"name": "Nope"}, {"bean_id": "01NOPE"}):
        with pytest.raises(ServiceValidationError) as err:
            await _bean_service(hass, "delete_bean", **data)
        assert err.value.translation_key == "bean_unknown"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": BEAN_SELECT, "option": "Kenya"},
        blocking=True,
    )
    with patch.object(
        hass.config_entries, "async_reload", side_effect=AssertionError("reload")
    ):
        # A non-active bean by id: the active one and the ratio stay.
        resp = await _bean_service(hass, "delete_bean", bean_id=ids["Guji"])
        assert resp == {"bean_id": ids["Guji"], "name": "Guji"}
        assert hass.states.get(BEAN_SELECT).attributes["options"] == [
            "Kenya", "Brazil",
        ]
        assert _state(hass, BEAN_SELECT) == "Kenya" and coord.autostop.ratio == 2.0
        # By name (case-insensitive), non-active.
        await _bean_service(hass, "delete_bean", name="BRAZIL")
        # Nothing given: the active bean; none active after, ratio stays.
        resp = await _bean_service(hass, "delete_bean", name="", active=True)
        assert resp["name"] == "Kenya"
        assert hass.states.get(BEAN_SELECT).attributes["options"] == ["none"]
        assert _state(hass, BEAN_SELECT) == "none"
        assert coord.autostop.bean_id is None and coord.autostop.ratio == 2.0
        await hass.async_block_till_done()
    assert entry.options["beans"] == [] and entry.runtime_data is coord


async def test_bean_service_strings() -> None:
    for path in (COMPONENT / "strings.json", *COMPONENT.glob("translations/*.json")):
        d = json.loads(path.read_text())
        for service in ("add_bean", "delete_bean"):
            assert d["services"][service]["name"], (path, service)
        for key in (
            "bean_entry_unknown", "bean_entry_ambiguous", "bean_name_empty",
            "bean_name_taken", "bean_ratio_range", "bean_ratio_missing",
            "bean_unknown", "bean_none_active", "bean_name_reserved",
            "bean_name_too_long", "bean_no_scale", "bean_delete_which",
        ):
            assert d["exceptions"][key]["message"], (path, key)
    services = (COMPONENT / "services.yaml").read_text()
    assert "add_bean:" in services and "delete_bean:" in services


# ------------------------------------------- 0.5.0b2 review round 1


@pytest.mark.parametrize(
    "data",
    [
        {},  # nothing: no destructive default
        {"name": ""},  # empty name counts as not given
        {"name": None, "bean_id": None, "active": False},
        {"name": "Kenya", "bean_id": "01X"},
        {"name": "Kenya", "active": True},
        {"bean_id": "01X", "active": True},
    ],
)
async def test_delete_bean_needs_exactly_one_selector(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, data
) -> None:
    await _bean_setup(hass, entry)
    await _bean_service(hass, "add_bean", name="Kenya", ratio=2.0)
    with pytest.raises(ServiceValidationError) as err:
        await _bean_service(hass, "delete_bean", **data)
    assert err.value.translation_key == "bean_delete_which"
    assert [b["name"] for b in entry.options["beans"]] == ["Kenya"]


async def test_bean_services_without_loaded_scale(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _bean_setup(hass, entry)
    entry_id = entry.entry_id
    # Disabled (unloaded): addressed by id it is unknown, without id none.
    await hass.config_entries.async_set_disabled_by(
        entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await _bean_service(hass, "add_bean", name="Kenya", ratio=2.0)
    assert err.value.translation_key == "bean_no_scale"
    with pytest.raises(ServiceValidationError) as err:
        await _bean_service(hass, "delete_bean", active=True, config_entry_id=entry_id)
    assert err.value.translation_key == "bean_entry_unknown"
    # Removed: zero entries.
    assert await hass.config_entries.async_remove(entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await _bean_service(hass, "delete_bean", name="Kenya")
    assert err.value.translation_key == "bean_no_scale"


@pytest.mark.parametrize("ratio", [0.5, 5.0])
async def test_add_bean_ratio_bounds_accepted(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, ratio
) -> None:
    coord = await _bean_setup(hass, entry)
    resp = await _bean_service(hass, "add_bean", name="Edge", ratio=ratio)
    assert resp["ratio"] == ratio and coord.autostop.ratio == ratio


async def test_add_bean_persists_across_restart(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """add_bean, then a restart: the profile is in the stored config entry
    and the active bean in the integration's store; a new setup from them
    restores both."""
    await _bean_setup(hass, entry, ratio=2.4)
    resp = await _bean_service(hass, "add_bean", name="Kenya", ratio=2.2)
    # HA stop: entries are not unloaded, the integration's stores are
    # flushed (HA stop listener).
    await entry.runtime_data.async_shutdown()
    async_fire_time_changed(hass, fire_all=True)
    await hass.async_block_till_done()
    # What HA writes to core.config_entries (ConfigEntry.as_dict; the
    # config entry store itself is HA's job and not mocked here).
    stored = entry.as_dict()
    assert stored["options"]["beans"] == [
        # 0.7.0b6: the ratio per pump profile too (Standard is active).
        {"id": resp["bean_id"], "name": "Kenya", "ratio": 2.2, "note": None,
         "ratios": {"standard": 2.2}, "ratio_written": 2.2}
    ]
    settings = hass_storage[STOP_STORAGE_KEY.format(entry.entry_id)]["data"]["settings"]
    assert settings == {
        "target_ratio": 2.2, "target_yield": 0.0, "bean_id": resp["bean_id"],
    }
    # New start from what was stored.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(entry, options=stored["options"])
    await _setup(hass, entry)
    assert _state(hass, BEAN_SELECT) == "Kenya" and _state(hass, RATIO) == "2.2"
    assert hass.states.get(BEAN_SELECT).attributes["options"] == ["Kenya"]


# ------------------------------------- 0.5.0b3 a bean is always active


async def test_first_bean_always_active_and_service_delete_falls_back(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    coord = await _bean_setup(hass, entry, ratio=2.4)
    # First bean: active although activate is false.
    resp = await _bean_service(hass, "add_bean", name="Kenya", ratio=2.0, activate=False)
    assert resp["active"] is True
    assert _state(hass, BEAN_SELECT) == "Kenya" and coord.autostop.ratio == 2.0
    # With beans, activate false keeps the current one.
    await _bean_service(hass, "add_bean", name="Guji", ratio=2.5, activate=False)
    await _bean_service(hass, "add_bean", name="Brazil", ratio=1.8, activate=False)
    assert _state(hass, BEAN_SELECT) == "Kenya"
    # Non-active deleted: nothing changes.
    await _bean_service(hass, "delete_bean", name="guji")
    assert _state(hass, BEAN_SELECT) == "Kenya" and coord.autostop.ratio == 2.0
    # Active deleted: the newest remaining (last created) takes over, with
    # its ratio, at once.
    await _bean_service(hass, "delete_bean", active=True)
    assert _state(hass, BEAN_SELECT) == "Brazil" and coord.autostop.ratio == 1.8
    assert _state(hass, RATIO) == "1.8"
    assert hass.states.get(BEAN_SELECT).attributes["options"] == ["Brazil"]
    # An options save later does not move the ratio again (applied = 1.8).
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": 0.3}, blocking=True
    )
    result = await _options_step(hass, entry, "detection")
    await hass.config_entries.options.async_configure(result["flow_id"], dict(OPTIONS))
    await hass.async_block_till_done()
    assert coord.autostop.ratio == 0.3
    # Last one deleted: none, the ratio stays.
    await _bean_service(hass, "delete_bean", active=True)
    assert _state(hass, BEAN_SELECT) == "none" and coord.autostop.ratio == 0.3
    assert hass.states.get(BEAN_SELECT).attributes["options"] == ["none"]


def _beans_options(*beans: tuple[str, str, float]) -> dict[str, Any]:
    return {
        **OPTIONS,
        "beans": [
            {"id": bean_id, "name": name, "ratio": ratio, "note": None}
            for bean_id, name, ratio in beans
        ],
    }


@pytest.mark.parametrize("stored_bean", [None, "01GONE"])
async def test_setup_activates_newest_bean_when_none_stored(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock,
    stored_bean,
) -> None:
    """Beans but no (valid) active one stored, e.g. 0.5.0b2 after "none":
    the newest becomes active at setup and its ratio applies; saved."""
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    hass_storage[key] = {
        "version": 1, "minor_version": 2, "key": key,
        "data": {
            "model": LIVE["stop_store"]["data"]["model"], "last": None,
            "settings": {"target_ratio": 0.0, "target_yield": 38.0, "bean_id": stored_bean},
        },
    }
    hass.config_entries.async_update_entry(
        entry, options=_beans_options(("01A", "Kenya", 2.0), ("01B", "Guji", 2.5))
    )
    await _setup(hass, entry)
    autostop = entry.runtime_data.autostop
    assert _state(hass, BEAN_SELECT) == "Guji" and autostop.ratio == 2.5
    assert autostop.target == 38.0
    async_fire_time_changed(hass, fire_all=True)
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["settings"]["bean_id"] == "01B"


async def test_restart_without_changes_keeps_a_manual_ratio(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """Stored active bean still there: the stored ratio stays, even one set
    by hand that differs from the bean's ratio."""
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    hass_storage[key] = {
        "version": 1, "minor_version": 2, "key": key,
        "data": {
            "model": LIVE["stop_store"]["data"]["model"], "last": None,
            "settings": {"target_ratio": 0.3, "target_yield": 0.0, "bean_id": "01A"},
        },
    }
    hass.config_entries.async_update_entry(
        entry, options=_beans_options(("01A", "Kenya", 2.0), ("01B", "Guji", 2.5))
    )
    await _setup(hass, entry)
    assert _state(hass, BEAN_SELECT) == "Kenya"
    assert entry.runtime_data.autostop.ratio == 0.3
    # And again after a restart (shutdown flushes, new setup reads it).
    await entry.runtime_data.async_shutdown()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _setup(hass, entry)
    assert _state(hass, BEAN_SELECT) == "Kenya"
    assert entry.runtime_data.autostop.ratio == 0.3
    assert hass_storage[key]["data"]["settings"]["bean_id"] == "01A"


async def test_active_bean_deleted_during_brew(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Like selecting another bean mid-brew: since 0.7.0b6 the running brew
    keeps its target (the cut uses the old ratio), the new bean's ratio
    applies once the brew has ended; the brew keeps the bean of its start."""
    calls = await _setup_autostop(hass, entry, PLUG, 0.0)
    await _bean_service(hass, "add_bean", name="Kenya", ratio=2.4)
    await _bean_service(hass, "add_bean", name="Guji", ratio=2.0, activate=False)
    coord = entry.runtime_data
    assert coord.autostop.active_bean_name == "Kenya"
    events = _events(hass, AUTO_STOP_EVENT)
    original = coord.autostop.feed_sample
    deleted = False

    def _feed(t: float, weight: float, placed: float | None = None) -> None:
        nonlocal deleted
        if not deleted and coord.brew_tracker.state == BREWING and weight > 5:
            deleted = True
            hass.async_create_task(
                _bean_service(hass, "delete_bean", active=True)
            )
        original(t, weight)

    scale = Scale(coord, clock)
    scale.calls = calls
    with patch.object(coord.autostop, "feed_sample", _feed):
        assert await _auto_shot(hass, scale, dose=18.0) is not None
    assert deleted and coord.autostop.active_bean_name == "Guji"
    assert coord.autostop.ratio == 2.0  # after the brew
    cut = events[0].attributes
    assert cut["target"] == 43.2  # 2.4 x 18 g: the ratio the brew started with
    assert cut["bean"] == "Kenya"  # fixed at the brew start
    assert coord.brews.last["bean"] == "Kenya"


# ------------------------------------------- 0.5.0b3 target in the card


async def test_live_and_ready_messages_carry_the_target(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    live: list[dict] = []
    # A callback: a plain ``list.append`` would run in the executor (racy).
    async_dispatcher_connect(
        hass, SIGNAL_LIVE.format(entry.entry_id), callback(lambda msg: live.append(msg))
    )
    # Ready without a dose: the fixed target yield.
    coord.async_set_brew_ready(True)
    assert live[-1]["target"] == 38.0 and live[-1]["target_source"] == "yield"
    assert coord.ready_snapshot()["target"] == 38.0
    # Ratio set: still no dose, so the yield applies; a weighed dose then
    # gives ratio x dose (pushed with the dose).
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": 2.0}, blocking=True
    )
    scale = Scale(coord, clock)
    scale.calls = calls
    scale.weigh_dose(18.0)
    assert live[-1]["dose"] == 18.0 and live[-1]["target"] == 36.0
    assert live[-1]["target_source"] == "ratio"
    # A new ratio is pushed at once (no weight sample needed).
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": 2.2}, blocking=True
    )
    assert live[-1]["type"] == "live_ready" and live[-1]["target"] == 39.6
    # The brew: live_start and live_points carry the same target.
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(3, 0.0)
    start = next(m for m in live if m["type"] == "live_start")
    assert start["target"] == 39.6 and start["target_source"] == "ratio"
    assert coord.live_snapshot()["target"] == 39.6
    # Auto-stop switched off (option emptied): no target any more.
    result = await _options_step(hass, entry, "detection")
    await hass.config_entries.options.async_configure(result["flow_id"], dict(OPTIONS))
    await hass.async_block_till_done()
    scale.run(1, 0.5)
    points = [m for m in live if m["type"] == "live_points"]
    assert points[-1]["target"] is None and points[-1]["target_source"] is None


async def _stop_by_hand(hass: HomeAssistant, scale: Scale) -> None:
    """The user stops the timer (no auto-stop cut); the brew is stored."""
    weight = scale.coord.data.sensor.weight_grams
    scale.frozen = scale.timer()
    scale.run(5, weight)
    scale.status(DeviceState.TIMER_PAUSE)
    scale.timer_start = None
    scale.frozen = None
    scale.run(1, weight)
    await hass.async_block_till_done()


async def test_stored_brew_target_cut_or_effective_or_none(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 30.0)
    coord = entry.runtime_data
    completed: list = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, completed.append)
    # 1. Cut: the target the auto-stop aimed at.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": 2.0}, blocking=True
    )
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale, dose=18.0) is not None
    brew = coord.brews.last
    assert (brew["target"], brew["target_source"]) == (36.0, "ratio")
    assert coord.autostop.last["target"] == 36.0
    assert completed[-1].data["target"] == 36.0
    assert completed[-1].data["target_source"] == "ratio"
    # 2. No cut (target not reached): the effective target, ratio x dose.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": 3.0}, blocking=True
    )
    clock.t += 120
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale, dose=18.0, seconds=20) is None
    await _stop_by_hand(hass, scale)
    brew = coord.brews.last
    assert coord.brews.brew_count == 2
    assert (brew["target"], brew["target_source"]) == (54.0, "ratio")
    # 3. Auto-stop off: no target.
    result = await _options_step(hass, entry, "detection")
    await hass.config_entries.options.async_configure(result["flow_id"], dict(OPTIONS))
    await hass.async_block_till_done()
    clock.t += 120
    scale = Scale(coord, clock)
    assert await _auto_shot(hass, scale, dose=18.0, seconds=20) is None
    await _stop_by_hand(hass, scale)
    brew = coord.brews.last
    assert coord.brews.brew_count == 3
    assert brew["target"] is None and brew["target_source"] is None
    assert completed[-1].data["target"] is None
    # Websocket: summaries and the full brew carry it.
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/brews"})
    brews = (await ws.receive_json())["result"]["brews"]
    assert [b["target"] for b in brews] == [None, 54.0, 36.0]
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/brew", "brew_id": brews[2]["id"]})
    full = (await ws.receive_json())["result"]["brew"]
    assert full["target"] == 36.0 and full["target_source"] == "ratio"


# --------------------------------- 0.5.0b4 settle + tail after the cut


def _record_timer_stop(coord, clock) -> list[float]:
    """Clock times at which the auto-stop asked to stop the timer."""
    stops: list[float] = []
    original = coord.autostop._stop_timer_once

    def _wrapped() -> None:
        if not coord.autostop._timer_stopped:
            stops.append(clock.t)
        original()

    coord.autostop._stop_timer_once = _wrapped
    return stops


async def test_timer_stops_after_calm_plus_tail_and_late_drop_counts(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    stops = _record_timer_stop(coord, clock)
    events = _events(hass, AUTO_STOP_EVENT)
    scale = Scale(coord, clock)
    scale.calls = calls
    # A 0.5 g drop lands 1.5 s after the weight was calm (2026-11-21 15:55).
    cut_t = await _auto_shot(hass, scale, late_drop=(1.5, 0.5), seconds=50)
    assert cut_t is not None and len(stops) == 1
    settled_t = coord.autostop._settled_t
    tail = OPTIONS["tail_seconds"]
    # Stopped tail_seconds after calm (one sample late at most), within the
    # backstop.
    assert tail <= stops[0] - settled_t <= tail + 0.15, (stops, settled_t)
    assert stops[0] - cut_t < 20
    last = coord.autostop.last
    brew = coord.brews.last
    assert last["accepted"] and events[-1].attributes["event_type"] == "learned"
    # The late drop is in the yield, the final weight and the learning sample.
    assert last["final_yield"] - coord.autostop._settled >= 0.45
    assert abs(brew["yield"] - last["final_yield"]) <= 0.1, (brew, last)
    assert last["overshoot"] == round(last["final_yield"] - last["cut_weight"], 2)
    assert last["deviation"] == round(last["final_yield"] - 38.0, 1)
    # Brew time = the cut (pump off), not the timer stop.
    assert abs(brew["duration"] - (cut_t - scale.shot_start)) <= 0.2


async def test_cup_lifted_during_tail_stops_at_once_and_learns(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    stops = _record_timer_stop(coord, clock)
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale, lift_after_settle=1.0, seconds=50) is not None
    settled_t = coord.autostop._settled_t
    assert len(stops) == 1 and 1.0 <= stops[0] - settled_t <= 1.15
    last = coord.autostop.last
    # Calm before the lift: the weight before it is final and learned.
    assert last["accepted"] and last["reason"] is None
    assert last["final_yield"] > last["cut_weight"]
    assert coord.brews.brew_count == 1
    assert abs(coord.brews.last["yield"] - last["final_yield"]) <= 0.1


async def test_backstop_stops_timer_when_never_calm(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    stops = _record_timer_stop(coord, clock)
    scale = Scale(coord, clock)
    scale.calls = calls
    # The pump runs on for 30 s: the weight never calms down.
    cut_t = await _auto_shot(hass, scale, latency=30.0, seconds=60)
    assert cut_t is not None and len(stops) == 1
    cut_sample_t = coord.autostop._last_cut_target[3]  # the cut sample
    assert 20.0 <= stops[0] - cut_sample_t <= 20.15
    last = coord.autostop.last
    assert last["reason"] == "not_settled" and not last["accepted"]
    assert coord.autostop.model.count == 0


async def test_manual_stop_keeps_timer_duration(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Without auto-stop the brew time is the timer run, as before."""
    await _setup(hass, entry)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.weigh_dose(18.0)
    scale.shot()
    await hass.async_block_till_done()
    brew = coord.brews.last
    assert 25.0 <= brew["duration"] <= 27.0, brew
    assert coord.autostop.cut_offset(scale.shot_start) is None  # no cut


async def test_live_time_freezes_at_the_cut(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    live: list[dict] = []
    # A callback: a plain ``list.append`` would run in the executor (racy).
    async_dispatcher_connect(
        hass, SIGNAL_LIVE.format(entry.entry_id), callback(lambda msg: live.append(msg))
    )
    scale = Scale(coord, clock)
    scale.calls = calls
    cut_t = await _auto_shot(hass, scale, seconds=50)
    msgs = [m for m in live if m["type"] in ("live_start", "live_points")]
    before = [m for m in msgs if m["cut"] is None]
    after = [m for m in msgs if m["cut"] is not None]
    assert before and len(after) > 4
    cut = after[0]["cut"]
    assert abs(cut - (cut_t - scale.shot_start)) <= 0.2
    # Weighing goes on (weights change), the time stays at the cut.
    assert {m["elapsed"] for m in after} == {cut}
    assert len({m["weight"] for m in after}) > 2
    assert before[-1]["elapsed"] <= cut + 0.3


async def test_tail_seconds_option_text() -> None:
    for path, words in (
        (COMPONENT / "strings.json", ("calm", "paused scale sends no weight")),
        (COMPONENT / "translations/en.json", ("calm", "paused scale sends no weight")),
        (COMPONENT / "translations/de.json", ("ruhig", "sendet kein Gewicht")),
    ):
        text = json.loads(path.read_text())["options"]["step"]["detection"][
            "data_description"
        ]["tail_seconds"]
        assert all(w in text for w in words), path


# ------------------------------------------- 0.5.0b4 review round 1


async def _hand_stop_paused(hass, coord, scale, clock, seconds: float = 10.0) -> None:
    """Timer stopped by hand; the paused scale sends no weight (ticks only)."""
    scale.frozen = scale.timer()
    scale.status(DeviceState.TIMER_PAUSE)
    for _ in range(round(seconds / 0.1)):
        clock.t = round(clock.t + 0.1, 3)
        scale._tick()
    await hass.async_block_till_done()


async def _next_brew_not_stopped(hass, coord, clock, calls) -> None:
    clock.t += 30
    s2 = Scale(coord, clock)
    s2.calls = calls
    s2.weigh_dose(18.0)
    s2.client.written.clear()
    s2.client.on_status = lambda: s2.status(DeviceState.TIMING)
    s2.status(DeviceState.TIMING)
    s2.timer_start = clock.t
    s2.run(5, lambda r: max(0.0, round((r - 3) * 2, 1)))
    await hass.async_block_till_done()
    assert CMD_TIMER_STOP not in s2.client.written, "the new brew was stopped"
    assert coord.brew_tracker.state == BREWING


async def test_hand_stop_before_calm_concludes_and_spares_next_brew(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    events = _events(hass, AUTO_STOP_EVENT)
    # Pump runs on (never calm); stopped by hand 4 s after the cut.
    assert await _auto_shot(hass, scale, latency=30.0, seconds=26) is not None
    assert coord.autostop._settled is None
    await _hand_stop_paused(hass, coord, scale, clock)
    assert coord.brews.brew_count == 1
    assert coord.autostop._cut is None
    assert coord.autostop.last["reason"] == "not_settled"
    assert events[-1].attributes["event_type"] == "not_settled"
    await _next_brew_not_stopped(hass, coord, clock, calls)
    assert coord.autostop.last["reason"] == "not_settled"  # not cup_lifted


async def test_hand_stop_during_tail_learns_last_weight(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    autostop = coord.autostop
    scale = Scale(coord, clock)
    scale.calls = calls
    # Stop the shot loop shortly after the weight became calm (tail runs).
    original = autostop._observe
    stop_at: list[float] = []

    def _observe(t: float, weight: float) -> None:
        original(t, weight)
        if autostop._settled is not None and not stop_at:
            stop_at.append(t)

    with patch.object(autostop, "_observe", _observe):
        cut_t = None
        # Drive manually: run the shot until 1 s after calm, then stop.
        scale.weigh_dose(18.0)
        scale.client.on_status = lambda: scale.status(
            DeviceState.TIMER_PAUSE if CMD_TIMER_STOP in scale.client.written
            else DeviceState.TIMING
        )
        scale.status(DeviceState.TIMING)
        scale.timer_start = clock.t
        w = 0.0
        start = clock.t
        while not stop_at or clock.t < stop_at[0] + 1.0:
            if cut_t is None and autostop._cut is not None:
                cut_t = clock.t
            if cut_t is None:
                w += 0.2 if clock.t - start > 3 else 0.0
            elif clock.t < cut_t + 1.0:
                w += 0.2
            scale.run(0.1, round(w, 1))
            await hass.async_block_till_done()
            assert clock.t - start < 60
    assert autostop._final is None and autostop._cut is not None
    calm_weight = w
    await _hand_stop_paused(hass, coord, scale, clock)
    last = autostop.last
    assert last["accepted"] and last["final_yield"] == round(calm_weight, 1)
    assert coord.brews.brew_count == 1
    await _next_brew_not_stopped(hass, coord, clock, calls)


async def test_brew_ends_at_auto_stop_timer_stop_without_extra_tail(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    stops = _record_timer_stop(coord, clock)
    stored: list[float] = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, lambda e: stored.append(clock.t))
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale, seconds=50) is not None
    assert len(stops) == 1 and len(stored) == 1
    # Stored at the next tick after the stop, not tail_seconds (3 s) later.
    assert stored[0] - stops[0] <= 0.5, (stops, stored)
