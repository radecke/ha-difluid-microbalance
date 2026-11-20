"""Connection handling and data push for the DiFluid Microbalance."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bluetooth_data_tools import monotonic_time_coarse
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    ADVERTISEMENT_MAX_AGE,
    CHAR_UUID,
    CONNECT_RETRIES,
    CONNECT_RETRY_DELAY,
    DOMAIN,
    STATUS_POLL_INTERVAL,
)
from .protocol import (
    CMD_AUTO_SEND_ON,
    CMD_GET_FIRMWARE,
    CMD_GET_MODEL,
    CMD_GET_SERIAL,
    CMD_GET_STATUS,
    CMD_INFO_FIRMWARE,
    CMD_INFO_MODEL,
    CMD_SETTINGS_AUTO_DETECT_TIMING,
    CMD_SETTINGS_AUTO_STOP_TIMING,
    CMD_SETTINGS_WEIGHT_UNIT,
    FUNC_INFO,
    Frame,
    FrameBuffer,
    SensorData,
    SerialAssembler,
    StatusData,
    build_get_setting,
    build_set_setting,
    is_key_frame,
    parse_info_string,
    parse_key_event,
    parse_sensor,
    parse_setting,
    parse_status,
)

_LOGGER = logging.getLogger(__name__)

INFO_TIMEOUT = 2.0  # seconds per device-info request (best effort)

# Settings read on every connect (Func 1 get requests).
READ_SETTINGS = (
    CMD_SETTINGS_AUTO_DETECT_TIMING,
    CMD_SETTINGS_AUTO_STOP_TIMING,
    CMD_SETTINGS_WEIGHT_UNIT,
)
# Func 1 Cmd 3 (weight unit switch lock) is deliberately not exposed: on
# firmware V320 and V326 neither HA nor the DiFluid app could make the setting
# stick (tested 2026-11-20).

INFO_SERIAL = "serial"
INFO_MODEL = "model"
INFO_FIRMWARE = "firmware"

type KeyEventListener = Callable[[str], None]

type DifluidConfigEntry = ConfigEntry[DifluidCoordinator]


@dataclass(frozen=True)
class DifluidData:
    """Latest known values from the scale."""

    sensor: SensorData | None = None
    status: StatusData | None = None
    # Func 1 setting cmd -> last reported value (from responses/echoes; the
    # weight unit is also taken from sensor frames).
    settings: dict[int, int] = field(default_factory=dict)


class DifluidCoordinator(DataUpdateCoordinator[DifluidData]):
    """Owns the GATT connection and pushes decoded frames to entities.

    The scale is off most of the time: we connect when an advertisement is
    seen and simply wait for the next one after a disconnect.
    """

    config_entry: DifluidConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: DifluidConfigEntry, address: str
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {address}",
            update_interval=None,
        )
        self.address = address
        self.data = DifluidData()
        self._client: BleakClientWithServiceCache | None = None
        self._connect_lock = asyncio.Lock()
        self._buffer = FrameBuffer()
        self._cancel_poll: CALLBACK_TYPE | None = None
        self._unloading = False
        self._status_event = asyncio.Event()
        self._key_listeners: list[KeyEventListener] = []
        self._serial = SerialAssembler()
        self._info_waiters: dict[str, asyncio.Future[str]] = {}
        self.device_info: dict[str, str] = {}
        self._connecting = False

    @property
    def connected(self) -> bool:
        """Return True while a GATT connection is up."""
        return self._client is not None and self._client.is_connected

    async def _async_update_data(self) -> DifluidData:
        """Push-only coordinator: nothing to fetch."""
        return self.data

    # ------------------------------------------------------------------ setup

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Listen for advertisements; return a cancel callback."""
        cancel = bluetooth.async_register_callback(
            self.hass,
            self._async_on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        # Scale may already be on and advertising when HA starts. If HA only
        # remembers an old advertisement, clear it so the next (unchanged)
        # one after power-on reaches the callback.
        if self._advertisement_fresh():
            self._schedule_connect()
        else:
            self._clear_advertisement_history()
        return cancel

    async def async_shutdown(self) -> None:
        """Disconnect cleanly on unload."""
        self._unloading = True
        self._stop_poll()
        client, self._client = self._client, None
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except BleakError as err:
                _LOGGER.debug("%s: error during disconnect: %s", self.address, err)
        await super().async_shutdown()

    # -------------------------------------------------------------- connection

    @callback
    def _async_on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Connect when the scale advertises and we are not connected."""
        _LOGGER.debug(
            "%s: advertisement (rssi %s, connected=%s, connecting=%s)",
            self.address,
            service_info.rssi,
            self.connected,
            self._connecting,
        )
        if self.connected or self._connecting:
            return
        self._schedule_connect()

    @callback
    def _clear_advertisement_history(self) -> None:
        """Let the next advertisement through HA's change detection.

        The scale's advertisement never changes, so HA's Bluetooth manager
        drops it as a duplicate and no callback fires after a quick power
        cycle. Clearing the history makes the next one count as new."""
        bluetooth.async_clear_advertisement_history(self.hass, self.address)

    @callback
    def _advertisement_fresh(self) -> bool:
        """True if the scale advertised within ADVERTISEMENT_MAX_AGE."""
        info = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=True
        )
        return (
            info is not None
            and monotonic_time_coarse() - info.time <= ADVERTISEMENT_MAX_AGE
        )

    @callback
    def _schedule_connect(self) -> None:
        if self._unloading or self._connecting:
            _LOGGER.debug(
                "%s: connect skipped (unloading=%s, connecting=%s)",
                self.address,
                self._unloading,
                self._connecting,
            )
            return
        _LOGGER.debug("%s: connecting", self.address)
        self._connecting = True
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_connect_with_retry(),
            f"{DOMAIN}_connect_{self.address}",
        )

    async def _async_connect_with_retry(self) -> None:
        """Connect; retry a few times, since a failed attempt (e.g. GATT
        error 133 during setup) is not reliably followed by another
        advertisement callback before the scale powers itself off."""
        try:
            for attempt in range(1 + CONNECT_RETRIES):
                if await self._async_connect():
                    return
                if self._unloading or not self._advertisement_fresh():
                    break
                if attempt < CONNECT_RETRIES:
                    _LOGGER.debug(
                        "%s: retrying connect in %ss (%s/%s)",
                        self.address,
                        CONNECT_RETRY_DELAY,
                        attempt + 1,
                        CONNECT_RETRIES,
                    )
                    await asyncio.sleep(CONNECT_RETRY_DELAY)
            # Not connected: let the next advertisement trigger a new attempt.
            _LOGGER.debug("%s: not connected, waiting for advertisement", self.address)
            if not self._unloading:
                self._clear_advertisement_history()
        finally:
            self._connecting = False

    async def _async_connect(self) -> bool:
        """Establish the connection and enable auto-send.

        Returns True when connected (or nothing left to do)."""
        async with self._connect_lock:
            if self.connected:
                return True
            if self._unloading:
                return True
            ble_device: BLEDevice | None = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                _LOGGER.debug("%s: no connectable BLE device", self.address)
                return False
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    ble_device.name or self.address,
                    disconnected_callback=self._on_disconnected,
                )
            except (BleakError, TimeoutError) as err:
                _LOGGER.debug("%s: connect failed: %s", self.address, err)
                return False

            self._client = client
            self._buffer.clear()
            try:
                await client.start_notify(CHAR_UUID, self._on_notification)
                await client.write_gatt_char(CHAR_UUID, CMD_GET_STATUS, response=True)
                await client.write_gatt_char(CHAR_UUID, CMD_AUTO_SEND_ON, response=True)
            except (BleakError, TimeoutError) as err:
                _LOGGER.debug("%s: setup after connect failed: %s", self.address, err)
                self._client = None
                try:
                    await client.disconnect()
                except BleakError:
                    pass
                self.async_update_listeners()
                return False

            _LOGGER.debug("%s: connected", self.address)
            self._start_poll()
            self.async_update_listeners()
            self.config_entry.async_create_background_task(
                self.hass,
                self._async_after_connect(),
                f"{DOMAIN}_init_{self.address}",
            )
            return True

    async def _async_after_connect(self) -> None:
        """Read settings and device info; best effort, never fatal."""
        for cmd in READ_SETTINGS:
            try:
                await self._async_write(build_get_setting(cmd))
            except HomeAssistantError as err:
                _LOGGER.debug(
                    "%s: reading setting %s failed: %s", self.address, cmd, err
                )
                return
        if len(self.device_info) < 3:
            await self._async_fetch_device_info()

    async def _async_fetch_device_info(self) -> None:
        """Request serial, model and firmware; update the device registry."""
        for key, frame in (
            (INFO_SERIAL, CMD_GET_SERIAL),
            (INFO_MODEL, CMD_GET_MODEL),
            (INFO_FIRMWARE, CMD_GET_FIRMWARE),
        ):
            if key in self.device_info:
                continue
            future: asyncio.Future[str] = self.hass.loop.create_future()
            self._info_waiters[key] = future
            if key == INFO_SERIAL:
                self._serial.reset()
            try:
                await self._async_write(frame)
                self.device_info[key] = await asyncio.wait_for(future, INFO_TIMEOUT)
            except HomeAssistantError as err:
                _LOGGER.debug("%s: %s request failed: %s", self.address, key, err)
                return
            except TimeoutError:
                _LOGGER.debug("%s: no %s response", self.address, key)
            finally:
                self._info_waiters.pop(key, None)
        _LOGGER.debug("%s: device info %s", self.address, self.device_info)
        self._async_update_device_registry()

    @callback
    def _async_update_device_registry(self) -> None:
        if not self.device_info:
            return
        registry = dr.async_get(self.hass)
        devices = dr.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        )
        if not devices:
            return
        device = devices[0]
        changes: dict[str, str] = {}
        if (fw := self.device_info.get(INFO_FIRMWARE)) and device.sw_version != fw:
            changes["sw_version"] = fw
        if (sn := self.device_info.get(INFO_SERIAL)) and device.serial_number != sn:
            changes["serial_number"] = sn
        if (model := self.device_info.get(INFO_MODEL)) and device.model_id != model:
            changes["model_id"] = model
        if changes:
            registry.async_update_device(device.id, **changes)

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Bleak disconnect callback (may not run in the event loop)."""
        self.hass.loop.call_soon_threadsafe(self._async_handle_disconnect, client)

    @callback
    def _async_handle_disconnect(self, client: BleakClientWithServiceCache) -> None:
        if self._client is not client and self._client is not None:
            return  # stale callback from an older connection
        _LOGGER.debug("%s: disconnected", self.address)
        self._client = None
        self._stop_poll()
        self._buffer.clear()
        self._serial.reset()
        self.async_update_listeners()
        # Reconnect happens on the next advertisement; clear HA's history so
        # an unchanged advertisement (quick power cycle) is not dropped.
        if not self._unloading:
            self._clear_advertisement_history()

    # ------------------------------------------------------------- status poll

    @callback
    def _start_poll(self) -> None:
        self._stop_poll()
        self._cancel_poll = async_track_time_interval(
            self.hass, self._async_poll_status, STATUS_POLL_INTERVAL
        )

    @callback
    def _stop_poll(self) -> None:
        if self._cancel_poll is not None:
            self._cancel_poll()
            self._cancel_poll = None

    async def _async_poll_status(self, _now: datetime) -> None:
        if not self.connected:
            return
        try:
            await self._async_write(CMD_GET_STATUS)
        except HomeAssistantError as err:
            _LOGGER.debug("%s: status poll failed: %s", self.address, err)

    # ------------------------------------------------------------- data in/out

    def _on_notification(self, _char: object, payload: bytearray) -> None:
        """Handle a notification; one packet may hold several frames."""
        changed = False
        sensor = self.data.sensor
        status = self.data.status
        settings = self.data.settings
        events: list[str] = []
        for frame in self._buffer.feed(bytes(payload)):
            if (new_sensor := parse_sensor(frame)) is not None:
                if sensor is None or (
                    new_sensor.weight_raw,
                    new_sensor.flow_raw,
                    new_sensor.timer_raw,
                    new_sensor.unit,
                ) != (sensor.weight_raw, sensor.flow_raw, sensor.timer_raw, sensor.unit):
                    changed = True
                sensor = new_sensor
                if settings.get(CMD_SETTINGS_WEIGHT_UNIT) != new_sensor.unit:
                    settings = {**settings, CMD_SETTINGS_WEIGHT_UNIT: new_sensor.unit}
                    changed = True
            elif (new_status := parse_status(frame)) is not None:
                if status is None or new_status.raw != status.raw:
                    changed = True
                status = new_status
                self._status_event.set()
            elif (setting := parse_setting(frame)) is not None:
                cmd, value = setting
                if settings.get(cmd) != value:
                    settings = {**settings, cmd: value}
                    changed = True
            elif is_key_frame(frame):
                # App command echoes yield None and are ignored.
                if (event := parse_key_event(frame)) is not None:
                    events.append(event)
            elif frame.func == FUNC_INFO:
                self._handle_info_frame(frame)
            else:
                _LOGGER.debug(
                    "%s: unknown frame func=%s cmd=%s data=%s",
                    self.address,
                    frame.func,
                    frame.cmd,
                    frame.data.hex(),
                )
        new_data = DifluidData(sensor=sensor, status=status, settings=settings)
        if changed:
            self.async_set_updated_data(new_data)
        else:
            # Keep the tick (not exposed) current without writing state.
            self.data = new_data
        for event in events:
            _LOGGER.debug("%s: key event %s", self.address, event)
            for listener in list(self._key_listeners):
                listener(event)

    def _handle_info_frame(self, frame: Frame) -> None:
        """Resolve pending device-info requests from Func 0 responses."""
        key: str | None = None
        value: str | None = None
        if (serial := self._serial.feed(frame)) is not None:
            key, value = INFO_SERIAL, serial
        elif (model := parse_info_string(frame, CMD_INFO_MODEL)) is not None:
            key, value = INFO_MODEL, model
        elif (fw := parse_info_string(frame, CMD_INFO_FIRMWARE)) is not None:
            key, value = INFO_FIRMWARE, fw
        if key is None or value is None:
            return
        future = self._info_waiters.get(key)
        if future is not None and not future.done():
            future.set_result(value)

    @callback
    def async_add_key_listener(self, listener: KeyEventListener) -> CALLBACK_TYPE:
        """Register a callback for physical key events; return unsubscribe."""
        self._key_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._key_listeners:
                self._key_listeners.remove(listener)

        return _remove

    async def async_set_setting(self, cmd: int, value: int) -> None:
        """Write a Func 1 setting; the echo updates ``data.settings``."""
        await self.async_send(build_set_setting(cmd, value))

    async def async_request_status(self, timeout: float = 2.0) -> int | None:
        """Ask for a fresh status frame and return the device state."""
        self._status_event.clear()
        await self.async_send(CMD_GET_STATUS)
        try:
            await asyncio.wait_for(self._status_event.wait(), timeout)
        except TimeoutError:
            return None
        status = self.data.status
        return status.state if status else None

    async def async_send(self, frame: bytes) -> None:
        """Write a frame."""
        await self._async_write(frame)

    async def _async_write(self, frame: bytes) -> None:
        """Write a frame to the cleartext characteristic."""
        client = self._client
        if client is None or not client.is_connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            )
        try:
            await client.write_gatt_char(CHAR_UUID, frame, response=True)
        except (BleakError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"error": str(err)},
            ) from err
