"""Pump throttle (0.6.0) at the Home Assistant level.

The scale is driven like in ``test_integration`` (frames at 10 Hz on a
controlled clock); the pump is a plain ``number`` state whose
``number.set_value`` service is replaced by a fake that records the calls
and writes the new state (the shot's flow follows it). The throttle's
monotonic clock is the same controlled clock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, ServiceCall  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from conftest import Clock  # noqa: E402
from test_integration import (  # noqa: E402
    OPTIONS,
    PLUG_ATTRS,
    Scale,
    _setup,
    _state,
    mock_plug,
)

from custom_components.difluid_scale import throttle as throttle_mod  # noqa: E402
from custom_components.difluid_scale.const import (  # noqa: E402
    STOP_STORAGE_KEY,
    STORAGE_KEY,
)
from custom_components.difluid_scale.protocol import (  # noqa: E402
    CMD_TIMER_STOP,
    DeviceState,
)
from custom_components.difluid_scale.throttle import pump_grid  # noqa: E402
from custom_components.difluid_scale.throttle_model import ThrottleModel  # noqa: E402

PLUG = "switch.coffee_machine"  # the pump relay (stop switch)
PUMP = "number.pump_power"
MODE = "select.microbalance_pump_throttle"
PREDICTED = "sensor.microbalance_predicted_shot_time"
FIXTURE = Path(__file__).parent / "fixtures" / "stop_learning_host_2026-11-23.json"


class ThrottleClock:
    """The throttle's ``time``: monotonic and wall time = the test clock."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def monotonic(self) -> float:
        return self.clock.t

    def time(self) -> float:
        return self.clock.t


@pytest.fixture
def tclock(clock: Clock) -> Generator[Clock]:
    with patch.object(throttle_mod, "time", ThrottleClock(clock)):
        yield clock


PUMPS: dict[str, Any] = {}


class Pump:
    """``number.set_value`` of a pump power entity (state follows)."""

    def __init__(
        self,
        hass: HomeAssistant,
        low: float = 0.0,
        high: float = 100.0,
        step: float = 2.0,
        fail: bool = False,
        feedback: bool = True,
        entity_id: str = PUMP,
        slow: bool = False,
        report: str = "sync",
        report_scale: float | None = None,
    ) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.attrs = {"min": low, "max": high, "step": step, "mode": "slider"}
        self.calls: list[float] = []
        self.fail = fail
        # Without feedback the entity keeps its state (a number that does
        # not report back); the flow still follows the command.
        self.feedback = feedback
        self.slow = slow  # the call takes a few loop turns (network)
        # How the entity reports a new value (real ESPHome/MQTT/Zigbee
        # numbers report AFTER the service call returned): "sync" (inside
        # the call, the old fake), "delayed" (a few loop turns after the
        # call returned), "never_low" (only values at the maximum are
        # reported: a pump without feedback below it). ``report_scale``:
        # the reported value on another grid (e.g. 0.999: 100 -> 99.9).
        self.report = report
        self.report_scale = report_scale
        self.commanded = high
        hass.states.async_set(entity_id, str(high), self.attrs)

        async def _set_value(call: ServiceCall) -> None:
            if call.data["entity_id"] != self.entity_id:
                other = PUMPS.get(call.data["entity_id"])
                if other is not None:
                    await other._handle(call)  # noqa: SLF001
                return
            await self._handle(call)

        PUMPS[entity_id] = self
        hass.services.async_register("number", "set_value", _set_value)

    async def _handle(self, call: ServiceCall) -> None:
        self.calls.append(float(call.data["value"]))
        if self.slow:
            for _ in range(5):
                await asyncio.sleep(0)
        if self.fail:
            raise HomeAssistantError("pump offline")
        self.commanded = float(call.data["value"])
        if not self.feedback:
            return
        value = float(call.data["value"])
        shown = value if self.report_scale is None else round(value * self.report_scale, 1)
        if self.report == "delayed":
            self.hass.async_create_task(self._report_later(shown))
        elif self.report == "never_low":
            if value >= float(self.attrs["max"]):
                self.hass.states.async_set(self.entity_id, str(shown), self.attrs)
        else:
            self.hass.states.async_set(self.entity_id, str(shown), self.attrs)

    async def _report_later(self, value: float) -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        self.hass.states.async_set(self.entity_id, str(value), self.attrs)

    @property
    def percent(self) -> float:
        """What the pump really runs at (% of max)."""
        return self.commanded / float(self.attrs["max"]) * 100.0

    @property
    def state_percent(self) -> float:
        state = self.hass.states.get(self.entity_id)
        return pump_grid(state).percent(float(state.state))


