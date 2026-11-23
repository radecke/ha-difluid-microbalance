"""Home Assistant level tests: setup, storage, websocket, live mode, frontend."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import json
from pathlib import Path
import statistics
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, ServiceCall  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402
from homeassistant.helpers.dispatcher import async_dispatcher_connect  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from conftest import Clock  # noqa: E402

from custom_components.difluid_scale import autostop as autostop_mod  # noqa: E402
from custom_components.difluid_scale import coordinator as coord_mod  # noqa: E402
from custom_components.difluid_scale.const import (  # noqa: E402
    CARD_URL,
    DOMAIN,
    EVENT_BREW_COMPLETED,
    READY_TIMEOUT_SECONDS,
    SIGNAL_BREWS_UPDATED,
    SIGNAL_LIVE,
    STOP_STORAGE_KEY,
    STORAGE_KEY,
)
from custom_components.difluid_scale.frontend import (  # noqa: E402
    _async_ensure_resource,
)
from custom_components.difluid_scale.stop_model import StopModel  # noqa: E402
from custom_components.difluid_scale.protocol import (  # noqa: E402
    CMD_GET_STATUS,
    CMD_TIMER_STOP,
    CMD_DEVICE_SENSOR,
    CMD_DEVICE_STATUS,
    FUNC_DEVICE,
    DeviceState,
    build_frame,
)

# The card resource carries the manifest version (?v=...).
VERSION = json.loads(
    (Path(__file__).resolve().parents[1] / "custom_components" / "difluid_scale"
     / "manifest.json").read_text()
)["version"]
BREW_ENTITIES = (
    "sensor.microbalance_last_dose",
    "sensor.microbalance_last_yield",
    "sensor.microbalance_brew_ratio",
    "sensor.microbalance_brew_duration",
    "number.microbalance_dose_correction",
)
OPTIONS = {
    "tail_seconds": 3,
    "min_duration": 5,
    "min_yield": 5,
    "dose_min": 12,
    "dose_max": 25,
    "stability_tolerance": 0.3,
    "stability_seconds": 3,
    "dose_lookback_minutes": 30,
}


class FakeClient:
    is_connected = True

    def __init__(self) -> None:
        self.written: list[bytes] = []
        # Answer to a status request (the scale's reply), if set.
        self.on_status: Callable[[], None] | None = None

    async def disconnect(self) -> None:
        self.is_connected = False

    async def write_gatt_char(self, _char: str, data: bytes, response: bool) -> None:
        self.written.append(bytes(data))
        if bytes(data) == CMD_GET_STATUS and self.on_status is not None:
            self.on_status()


def sensor_frame(weight: float, flow: float, timer: int) -> bytes:
    data = (
        int(round(weight * 10)).to_bytes(4, "big", signed=True)
        + int(round(flow * 10)).to_bytes(2, "big", signed=True)
        + int(timer).to_bytes(2, "big")
        + (0).to_bytes(4, "big")
        + b"\x00"
    )
    return build_frame(FUNC_DEVICE, CMD_DEVICE_SENSOR, data)


def status_frame(state: int) -> bytes:
    return build_frame(FUNC_DEVICE, CMD_DEVICE_STATUS, bytes((state, 80, 0, 0, 0, 0, 0, 0)))


class Scale:
    """Drives the coordinator like the real scale (10 Hz frames)."""

    def __init__(self, coordinator: coord_mod.DifluidCoordinator, clock: Clock) -> None:
        self.coord = coordinator
        self.clock = clock
        self.timer_start: float | None = None
        self.frozen: int | None = None
        self.client = FakeClient()
        self.next_tick = clock.t
        self.shot_start = clock.t
        self.calls: list = []
        self.calls_at_cut = 0
        self.written_at_cut: list[bytes] = []
        self.connect()

    def connect(self) -> None:
        self.client = FakeClient()
        self.coord._client = self.client  # noqa: SLF001

    def disconnect(self) -> None:
        self.coord._async_handle_disconnect(self.client)  # noqa: SLF001

    def timer(self) -> int:
        if self.frozen is not None:
            return self.frozen
        if self.timer_start is None:
            return 0
        return int(self.clock.t - self.timer_start)

    def run(
        self,
        seconds: float,
        weight: Callable[[float], float] | float,
        flow: Callable[[float], float] | float = 0.0,
        tick: bool = True,
    ) -> None:
        start = self.clock.t
        for i in range(round(seconds * 10)):
            r = self.clock.t - start
            w = weight(r) if callable(weight) else weight
            f = flow(r) if callable(flow) else flow
            self.coord._on_notification(  # noqa: SLF001
                None, bytearray(sensor_frame(w, f, self.timer()))
            )
            self.clock.t = round(self.clock.t + 0.1, 3)
            if tick:
                self._tick()

    def _tick(self) -> None:
        """The coordinator's 0.25 s brew tick (async_track_time_interval)."""
        while self.clock.t >= self.next_tick:
            self.next_tick = round(self.next_tick + 0.25, 3)
            if self.coord._cancel_brew_tick:  # noqa: SLF001
                self.coord._async_brew_tick(None)  # noqa: SLF001

    def gap(self, seconds: float) -> None:
        """Link down: no frames, the tick still runs."""
        for _ in range(round(seconds / 0.25)):
            self.clock.t = round(self.clock.t + 0.25, 3)
            self._tick()

    def status(self, state: int) -> None:
        self.coord._on_notification(None, bytearray(status_frame(state)))  # noqa: SLF001

    def weigh_dose(self, dose: float = 18.0) -> None:
        self.run(2, 0.0)
        self.run(5, dose)
        self.run(2, -150.0)
        self.run(2, 0.0)
        self.run(2, 118.0)
        self.run(2, 0.0)

    def shot(self, cut: float = 36.0) -> None:
        self.status(DeviceState.TIMING)
        self.timer_start = self.clock.t
        self.frozen = None
        self.run(
            26,
            lambda r: min(cut, max(0.0, round((r - 5) * 1.8, 1))),
            lambda r: 1.8 if 5 < r < 25 else 0.0,
        )
        self.frozen = self.timer()
        self.run(5, cut + 0.4)
        self.status(DeviceState.TIMER_PAUSE)
        self.timer_start = None
        self.frozen = None
        self.run(1, cut + 0.4)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


# --------------------------------------------------------------- lifecycle


