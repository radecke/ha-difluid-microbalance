"""Connection handling and data push for the DiFluid Microbalance."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging
import time

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bluetooth_data_tools import monotonic_time_coarse
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .autostop import AutoStop
from .brew import BrewResult, BrewTracker
from .brews import BrewHistory, brew_config_from_options, brew_summary
from .const import (
    ADVERTISEMENT_MAX_AGE,
    BREW_TICK_INTERVAL,
    CHAR_UUID,
    CONNECT_RETRIES,
    CONNECT_RETRY_DELAY,
    DOMAIN,
    READY_TIMEOUT_SECONDS,
    SETUP_COMMAND_GAP,
    SETUP_DELAY,
    SIGNAL_LIVE,
    STATUS_POLL_INTERVAL,
)
from .live import LIVE_IDLE, LiveBrew, end_message, ready_message
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
    CMD_TIMER_STOP,
    FUNC_INFO,
    DeviceState,
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
# A failing frame consumer logs its stack trace at most this often.
ERROR_LOG_INTERVAL = 60.0  # seconds

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

# Device states that tell the brew tracker the timer runs / does not run.
# Others (e.g. taring) say nothing about the timer and are ignored.
_TIMER_RUNNING_STATES = {DeviceState.TIMING}
_TIMER_STOPPED_STATES = {
    DeviceState.POWER_DOWN,
    DeviceState.LOW_BATTERY_SHUTDOWN,
    DeviceState.IDLE,
    DeviceState.TIMER_PAUSE,
    DeviceState.AUTO_STOP_TIMING_TRIGGER,
    DeviceState.TIMER_READY,
}
# Stopped states in which the timer is back at 0 (a paused brew can no
# longer be continued).
_TIMER_RESET_STATES = {DeviceState.IDLE, DeviceState.TIMER_READY}

type KeyEventListener = Callable[[str], None]
type ReadyListener = Callable[[], None]

type DifluidConfigEntry = ConfigEntry[DifluidCoordinator]


# Diagnostics (0.5.0): log raw bytes when the raw weight jumps this much
# (x10 g, i.e. 50 g); hunting a recurring single -197.5 g frame.
WEIGHT_JUMP_LOG_RAW = 500


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
        # Diagnostics (0.5.0): last raw notifications, logged on a weight jump.
        self._recent_payloads: deque[bytes] = deque(maxlen=6)
        self._cancel_poll: CALLBACK_TYPE | None = None
        self._unloading = False
        self._status_event = asyncio.Event()
        self._key_listeners: list[KeyEventListener] = []
        self._serial = SerialAssembler()
        self._info_waiters: dict[str, asyncio.Future[str]] = {}
        self.device_info: dict[str, str] = {}
        self._connecting = False
        self.brew_tracker = BrewTracker(brew_config_from_options(dict(entry.options)))
        self.brews = BrewHistory(hass, entry.entry_id, self._device_id)
        self._cancel_brew_tick: CALLBACK_TYPE | None = None
        self._live: LiveBrew | None = None
        # "Prepare brew" switch: integration state, independent of the scale.
        self.brew_ready = False
        self._ready_dose: float | None = None
        self._ready_target: dict | None = None
        self._cancel_ready_timeout: CALLBACK_TYPE | None = None
        self._ready_listeners: list[ReadyListener] = []
        # Adaptive stop at the target yield.
        self.autostop = AutoStop(hass, self, entry.entry_id)
        # The ready view shows the target: follow target and option changes.
        self.autostop.async_add_listener(self._refresh_ready_dose)
        # Data decoded from the notification being handled.
        self._sensor: SensorData | None = None
        self._status: StatusData | None = None
        self._settings: dict[int, int] = {}
        self._changed = False
        # Throttled error logging per handler (_guarded).
        self._error_logged: dict[str, float] = {}
        self._error_repeats: dict[str, int] = {}

    @property
    def unloading(self) -> bool:
        """True once this instance is shutting down (unload, HA stop)."""
        return self._unloading

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

    @callback
    def async_listen_ha_stop(self) -> CALLBACK_TYPE:
        """Hand a pending brew over on HA stop too (entries are not unloaded).

        Returns a remover for ``async_on_unload``; safe after the event fired.
        """
        cancel: CALLBACK_TYPE | None = None

        async def _on_stop(_event: Event) -> None:
            nonlocal cancel
            cancel = None
            await self.async_shutdown()

        cancel = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)

        @callback
        def _remove() -> None:
            nonlocal cancel
            if cancel is not None:
                cancel()
                cancel = None

        return _remove

    @callback
    def async_restore_brew(self) -> None:
        """Take over a brew in progress from the previous instance (reload)."""
        if (active := self.brews.take_active()) is None:
            return
        if self.brew_tracker.restore_active(active):
            _LOGGER.debug("%s: brew in progress restored", self.address)
            self._handle_brew_result(None)

    async def async_shutdown(self) -> None:
        """Disconnect cleanly on unload or HA stop.

        A brew in progress is not finalized: it is handed to the next
        instance (reload) through the store, which continues it like after
        a reconnect. Nothing of this instance acts after this point.
        """
        if self._unloading:
            return  # also registered via async_on_unload
        self._unloading = True
        self._stop_poll()
        self._stop_brew_tick()
        self._stop_ready_timeout()
        self.autostop.async_stop_watching()
        self.brews.set_active(self.brew_tracker.export_active(time.time()))
        if self._live is not None:
            self._live = None
            self._send_live(end_message(reason="unloaded"))
        client, self._client = self._client, None
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except BleakError as err:
                _LOGGER.debug("%s: error during disconnect: %s", self.address, err)
        await self.brews.async_flush()
        await self.autostop.async_flush()
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
            if self._unloading:
                # Unloaded while connecting: nothing of this instance may
                # keep the link or start polling.
                await self._async_drop_client(client)
                return True

            self._client = client
            self._buffer.clear()
            try:
                for pause, step in (
                    (
                        SETUP_DELAY,
                        lambda: client.start_notify(
                            CHAR_UUID, self._on_notification
                        ),
                    ),
                    (
                        SETUP_COMMAND_GAP,
                        lambda: client.write_gatt_char(
                            CHAR_UUID, CMD_GET_STATUS, response=True
                        ),
                    ),
                    (
                        SETUP_COMMAND_GAP,
                        lambda: client.write_gatt_char(
                            CHAR_UUID, CMD_AUTO_SEND_ON, response=True
                        ),
                    ),
                ):
                    await asyncio.sleep(pause)
                    if self._unloading:
                        if self._client is client:
                            self._client = None
                        await self._async_drop_client(client)
                        return True
                    await step()
                    if self._unloading:
                        if self._client is client:
                            self._client = None
                        await self._async_drop_client(client)
                        return True
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

    async def _async_drop_client(self, client: BleakClientWithServiceCache) -> None:
        """Disconnect a client this instance no longer uses (unloading)."""
        _LOGGER.debug("%s: unloaded while connecting, disconnecting", self.address)
        try:
            await client.disconnect()
        except BleakError as err:
            _LOGGER.debug("%s: error during disconnect: %s", self.address, err)

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
        if self._unloading:
            # Unloaded (bleak reports our own disconnect late): the brew was
            # handed over, nothing may park it or restart the tick here.
            if self._client is client:
                self._client = None
            return
        if self._client is not client and self._client is not None:
            return  # stale callback from an older connection
        _LOGGER.debug("%s: disconnected", self.address)
        self._client = None
        self._stop_poll()
        self._buffer.clear()
        self._serial.reset()
        # A running brew is parked, not closed: it resumes if the scale comes
        # back with the same running timer (see BrewTracker.disconnect).
        self._handle_brew_result(self.brew_tracker.disconnect(time.time()))
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
        """Handle a notification; one packet may hold several frames.

        Every frame and every consumer (brew tracker, auto-stop, entities,
        key listeners) is guarded on its own: an error in one is logged and
        does not drop the rest of the packet.
        """
        if self._unloading:
            return
        self._sensor = self.data.sensor
        self._status = self.data.status
        self._settings = self.data.settings
        self._changed = False
        events: list[str] = []
        self._recent_payloads.append(bytes(payload))
        try:
            frames = self._buffer.feed(bytes(payload))
        except Exception:
            _LOGGER.exception("%s: could not split notification", self.address)
            self._buffer.clear()
            return
        for frame in frames:
            self._guarded(
                f"frame func={frame.func} cmd={frame.cmd}",
                self._handle_frame,
                frame,
                events,
            )
        new_data = DifluidData(
            sensor=self._sensor, status=self._status, settings=self._settings
        )
        if self._changed:
            self._guarded("updating entities", self.async_set_updated_data, new_data)
        else:
            # Keep the tick (not exposed) current without writing state.
            self.data = new_data
        for event in events:
            _LOGGER.debug("%s: key event %s", self.address, event)
            for listener in list(self._key_listeners):
                self._guarded("key listener", listener, event)

    def _guarded(self, what: str, func: Callable[..., object], *args: object) -> None:
        """Call ``func``; log (not raise) an error.

        Throttled per handler: the full stack trace at most once per
        ERROR_LOG_INTERVAL, repeats in between only as a debug line with a
        count (a failing handler would otherwise log 10 times a second).
        """
        try:
            func(*args)
        except Exception:
            now = monotonic_time_coarse()
            last = self._error_logged.get(what)
            if last is None or now - last >= ERROR_LOG_INTERVAL:
                repeats = self._error_repeats.pop(what, 0)
                self._error_logged[what] = now
                _LOGGER.exception(
                    "%s: error in %s (%s more since the last report)",
                    self.address,
                    what,
                    repeats,
                )
            else:
                self._error_repeats[what] = self._error_repeats.get(what, 0) + 1
                _LOGGER.debug(
                    "%s: error in %s again (%s since the last report)",
                    self.address,
                    what,
                    self._error_repeats[what],
                    exc_info=True,
                )

    def _handle_frame(self, frame: Frame, events: list[str]) -> None:
        """Decode one frame into the pending data of this notification."""
        if (new_sensor := parse_sensor(frame)) is not None:
            sensor = self._sensor
            if (
                sensor is not None
                and abs(new_sensor.weight_raw - sensor.weight_raw) >= WEIGHT_JUMP_LOG_RAW
            ):
                _LOGGER.warning(
                    "%s: weight jump %s -> %s (raw), frame data %s, "
                    "recent notifications %s",
                    self.address,
                    sensor.weight_raw,
                    new_sensor.weight_raw,
                    frame.data.hex(),
                    [p.hex() for p in self._recent_payloads],
                )
            if sensor is None or (
                new_sensor.weight_raw,
                new_sensor.flow_raw,
                new_sensor.timer_raw,
                new_sensor.unit,
            ) != (sensor.weight_raw, sensor.flow_raw, sensor.timer_raw, sensor.unit):
                self._changed = True
            self._sensor = new_sensor
            if self._settings.get(CMD_SETTINGS_WEIGHT_UNIT) != new_sensor.unit:
                self._settings = {
                    **self._settings,
                    CMD_SETTINGS_WEIGHT_UNIT: new_sensor.unit,
                }
                self._changed = True
            if (grams := new_sensor.weight_grams) is not None:
                now = time.time()
                self._guarded(
                    "brew tracker",
                    lambda: self._handle_brew_result(
                        self.brew_tracker.feed_sample(
                            now, grams, new_sensor.flow_rate, new_sensor.timer
                        )
                    ),
                )
                # After the tracker (brew state is current), before the
                # rest of the frame: the cut goes out without delay.
                self._guarded("auto-stop", self.autostop.feed_sample, now, grams)
                self._guarded("ready dose", self._refresh_ready_dose)
        elif (new_status := parse_status(frame)) is not None:
            if self._status is None or new_status.raw != self._status.raw:
                self._changed = True
            self._status = new_status
            self._status_event.set()
            self._guarded("brew tracker", self._feed_brew_state, new_status.state)
        elif (setting := parse_setting(frame)) is not None:
            cmd, value = setting
            if self._settings.get(cmd) != value:
                self._settings = {**self._settings, cmd: value}
                self._changed = True
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

    # ---------------------------------------------------------------- brews

    @callback
    def _device_id(self) -> str | None:
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self.address)}
        )
        return device.id if device else None

    @callback
    def _feed_brew_state(self, state: int) -> None:
        if state in _TIMER_RUNNING_STATES:
            running = True
        elif state in _TIMER_STOPPED_STATES:
            running = False
        else:
            return
        self._handle_brew_result(
            self.brew_tracker.feed_timer_state(
                time.time(), running, reset=state in _TIMER_RESET_STATES
            )
        )

    @callback
    def _handle_brew_result(self, result: BrewResult | None) -> None:
        """Store a finished brew; keep live data and the tick in step."""
        if self._unloading:
            return
        tracker = self.brew_tracker
        self.autostop.brew_finished(result, tracker.last_discard_reason is not None)
        if result is not None:
            brew = self.brews.async_add_result(
                result,
                bean=self.autostop.brew_bean(result.start),
                target=self.autostop.brew_target(result.start, result.dose),
                cut_at=self.autostop.cut_offset(result.start),
            )
            self._end_live(end_message(brew=brew_summary(brew)))
        elif tracker.last_discard_reason:
            _LOGGER.debug(
                "%s: run discarded (%s)", self.address, tracker.last_discard_reason
            )
            self._end_live(
                end_message(
                    reason=tracker.last_discard_code,
                    detail=tracker.last_discard_reason,
                )
            )
            tracker.last_discard_reason = None
            tracker.last_discard_code = None
        if tracker.active and self._live is None:
            # Brew start: its bean is fixed now (stored brew, auto-stop).
            self.autostop.brew_bean(tracker.start)
            self._live = LiveBrew(
                tracker, self.autostop.brew_target, self.autostop.cut_offset
            )
            # A running brew suspends the backstop; only its end turns the
            # "Prepare brew" switch off.
            self._stop_ready_timeout()
            self._send_live(self._live.start_message(tracker, time.time()))
        elif not tracker.active and self._live is not None:
            self._end_live(end_message(reason="ended"))
        if tracker.active and self._cancel_brew_tick is None:
            self._cancel_brew_tick = async_track_time_interval(
                self.hass, self._async_brew_tick, BREW_TICK_INTERVAL
            )
        elif not tracker.active:
            self._stop_brew_tick()

    @callback
    def _async_brew_tick(self, _now: datetime) -> None:
        """Stall check, end of tail, parking backstop; coalesced live push."""
        if self._unloading:
            return
        now = time.time()
        self._handle_brew_result(self.brew_tracker.tick(now))
        if self._live is not None:
            if (message := self._live.update_message(self.brew_tracker, now)):
                self._send_live(message)

    @callback
    def _end_live(self, message: dict) -> None:
        if self._live is None:
            return
        # Points recorded since the last tick are not sent: the card shows
        # the stored brew (full profile) next.
        self._live = None
        # The brew is over (stored or discarded): disarm "Prepare brew".
        # Sent before live_end so the card never shows the empty ready
        # chart in between.
        self.async_set_brew_ready(False)
        self._send_live(message)

    @callback
    def _send_live(self, message: dict) -> None:
        async_dispatcher_send(
            self.hass, SIGNAL_LIVE.format(self.config_entry.entry_id), message
        )

    @callback
    def live_snapshot(self) -> dict:
        """What a new live subscriber gets first."""
        if self._live is None:
            return {"type": LIVE_IDLE}
        return self._live.snapshot(self.brew_tracker, time.time())

    # ---------------------------------------------------------- brew ready

    @callback
    def async_set_brew_ready(self, ready: bool) -> None:
        """Arm/disarm "Prepare brew" (the card shows an empty live chart).

        Armed without a brew, it turns itself off after READY_TIMEOUT_SECONDS;
        while a brew is live only the brew's end turns it off.
        """
        if self._unloading:
            return
        if ready and self._live is None:
            self._stop_ready_timeout()
            self._cancel_ready_timeout = async_call_later(
                self.hass, READY_TIMEOUT_SECONDS, self._async_ready_timeout
            )
        elif not ready:
            self._stop_ready_timeout()
        if ready == self.brew_ready:
            return
        self.brew_ready = ready
        _LOGGER.debug("%s: brew ready %s", self.address, ready)
        self._ready_dose = self.ready_dose() if ready else None
        self._ready_target = (
            self.autostop.target_for_dose(self._ready_dose) if ready else None
        )
        self._send_live(ready_message(ready, self._ready_dose, self._ready_target))
        for listener in list(self._ready_listeners):
            listener()

    def ready_dose(self) -> float | None:
        """Dose the next brew would get (shown in the ready view)."""
        return self.brew_tracker.next_dose(time.time())

    def ready_snapshot(self) -> dict:
        """``live_ready`` for a new subscriber."""
        dose = self.ready_dose() if self.brew_ready else None
        return ready_message(
            self.brew_ready, dose, self.autostop.target_for_dose(dose)
        )

    @callback
    def _refresh_ready_dose(self) -> None:
        """Resend ``live_ready`` when the dose for the next brew or its
        auto-stop target changes (weight samples, target/option changes)."""
        if self._unloading or not self.brew_ready or self._live is not None:
            return
        dose = self.ready_dose()
        target = self.autostop.target_for_dose(dose)
        if dose != self._ready_dose or target != self._ready_target:
            self._ready_dose = dose
            self._ready_target = target
            self._send_live(ready_message(True, dose, target))

    @callback
    def _async_ready_timeout(self, _now: datetime) -> None:
        self._cancel_ready_timeout = None
        if self._live is None:
            _LOGGER.debug("%s: no brew started, brew ready off", self.address)
            self.async_set_brew_ready(False)

    @callback
    def _stop_ready_timeout(self) -> None:
        if self._cancel_ready_timeout is not None:
            self._cancel_ready_timeout()
            self._cancel_ready_timeout = None

    @callback
    def async_add_ready_listener(self, listener: ReadyListener) -> CALLBACK_TYPE:
        """Register a callback for brew ready changes; return unsubscribe."""
        self._ready_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._ready_listeners:
                self._ready_listeners.remove(listener)

        return _remove

    @callback
    def _stop_brew_tick(self) -> None:
        if self._cancel_brew_tick is not None:
            self._cancel_brew_tick()
            self._cancel_brew_tick = None

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

    async def async_stop_timer(self) -> None:
        """DLink press, then ask for the state so the stop is seen at once
        (the brew tracker otherwise notices it only by the timer stalling)."""
        await self.async_send(CMD_TIMER_STOP)
        try:
            await self.async_request_status()
        except HomeAssistantError:
            pass  # the stop itself went out; the stall check still catches it

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
