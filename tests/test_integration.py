"""Home Assistant level tests: setup, storage, websocket, live mode, frontend."""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers.dispatcher import async_dispatcher_connect  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.difluid_scale import coordinator as coord_mod  # noqa: E402
from custom_components.difluid_scale.const import (  # noqa: E402
    CARD_URL,
    DOMAIN,
    EVENT_BREW_COMPLETED,
    READY_TIMEOUT_SECONDS,
    SIGNAL_LIVE,
    STORAGE_KEY,
)
from custom_components.difluid_scale.frontend import (  # noqa: E402
    _async_ensure_resource,
)
from custom_components.difluid_scale.protocol import (  # noqa: E402
    CMD_GET_STATUS,
    CMD_TIMER_STOP,
    CMD_DEVICE_SENSOR,
    CMD_DEVICE_STATUS,
    FUNC_DEVICE,
    DeviceState,
    build_frame,
)

ADDR = "AA:BB:CC:DD:EE:FF"
VERSION = "0.3.1"
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


class Clock:
    """Replaces ``time`` in the coordinator."""

    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def time(self) -> float:
        return self.t


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


@pytest.fixture
def clock() -> Generator[Clock]:
    c = Clock()
    with patch.object(coord_mod, "time", c):
        yield c


@pytest.fixture
async def lovelace(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    hass.config.components.add("bluetooth_adapters")
    hass.config.components.add("bluetooth")
    e = MockConfigEntry(
        domain=DOMAIN, data={"address": ADDR}, title="Microbalance", unique_id=ADDR
    )
    e.add_to_hass(hass)
    return e


@pytest.fixture
def no_ble() -> Generator[None]:
    with (
        patch.object(
            coord_mod.DifluidCoordinator, "async_start", lambda self: (lambda: None)
        ),
        patch.object(
            coord_mod.DifluidCoordinator, "_clear_advertisement_history", lambda self: None
        ),
    ):
        yield


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
        "type": "live_ready", "ready": False, "dose": None, "entry_id": entry.entry_id
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
    assert msg == {"type": "live_ready", "ready": True, "dose": None, "entry_id": entry.entry_id}

    # The dose of the next brew comes with it as soon as a plateau is known.
    scale = Scale(coord, clock)
    scale.weigh_dose(18.0)
    msg = (await ws.receive_json())["event"]
    assert msg == {"type": "live_ready", "ready": True, "dose": 18.0, "entry_id": entry.entry_id}
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


async def test_options_flow(hass: HomeAssistant, entry, no_ble, lovelace, clock) -> None:
    await _setup(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form" and result["step_id"] == "init"
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