async def test_setup_unload_remove_deletes_store(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    key = STORAGE_KEY.format(entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "brews": [
                {
                    "id": "01OLD",
                    "start": "2026-11-19T07:00:00+00:00",
                    "duration": 27.0,
                    "dose": 18.0,
                    "dose_source": "auto",
                    "yield": 36.0,
                    "ratio": 2.0,
                    "profile": [[0.0, 0.0, 0.0], [27.0, 36.0, 0.0]],
                }
            ],
            "brew_count": 1,
            "coffee_total": 18.0,
        },
    }
    await _setup(hass, entry)
    coord = entry.runtime_data
    # Migration: brews stored before 0.3.0b2 count their dose as counted.
    assert coord.brews.last["counted_dose"] == 18.0
    assert _state(hass, "sensor.microbalance_last_dose") == "18.0"
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.microbalance_dose_correction", "value": 17.0},
        blocking=True,
    )
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.microbalance_dose_correction", "value": 18.0},
        blocking=True,
    )
    assert _state(hass, "sensor.microbalance_coffee_ground_total") == "18.0"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # Pending delayed save was flushed on unload.
    assert hass_storage[key]["data"]["brews"][0]["dose"] == 18.0
    assert hass_storage[key]["data"]["brews"][0]["dose_source"] == "manual"

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_store_round_trip_across_reload(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    scale = Scale(entry.runtime_data, clock)
    scale.weigh_dose(18.0)
    scale.shot()
    await hass.async_block_till_done()
    first = entry.runtime_data.brews.last
    assert first is not None and first["dose"] == 18.0

    # Reload right away (inside the 1 s delayed-save window).
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    history = entry.runtime_data.brews
    assert history.brew_count == 1 and history.coffee_total == 18.0
    assert history.last == first
    assert _state(hass, "sensor.microbalance_last_yield") == str(first["yield"])
    assert _state(hass, "sensor.microbalance_brew_count") == "1"


async def test_brew_sensors_unknown_before_brew_and_kept_after_disconnect(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    for entity_id in BREW_ENTITIES:
        assert _state(hass, entity_id) == "unknown", entity_id
    assert _state(hass, "sensor.microbalance_brew_count") == "0"
    assert _state(hass, "sensor.microbalance_weight") == "unavailable"

    events: list[dict] = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, lambda e: events.append(e.data))
    scale = Scale(entry.runtime_data, clock)
    scale.weigh_dose(18.0)
    scale.shot()
    await hass.async_block_till_done()
    assert len(events) == 1 and events[0]["dose"] == 18.0
    before = {e: _state(hass, e) for e in BREW_ENTITIES}
    assert before["sensor.microbalance_last_dose"] == "18.0"

    scale.disconnect()
    await hass.async_block_till_done()
    assert _state(hass, "sensor.microbalance_weight") == "unavailable"
    assert {e: _state(hass, e) for e in BREW_ENTITIES} == before


async def test_ble_drop_mid_shot_is_one_brew(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.weigh_dose(18.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(12, lambda r: max(0.0, round((r - 3) * 1.8, 1)), 1.8)
    scale.disconnect()
    assert coord.brew_tracker.state == "parked"
    assert coord._cancel_brew_tick is not None  # noqa: SLF001 - tick keeps running
    scale.gap(4)
    scale.connect()
    scale.status(DeviceState.TIMING)
    scale.run(10, lambda r: min(36.0, round((r + 13) * 1.8, 1)), 1.8)
    scale.frozen = scale.timer()
    scale.run(5, 36.2)
    await hass.async_block_till_done()
    assert coord.brews.brew_count == 1
    assert coord.brews.last["dose"] == 18.0 and coord.brews.last["yield"] == 36.2
    assert _state(hass, "sensor.microbalance_coffee_ground_total") == "18.0"


async def test_unload_mid_brew_then_late_disconnect_is_inert(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    entry,
    no_ble,
    lovelace,
    clock,
) -> None:
    """Review 4: bleak reports the disconnect after the unload."""
    await _setup(hass, entry)
    coord = entry.runtime_data
    events: list[dict] = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, lambda e: events.append(e.data))
    live: list[dict] = []
    async_dispatcher_connect(hass, SIGNAL_LIVE.format(entry.entry_id), live.append)
    scale = Scale(coord, clock)
    scale.weigh_dose(18.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(10, lambda r: max(0.0, round((r - 3) * 1.8, 1)), 1.8)
    assert coord.brew_tracker.state == "brewing"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert live[-1]["type"] == "live_end" and live[-1]["reason"] == "unloaded"
    key = STORAGE_KEY.format(entry.entry_id)
    stored = hass_storage[key]["data"]
    assert stored["active"]["dose"][0] == 18.0 and stored["brew_count"] == 0
    snapshot = repr(hass_storage[key])
    live.clear()

    coord._async_handle_disconnect(scale.client)  # noqa: SLF001 - late callback
    scale.run(2, 5.0)  # late notifications are ignored as well
    scale.gap(40)
    await hass.async_block_till_done()
    assert events == [] and live == []
    assert coord._cancel_brew_tick is None  # noqa: SLF001
    assert coord.brews.brew_count == 0
    assert repr(hass_storage[key]) == snapshot


async def test_reload_mid_brew_is_one_brew(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    events: list[dict] = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, lambda e: events.append(e.data))
    old = entry.runtime_data
    scale = Scale(old, clock)
    scale.weigh_dose(18.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    start = clock.t
    scale.run(10, lambda r: max(0.0, round((r - 3) * 1.8, 1)), 1.8)
    old_client = scale.client

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coord = entry.runtime_data
    assert coord is not old
    assert coord.brew_tracker.state == "parked"  # handed over
    assert coord.brew_tracker.dose_candidate == 18.0
    old._async_handle_disconnect(old_client)  # noqa: SLF001 - late, inert

    scale2 = Scale(coord, clock)
    scale2.next_tick = clock.t
    scale2.timer_start = start
    scale2.gap(1)
    scale2.run(12, lambda r: min(36.0, round((r + 8) * 1.8, 1)), 1.8)
    scale2.frozen = scale2.timer()
    scale2.run(5, 36.2)
    await hass.async_block_till_done()
    assert len(events) == 1, events
    assert coord.brews.brew_count == 1 and coord.brews.coffee_total == 18.0
    brew = coord.brews.last
    assert brew["dose"] == 18.0 and brew["yield"] == 36.2
    assert brew["profile"][0][0] < 1.0  # profile from the real start


async def test_timer_stop_button_requests_status(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Review 2: a stop via HA is seen at once, not after the stall check."""
    await _setup(hass, entry)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.weigh_dose(18.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(26, lambda r: min(36.0, max(0.0, round((r - 5) * 1.8, 1))), 1.8)
    scale.client.on_status = lambda: scale.status(DeviceState.TIMER_PAUSE)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.microbalance_stop_timer"},
        blocking=True,
    )
    assert scale.client.written[-2:] == [CMD_TIMER_STOP, CMD_GET_STATUS]
    assert coord.brew_tracker.state == "tail"
    scale.frozen = scale.timer()
    scale.run(1.0, 36.0)
    scale.run(4, -300.0)  # cup lifted 1 s after the stop
    await hass.async_block_till_done()
    assert coord.brews.brew_count == 1 and coord.brews.last["yield"] == 36.0


async def test_pour_over_pause_updates_stored_brew(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Review 3: resumed after a long pause = same stored brew, updated."""
    await _setup(hass, entry)
    coord = entry.runtime_data
    events: list[dict] = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, lambda e: events.append(e.data))
    scale = Scale(coord, clock)
    scale.run(2, 0.0)
    scale.run(5, 15.0)
    scale.run(3, 0.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(30, lambda r: min(50.0, r * 3))
    scale.frozen = scale.timer()
    scale.status(DeviceState.TIMER_PAUSE)
    scale.run(10, 50.0)
    await hass.async_block_till_done()
    assert len(events) == 1 and not events[0]["updated"]
    first = dict(coord.brews.last)
    assert coord.brews.coffee_total == 15.0

    scale.timer_start = clock.t - scale.frozen
    scale.frozen = None
    scale.status(DeviceState.TIMING)
    scale.run(60, lambda r: 50 + min(200.0, r * 4))
    scale.frozen = scale.timer()
    scale.status(DeviceState.TIMER_PAUSE)
    scale.run(5, 250.0)
    await hass.async_block_till_done()
    assert len(events) == 2 and events[1]["updated"] is True
    assert events[1]["brew_id"] == first["id"]
    history = coord.brews
    assert history.brew_count == 1 and history.coffee_total == 15.0
    assert len(history.brews) == 1
    last = history.last
    assert last["id"] == first["id"] and last["start"] == first["start"]
    assert last["yield"] == 250.0 and last["dose"] == 15.0
    assert 99.0 <= last["duration"] <= 101.5
    assert _state(hass, "sensor.microbalance_last_yield") == "250.0"


async def test_long_drop_reconnect_status_first_is_one_brew(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Review 5: drop past the grace, status reply before the first frame."""
    await _setup(hass, entry)
    coord = entry.runtime_data
    events: list[dict] = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, lambda e: events.append(e.data))
    scale = Scale(coord, clock)
    scale.weigh_dose(18.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(12, lambda r: max(0.0, round((r - 3) * 1.8, 1)), 1.8)
    scale.gap(9)  # silence, then the disconnect is reported
    scale.disconnect()
    scale.gap(30)  # reconnect after ~39 s, timer still running
    scale.connect()
    scale.status(DeviceState.TIMING)  # reply to CMD_GET_STATUS comes first
    scale.run(10, lambda r: min(36.0, round((r + 51) * 1.8, 1)), 1.8)
    scale.frozen = scale.timer()
    scale.run(5, 36.2)
    await hass.async_block_till_done()
    assert coord.brews.brew_count == 1, events
    assert [e["updated"] for e in events] == [False, True], events
    brew = coord.brews.last
    assert brew["dose"] == 18.0 and brew["yield"] == 36.2
    assert coord.brews.coffee_total == 18.0


async def test_ha_stop_hands_over_brew(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    entry,
    no_ble,
    lovelace,
    clock,
    caplog,
) -> None:
    """Review 5: HA stop does not unload entries; the brew is still saved."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    await _setup(hass, entry)
    coord = entry.runtime_data
    events: list[dict] = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, lambda e: events.append(e.data))
    scale = Scale(coord, clock)
    scale.weigh_dose(18.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    start = clock.t
    scale.run(10, lambda r: max(0.0, round((r - 3) * 1.8, 1)), 1.8)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    key = STORAGE_KEY.format(entry.entry_id)
    stored = hass_storage[key]["data"]
    assert stored["active"]["state"] == "parked" and stored["active"]["dose"][0] == 18.0
    scale.run(2, 20.0)  # inert after the stop
    scale.gap(40)
    assert events == [] and coord.brews.brew_count == 0

    # Next start: same entry set up again from the store.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "Unable to remove unknown job listener" not in caplog.text
    clock.t = start + 14
    await _setup(hass, entry)
    coord = entry.runtime_data
    assert coord.brew_tracker.state == "parked"
    scale2 = Scale(coord, clock)
    scale2.next_tick = clock.t
    scale2.timer_start = start
    scale2.run(12, lambda r: min(36.0, round((r + 11) * 1.8, 1)), 1.8)
    scale2.frozen = scale2.timer()
    scale2.run(5, 36.2)
    await hass.async_block_till_done()
    assert len(events) == 1 and coord.brews.brew_count == 1, events
    assert coord.brews.last["dose"] == 18.0 and coord.brews.last["yield"] == 36.2


async def test_unload_removes_ha_stop_listener(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, caplog
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    await _setup(hass, entry)
    coord = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    with patch.object(coord, "async_shutdown") as shutdown:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    shutdown.assert_not_called()
    assert "Unable to remove unknown job listener" not in caplog.text


async def test_reload_during_pour_over_pause_still_merges(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Review 5: the resume candidate survives a reload."""
    await _setup(hass, entry)
    events: list[dict] = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, lambda e: events.append(e.data))
    scale = Scale(entry.runtime_data, clock)
    scale.run(2, 0.0)
    scale.run(5, 15.0)
    scale.run(3, 0.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(30, lambda r: min(50.0, r * 3))
    frozen = scale.frozen = scale.timer()
    scale.status(DeviceState.TIMER_PAUSE)
    scale.run(10, 50.0)
    await hass.async_block_till_done()
    assert len(events) == 1

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coord = entry.runtime_data
    assert coord.brew_tracker._resume is not None  # noqa: SLF001
    scale2 = Scale(coord, clock)
    scale2.next_tick = clock.t
    scale2.gap(3)
    scale2.status(DeviceState.TIMER_PAUSE)  # reply on connect
    scale2.frozen = frozen
    scale2.run(2, 50.0)
    scale2.timer_start = clock.t - frozen
    scale2.frozen = None
    scale2.status(DeviceState.TIMING)
    scale2.run(30, lambda r: 50 + min(70.0, r * 3))
    scale2.frozen = scale2.timer()
    scale2.status(DeviceState.TIMER_PAUSE)
    scale2.run(5, 120.0)
    await hass.async_block_till_done()
    assert [e["updated"] for e in events] == [False, True], events
    history = coord.brews
    assert history.brew_count == 1 and history.coffee_total == 15.0
    assert history.last["yield"] == 120.0 and history.last["dose"] == 15.0


# --------------------------------------------------------------- websocket


async def test_websocket_brews_brew_and_subscribe(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    ws = await hass_ws_client(hass)

    await ws.send_json({"id": 1, "type": f"{DOMAIN}/brews"})
    msg = await ws.receive_json()
    assert msg["success"] and msg["result"]["brews"] == []
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/brew"})
    msg = await ws.receive_json()
    assert msg["success"] and msg["result"]["brew"] is None
    await ws.send_json({"id": 3, "type": f"{DOMAIN}/brews", "entry_id": "nope"})
    msg = await ws.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "not_found"

    await ws.send_json({"id": 4, "type": f"{DOMAIN}/subscribe_brews"})
    assert (await ws.receive_json())["success"]

    scale = Scale(entry.runtime_data, clock)
    scale.weigh_dose(18.0)
    scale.shot()
    await hass.async_block_till_done()
    push = await ws.receive_json()
    assert push["id"] == 4 and push["event"]["last"]["dose"] == 18.0
    assert "profile" not in push["event"]["last"]
    assert "counted_dose" not in push["event"]["last"]
    brew_id = push["event"]["last"]["id"]

    await ws.send_json({"id": 5, "type": f"{DOMAIN}/brews"})
    msg = await ws.receive_json()
    assert [b["id"] for b in msg["result"]["brews"]] == [brew_id]
    assert msg["result"]["brews"][0]["points"] > 50
    await ws.send_json({"id": 6, "type": f"{DOMAIN}/brew", "brew_id": brew_id})
    msg = await ws.receive_json()
    brew = msg["result"]["brew"]
    assert brew["id"] == brew_id and len(brew["profile"]) > 50
    await ws.send_json({"id": 7, "type": f"{DOMAIN}/brew", "brew_id": "unknown"})
    msg = await ws.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "not_found"

    # Dose correction pushes too, with the new ratio.
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.microbalance_dose_correction", "value": 18.5},
        blocking=True,
    )
    push = await ws.receive_json()
    assert push["id"] == 4
    last = push["event"]["last"]
    assert last["dose"] == 18.5 and last["dose_source"] == "manual"
    assert last["ratio"] == round(last["yield"] / 18.5, 2)

    # After a reload the subscription pushes the new instance's data.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.microbalance_dose_correction", "value": 19.0},
        blocking=True,
    )
    push = await ws.receive_json()
    assert push["id"] == 4 and push["event"]["last"]["dose"] == 19.0

    # device_id targeting works as well.
    device_id = entry.runtime_data._device_id()  # noqa: SLF001
    await ws.send_json({"id": 8, "type": f"{DOMAIN}/brews", "device_id": device_id})
    msg = await ws.receive_json()
    assert msg["success"] and msg["result"]["entry_id"] == entry.entry_id


async def test_live_mode_stream(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/subscribe_live"})
    assert (await ws.receive_json())["success"]
    idle = await ws.receive_json()
    assert idle["event"]["type"] == "live_idle"
    ready = await ws.receive_json()
    assert ready["event"] == {
        "type": "live_ready", "ready": False, "dose": None, "entry_id": entry.entry_id,
        "target": None, "target_source": None,
    }

    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.weigh_dose(18.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(8, lambda r: max(0.0, round((r - 3) * 1.8, 1)), lambda r: 1.8 if r > 3 else 0.0)
    await hass.async_block_till_done()

    start = (await ws.receive_json())["event"]
    assert start["type"] == "live_start" and start["dose"] == 18.0
    assert start["phase"] == "brewing" and start["entry_id"] == entry.entry_id
    points = list(start["points"])
    batches = 0
    sent = coord._live.points  # noqa: SLF001 - what the server has pushed
    while len(points) < len(sent):
        msg = (await ws.receive_json())["event"]
        assert msg["type"] == "live_points"
        batches += 1
        points.extend(msg["points"])
    assert points == sent and msg["elapsed"] >= 7.0
    # Coalesced: ~4 messages per second, not one per 10 Hz frame.
    assert batches <= 8 * 4 + 1
    assert all(b[0] > a[0] for a, b in zip(points, points[1:]))
    assert msg["weight"] is not None and msg["flow"] == 1.8

    # A client subscribing mid-brew gets every point so far first.
    ws2 = await hass_ws_client(hass)
    await ws2.send_json({"id": 1, "type": f"{DOMAIN}/subscribe_live"})
    assert (await ws2.receive_json())["success"]
    late = (await ws2.receive_json())["event"]
    assert late["type"] == "live_start" and late["points"] == points
    assert (await ws2.receive_json())["event"]["type"] == "live_ready"

    # Parked during a BLE drop, then resumed as the same brew.
    scale.disconnect()
    scale.gap(1)
    msg = (await ws.receive_json())["event"]
    while msg["phase"] != "parked":
        msg = (await ws.receive_json())["event"]
    assert msg["type"] == "live_points" and msg["points"] == []
    scale.connect()

    scale.run(14, lambda r: min(36.0, round((r + 6) * 1.8, 1)), 1.8)
    scale.frozen = scale.timer()
    scale.run(6, 36.3)
    await hass.async_block_till_done()

    def drain(client_msgs: list[dict]) -> dict:
        return next(m for m in client_msgs if m["type"] == "live_end")

    msgs = []
    while True:
        m = (await ws.receive_json())["event"]
        msgs.append(m)
        if m["type"] == "live_end":
            break
    end = drain(msgs)
    assert end["brew_id"] == coord.brews.last["id"]
    assert end["brew"]["dose"] == 18.0 and end["brew"]["yield"] == 36.3
    assert any(m.get("phase") == "tail" and m.get("stop") for m in msgs[:-1])
    msgs2 = []
    while True:
        m = (await ws2.receive_json())["event"]
        msgs2.append(m)
        if m["type"] == "live_end":
            break
    assert msgs2[-1]["brew_id"] == end["brew_id"]

    # Unsubscribe client 2; a discarded run reaches client 1 only.
    await ws2.send_json({"id": 2, "type": "unsubscribe_events", "subscription": 1})
    assert (await ws2.receive_json())["success"]
    scale.status(DeviceState.IDLE)
    scale.timer_start = None
    scale.frozen = None
    scale.run(2, 0.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(3, 0.0)
    scale.frozen = scale.timer()
    scale.run(6, 0.0)
    await hass.async_block_till_done()
    msgs = []
    while True:
        m = (await ws.receive_json())["event"]
        msgs.append(m)
        if m["type"] == "live_end":
            break
    assert msgs[0]["type"] == "live_start" and msgs[0]["dose"] is None
    assert msgs[-1]["brew_id"] is None and msgs[-1]["reason"] == "too_short"
    assert coord.brews.brew_count == 1


# ------------------------------------------------------------- brew ready

READY = "switch.microbalance_prepare_brew"


async def _switch(hass: HomeAssistant, on: bool) -> None:
    await hass.services.async_call(
        "switch", "turn_on" if on else "turn_off", {"entity_id": READY}, blocking=True
    )


def _start_brew(scale: Scale, clock: Clock) -> None:
    scale.weigh_dose(18.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(8, lambda r: max(0.0, round((r - 3) * 1.8, 1)), 1.8)


def _finish_brew(scale: Scale) -> None:
    scale.run(14, lambda r: min(36.0, round((r + 6) * 1.8, 1)), 1.8)
    scale.frozen = scale.timer()
    scale.run(6, 36.3)


async def _later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_brew_ready_switch_manual_and_without_scale(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    # Scale not connected: available (dashboard can be armed beforehand).
    assert not coord.connected
    assert _state(hass, READY) == "off"
    await _switch(hass, True)
    assert _state(hass, READY) == "on" and coord.brew_ready
    await _switch(hass, False)
    assert _state(hass, READY) == "off" and not coord.brew_ready
    # Not restored: a reload starts off.
    await _switch(hass, True)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, READY) == "off"
    # The old instance's backstop is gone with it (no error, stays off).
    await _later(hass, READY_TIMEOUT_SECONDS + 1)
    assert _state(hass, READY) == "off"


async def test_brew_ready_off_when_brew_stored(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    await _switch(hass, True)
    _start_brew(scale, clock)
    await hass.async_block_till_done()
    assert _state(hass, READY) == "on"
    _finish_brew(scale)
    await hass.async_block_till_done()
    assert coord.brews.brew_count == 1
    assert _state(hass, READY) == "off"


async def test_brew_ready_off_when_run_discarded(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    await _switch(hass, True)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(3, 0.0)
    await hass.async_block_till_done()
    assert coord._live is not None and _state(hass, READY) == "on"  # noqa: SLF001
    scale.frozen = scale.timer()
    scale.run(6, 0.0)
    await hass.async_block_till_done()
    assert coord.brews.brew_count == 0
    assert _state(hass, READY) == "off"


async def test_brew_ready_backstop(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    await _switch(hass, True)
    await _later(hass, READY_TIMEOUT_SECONDS - 5)
    assert _state(hass, READY) == "on"
    await _later(hass, READY_TIMEOUT_SECONDS + 1)
    assert _state(hass, READY) == "off"


async def test_brew_ready_backstop_suspended_while_brewing(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    await _switch(hass, True)
    _start_brew(scale, clock)
    await hass.async_block_till_done()
    await _later(hass, READY_TIMEOUT_SECONDS + 60)
    assert coord._live is not None  # noqa: SLF001
    assert _state(hass, READY) == "on"
    # Turned on during a brew: no backstop either, the brew's end decides.
    await _switch(hass, False)
    await _switch(hass, True)
    await _later(hass, 2 * READY_TIMEOUT_SECONDS + 60)
    assert _state(hass, READY) == "on"
    _finish_brew(scale)
    await hass.async_block_till_done()
    assert _state(hass, READY) == "off"


async def test_brew_ready_websocket(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    await _switch(hass, True)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/subscribe_live"})
    assert (await ws.receive_json())["success"]
    assert (await ws.receive_json())["event"]["type"] == "live_idle"
    ready = (await ws.receive_json())["event"]
    assert ready["type"] == "live_ready" and ready["ready"] is True

    await _switch(hass, False)
    assert (await ws.receive_json())["event"]["ready"] is False
    await _switch(hass, False)  # unchanged: no message
    await _switch(hass, True)
    msg = (await ws.receive_json())["event"]
    assert msg == {
        "type": "live_ready", "ready": True, "dose": None, "entry_id": entry.entry_id,
        "target": None, "target_source": None,  # auto-stop off
    }

    # The dose of the next brew comes with it as soon as a plateau is known.
    scale = Scale(coord, clock)
    scale.weigh_dose(18.0)
    msg = (await ws.receive_json())["event"]
    assert msg == {
        "type": "live_ready", "ready": True, "dose": 18.0, "entry_id": entry.entry_id,
        "target": None, "target_source": None,
    }
    await _switch(hass, False)
    assert (await ws.receive_json())["event"]["dose"] is None
    await _switch(hass, True)
    assert (await ws.receive_json())["event"]["dose"] == 18.0

    # Brew end: ready false comes right before live_end.
    scale = Scale(coord, clock)
    _start_brew(scale, clock)
    _finish_brew(scale)
    await hass.async_block_till_done()
    types = []
    while True:
        m = (await ws.receive_json())["event"]
        types.append(m["type"])
        if m["type"] == "live_end":
            break
    assert types[0] == "live_start"
    assert types[-2:] == ["live_ready", "live_end"]
    assert types.count("live_ready") == 1


# ------------------------------------------------------------ config flow


async def _options_step(hass: HomeAssistant, entry, step: str) -> dict:
    """Open the options flow and pick ``step`` from the menu."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu" and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    assert result["step_id"] == step, result
    return result


async def test_options_flow(hass: HomeAssistant, entry, no_ble, lovelace, clock) -> None:
    await _setup(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    # No bean profiles yet: nothing to edit or delete.
    assert result["menu_options"] == ["detection", "add_bean"]
    result = await _options_step(hass, entry, "detection")
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, "dose_min": 26, "dose_max": 25}
    )
    assert result["type"] == "form" and result["errors"] == {"base": "dose_range"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, "tail_seconds": 2}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    # Applied live, without a reload (same coordinator, same BLE link).
    assert entry.runtime_data.brew_tracker.config.tail_seconds == 2
    assert entry.options["tail_seconds"] == 2


# ---------------------------------------------------------------- frontend


async def test_lovelace_resource_storage_mode_registered_once(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    resources = hass.data["lovelace"].resources
    ours = [r for r in resources.async_items() if r["url"].startswith(CARD_URL)]
    assert [r["url"] for r in ours] == [f"{CARD_URL}?v={VERSION}"]
    assert ours[0]["type"] == "module"

    # Again (e.g. next start): no duplicate.
    await _async_ensure_resource(hass, f"{CARD_URL}?v={VERSION}")
    ours = [r for r in resources.async_items() if r["url"].startswith(CARD_URL)]
    assert len(ours) == 1

    # New version: the existing item is updated in place.
    await _async_ensure_resource(hass, f"{CARD_URL}?v=9.9.9")
    ours = [r for r in resources.async_items() if r["url"].startswith(CARD_URL)]
    assert [r["url"] for r in ours] == [f"{CARD_URL}?v=9.9.9"]

    # The card file is served.
    assert any(
        getattr(r, "canonical", None) == CARD_URL
        for r in hass.http.app.router.resources()
    )


async def test_lovelace_yaml_mode_does_not_crash(
    hass: HomeAssistant, entry, no_ble, clock, caplog
) -> None:
    assert await async_setup_component(
        hass, "lovelace", {"lovelace": {"mode": "yaml", "resource_mode": "yaml"}}
    )
    await _setup(hass, entry)
    await hass.async_block_till_done()
    assert entry.state.name == "LOADED"
    resources = hass.data["lovelace"].resources
    assert not [r for r in resources.async_items() if r["url"].startswith(CARD_URL)]
    await _async_ensure_resource(hass, f"{CARD_URL}?v={VERSION}")
    assert "YAML mode" in caplog.text


async def test_no_lovelace_does_not_crash(hass: HomeAssistant, caplog) -> None:
    await _async_ensure_resource(hass, f"{CARD_URL}?v={VERSION}")
    assert "Lovelace not loaded" in caplog.text


# ------------------------------------------------------------ auto-stop

# The stop switch: a smart plug (attributes like a live one, neutral name).
PLUG = "switch.coffee_machine"
PLUG_ATTRS = {"friendly_name": "Coffee machine"}
TARGET = "number.microbalance_target_yield"
STOP_SENSORS = (
    "sensor.microbalance_learned_stop_latency",
    "sensor.microbalance_stop_drip",
    "sensor.microbalance_stop_learning_samples",
    "sensor.microbalance_stop_deviation",
)


# Nominal latency of the fake plug (the measured median of the real one,
# 2026-11-23: 68 ms; 0.07 s = the default nominal latency, so a shot at it
# learns uncorrected).
PLUG_LATENCY = 0.07


class PlugCalls(list):
    """The plug's turn_off calls; ``reports`` records for every ``off``
    report whether the service call had returned by then."""

    def __init__(self) -> None:
        super().__init__()
        self.reports: list[bool] = []


def _call_returned(hass: HomeAssistant) -> bool:
    """The auto-stop's running measurement has seen its call return (or
    there is none)."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        coord = getattr(entry, "runtime_data", None)
        probe = coord.autostop._probe if coord is not None else None  # noqa: SLF001
        if probe is not None and probe._call_ok is None:  # noqa: SLF001
            return False
    return True


def mock_plug(
    hass: HomeAssistant,
    *,
    latency: float | None = PLUG_LATENCY,
    report: str = "after",
) -> PlugCalls:
    """``switch.turn_off`` of a non-optimistic smart plug: its state becomes
    ``off`` only when the plug reports, ``latency`` s (switch clock) after
    the measurement start.

    ``report``: "after" (default, like Zigbee2MQTT: the MQTT publish returns
    at once, the report arrives after the call returned, a few loop turns
    later, before the shot goes on); "during" = before the call returns;
    "never" = no report (latency ignored; for a late report use
    ``plug_reports_off``)."""
    calls = PlugCalls()

    def _report(entity_id: str) -> None:
        calls.reports.append(_call_returned(hass))
        plug_reports_off(hass, entity_id, latency)

    async def _later(entity_id: str) -> None:
        for _ in range(50):
            await asyncio.sleep(0)
            if _call_returned(hass):
                break
        _report(entity_id)

    async def _turn_off(call: ServiceCall) -> None:
        calls.append(call)
        if latency is None or report == "never":
            return
        if report == "during":
            _report(call.data["entity_id"])
        else:
            hass.async_create_task(_later(call.data["entity_id"]))

    hass.services.async_register("switch", "turn_off", _turn_off)
    return calls


def plug_reports_off(hass: HomeAssistant, entity_id: str, latency: float) -> None:
    """The plug reports ``off`` ``latency`` s after the measurement start."""
    clock = autostop_mod.time
    clock.t += latency
    state = hass.states.get(entity_id)
    hass.states.async_set(entity_id, "off", state.attributes if state else PLUG_ATTRS)


async def _setup_autostop(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    switch: str | None,
    target: float,
    *,
    latency: float | None = PLUG_LATENCY,
    report: str = "after",
) -> list:
    options = dict(OPTIONS)
    if switch is not None:
        options["stop_switch"] = switch
        # The plug is on while the machine is in use.
        hass.states.async_set(switch, "on", PLUG_ATTRS)
    hass.config_entries.async_update_entry(entry, options=options)
    await _setup(hass, entry)
    calls = mock_plug(hass, latency=latency, report=report)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": TARGET, "value": target}, blocking=True
    )
    return calls


async def _auto_shot(
    hass: HomeAssistant,
    scale: Scale,
    *,
    flow: float = 2.0,
    latency: float = 1.2,
    drip: float = 0.5,
    seconds: float = 45.0,
    start_weight: float = 0.0,
    lift_after_cut: float | None = None,
    dose: float | None = 18.0,
    late_drop: tuple[float, float] | None = None,
    lift_after_settle: float | None = None,
    glitch: tuple[float, float, int] | None = None,
) -> float | None:
    """A shot whose pump stops ``latency`` s after the auto-stop cut, then
    drips ``drip`` g. The scale answers the timer stop like the real one.
    ``glitch`` = (weight, value, frames): once the weight reaches ``weight``
    the scale sends ``value`` instead for that many frames (bad frames).
    Returns the cut time (None if never cut)."""
    coord = scale.coord
    clock = scale.clock
    if dose is not None:
        scale.weigh_dose(dose)

    def _on_status() -> None:
        # Like the real scale: a status request is answered with the current
        # state; paused once the stop (DLink press) went out.
        if CMD_TIMER_STOP in scale.client.written:
            scale.frozen = scale.timer()
            scale.status(DeviceState.TIMER_PAUSE)
        else:
            scale.status(DeviceState.TIMING)

    scale.client.on_status = _on_status
    # The machine is switched on again for every shot (the plug reported
    # off after the previous auto-stop).
    switch = coord.autostop.switch_entity
    if switch and (st := hass.states.get(switch)) and st.state == "off":
        hass.states.async_set(switch, "on", st.attributes)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    w = start_weight
    dripped = 0.0
    cut_t: float | None = None
    start = scale.shot_start = clock.t
    while clock.t - start < seconds:
        if cut_t is None and coord.autostop._cut is not None:  # noqa: SLF001
            cut_t = clock.t
            # Issued from the sample handler itself, before anything awaits.
            scale.calls_at_cut = len(scale.calls)
            scale.written_at_cut = list(scale.client.written)
            await hass.async_block_till_done()
        off = cut_t is not None and clock.t >= cut_t + latency
        if clock.t - start > 3 and not off:
            w += flow * 0.1
        elif off:
            step = (drip - dripped) * 0.3
            dripped += step
            w += step
        settled_t = coord.autostop._settled_t  # noqa: SLF001
        if late_drop is not None and settled_t is not None and cut_t is not None:
            # One late drop (grams) this long after the weight became calm.
            delay, grams = late_drop
            if clock.t >= settled_t + delay:
                w += grams
                late_drop = None
        if (
            lift_after_cut is not None
            and cut_t is not None
            and clock.t >= cut_t + lift_after_cut
        ) or (
            lift_after_settle is not None
            and settled_t is not None
            and cut_t is not None
            and clock.t >= settled_t + lift_after_settle
        ):
            w = -300.0
        if scale.frozen is not None:
            # Like the real scale (2026-11-21): no weight frames while the
            # timer is paused, only time passes.
            scale.clock.t = round(scale.clock.t + 0.1, 3)
            scale._tick()  # noqa: SLF001
            await hass.async_block_till_done()
            continue
        if glitch is not None and w >= glitch[0] and glitch[2] > 0:
            scale.run(0.1, glitch[1])
            glitch = (glitch[0], glitch[1], glitch[2] - 1)
            continue
        scale.run(0.1, round(w, 1))
    await hass.async_block_till_done()
    return cut_t


async def test_autostop_cuts_once_learns_and_persists(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    assert _state(hass, TARGET) == "38.0"
    assert _state(hass, "sensor.microbalance_learned_stop_latency") == "1.0"
    assert _state(hass, "sensor.microbalance_stop_drip") == "0.3"
    assert _state(hass, "sensor.microbalance_stop_learning_samples") == "0"
    assert _state(hass, "sensor.microbalance_stop_deviation") == "unknown"
    scale = Scale(coord, clock)
    scale.calls = calls
    cut_t = await _auto_shot(hass, scale)
    assert cut_t is not None
    assert scale.calls_at_cut == 1
    # The timer keeps running past the cut (the paused scale sends no
    # weight, the drips would be missed); it is stopped once settled.
    assert CMD_TIMER_STOP not in scale.written_at_cut
    assert len(calls) == 1 and calls[0].data == {"entity_id": PLUG}
    assert scale.client.written.count(CMD_TIMER_STOP) == 1
    # Prior: 1.0 s * ~2 g/s + 0.3 g -> cut at about 35.7 g.
    last = coord.autostop.last
    assert last is not None and last["accepted"], last
    assert 35.3 <= last["cut_weight"] <= 36.1, last
    assert 1.8 <= last["cut_flow"] <= 2.2, last
    # Real overshoot: 1.2 s * 2 g/s + 0.5 g drip = 2.9 g.
    assert 2.6 <= last["overshoot"] <= 3.1, last
    assert last["deviation"] == round(last["final_yield"] - 38.0, 1)
    assert coord.brews.brew_count == 1
    brew = coord.brews.last
    # The saved yield includes the drips (timer stopped only once settled).
    assert abs(brew["yield"] - last["final_yield"]) <= 0.1, (brew, last)
    assert brew["yield"] > last["cut_weight"] + 2.0, (brew, last)
    # The brew time ends at the cut (pump off), not at the timer stop after
    # the drips (0.5.0b4).
    over = brew["duration"] - (cut_t - scale.shot_start)
    assert -0.2 <= over <= 0.1, brew
    assert coord.autostop.model.count == 1
    assert _state(hass, "sensor.microbalance_stop_learning_samples") == "1"
    # Prior phase: tau learned, c stays at its prior.
    assert float(_state(hass, "sensor.microbalance_learned_stop_latency")) != 1.0
    assert _state(hass, "sensor.microbalance_stop_drip") == "0.3"
    attrs = hass.states.get("sensor.microbalance_stop_deviation").attributes
    assert attrs["accepted"] is True and attrs["cut_weight"] == last["cut_weight"]

    # Learned state survives a reload (flushed on unload); target restored.
    model = coord.autostop.model.to_dict()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    assert hass_storage[key]["data"]["model"] == model
    coord = entry.runtime_data
    assert coord.autostop.model.to_dict() == model
    assert coord.autostop.target == 38.0 and _state(hass, TARGET) == "38.0"
    assert _state(hass, "sensor.microbalance_stop_learning_samples") == "1"

    # Second shot learns again; the switch is turned off once more.
    scale = Scale(coord, clock)
    assert await _auto_shot(hass, scale) is not None
    assert len(calls) == 2
    assert coord.autostop.model.count == 2
    # The learned offset brings the second shot closer to the target.
    assert abs(coord.autostop.last["deviation"]) < abs(last["deviation"])

    # Reset button: back to the prior (works with the scale connected or not).
    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.microbalance_reset_stop_learning"}, blocking=True,
    )
    assert coord.autostop.model.count == 0
    assert _state(hass, "sensor.microbalance_stop_learning_samples") == "0"
    assert _state(hass, "sensor.microbalance_learned_stop_latency") == "1.0"

    # Removing the entry deletes the learned model.
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


@pytest.mark.parametrize(("switch", "target"), [(None, 38.0), (PLUG, 0.0)])
async def test_autostop_inactive_without_switch_or_target(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, switch, target
) -> None:
    calls = await _setup_autostop(hass, entry, switch, target)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    assert await _auto_shot(hass, scale, seconds=30) is None
    assert calls == []
    assert CMD_TIMER_STOP not in scale.client.written
    assert coord.autostop.last is None and coord.autostop.model.count == 0
    for entity_id in STOP_SENSORS:
        assert hass.states.get(entity_id) is not None, entity_id


async def test_autostop_option_applies_live(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Choosing the switch in the options flow arms it without a reload."""
    calls = await _setup_autostop(hass, entry, None, 38.0)
    result = await _options_step(hass, entry, "detection")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, "stop_switch": PLUG}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["stop_switch"] == PLUG
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    assert await _auto_shot(hass, scale) is not None
    assert len(calls) == 1
    # Cleared again: off.
    result = await _options_step(hass, entry, "detection")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], dict(OPTIONS)
    )
    await hass.async_block_till_done()
    assert "stop_switch" not in entry.options
    assert coord.autostop.switch_entity is None and not coord.autostop.enabled


# ---------------------------------------------- 0.5.0b6 bad weight frames


def _model_0752(coord) -> None:
    """The learned model before the brew 2026-11-22 07:52."""
    coord.autostop.model = StopModel.from_dict(
        {"tau": 0.51, "c": 0.071, "p": [[0.05, 0.0], [0.0, 0.3]], "count": 6}
    )


async def test_autostop_single_bad_frame_does_not_cut(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """2026-11-22 07:52: one -197.4 g frame at 12.8 g made 0.5.0b5 read
    229.85 g/s and cut at 15.8 g (target 41.3 g). Now it is dropped: the
    cut comes at the target and the shot is learned normally."""
    calls = await _setup_autostop(hass, entry, PLUG, 41.3)
    coord = entry.runtime_data
    _model_0752(coord)
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale, glitch=(12.8, -197.4, 1)) is not None
    assert len(calls) == 1
    last = coord.autostop.last
    assert last["cut_weight"] > 39.0, last
    assert 1.5 < last["cut_flow"] < 2.5, last
    assert last["accepted"] and last["reason"] is None, last
    assert coord.autostop.model.count == 7


@pytest.mark.parametrize(
    ("glitch", "learned"),
    [
        # Two bad frames in a row, long before the cut.
        ((12.8, -197.4, 2), True),
        # One bad frame upwards (+200 g), 1.5 s before the cut.
        ((36.0, 236.0, 1), True),
        # One bad frame 0.5 s before the cut ("end of the flow window").
        ((38.0, -197.4, 1), True),
    ],
)
async def test_autostop_bad_frames_never_drive_the_cut(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, glitch, learned
) -> None:
    """Bad frames never cut early. A dropped frame is left out of the flow
    estimate, so the shot is still learned (since 0.5.0b7)."""
    calls = await _setup_autostop(hass, entry, PLUG, 41.3)
    coord = entry.runtime_data
    _model_0752(coord)
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale, glitch=glitch) is not None
    assert len(calls) == 1
    last = coord.autostop.last
    assert last["cut_weight"] > 39.0 and 1.5 < last["cut_flow"] < 2.5, last
    assert last["final_yield"] is not None, last
    if learned:
        assert last["accepted"] and coord.autostop.model.count == 7, last
    else:
        assert not last["accepted"] and last["reason"] == "outlier", last
        assert coord.autostop.model.count == 6


async def test_autostop_single_spike_up_does_not_cut(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """One frame +6 g above the line (inside the frame filter) does not cut
    on its own: the cut comes at the normal weight."""
    calls = await _setup_autostop(hass, entry, PLUG, 41.3)
    coord = entry.runtime_data
    _model_0752(coord)
    scale = Scale(coord, clock)
    scale.calls = calls
    # At 36 g the scale sends 40.7 once: 40.7 + 1.1 + lead would cut.
    assert await _auto_shot(hass, scale, glitch=(36.0, 40.7, 1)) is not None
    last = coord.autostop.last
    assert last["cut_weight"] != 40.7 and 39.5 < last["cut_weight"] < 40.5, last
    assert len(calls) == 1


async def test_autostop_single_bad_frame_after_cut_is_no_lift(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """A single negative frame while it still drips is not a lifted cup
    (one frame); the drips are weighed and the shot is learned."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    # Prior model: cut at ~35.6 g, the pump stops 1.2 s later (~38 g).
    assert await _auto_shot(hass, scale, glitch=(37.0, -197.4, 1)) is not None
    last = coord.autostop.last
    assert last["cut_weight"] < 37.0, last
    assert last["accepted"] and last["reason"] is None, last
    assert last["final_yield"] > 38.0, last


async def test_autostop_implausible_flow_backstop(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """A flow above FLOW_MAX never drives the cut (only weight + c does) and
    the shot is not learned."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    # 10 g/s (1 g per 0.1 s frame: passes the frame filter).
    assert await _auto_shot(hass, scale, flow=10.0, seconds=20) is not None
    last = coord.autostop.last
    assert last["cut_flow"] > 8.0, last
    assert last["cut_weight"] >= 38.0 - 0.3 - 0.1, last  # weight + c, no flow
    assert last["predicted_overshoot"] == 0.3, last
    assert not last["accepted"] and last["reason"] == "outlier", last
    assert coord.autostop.model.count == 0


async def test_autostop_cup_lifted_is_not_learned(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    # Lifted 1.5 s after the cut, while it still flows: no settled yield
    # near the cut, the settled weight is the empty scale.
    assert await _auto_shot(hass, scale, lift_after_cut=1.5) is not None
    assert len(calls) == 1
    last = coord.autostop.last
    assert last is not None and not last["accepted"], last
    assert last["reason"] == "cup_lifted", last
    # The brew is still saved, cut at the lift.
    assert coord.brews.brew_count == 1
    assert coord.brews.last["yield"] > 30, coord.brews.last
    assert coord.autostop.model.count == 0


async def test_autostop_untared_cup_never_cuts(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Weight far above the target from the start (cup not tared): no cut."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    assert await _auto_shot(hass, scale, start_weight=250.0, seconds=30) is None
    assert calls == []


# ------------------------------------------------------------- services

# Real brew 2026-11-21 (plug off at +27.83 s), profile around the cut.
REAL_BREW = {
    "id": "01REAL",
    "start": "2026-11-21T06:43:47.825000+00:00",
    "duration": 28.0,
    "dose": 18.0,
    "dose_source": "auto",
    "yield": 38.6,
    "ratio": 2.14,
    "counted_dose": 18.0,
    "profile": [
        [25.0, 28.0, 3.0],
        [26.83, 33.7, 3.0],
        [27.14, 34.8, 3.1],
        [27.44, 34.4, 3.0],
        [27.75, 36.8, 3.2],
        [28.04, 37.4, 3.0],
        [31.0, 38.6, 0.0],
    ],
}
# Brew 2026-11-22 07:52 around its cut (17.30 s), then (for the test only)
# a part rising 3 g per 0.25 s (12 g/s).
GLITCH_BREW = {
    "id": "01GLITCH",
    "start": "2026-11-22T06:51:50.196000+00:00",
    "duration": 17.3,
    "dose": 15.9,
    "dose_source": "auto",
    "yield": 18.3,
    "ratio": 1.15,
    "counted_dose": 15.9,
    "profile": [
        [t, w, 0.0]
        for t, w in (
            (14.33, 9.2), (14.57, 9.7), (14.85, 9.7), (15.25, 10.4),
            (15.46, 10.2), (15.76, 12.2), (16.07, 12.8), (16.37, -197.4),
            (16.63, 14.9), (17.0, 15.0), (17.3, 15.8), (17.61, 15.7),
            (17.81, 17.3), (18.04, 18.0), (19.0, 18.3),
            *((25.0 + i * 0.25, 18.3 + 3.0 * i) for i in range(21)),
        )
    ],
}
# The points of REAL_BREW within 2 s before its cut (27.75 s).
_PTS = [(26.83, 33.7), (27.14, 34.8), (27.44, 34.4), (27.75, 36.8)]
OTHER_BREW = {
    "id": "01OTHER",
    "start": "2026-11-21T07:10:00+00:00",
    "duration": 27.0,
    "dose": 17.5,
    "dose_source": "manual",
    "yield": 36.0,
    "ratio": 2.06,
    "counted_dose": 18.0,
    "profile": [[0.0, 0.0, 0.0], [27.0, 36.0, 0.0]],
}


def _store_brews(hass_storage: dict[str, Any], entry: MockConfigEntry) -> str:
    key = STORAGE_KEY.format(entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "brews": [dict(REAL_BREW), dict(OTHER_BREW)],
            "brew_count": 5,
            "coffee_total": 90.0,
        },
    }
    return key


async def test_delete_brew_service(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    key = _store_brews(hass_storage, entry)
    await _setup(hass, entry)
    coord = entry.runtime_data
    events: list = []
    hass.bus.async_listen(EVENT_BREW_COMPLETED, events.append)
    signals: list = []
    async_dispatcher_connect(
        hass, SIGNAL_BREWS_UPDATED.format(entry.entry_id), lambda: signals.append(1)
    )
    assert _state(hass, "sensor.microbalance_last_yield") == "36.0"

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "delete_brew", {"brew_id": "01NOPE"}, blocking=True
        )
    # Deleting the newest: the totals stay (total_increasing, since
    # 0.5.1); sensors follow the new newest brew. Not in the learning
    # journal: the model is untouched.
    response = await hass.services.async_call(
        DOMAIN, "delete_brew",
        {"brew_id": "01OTHER", "entry_id": entry.entry_id}, blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()
    assert response == {
        "brew_id": "01OTHER",
        "removed_from_learning": False,
        "model": {"tau": 1.0, "c": 0.3, "count": 0},
    }
    assert [b["id"] for b in coord.brews.brews] == ["01REAL"]
    assert coord.brews.brew_count == 5 and coord.brews.coffee_total == 90.0
    assert _state(hass, "sensor.microbalance_brew_count") == "5"
    assert _state(hass, "sensor.microbalance_coffee_ground_total") == "90.0"
    assert _state(hass, "sensor.microbalance_last_yield") == "38.6"
    assert signals and events == []
    # Persisted (flushed on unload).
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    data = hass_storage[key]["data"]
    assert [b["id"] for b in data["brews"]] == ["01REAL"]
    assert data["brew_count"] == 5 and data["coffee_total"] == 90.0


async def test_learn_stop_from_brew_service(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    key = _store_brews(hass_storage, entry)
    hass_storage[key]["data"]["brews"].append(dict(GLITCH_BREW))
    await _setup(hass, entry)
    coord = entry.runtime_data
    response = await hass.services.async_call(
        DOMAIN, "learn_stop_from_brew",
        {"brew_id": "01REAL", "cut_offset": 27.83, "target": 38.0},
        blocking=True, return_response=True,
    )
    # Same Theil-Sen flow as the live cut (median of the pairwise slopes
    # of the points within 2 s before the cut).
    flow = statistics.median(
        (w2 - w1) / (t2 - t1)
        for i, (t1, w1) in enumerate(_PTS)
        for t2, w2 in _PTS[i + 1 :]
    )
    assert response["cut_point_t"] == 27.75
    assert response["weight_at_cut"] == 36.8
    assert abs(response["flow_at_cut"] - round(flow, 3)) < 1e-9
    assert response["clean_cut"] is True
    assert response["final_yield"] == 38.6 and response["overshoot"] == 1.8
    assert response["deviation"] == 0.6 and response["accepted"] is True
    assert response["predicted_overshoot_before"] == round(0.3 + 1.0 * flow, 2)
    assert response["model"]["count"] == 1
    # Prior phase: only tau moved, towards (1.8 - 0.3) / flow.
    assert coord.autostop.model.c == 0.3
    assert coord.autostop.model.tau < 1.0
    assert _state(hass, "sensor.microbalance_stop_learning_samples") == "1"

    # Outlier (cut far before the end: overshoot > 5 g): not learned.
    response = await hass.services.async_call(
        DOMAIN, "learn_stop_from_brew",
        {"brew_id": "01REAL", "cut_offset": 25.5},
        blocking=True, return_response=True,
    )
    assert response["accepted"] is False and response["reason"] == "outlier"
    assert response["overshoot"] == 10.6 and response["deviation"] is None
    assert coord.autostop.model.count == 1

    # The brew 2026-11-22 07:52 (one -197.4 g frame 0.93 s before the cut):
    # the frame is left out, the flow is plausible: learned (since 0.5.0b7).
    response = await hass.services.async_call(
        DOMAIN, "learn_stop_from_brew",
        {"brew_id": "01GLITCH", "cut_offset": 17.36},
        blocking=True, return_response=True,
    )
    assert response["weight_at_cut"] == 15.8 and response["clean_cut"] is True
    assert 1.0 < response["flow_at_cut"] < 3.5
    assert response["accepted"] is True and response["reason"] is None
    # Too fast to be a shot (12 g/s): the flow is implausible.
    response = await hass.services.async_call(
        DOMAIN, "learn_stop_from_brew",
        {"brew_id": "01GLITCH", "cut_offset": 30.0},
        blocking=True, return_response=True,
    )
    assert response["flow_at_cut"] > 8.0 and response["clean_cut"] is True
    assert response["accepted"] is False and response["reason"] == "outlier"
    assert coord.autostop.model.count == 2

    for data in (
        {"brew_id": "01NOPE", "cut_offset": 27.0},
        {"brew_id": "01REAL", "cut_offset": 1.0},  # before the first point
    ):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "learn_stop_from_brew", data,
                blocking=True, return_response=True,
            )

    # Persisted.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    assert hass_storage[key]["data"]["model"]["count"] == 2


RATIO = "number.microbalance_target_ratio"


async def test_autostop_target_ratio_uses_dose_else_fixed_yield(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Ratio x detected dose is the target; without a dose the fixed target
    yield is the fallback."""
    calls = await _setup_autostop(hass, entry, PLUG, 30.0)
    coord = entry.runtime_data
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": 2.0}, blocking=True
    )
    assert _state(hass, RATIO) == "2.0"
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale, dose=18.0) is not None
    last = coord.autostop.last
    assert last is not None, last
    assert last["target"] == 36.0 and last["target_source"] == "ratio", last
    assert last["dose"] == 18.0, last
    assert 33.0 <= last["cut_weight"] <= 36.0, last

    # Second shot: no dose weighed -> fixed target yield (30 g).
    clock.t += 120
    scale2 = Scale(coord, clock)
    scale2.calls = calls
    assert await _auto_shot(hass, scale2, dose=None) is not None
    last = coord.autostop.last
    assert last["target"] == 30.0 and last["target_source"] == "yield", last
    assert last["dose"] is None, last
    assert len(calls) == 2


async def test_autostop_ratio_without_dose_and_no_yield_does_not_cut(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 0.0)
    coord = entry.runtime_data
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": 2.5}, blocking=True
    )
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale, dose=None, seconds=30) is None
    assert calls == []
