"""0.7.0b2: fixes from the full code audit of 2026-11-24 (IDs A*, B*, C*).

Driven like ``test_integration`` / ``test_throttle_ha`` (frames at 10 Hz
on a controlled clock, fake plug and pump). Startup sequences (unknown ->
value) and more than one device are covered where the fix depends on them.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, ServiceCall  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_time_changed,
)

from conftest import ADDR, Clock  # noqa: E402
from test_integration import (  # noqa: E402
    PLUG_ATTRS,
    Scale,
    _setup,
    _setup_autostop,
    mock_plug,
    plug_reports_off,
)
from test_throttle_ha import (  # noqa: E402
    PLUG,
    Pump,
    ThrottleClock,
    _relay,
    _setup_throttle,
    _shot,
)

from custom_components.difluid_scale import autostop as autostop_mod  # noqa: E402
from custom_components.difluid_scale import coordinator as coord_mod  # noqa: E402
from custom_components.difluid_scale import throttle as throttle_mod  # noqa: E402
from custom_components.difluid_scale.const import DOMAIN  # noqa: E402
from custom_components.difluid_scale.protocol import (  # noqa: E402
    CMD_DEVICE_SENSOR,
    CMD_TIMER_START,
    FUNC_DEVICE,
    DeviceState,
    build_frame,
)



@pytest.fixture
def tclock(clock: Clock):
    """The throttle's (and the profile run's) clock = the test clock."""
    with patch.object(throttle_mod, "time", ThrottleClock(clock)):
        yield clock


def _autostop_events(coord) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    coord.autostop.async_add_event_listener(lambda k, d: events.append((k, d)))
    return events


async def _later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


# ------------------------------------------------------------------ B1


def _brew_until(scale: Scale, weight_at_gap: float, flow: float = 2.0) -> None:
    """Timer on, the cup fills at ``flow`` g/s up to ``weight_at_gap``."""
    scale.status(DeviceState.TIMING)
    scale.timer_start = scale.clock.t
    seconds = weight_at_gap / flow + 2.0
    scale.run(seconds, lambda r: max(0.0, round((r - 2.0) * flow, 1)), flow)


async def test_b1_cut_at_once_after_a_ble_gap_past_the_cut_point(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """36 g target, 2 g/s: the link drops at 26 g, comes back 10 s later at
    ~50 g (same brew resumed): cut at once (was: the "untared cup" guard
    above target + 5 g blocked it and the machine ran on). Not learned."""
    calls = await _setup_autostop(hass, entry, PLUG, 36.0)
    coord = entry.runtime_data
    events = _autostop_events(coord)
    scale = Scale(coord, clock)
    scale.calls = calls
    _brew_until(scale, 26.0)
    start = coord.brew_tracker.start
    assert coord.autostop._cut is None and len(calls) == 0  # noqa: SLF001
    scale.disconnect()
    scale.gap(10.0)
    scale.connect()
    scale.status(DeviceState.TIMING)
    scale.run(0.3, lambda r: round(46.0 + r * 2.0, 1), 2.0)
    await hass.async_block_till_done()
    assert coord.brew_tracker.start == start  # the same brew resumed
    cut = coord.autostop._cut  # noqa: SLF001
    assert cut is not None and len(calls) == 1, "cut within 3 frames"
    assert cut.weight > 41.0 and cut.late and not cut.clean
    assert events[0][0] == "cut"


async def test_b1_new_brew_while_the_shot_runs_on_is_cut(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """The scale comes back after the park grace with its timer reset (a
    new tracker brew): the last brew was below its cut point, never cut,
    and the stop switch is still on -> the new brew is cut at once. With
    the switch off meanwhile (machine stopped by hand) nothing carries."""
    calls = await _setup_autostop(hass, entry, PLUG, 36.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    _brew_until(scale, 26.0)
    first = coord.brew_tracker.start
    scale.disconnect()
    scale.gap(35.0)  # beyond the 30 s park grace: closed as truncated
    assert coord.brew_tracker.start != first or not coord.brew_tracker.active
    scale.connect()
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t  # timer restarted
    scale.run(3.0, lambda r: round(60.0 + r * 2.0, 1), 2.0)
    await hass.async_block_till_done()
    assert coord.brew_tracker.start not in (None, first)
    assert len(calls) == 1 and coord.autostop._cut.late  # noqa: SLF001


async def test_b1_no_carry_after_the_switch_went_off(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, 36.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    _brew_until(scale, 26.0)
    scale.disconnect()
    hass.states.async_set(PLUG, "off", PLUG_ATTRS)  # stopped by hand
    await hass.async_block_till_done()
    scale.gap(35.0)
    hass.states.async_set(PLUG, "on", PLUG_ATTRS)  # next shot, a cup not tared
    await hass.async_block_till_done()
    scale.connect()
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(3.0, lambda r: round(60.0 + r * 2.0, 1), 2.0)
    await hass.async_block_till_done()
    assert calls == []  # the "untared cup" guard holds again


async def test_b1_stopped_below_then_untared_cup_is_not_cut(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Fix round 1: a shot stopped at the machine below the target (timer
    stopped, the plug stays on), then a new brew with an untared 180 g cup
    within 120 s: no cut (nothing carries over from a regular end)."""
    from test_features_050 import _stop_by_hand

    calls = await _setup_autostop(hass, entry, PLUG, 36.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    _brew_until(scale, 26.0)
    await _stop_by_hand(hass, scale)
    scale.run(5, 26.0)
    assert coord.brews.last is not None and calls == []
    scale.status(DeviceState.IDLE)
    scale.run(3, 0.0)
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(5.0, lambda r: round(180.0 + r * 2.0, 1), 2.0)
    await hass.async_block_till_done()
    assert calls == []


async def test_b1_stopped_below_then_normal_shot_learns(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Fix round 1: after a shot stopped below the target the next tared
    shot is cut normally (not late) and learned."""
    from test_features_050 import _stop_by_hand
    from test_integration import _auto_shot

    calls = await _setup_autostop(hass, entry, PLUG, 36.0)
    coord = entry.runtime_data
    events = _autostop_events(coord)
    scale = Scale(coord, clock)
    scale.calls = calls
    _brew_until(scale, 26.0)
    await _stop_by_hand(hass, scale)
    scale.status(DeviceState.IDLE)
    scale.run(3, 0.0)
    assert await _auto_shot(hass, scale, dose=None) is not None
    assert len(calls) == 1
    assert [k for k, _ in events] == ["cut", "learned"]
    assert coord.autostop.last["clean_cut"] is True


async def test_b1_untared_mug_after_a_gap_brew_is_not_cut(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Fix round 2 (N1): a gap brew at 2 g/s parked and closed, the plug
    stays on, then an untared 180 g mug: beyond what the shot could pour
    in the time (last flow x 1.5) -> no carry-over, no cut."""
    calls = await _setup_autostop(hass, entry, PLUG, 36.0)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    _brew_until(scale, 26.0)
    scale.disconnect()
    scale.gap(35.0)
    scale.connect()
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(3.0, lambda r: round(180.0 + r * 0.1, 1), 0.0)
    await hass.async_block_till_done()
    assert calls == []


async def test_b1_no_carry_after_the_switch_was_unavailable(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Fix round 2: the stop switch passed through unavailable -> on
    during the gap (not known to be the same relay start): no carry-over."""
    calls = await _setup_autostop(hass, entry, PLUG, 36.0, report="never")
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    _brew_until(scale, 26.0)
    scale.disconnect()
    hass.states.async_set(PLUG, "unavailable", {})
    await hass.async_block_till_done()
    scale.gap(35.0)
    hass.states.async_set(PLUG, "on", PLUG_ATTRS)
    await hass.async_block_till_done()
    scale.connect()
    scale.status(DeviceState.TIMING)
    scale.timer_start = clock.t
    scale.run(3.0, lambda r: round(60.0 + r * 2.0, 1), 2.0)
    await hass.async_block_till_done()
    assert calls == []


# ------------------------------------------------------------ B2 / B3


async def test_b3_pump_restored_only_after_the_relay_reports_off(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """A throttled shot: at the cut the pump stays at its throttled power
    until the relay reports off (was: 100 % in the same loop turn as the
    turn_off call, while the pump still ran)."""
    await _setup_throttle(hass, entry)
    plug = mock_plug(hass, report="never")
    pump = Pump(hass)
    coord = entry.runtime_data
    throttle = coord.autostop.throttle
    info = await _shot(hass, Scale(coord, tclock), tclock, pump=pump, seconds=32.0)
    assert "cut" in info and len(plug) == 1
    assert pump.percent < 100.0 and throttle.restore_held
    plug_reports_off(hass, PLUG, 0.3)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.percent == 100.0 and not throttle.restore_held
    record = throttle.last
    assert record["power"][-1][1] == 100.0 and record["end_reason"] == "cut"


async def test_b3_backstop_when_the_relay_never_reports(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_throttle(hass, entry)
    mock_plug(hass, report="never")
    pump = Pump(hass)
    coord = entry.runtime_data
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump, seconds=32.0)
    assert pump.percent < 100.0
    await _later(hass, 5.5)
    assert pump.percent == 100.0 and not coord.autostop.throttle.restore_held


async def test_b2_failed_cut_is_retried_and_pump_held(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """The turn_off call fails: ``switch_failed`` (attempt 1), the pump is
    NOT set back to 100 % (it still runs), the cut is retried after 1 s;
    the retry succeeds: ``cut`` (attempt 2); the relay's off report then
    releases the pump."""
    await _setup_throttle(hass, entry)
    pump = Pump(hass)
    calls: list[ServiceCall] = []
    fail = {"n": 2}

    async def _turn_off(call: ServiceCall) -> None:
        calls.append(call)
        if fail["n"] > 0:
            fail["n"] -= 1
            raise HomeAssistantError("plug offline")
        hass.states.async_set(PLUG, "off", PLUG_ATTRS)

    hass.services.async_register("switch", "turn_off", _turn_off)
    coord = entry.runtime_data
    events = _autostop_events(coord)
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump, seconds=32.0)
    assert len(calls) == 1
    assert [k for k, _ in events][:1] == ["switch_failed"]
    assert events[0][1]["attempt"] == 1
    assert pump.percent < 100.0 and coord.autostop.throttle.restore_held
    await _later(hass, 1.1)  # retry 2 fails too (debug only, no event)
    assert len(calls) == 2 and pump.percent < 100.0
    assert [k for k, _ in events].count("switch_failed") == 1
    await _later(hass, 1.1)  # retry 3 succeeds, the plug reports off
    assert len(calls) == 3
    cut_events = [d for k, d in events if k == "cut"]
    assert cut_events and cut_events[-1]["attempt"] == 3
    assert pump.percent == 100.0 and not coord.autostop.throttle.restore_held
    await _later(hass, 5.0)
    assert len(calls) == 3  # no more retries


async def test_b2_unavailable_switch_retried_when_back(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """The stop switch is unavailable at the cut: retried as soon as it
    shows ``on`` again (without waiting for the interval); bounded."""
    # An unavailable plug never reports off for the skipped call.
    calls = await _setup_autostop(hass, entry, PLUG, 36.0, report="never")
    coord = entry.runtime_data
    events = _autostop_events(coord)
    scale = Scale(coord, clock)
    scale.calls = calls
    _brew_until(scale, 30.0)
    hass.states.async_set(PLUG, "unavailable", {})
    await hass.async_block_till_done()
    scale.run(3.0, lambda r: round(31.0 + r * 2.0, 1), 2.0)
    await hass.async_block_till_done()
    assert events[0][0] == "switch_failed"
    n = len(calls)
    hass.states.async_set(PLUG, "on", PLUG_ATTRS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == n + 1
    assert [d.get("attempt") for k, d in events if k == "cut"] == [2]


async def test_b2_give_up_after_the_window_releases_and_reports(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Fix round 1: the cut keeps failing for 2 minutes -> ``cut_failed``
    (attempts), a repair issue, and the pump goes back to full power (never
    held low forever); the next shot is throttled normally again, and the
    switch's off report clears the issue."""
    from homeassistant.helpers import issue_registry as ir

    await _setup_throttle(hass, entry)
    pump = Pump(hass)
    calls: list[ServiceCall] = []

    async def _raise(call: ServiceCall) -> None:
        calls.append(call)
        raise HomeAssistantError("plug offline")

    hass.services.async_register("switch", "turn_off", _raise)
    coord = entry.runtime_data
    throttle = coord.autostop.throttle
    events = _autostop_events(coord)
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump, seconds=32.0)
    assert pump.percent < 100.0 and throttle.restore_held
    for _ in range(12):  # the fast retries, then the slow ones
        await _later(hass, 1.1)
    for _ in range(3):
        await _later(hass, 5.5)
    assert coord.autostop.cut_retry_pending and pump.percent < 100.0
    n = len(calls)
    assert n == 13  # 1 + 9 fast (1 s) + 3 slow (5 s)
    await _later(hass, 121.0)  # the window is over
    kinds = [k for k, _ in events]
    assert kinds.count("cut_failed") == 1 and kinds.count("switch_failed") == 1
    failed = next(d for k, d in events if k == "cut_failed")
    assert failed["attempt"] == len(calls)
    assert not coord.autostop.cut_retry_pending
    assert pump.percent == 100.0 and not throttle.restore_held
    issue_id = f"auto_stop_cut_failed_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    await _later(hass, 30.0)
    assert len(calls) == failed["attempt"]  # no more retries
    # The switch finally reports off: the issue goes.
    _relay(hass, False)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def _turns(n: int = 20) -> None:
    import asyncio

    for _ in range(n):
        await asyncio.sleep(0)


def _cut_issue(hass: HomeAssistant, entry):
    from homeassistant.helpers import issue_registry as ir

    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"auto_stop_cut_failed_{entry.entry_id}"
    )


async def test_b2_issue_at_the_first_failure_and_gone_on_success(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Fix round 2: the repair issue appears with the first failed attempt
    (the cup overflows long before the 2 minutes), says it keeps trying,
    and goes when a retry succeeds."""
    await _setup_throttle(hass, entry)
    Pump(hass)
    fail = {"n": 1}

    async def _turn_off(call: ServiceCall) -> None:
        if fail["n"] > 0:
            fail["n"] -= 1
            raise HomeAssistantError("plug offline")
        hass.states.async_set(PLUG, "off", PLUG_ATTRS)

    hass.services.async_register("switch", "turn_off", _turn_off)
    coord = entry.runtime_data
    await _shot(hass, Scale(coord, tclock), tclock, pump=None, seconds=32.0)
    issue = _cut_issue(hass, entry)
    assert issue is not None and issue.translation_key == "auto_stop_cut_failed_retrying"
    await _later(hass, 1.1)
    assert _cut_issue(hass, entry) is None


@pytest.mark.parametrize("pump", [True, False])
async def test_b2_give_up_text_names_the_pump_only_with_one(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock, pump
) -> None:
    await _setup_throttle(hass, entry, pump=pump)
    if pump:
        Pump(hass)

    async def _raise(call: ServiceCall) -> None:
        raise HomeAssistantError("plug offline")

    hass.services.async_register("switch", "turn_off", _raise)
    coord = entry.runtime_data
    await _shot(hass, Scale(coord, tclock), tclock, pump=None, seconds=32.0)
    await _later(hass, 121.0)
    issue = _cut_issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == (
        "auto_stop_cut_failed_pump" if pump else "auto_stop_cut_failed"
    )


async def test_b2_attempt_in_flight_at_the_window_end_decides(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Fix round 2 (N2): the window ends while an attempt's call is still
    running; that attempt succeeds -> ``cut``, no ``cut_failed``, no issue."""
    import asyncio

    await _setup_throttle(hass, entry)
    pump = Pump(hass)
    gate = asyncio.Event()
    calls: list[ServiceCall] = []

    async def _turn_off(call: ServiceCall) -> None:
        calls.append(call)
        if len(calls) == 1:
            raise HomeAssistantError("plug offline")
        await gate.wait()  # the reconnecting relay answers late
        hass.states.async_set(PLUG, "off", PLUG_ATTRS)

    hass.services.async_register("switch", "turn_off", _turn_off)
    coord = entry.runtime_data
    events = _autostop_events(coord)
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump, seconds=32.0)
    # (No async_block_till_done while the call hangs: it would wait for it.)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1.1))
    await _turns()
    assert len(calls) == 2  # attempt 2 in flight
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=121))
    await _turns()
    assert coord.autostop.cut_retry_pending  # waits for that attempt
    assert "cut_failed" not in [k for k, _ in events]
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    kinds = [k for k, _ in events]
    assert "cut_failed" not in kinds and kinds.count("cut") == 1
    assert _cut_issue(hass, entry) is None
    assert pump.percent == 100.0 and not coord.autostop.cut_retry_pending
    assert len(calls) == 2