async def _setup_throttle(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    mode: str = "throttle",
    pump: bool = True,
    target: float = 36.0,
) -> None:
    options = dict(OPTIONS, stop_switch=PLUG)
    if pump:
        options["pump_power"] = PUMP
    hass.states.async_set(PLUG, "off", PLUG_ATTRS)
    hass.config_entries.async_update_entry(entry, options=options)
    await _setup(hass, entry)
    coord = entry.runtime_data
    coord.autostop.set_target(target)
    coord.autostop.throttle.set_mode(mode)
    # 0.7.0: the throttle is the Zielzeit pump profile (a new store starts
    # at Standard, which never throttles).
    coord.autostop.throttle.select_profile("target_time")
    await hass.async_block_till_done()


def _relay(hass: HomeAssistant, on: bool) -> None:
    hass.states.async_set(PLUG, "on" if on else "off", PLUG_ATTRS)


async def _shot(
    hass: HomeAssistant,
    scale: Scale,
    clock: Clock,
    *,
    pump: Pump | None,
    relay: bool = True,
    timer_delay: float = 0.6,
    first_drop: float = 5.0,
    qa: float = 0.6,
    seconds: float = 45.0,
    disconnect_at: float | None = None,
) -> dict[str, Any]:
    """A shot: relay on, scale timer ``timer_delay`` s later, flow at 100 %
    ``qa * (s - first_drop + 2) ** 0.55`` scaled by the pump power (when a
    pump is set up), cut by the auto-stop (the plug then reports off), the
    timer stopped by the integration once the weight settled."""
    coord = scale.coord

    def _on_status() -> None:
        if CMD_TIMER_STOP in scale.client.written:
            scale.frozen = scale.timer()
            scale.status(DeviceState.TIMER_PAUSE)
        else:
            scale.status(DeviceState.TIMING)

    scale.client.on_status = _on_status
    t0 = clock.t
    if relay:
        _relay(hass, True)
    await hass.async_block_till_done()
    weight = 0.0
    timer_started = False
    cut_at = None
    info: dict[str, Any] = {"t0": t0, "powers": []}
    while clock.t - t0 < seconds:
        s = clock.t - t0
        if not timer_started and s >= timer_delay - 1e-6:
            # The scale timer starts (water may already be in the cup).
            scale.status(DeviceState.TIMING)
            scale.timer_start = clock.t
            timer_started = True
        if cut_at is None and coord.autostop._cut is not None:  # noqa: SLF001
            cut_at = s
            info["cut"] = s
        if disconnect_at is not None and s >= disconnect_at:
            info["percent_before_drop"] = pump.percent if pump else None
            scale.disconnect()
            await hass.async_block_till_done()
            info["disconnected"] = s
            return info
        pct = pump.percent if pump is not None else 100.0
        if cut_at is None and s >= first_drop - 2.0:
            weight += qa * (s - first_drop + 2.0) ** 0.55 * pct / 100.0 * 0.1
        info["powers"].append(pct)
        if scale.frozen is not None:
            clock.t = round(clock.t + 0.1, 3)
            scale._tick()  # noqa: SLF001
        else:
            scale.run(0.1, round(weight, 1))
        await hass.async_block_till_done()
    return info


def _events(hass: HomeAssistant) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    coord = hass.config_entries.async_entries("difluid_scale")[0].runtime_data
    coord.autostop.throttle.async_add_event_listener(
        lambda kind, data: events.append((kind, data))
    )
    return events


# ------------------------------------------------------------- scenarios