async def test_b2_attempt_in_flight_at_the_window_end_fails_then_gives_up(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    import asyncio

    await _setup_throttle(hass, entry)
    pump = Pump(hass)
    gate = asyncio.Event()
    calls: list[ServiceCall] = []

    async def _turn_off(call: ServiceCall) -> None:
        calls.append(call)
        if len(calls) == 2:
            await gate.wait()
        raise HomeAssistantError("plug offline")

    hass.services.async_register("switch", "turn_off", _turn_off)
    coord = entry.runtime_data
    events = _autostop_events(coord)
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump, seconds=32.0)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1.1))
    await _turns()
    assert len(calls) == 2
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=121))
    await _turns()
    assert "cut_failed" not in [k for k, _ in events]
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [k for k, _ in events].count("cut_failed") == 1
    assert pump.percent == 100.0 and len(calls) == 2


async def test_b2_relay_back_after_30_s_is_still_cut(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Fix round 1: a relay that reconnects only after 30 s (beyond the
    fast attempts) is cut at once when it shows ``on``."""
    await _setup_throttle(hass, entry)
    plug = mock_plug(hass, report="never")
    pump = Pump(hass)
    coord = entry.runtime_data
    events = _autostop_events(coord)
    scale = Scale(coord, tclock)

    real_ok = autostop_mod.stop_switch_ok

    # The relay drops off just before the cut (no relay event: the shot
    # data stays as it is) and the call cannot reach it.
    unreachable = {"on": True}

    def _ok(hass_, entity_id):  # noqa: ANN001
        return False if unreachable["on"] else real_ok(hass_, entity_id)

    with patch.object(autostop_mod, "stop_switch_ok", _ok):
        await _shot(hass, scale, tclock, pump=pump, seconds=32.0)
        assert events[0][0] == "switch_failed"
        for _ in range(12):
            await _later(hass, 1.1)
        await _later(hass, 5.5)
        await _later(hass, 5.5)
        assert coord.autostop.cut_retry_pending and pump.percent < 100.0
        unreachable["on"] = False
        hass.states.async_set(PLUG, "unavailable", {})
        await hass.async_block_till_done()
        before = len(plug)
        _relay(hass, True)  # back: unavailable -> on
        await hass.async_block_till_done(wait_background_tasks=True)
    assert len(plug) == before + 1
    cut = [d for k, d in events if k == "cut"]
    assert cut and cut[-1]["attempt"] > 10
    assert not coord.autostop.cut_retry_pending
    assert pump.percent < 100.0  # held until off (or the backstop)
    await _later(hass, 5.5)
    assert pump.percent == 100.0


async def test_b3_relay_on_again_without_retry_releases(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Fix round 1: a successful cut, the relay goes unavailable and comes
    back ``on`` (no retry pending): the hold ends at once, not only at the
    backstop; held values were never counted as in effect."""
    await _setup_throttle(hass, entry)
    mock_plug(hass, report="never")
    pump = Pump(hass)
    coord = entry.runtime_data
    throttle = coord.autostop.throttle
    await _shot(hass, Scale(coord, tclock), tclock, pump=pump, seconds=32.0)
    assert throttle.restore_held
    brew = throttle._brew  # noqa: SLF001
    before = brew.command_value
    brew.ended = False  # a pump value asked for while held ...
    assert throttle._set_pump(brew, 60.0) is False  # noqa: SLF001
    assert brew.command_value == before  # ... is not counted as sent
    brew.ended = True
    hass.states.async_set(PLUG, "unavailable", {})
    await hass.async_block_till_done()
    assert throttle.restore_held
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not throttle.restore_held and pump.percent == 100.0


# ------------------------------------------------------------------ B4


async def test_b4_mode_change_mid_profile_shot_ends_it(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    from test_profiles_ha import _setup_profiles, _tare

    await _setup_profiles(hass, entry, profile="blooming")
    pump = Pump(hass)
    coord = entry.runtime_data
    profiles = coord.autostop.throttle.profiles
    _tare(Scale(coord, tclock))
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    for _ in range(60):
        tclock.t = round(tclock.t + 0.25, 3)
        profiles.tick()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.percent == 0.0
    coord.autostop.throttle.set_mode("throttle")  # unchanged: nothing ends
    assert profiles.current is not None
    coord.autostop.throttle.set_mode("off")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is None and profiles.last_run.end_reason == "mode"
    assert pump.percent == 100.0


# ------------------------------------------------------------------ B8


def _gering_frame(weight: float, timer: int) -> bytes:
    data = (
        int(round(weight * 10)).to_bytes(4, "big", signed=True)
        + (0).to_bytes(2, "big", signed=True)
        + int(timer).to_bytes(2, "big")
        + (0).to_bytes(4, "big")
        + b"\x02"  # unit "gering": no gram conversion
    )
    return build_frame(FUNC_DEVICE, CMD_DEVICE_SENSOR, data)


async def test_b8_unit_without_grams_mid_shot_restores_and_warns(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock, caplog
) -> None:
    await _setup_throttle(hass, entry)
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    await _shot(hass, scale, tclock, pump=pump, seconds=14.0)
    assert pump.percent < 100.0
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            coord._on_notification(None, bytearray(_gering_frame(20.0, scale.timer())))  # noqa: SLF001
            tclock.t = round(tclock.t + 0.1, 3)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.percent == 100.0
    assert coord.autostop.throttle._brew.end_reason == "no_weight"  # noqa: SLF001
    warnings = [r for r in caplog.records if "cannot be converted" in r.getMessage()]
    assert len(warnings) == 1


async def test_b8_unit_without_grams_ends_a_profile_run(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Fix round 1: without weight no auto-stop could end a profile shot:
    the run ends (``no_weight``), the pump goes back to full power."""
    from test_profiles_ha import _setup_profiles, _tare

    await _setup_profiles(hass, entry, profile="blooming")
    pump = Pump(hass)
    coord = entry.runtime_data
    profiles = coord.autostop.throttle.profiles
    scale = Scale(coord, tclock)
    _tare(scale)
    _relay(hass, True)
    await hass.async_block_till_done(wait_background_tasks=True)
    for _ in range(60):
        tclock.t = round(tclock.t + 0.25, 3)
        profiles.tick()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert pump.percent == 0.0
    coord._on_notification(None, bytearray(_gering_frame(0.0, 0)))  # noqa: SLF001
    await hass.async_block_till_done(wait_background_tasks=True)
    assert profiles.current is None and profiles.last_run.end_reason == "no_weight"
    assert pump.percent == 100.0


async def test_not_prepared_logged_once_at_info(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock, caplog
) -> None:
    from test_profiles_ha import _setup_profiles

    await _setup_profiles(hass, entry, profile="blooming")
    Pump(hass)
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            _relay(hass, True)
            await hass.async_block_till_done(wait_background_tasks=True)
            _relay(hass, False)
            await hass.async_block_till_done(wait_background_tasks=True)
    infos = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "skipped: 'Prepare brew' was off" in r.getMessage()
    ]
    assert len(infos) == 1


# ------------------------------------------------------------------ B9


async def test_b9_relearning_keeps_the_throttled_flag(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    autostop = entry.runtime_data.autostop
    start = "2026-11-24T08:00:00+00:00"
    assert autostop.async_learn(1.8, 1.6, None, start, throttled=True)
    assert autostop.journal.entries[-1]["throttled"] is True
    # Learned again (e.g. learn_stop_from_brew with another cut offset):
    # replaced in place, the flag stays.
    assert autostop.async_learn(1.9, 1.7, None, start)
    assert len(autostop.journal.entries) == 1
    assert autostop.journal.entries[0]["throttled"] is True


# ------------------------------------------------------------------ A*


async def test_a1_weight_jump_is_no_warning(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, caplog
) -> None:
    """A cup placed and lifted (0 -> 350 -> 0 g): no WARNING (and never the
    BLE address at warning level)."""
    await _setup(hass, entry)
    scale = Scale(entry.runtime_data, clock)
    with caplog.at_level(logging.DEBUG):
        scale.run(1.0, 0.0)
        scale.run(1.0, 350.0)
        scale.run(1.0, 0.0)
    assert not [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("custom_components")
    ]
    assert "weight jump" in caplog.text  # still there for debugging


async def test_a2_retries_use_the_best_path(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    from test_features_050 import ConnectClient

    await _setup(hass, entry)
    coord = entry.runtime_data
    device = type("Dev", (), {"name": "Microbalance", "address": ADDR})()
    better = type("Dev", (), {"name": "Microbalance", "address": ADDR})()
    establish = AsyncMock(return_value=ConnectClient())
    with (
        patch.object(
            coord_mod.bluetooth, "async_ble_device_from_address", return_value=device
        ),
        patch.object(coord_mod, "establish_connection", establish),
        patch.object(coord, "_async_after_connect", AsyncMock()),
    ):
        assert await coord._async_connect() is True  # noqa: SLF001
        callback = establish.call_args.kwargs["ble_device_callback"]
        assert callback() is device
    with patch.object(
        coord_mod.bluetooth, "async_ble_device_from_address", return_value=better
    ):
        assert callback() is better
    with patch.object(
        coord_mod.bluetooth, "async_ble_device_from_address", return_value=None
    ):
        assert callback() is device  # none known now: the one we had
    coord._stop_poll()  # noqa: SLF001


async def test_a3_firmware_read_on_every_connect(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    coord.device_info = {"serial": "S1", "model": "M1", "firmware": "V320"}
    seen: list[dict] = []

    async def _fetch() -> None:
        seen.append(dict(coord.device_info))

    with (
        patch.object(coord, "_async_write", AsyncMock()),
        patch.object(coord, "_async_fetch_device_info", _fetch),
    ):
        await coord._async_after_connect()  # noqa: SLF001
    assert seen == [{"serial": "S1", "model": "M1"}]  # firmware asked again


@pytest.mark.parametrize("button", ["timer_start", "timer_reset"])
async def test_a5_timer_buttons_never_click_blind(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, button
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    scale = Scale(coord, clock)  # no status answer (on_status unset)
    from custom_components.difluid_scale import button as button_mod

    press = {"timer_start": button_mod._timer_start, "timer_reset": button_mod._timer_reset}
    with (
        patch.object(coord, "async_request_status", AsyncMock(return_value=None)),
        pytest.raises(HomeAssistantError) as err,
    ):
        await press[button](coord)
    assert err.value.translation_key == "timer_state"
    assert CMD_TIMER_START not in scale.client.written


async def test_a6_reconfigure_moves_to_another_scale_and_keeps_everything(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup(hass, entry)
    coord = entry.runtime_data
    coord.autostop.set_target(33.0)
    await coord.autostop.async_flush()
    old_entry_id = entry.entry_id
    ent_reg = er.async_get(hass)
    weight_entity = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{ADDR}_weight")
    assert weight_entity == "sensor.microbalance_weight"
    new = "11:22:33:44:55:66"
    info = type(
        "Info", (), {"address": new, "name": "Microbalance 2", "service_uuids": []}
    )()
    with patch(
        "custom_components.difluid_scale.config_flow.async_discovered_service_info",
        return_value=[info],
    ):
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] == "form" and result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"address": new}
        )
    await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert entry.unique_id == new and entry.data["address"] == new
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{new}_weight") == weight_entity
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{ADDR}_weight") is None
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.identifiers for d in devices] == [{(DOMAIN, new)}]
    coord = entry.runtime_data
    assert coord.address == new and entry.entry_id == old_entry_id
    assert coord.autostop.target == 33.0  # the per-entry store is kept


async def test_a8_wall_clock_step_mid_brew_keeps_the_duration(
    hass: HomeAssistant, entry, no_ble, lovelace
) -> None:
    """NTP steps the wall clock +1 h mid-brew: the stored duration is the
    real one (monotonic inside the brew); the start stays wall time."""

    class SteppingClock(Clock):
        def __init__(self) -> None:
            super().__init__()
            self.offset = 0.0

        def time(self) -> float:
            return self.t + self.offset

    clock = SteppingClock()
    with patch.object(coord_mod, "time", clock):
        await _setup(hass, entry)
        coord = entry.runtime_data
        scale = Scale(coord, clock)
        scale.weigh_dose(18.0)
        start_wall = clock.time()
        scale.status(DeviceState.TIMING)
        scale.timer_start = clock.t
        scale.run(12, lambda r: max(0.0, round((r - 3) * 1.8, 1)), 1.8)
        clock.offset = 3600.0  # the step
        scale.run(13, lambda r: min(36.0, round((r + 9) * 1.8, 1)), 1.8)
        scale.frozen = scale.timer()
        scale.status(DeviceState.TIMER_PAUSE)
        scale.run(6, 36.2)
        await hass.async_block_till_done()
        brew = coord.brews.last
        assert brew is not None
        assert 23.0 <= brew["duration"] <= 27.0, brew["duration"]
        stored = dt_util.parse_datetime(brew["start"]).timestamp()
        assert abs(stored - start_wall) < 2.0
        # After the brew the brew clock follows the wall clock again.
        scale.run(1, 0.0)
        assert abs(coord._now() - clock.time()) < 1e-6  # noqa: SLF001


# ------------------------------------------------- websocket, two scales


async def test_websocket_read_only_user(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, hass_ws_client,
    hass_read_only_access_token,
) -> None:
    """The card's commands are read-only: a non-admin user may use them."""
    await _setup(hass, entry)
    ws = await hass_ws_client(hass, hass_read_only_access_token)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/brews"})
    msg = await ws.receive_json()
    assert msg["success"], msg
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/subscribe_live"})
    msg = await ws.receive_json()
    assert msg["success"], msg


async def test_websocket_two_scales_need_entry_id(
    hass: HomeAssistant, entry, no_ble, lovelace, clock, hass_ws_client
) -> None:
    other = MockConfigEntry(
        domain=DOMAIN, data={"address": "11:22:33:44:55:66"},
        title="Microbalance 2", unique_id="11:22:33:44:55:66",
    )
    other.add_to_hass(hass)
    await _setup(hass, entry)
    await hass.async_block_till_done()
    assert other.runtime_data is not None
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/brews"})
    msg = await ws.receive_json()
    assert not msg["success"]  # ambiguous without entry_id
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/brews", "entry_id": other.entry_id})
    msg = await ws.receive_json()
    assert msg["success"] and msg["result"]["entry_id"] == other.entry_id