async def test_fast_shot_is_throttled_and_pump_restored(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Zu schnell: triggered from first drop + 3 s, the pump is lowered on
    its 2 % grid, the shot is cut near the target time, the pump goes back
    to 100 % after the cut; the brew stores relay timing and the record."""
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass, step=2.0)
    coord = entry.runtime_data
    events = _events(hass)
    scale = Scale(coord, tclock)
    info = await _shot(hass, scale, tclock, pump=pump)
    assert pump.calls, "pump never set"
    assert all(abs(v / 2 - round(v / 2)) < 1e-9 for v in pump.calls), pump.calls
    assert min(pump.calls) >= 40.0
    assert pump.calls[-1] == 100.0, "restored after the cut"
    assert all(a != b for a, b in zip(pump.calls, pump.calls[1:], strict=False)), pump.calls
    kinds = [k for k, _ in events]
    assert kinds[0] == "triggered" and kinds[-1] == "ended"
    assert events[0][1]["applied"] is True and events[0][1]["grind_finer"] is True
    assert events[0][1]["predicted_shot_time"] <= 25.0
    assert 28.5 <= info["cut"] <= 31.5, info["cut"]
    brew = coord.brews.last
    # Receipt time of the newest frame vs. the relay: within a few ms.
    assert brew["relay_offset"] == pytest.approx(0.6, abs=0.005)
    assert brew["first_drop_shot"] == pytest.approx(
        brew["metrics"]["first_drop"] + brew["relay_offset"], abs=0.11
    )
    assert brew["shot_time"] == pytest.approx(brew["duration"] + brew["relay_offset"], abs=0.11)
    assert brew["since_last_relay_off"] is None  # no relay off seen before
    record = brew["throttle"]
    assert record["mode"] == "throttle" and record["applied"] and record["triggered"]
    assert record["unreachable"] is False and record["unreachable_reason"] is None
    # Learning is off by default: the shot never holds for it.
    assert coord.autostop.throttle._brew.shot.learn is False  # noqa: SLF001
    assert record["target_time"] == 30.0 and record["grind_finer"]
    assert record["power"][0][1] == 100.0 and record["power"][-1][1] == 100.0
    assert any(p[1] < 100 for p in record["power"])
    # Stop learning: the brew is marked throttled.
    assert coord.autostop.last["throttled"] is True
    if coord.autostop.journal.entries:
        assert coord.autostop.journal.entries[-1].get("throttled") is True
    assert coord.autostop.throttled_deviations
    attrs = hass.states.get("sensor.microbalance_stop_deviation").attributes
    assert attrs["throttled_deviation_count"] == 1
    # Really throttled: only the part up to the trigger may teach the
    # exponent, in the throttled pool.
    assert coord.autostop.throttle.exponent.samples == []
    samples = coord.autostop.throttle.exponent_throttled.samples
    assert all(x["brew_id"] == brew["id"] for x in samples)
    assert record["prediction_exponent"] is not None
    assert _state(hass, "sensor.microbalance_last_pump_power") != "unknown"
    # Second shot: since_last_relay_off is the gap since the plug went off.
    off_at = coord.autostop.throttle.last_relay_off
    assert off_at is not None
    scale.frozen = None
    scale.timer_start = None
    scale.run(5, 0.0)
    scale.status(DeviceState.IDLE)
    scale.run(5, 0.0)
    info2 = await _shot(hass, scale, tclock, pump=pump)
    second = coord.brews.last
    assert second["id"] != brew["id"]
    assert second["since_last_relay_off"] == pytest.approx(info2["t0"] - off_at, abs=0.11)


async def test_observe_never_touches_the_pump(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Beobachten: prediction, trigger and the would-be powers are stored and
    reported, the pump entity is never written (not even restored)."""
    await _setup_throttle(hass, entry, mode="observe")
    mock_plug(hass)
    pump = Pump(hass)
    hass.states.async_set(PUMP, "80", pump.attrs)  # not at 100: still untouched
    coord = entry.runtime_data
    events = _events(hass)
    scale = Scale(coord, tclock)
    await _shot(hass, scale, tclock, pump=None)
    assert pump.calls == []
    assert _state(hass, PUMP) == "80"
    assert events and all(d["applied"] is False for _, d in events)
    record = coord.brews.last["throttle"]
    assert record["mode"] == "observe" and record["triggered"] and not record["applied"]
    # Only the trigger step: 100 % and what it would have set.
    assert len(record["power"]) == 2 and record["power"][1][1] == record["would_set"]
    assert 40.0 <= record["would_set"] < 100.0
    assert record["unreachable"] is False and record["would_need_floor"] is False
    assert [k for k, _ in events] == ["triggered", "ended"]
    assert events[0][1]["would_set"] == record["would_set"]
    assert coord.autostop.last["throttled"] is False
    assert _state(hass, PREDICTED) not in ("unknown", "unavailable")


async def test_mode_off_still_stores_relay_timing(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_throttle(hass, entry, mode="off")
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    events = _events(hass)
    await _shot(hass, Scale(coord, tclock), tclock, pump=None)
    brew = coord.brews.last
    # 0.7.0b6: only the pump profile selected at the start, nothing else.
    assert brew["throttle"] == {"profile": "target_time"}
    assert events == [] and pump.calls == []
    assert brew["relay_offset"] is not None and brew["first_drop_shot"] is not None
    # Relay start + auto-stop target, not throttled: the prediction
    # exponent learns from it.
    samples = coord.autostop.throttle.exponent.samples
    assert len(samples) == 1 and samples[0]["brew_id"] == brew["id"]


async def test_brew_without_relay_start_is_inactive(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Started with the machine's button (no relay on): no throttle, the
    reason is stored, no relay fields."""
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump, relay=False)
    brew = coord.brews.last
    assert pump.calls == []
    assert brew["relay_offset"] is None and brew["first_drop_shot"] is None
    assert brew["throttle"]["reason"] == "no_relay_start"
    assert brew["throttle"]["active"] is False
    # 0.7.0b6: the selected pump profile is stored also without a relay
    # start (the card always shows it) and was sent live.
    assert brew["throttle"]["profile"] == "target_time"
    throttle = coord.autostop.throttle
    start = throttle._brew.start
    assert throttle.live_info(start) == {"profile": "target_time"}
    # Not started with the relay: no exponent sample.
    assert coord.autostop.throttle.exponent.samples == []
    assert (
        hass.states.get(PREDICTED).attributes["last_reason"] == "no_relay_start"
    )


async def test_descaling_and_flush_untouched(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Relay on without a scale timer (flush, descaling): nothing happens.
    A brew whose timer starts long after the relay went on (descaling run
    on the scale) does not match the relay start."""
    await _setup_throttle(hass, entry)
    pump = Pump(hass)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    _relay(hass, True)
    scale.run(20, lambda r: r * 2.0)  # water, but no timer: no brew
    _relay(hass, False)
    await hass.async_block_till_done()
    assert pump.calls == [] and coord.brews.last is None
    # Descaling: relay on for minutes, the timer starts 60 s later.
    scale.run(2, 0.0)
    _relay(hass, True)
    await hass.async_block_till_done()
    scale.run(60, 0.0)
    mock_plug(hass)
    await _shot(hass, scale, tclock, pump=pump, relay=False)
    assert pump.calls == []
    assert coord.brews.last["relay_offset"] is None


async def test_ble_drop_mid_shot_restores_pump_at_once(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    info = await _shot(
        hass, Scale(coord, tclock), tclock, pump=pump, disconnect_at=16.0
    )
    assert info["percent_before_drop"] < 100.0, info
    assert pump.calls[-1] == 100.0
    assert pump.percent == 100.0


async def test_unload_mid_shot_restores_pump(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    # Run the shot only until the pump was lowered.
    original = coord.autostop.throttle._set_pump  # noqa: SLF001
    lowered: list[float] = []

    def _spy(brew: Any, percent: float) -> None:
        lowered.append(percent)
        original(brew, percent)

    coord.autostop.throttle._set_pump = _spy  # noqa: SLF001
    await _shot(hass, scale, tclock, pump=pump, seconds=14.0)
    assert lowered and pump.percent < 100.0
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert pump.percent == 100.0


def _exp_brew(brew_id: str, start: str, p: float, applied: bool) -> dict:
    profile = [[round(i * 0.25, 2), round(0.4 * max(0.0, i * 0.25 - 3.0) ** p, 1), 0.0]
               for i in range(120)]
    return {
        "id": brew_id, "start": start, "duration": 29.0, "relay_offset": 0.5,
        "target": 40.0, "profile": profile, "metrics": {"first_drop": 5.0},
        "throttle": {"applied": applied, "trigger_shot_time": 12.5} if applied else None,
    }


async def test_exponent_from_throttled_brew_up_to_the_trigger(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """A throttled brew's exponent (profile up to the trigger only) goes to
    its own pool; a curve bent after the trigger does not count."""
    await _setup_throttle(hass, entry)
    throttle = entry.runtime_data.autostop.throttle
    brew = _exp_brew("x1", "2026-11-23T10:00:00+00:00", 1.8, True)
    assert throttle.brew_stored(brew)
    assert throttle.exponent.samples == []
    assert len(throttle.exponent_throttled.samples) == 1
    assert throttle.exponent_throttled.samples[0]["p"] == pytest.approx(1.8, abs=0.1)
    bent = {**brew, "id": "x2", "start": "2026-11-23T10:05:00+00:00",
            "profile": [[t, w if t <= 12.0 else w * 0.5, f] for t, w, f in brew["profile"]]}
    assert throttle.brew_stored(bent)
    assert throttle.exponent_throttled.samples[-1]["p"] == pytest.approx(1.8, abs=0.1)
    assert not throttle.brew_stored({**brew, "id": "x3", "relay_offset": None})


async def test_throttled_exponents_count_only_in_lock_in(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """With an unthrottled brew among the last 10 learnable ones the
    prediction uses the unthrottled pool only; when every recent shot was
    throttled (lock-in) the throttled samples count too."""
    await _setup_throttle(hass, entry)
    throttle = entry.runtime_data.autostop.throttle
    throttle.brew_stored(_exp_brew("u1", "2026-11-23T08:00:00+00:00", 1.3, False))
    for i in range(5):
        throttle.brew_stored(_exp_brew(f"t{i}", f"2026-11-23T09:0{i}:00+00:00", 2.0, True))
    unthrottled_only = throttle.exponent.value
    assert throttle.prediction_exponent == unthrottled_only < 1.55
    for i in range(5, 10):
        throttle.brew_stored(_exp_brew(f"t{i}", f"2026-11-23T09:1{i - 5}:00+00:00", 2.0, True))
    # 10 throttled in a row: the unthrottled one fell out of the window.
    assert not any(r["unthrottled"] for r in throttle.exponent_recent)
    assert throttle.prediction_exponent > 1.8
    # Deleting brews takes them out of the pools and the window.
    throttle.async_forget_brew("t9", "2026-11-23T09:14:00+00:00")
    assert len(throttle.exponent_throttled.samples) == 9
    throttle.async_reset()
    assert throttle.prediction_exponent == pytest.approx(1.55)
    assert throttle.exponent_recent == []


async def test_failed_set_reverts_to_the_state(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """A failed set_value: the pump still runs at its old value, so the
    command no longer counts as in effect."""
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass, fail=True)
    coord = entry.runtime_data
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump, seconds=14.0)
    await hass.async_block_till_done(wait_background_tasks=True)
    brew = coord.autostop.throttle._brew  # noqa: SLF001
    assert pump.calls and brew.command_value is None


async def test_hanging_set_value_times_out(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock, caplog
) -> None:
    await _setup_throttle(hass, entry)
    throttle = entry.runtime_data.autostop.throttle

    async def _hang(call: ServiceCall) -> None:
        await asyncio.sleep(3600)

    hass.services.async_register("number", "set_value", _hang)
    with patch.object(throttle_mod, "PUMP_CALL_TIMEOUT", 0.01):
        assert not await throttle._async_set_pump(PUMP, 100.0)  # noqa: SLF001
    assert "could not set" in caplog.text


async def test_pump_errors_never_break_the_auto_stop(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock, caplog
) -> None:
    await _setup_throttle(hass, entry)
    calls = mock_plug(hass)
    pump = Pump(hass, fail=True)
    coord = entry.runtime_data
    info = await _shot(hass, Scale(coord, tclock), tclock, pump=pump)
    assert pump.calls  # tried
    assert "Pump throttle could not set" in caplog.text
    assert len(calls) == 1 and info.get("cut") is not None  # still cut
    assert coord.brews.last is not None


async def test_pump_range_0_255_step_1(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Another machine: pump entity 0-255 in steps of 1, mapped to 0-100 %;
    restore = its max."""
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass, low=0.0, high=255.0, step=1.0)
    coord = entry.runtime_data
    info = await _shot(hass, Scale(coord, tclock), tclock, pump=pump, qa=0.65)
    assert pump.calls and pump.calls[-1] == 255.0
    assert all(float(v).is_integer() for v in pump.calls)
    assert min(pump.calls) >= 102  # 40 % of 255 = 102.0, never below
    assert all(a != b for a, b in zip(pump.calls, pump.calls[1:], strict=False))
    assert 28.5 <= info["cut"] <= 31.5, info["cut"]


def test_pump_grid_from_state() -> None:
    """% of the maximum; the entity's min is a bound; grid min + n x step;
    the floor rounded up."""

    class _State:
        def __init__(self, attrs: dict) -> None:
            self.attributes = attrs

    g = pump_grid(_State({"min": 0, "max": 255, "step": 1}))
    assert g.value(50.0) == 128.0 and g.percent(255.0) == 100.0
    assert g.floor_value(40.0) == 102.0
    g = pump_grid(_State({"min": 0, "max": 100, "step": 2}))
    assert g.floor_value(45.0) == 46.0 and g.quantize(20.0, 45.0) == 46.0
    g = pump_grid(_State({"min": 10, "max": 100, "step": 5}))
    assert g.percent(10.0) == 10.0 and g.value(0.0) == 10.0
    assert g.floor_value(41.0) == 45.0 and g.value(53.0) == 55.0
    g = pump_grid(_State({}))
    assert g.high == 100.0 and g.value(41.0) == 41.0


@pytest.mark.parametrize(
    ("low", "high", "step", "floor", "lowest"),
    [(0.0, 100.0, 2.0, 45.0, 46.0), (10.0, 100.0, 5.0, 40.0, 40.0), (0.0, 255.0, 1.0, 40.0, 102.0)],
)
async def test_pump_grids_and_floor(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock, low, high, step, floor, lowest
) -> None:
    """Unreachable shot: the pump goes down to the floor on its grid, never
    below it, never the same value twice in a row."""
    await _setup_throttle(hass, entry)
    entry.runtime_data.autostop.throttle.set_floor(floor)
    mock_plug(hass)
    pump = Pump(hass, low=low, high=high, step=step)
    await _shot(hass, Scale(entry.runtime_data, tclock), tclock, pump=pump, qa=1.6, first_drop=3.5)
    assert pump.calls, "pump never set"
    assert min(pump.calls) == lowest, pump.calls
    assert all(abs((v - low) / step - round((v - low) / step)) < 1e-9 for v in pump.calls)
    assert all(a != b for a, b in zip(pump.calls, pump.calls[1:], strict=False)), pump.calls
    assert pump.calls[-1] == high
    record = entry.runtime_data.brews.last["throttle"]
    assert record["unreachable"] and record["unreachable_reason"] == "floor"


async def test_pump_back_to_full_power_at_setup_and_relay_on(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Every shot starts at 100 %: a pump left lower is set to its maximum
    when the throttle starts (throttle mode) and when the relay goes on;
    never in observe."""
    await _setup_throttle(hass, entry, mode="observe")
    pump = Pump(hass)
    hass.states.async_set(PUMP, "60", pump.attrs)
    throttle = entry.runtime_data.autostop.throttle
    throttle.async_watch(PLUG)
    _relay(hass, True)
    await hass.async_block_till_done()
    _relay(hass, False)
    await hass.async_block_till_done()
    assert pump.calls == []  # observe: never
    throttle.set_mode("throttle")
    throttle.async_watch(PLUG)  # setup / options change
    await hass.async_block_till_done()
    assert pump.calls == [100.0]
    hass.states.async_set(PUMP, "70", pump.attrs)
    _relay(hass, True)
    await hass.async_block_till_done()
    assert pump.calls == [100.0, 100.0]


async def test_pump_without_feedback_uses_the_command(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock, caplog
) -> None:
    """A number that never reports the new value: the throttle goes on with
    its command (the shot still ends at the target) and warns once."""
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass, feedback=False)
    info = await _shot(hass, Scale(entry.runtime_data, tclock), tclock, pump=pump)
    assert 28.5 <= info["cut"] <= 31.5, info["cut"]
    assert caplog.text.count("does not show the commanded") == 1
    record = entry.runtime_data.brews.last["throttle"]
    assert record["applied"] and record["power"][-1][1] == 100.0


async def test_pump_commands_serialized_newest_wins(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_throttle(hass, entry)
    pump = Pump(hass, slow=True)
    throttle = entry.runtime_data.autostop.throttle
    throttle._request(PUMP, 60.0)  # noqa: SLF001
    throttle._request(PUMP, 50.0)  # noqa: SLF001
    throttle._request(PUMP, 100.0)  # noqa: SLF001 - restore after a late correction
    await hass.async_block_till_done(wait_background_tasks=True)
    # 60 was on its way; 50 was replaced by the restore before it was sent.
    assert pump.calls == [60.0, 100.0]
    assert _state(hass, PUMP) == "100.0"


async def test_options_change_mid_shot_resets_the_throttled_pump(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass)
    other = Pump(hass, entity_id="number.other_pump")
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    await _shot(hass, scale, tclock, pump=pump, seconds=14.0)
    assert pump.percent < 100.0
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "pump_power": "number.other_pump"}
    )
    await hass.async_block_till_done()
    coord.autostop.throttle.async_ble_lost()
    await hass.async_block_till_done()
    assert pump.percent == 100.0  # the pump of this shot
    assert other.calls == []


async def test_late_timer_start_has_its_own_reason(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """The scale timer started after the first drops (weight already ~3 g
    and rising): reason ``timer_after_first_drop``, not ``no_first_drop``."""
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    await _shot(
        hass, Scale(coord, tclock), tclock, pump=pump, timer_delay=9.0, first_drop=4.0
    )
    assert coord.brews.last["throttle"]["reason"] == "timer_after_first_drop"


async def test_throttle_mode_without_pump_only_observes(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """No pump power entity configured: clean, no errors, the reason shows."""
    await _setup_throttle(hass, entry, pump=False)
    mock_plug(hass)
    coord = entry.runtime_data
    await _shot(hass, Scale(coord, tclock), tclock, pump=None)
    record = coord.brews.last["throttle"]
    assert record["reason"] == "no_pump" and not record["applied"]
    attrs = hass.states.get(PREDICTED).attributes
    assert attrs["pump_configured"] is False and attrs["mode"] == "throttle"


async def test_bean_target_shot_time_overrides_global(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_throttle(hass, entry)
    coord = entry.runtime_data
    throttle = coord.autostop.throttle
    assert throttle.effective_target_time() == 30.0
    await hass.services.async_call(
        "difluid_scale", "add_bean", {"name": "Kenya", "ratio": 2.2, "shot_time": 27},
        blocking=True,
    )
    assert throttle.effective_target_time() == 27.0
    await hass.services.async_call(
        "difluid_scale", "add_bean", {"name": "Brazil", "ratio": 2.0}, blocking=True
    )
    assert throttle.effective_target_time() == 30.0  # no override: global
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.microbalance_target_shot_time", "value": 28},
        blocking=True,
    )
    assert throttle.effective_target_time() == 28.0


async def test_entities_and_settings_persist(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, tclock
) -> None:
    await _setup_throttle(hass, entry, mode="observe")
    for entity_id in (
        "number.microbalance_target_shot_time",
        "number.microbalance_pump_throttle_floor",
        MODE,
        "switch.microbalance_pump_throttle_learning",
        PREDICTED,
        "sensor.microbalance_last_pump_power",
        "sensor.microbalance_pump_power_exponent",
        "sensor.microbalance_pump_throttle_learning_samples",
        "button.microbalance_reset_pump_throttle_learning",
        "event.microbalance_pump_throttle",
    ):
        assert hass.states.get(entity_id) is not None, entity_id
    assert _state(hass, MODE) == "observe"
    assert _state(hass, "number.microbalance_target_shot_time") == "30.0"
    assert _state(hass, "number.microbalance_pump_throttle_floor") == "40.0"
    assert _state(hass, "switch.microbalance_pump_throttle_learning") == "off"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": MODE, "option": "throttle"}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.microbalance_pump_throttle_learning"},
        blocking=True,
    )
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.microbalance_pump_throttle_floor", "value": 55},
        blocking=True,
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    throttle = entry.runtime_data.autostop.throttle
    assert throttle.mode == "throttle" and throttle.learning and throttle.floor == 55.0
    stored = hass_storage[STOP_STORAGE_KEY.format(entry.entry_id)]
    assert stored["minor_version"] == 4
    assert stored["data"]["throttle"]["settings"]["floor"] == 55.0


async def test_host_store_migration(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, tclock
) -> None:
    """A real stop store (1.2, anonymized: ids, entity, bean and times
    replaced or shifted; model and journal numbers bit-identical): the stop
    model and its journal stay exactly, the throttle starts at its prior;
    written as 1.3."""
    host = json.loads(FIXTURE.read_text())
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    hass_storage[key] = {**host, "key": key}
    await _setup(hass, entry)
    stop = entry.runtime_data.autostop
    assert stop.model.tau == pytest.approx(0.5022520056392453, abs=0)
    assert stop.model.c == pytest.approx(0.6327966752443706, abs=0)
    assert stop.model.count == 8 and len(stop.journal) == 8
    assert stop.model.to_dict() == host["data"]["model"]
    assert stop.journal.to_dict() == host["data"]["journal"]
    assert stop.journal.replay().to_dict() == stop.model.to_dict()
    throttle = stop.throttle
    assert throttle.model.to_dict() == ThrottleModel().to_dict()
    assert throttle.model.gamma == 1.0 and throttle.model.count == 0
    assert len(throttle.journal) == 0 and throttle.mode == "observe"
    assert not throttle.learning and throttle.target_time == 30.0 and throttle.floor == 40.0
    assert throttle.exponent.value == pytest.approx(1.55)
    settings = stop.settings()
    assert settings["target_ratio"] == 2.7 and settings["target_yield"] == 40.0
    assert stop.latencies.to_dict() == {
        k: v for k, v in host["data"]["switch_latency"].items() if k != "last"
    }
    stop.async_schedule_save()
    await stop.async_flush()
    saved = hass_storage[key]
    assert saved["minor_version"] == 4
    assert saved["data"]["model"] == host["data"]["model"]
    assert saved["data"]["journal"] == host["data"]["journal"]
    assert saved["data"]["throttle"]["model"]["count"] == 0
    assert saved["data"]["throttle"]["journal"]["entries"] == []
    assert _state(hass, "sensor.microbalance_stop_learning_samples") == "8"
    assert _state(hass, "sensor.microbalance_pump_throttle_learning_samples") == "0"


async def test_delete_brew_takes_out_throttle_sample(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, tclock
) -> None:
    """A brew's power-model sample (and its exponent sample) leave the
    learning with the brew; the model is replayed from the base."""
    from custom_components.difluid_scale.stop_model import LearningJournal
    from custom_components.difluid_scale.throttle_model import (
        ThrottleModel,
        throttle_journal,
    )

    journal = throttle_journal()
    model = ThrottleModel()
    for brew_id, (a, b, r) in (("b1", (100, 50, 0.4)), ("b2", (100, 60, 0.55))):
        assert model.update(a, b, r)
        journal.add(
            LearningJournal.make({"d_from": a, "d_to": b, "ratio": r}, brew_id, None, None)
        )
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    throttle_data = throttle_mod.default_store()
    throttle_data["model"] = model.to_dict()
    throttle_data["journal"] = journal.to_dict()
    throttle_data["exponent"] = [{"brew_id": "b1", "start": None, "p": 2.0}]
    hass_storage[key] = {
        "version": 1, "minor_version": 3, "key": key,
        "data": {"settings": {"target_ratio": 0, "target_yield": 36, "bean_id": None},
                 "throttle": throttle_data,
                 "throttled_deviations": [
                     {"start": "2026-11-23T10:00:00+00:00", "deviation": 0.8},
                     {"start": "2026-11-23T11:00:00+00:00", "deviation": -0.4},
                 ]},
    }
    bkey = STORAGE_KEY.format(entry.entry_id)
    hass_storage[bkey] = {
        "version": 1, "minor_version": 2, "key": bkey,
        "data": {"brews": [
            {"id": "b1", "start": "2026-11-23T10:00:00+00:00", "duration": 20, "dose": 18,
             "yield": 36, "ratio": 2, "profile": [[0, 0, 0], [1, 1, 0]]},
            {"id": "b2", "start": "2026-11-23T11:00:00+00:00", "duration": 20, "dose": 18,
             "yield": 36, "ratio": 2, "profile": [[0, 0, 0], [1, 1, 0]]},
        ], "brew_count": 2, "coffee_total": 36},
    }
    await _setup(hass, entry)
    throttle = entry.runtime_data.autostop.throttle
    assert throttle.model.count == 2 and throttle.exponent.value > 1.55
    assert _state(hass, "sensor.microbalance_pump_throttle_learning_samples") == "2"
    response = await hass.services.async_call(
        "difluid_scale", "delete_brew", {"brew_id": "b1"}, blocking=True, return_response=True
    )
    assert response["removed_from_learning"] is True
    stop = entry.runtime_data.autostop
    assert stop.throttled_deviations == [
        {"start": "2026-11-23T11:00:00+00:00", "deviation": -0.4}
    ]
    attrs = hass.states.get("sensor.microbalance_stop_deviation").attributes
    assert attrs["throttled_deviation_count"] == 1
    expected = ThrottleModel()
    expected.update(100, 60, 0.55)
    assert throttle.model.to_dict() == expected.to_dict()
    assert throttle.exponent.value == pytest.approx(1.55)
    assert _state(hass, "sensor.microbalance_pump_throttle_learning_samples") == "1"
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.microbalance_reset_pump_throttle_learning"},
        blocking=True,
    )
    assert throttle.model.count == 0 and len(throttle.journal) == 0


async def test_learning_switch_learns_from_throttled_shot(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_throttle(hass, entry)
    coord = entry.runtime_data
    coord.autostop.throttle.set_learning(True)
    mock_plug(hass)
    pump = Pump(hass)
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump)
    throttle = coord.autostop.throttle
    record = coord.brews.last["throttle"]
    if record["sample"] is not None:
        assert record["learned"] and throttle.model.count == 1
        assert throttle.journal.entries[0]["brew_id"] == coord.brews.last["id"]
        assert coord.autostop.learned(coord.brews.last)
    else:  # pragma: no cover - the simulated shot always gives one
        pytest.fail(f"no learning sample: {record['sample_reason']}")


async def test_live_carries_throttle(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    await _shot(hass, scale, tclock, pump=pump, seconds=14.0)
    live = coord.live_snapshot().get("throttle")
    assert live is not None and live["relay_offset"] is not None
    assert live["target_time"] == 30.0 and live["power"][0][1] == 100.0
    assert live["triggered"] and len(live["power"]) > 1


async def test_diagnostics_carry_shot_timing(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    from custom_components.difluid_scale.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    timing = diag["brews"]["shot_timing"]
    assert timing and timing[0]["first_drop_shot"] is not None
    assert timing[0]["shot_time"] is not None and timing[0]["profile"]
    assert diag["throttle"]["pump_entity"] == PUMP
    assert diag["throttle"]["settings"]["mode"] == "throttle"


async def test_scale_only_brew_has_no_profile(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """0.7.0b6 (user decision): without a stop switch and without a pump
    power entity no pump profile is stored or sent - not even Standard."""
    from test_integration import _setup

    await _setup(hass, entry)
    coord = entry.runtime_data
    assert coord.autostop.switch_entity is None
    assert coord.autostop.throttle.pump_entity is None
    scale = Scale(coord, tclock)
    scale.weigh_dose()
    scale.shot()
    brew = coord.brews.last
    assert brew is not None
    record = brew.get("throttle") or {}
    assert not record.get("profile"), record
    throttle = coord.autostop.throttle
    info = throttle.live_info(throttle._brew.start) or {}
    assert not info.get("profile"), info
