"""Stop switch latency (0.5.1): measured per cut, taken out of learning.

The stop switch is a non-optimistic smart plug like the real one
(``switch.kuche_steckdose``, Zigbee2MQTT, live attributes
``{"friendly_name": "Kaffeemaschine"}``, briefly ``unknown`` after an HA
restart): its state becomes ``off`` only when the plug reports. Measured
2026-11-23: median 68 ms, p95 306 ms, outliers 0.3-3.1 s (one auto-stop
brew at 459 ms).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, ServiceCall  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    async_fire_time_changed,
)

from test_features_050 import AUTO_STOP_EVENT, _events  # noqa: E402
from test_integration import (  # noqa: E402
    PLUG,
    REAL_BREW,
    Scale,
    _auto_shot,
    _setup,
    _setup_autostop,
    _state,
    _store_brews,
    mock_plug,
    plug_reports_off,
)

from custom_components.difluid_scale.const import (  # noqa: E402
    STOP_STORAGE_KEY,
    STORAGE_KEY,
)
from custom_components.difluid_scale.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)
from custom_components.difluid_scale.stop_model import (  # noqa: E402
    SWITCH_LATENCY_DEFAULT,
    Cut,
    StopModel,
    adjusted_overshoot,
)

LATENCY_SENSOR = "sensor.microbalance_last_switch_latency"
# Live attributes of the real stop switch (2026-11-23).
LIVE_PLUG_ATTRS = {"friendly_name": "Kaffeemaschine"}
DOMAIN = "difluid_scale"


def _cut(start: float = 1.0) -> Cut:
    return Cut(
        start=start, t=start + 25.0, weight=35.7, flow=2.0, target=38.0,
        predicted=2.3, latency_pending=True,
    )


# ------------------------------------------------------- full shots


async def test_report_after_call_measured_stored_and_learned(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """The common case (~68 ms): the call returns at once, the plug reports
    off shortly after, before the brew is stored."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0, latency=0.068)
    coord = entry.runtime_data
    events = _events(hass, AUTO_STOP_EVENT)
    assert _state(hass, LATENCY_SENSOR) == "unknown"
    attrs = hass.states.get(LATENCY_SENSOR).attributes
    assert attrs["nominal_switch_latency"] == SWITCH_LATENCY_DEFAULT
    assert attrs["measurements"] == 0
    scale = Scale(coord, clock)
    assert await _auto_shot(hass, scale) is not None
    last = coord.autostop.last
    assert calls.reports == [True]  # reported after the call returned
    assert last["accepted"] and last["switch_latency"] == 0.068, last
    assert last["nominal_switch_latency"] == SWITCH_LATENCY_DEFAULT
    # Corrected by flow x (0.068 - 0.07): -2 ms x ~2 g/s.
    expected = adjusted_overshoot(
        last["overshoot"], last["cut_flow"], 0.068, SWITCH_LATENCY_DEFAULT
    )
    assert abs(last["adjusted_overshoot"] - expected) <= 0.011, last
    assert coord.brews.last["switch_latency"] == 0.068
    assert "switch_latency_unknown" not in coord.brews.last
    assert _state(hass, LATENCY_SENSOR) == "0.068"
    assert hass.states.get(LATENCY_SENSOR).attributes["measurements"] == 1
    types = [s.attributes["event_type"] for s in events]
    assert types == ["cut", "learned"], types
    # The cut event fires when the call returns: no latency there.
    assert "switch_latency" not in events[0].attributes
    assert events[1].attributes["switch_latency"] == 0.068
    # Diagnostics.
    diag = await async_get_config_entry_diagnostics(hass, entry)
    lat = diag["auto_stop"]["switch_latency"]
    assert lat["last"] == 0.068 and lat["values"] == [0.068]
    assert lat["entity_id"] == PLUG and lat["nominal"] == SWITCH_LATENCY_DEFAULT

    # Persisted with the model; restored after a reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    data = hass_storage[STOP_STORAGE_KEY.format(entry.entry_id)]["data"]
    assert data["switch_latency"] == {
        "entity_id": PLUG, "values": [0.068], "unknown_streak": 0, "last": 0.068,
    }
    brews = hass_storage[STORAGE_KEY.format(entry.entry_id)]["data"]["brews"]
    assert brews[-1]["switch_latency"] == 0.068
    coord = entry.runtime_data
    assert list(coord.autostop.latencies.values) == [0.068]
    assert _state(hass, LATENCY_SENSOR) == "0.068"


async def test_report_during_call_is_measured(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """A report that arrives before the call returns is not missed."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0, latency=0.05, report="during")
    coord = entry.runtime_data
    assert await _auto_shot(hass, Scale(coord, clock)) is not None
    assert calls.reports == [False]
    assert coord.autostop.last["switch_latency"] == 0.05
    assert coord.autostop.last["accepted"]


async def test_late_report_after_brew_stored_learns_corrected(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Report after the call returned, even after the brew was stored: the
    learning waits for it, the stored brew gets the latency afterwards."""
    await _setup_autostop(hass, entry, PLUG, 38.0, report="never")
    coord = entry.runtime_data
    events = _events(hass, AUTO_STOP_EVENT)
    scale = Scale(coord, clock)
    assert await _auto_shot(hass, scale) is not None
    # Brew stored, cut not concluded yet (waits for the switch).
    assert coord.brews.brew_count == 1
    assert coord.brews.last["switch_latency"] is None
    assert coord.autostop.last is None
    assert [s.attributes["event_type"] for s in events] == ["cut"]
    plug_reports_off(hass, PLUG, 0.459)
    await hass.async_block_till_done()
    last = coord.autostop.last
    assert last["accepted"] and last["switch_latency"] == 0.459, last
    assert coord.brews.last["switch_latency"] == 0.459
    expected = adjusted_overshoot(
        last["overshoot"], last["cut_flow"], 0.459, SWITCH_LATENCY_DEFAULT
    )
    # The pump ran 0.389 s longer than nominal at ~2 g/s: ~0.78 g less.
    assert last["overshoot"] - last["adjusted_overshoot"] > 0.7, last
    assert abs(last["adjusted_overshoot"] - expected) <= 0.011, last
    assert events[-1].attributes["event_type"] == "learned"
    assert events[-1].attributes["switch_latency"] == 0.459
    assert coord.autostop._probe is None  # noqa: SLF001


async def test_no_report_times_out_and_is_not_learned(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_autostop(hass, entry, PLUG, 38.0, report="never")
    coord = entry.runtime_data
    events = _events(hass, AUTO_STOP_EVENT)
    scale = Scale(coord, clock)
    assert await _auto_shot(hass, scale) is not None
    assert coord.autostop.last is None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5.1))
    await hass.async_block_till_done()
    last = coord.autostop.last
    assert last["accepted"] is False, last
    assert last["reason"] == "switch_latency_unknown"
    assert last["switch_latency"] is None and last["adjusted_overshoot"] is None
    assert coord.autostop.model.count == 0
    # Stored before the timeout; marked once the measurement ended empty.
    assert coord.brews.last["switch_latency"] is None
    assert coord.brews.last["switch_latency_unknown"] is True
    assert _state(hass, LATENCY_SENSOR) == "unknown"
    types = [s.attributes["event_type"] for s in events]
    assert types == ["cut", "switch_latency_unknown"], types
    assert events[-1].attributes["switch_latency"] is None
    # Listener and timer are gone: a late report changes nothing.
    assert coord.autostop._probe is None  # noqa: SLF001
    plug_reports_off(hass, PLUG, 6.0)
    await hass.async_block_till_done()
    assert coord.autostop.last_switch_latency is None
    assert len(coord.autostop.latencies.values) == 0
    assert coord.autostop.latencies.unknown_streak == 1


async def test_outlier_latency_learns_the_same_tau_as_nominal(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """A shot whose plug took 0.47 s (pump ran 0.4 s longer) teaches the
    same tau as the same shot at the nominal 0.07 s. (Pump latencies
    between the 0.1 s simulation steps: 4 steps = 0.4 s apart.)"""
    await _setup_autostop(hass, entry, PLUG, 38.0, latency=0.47)
    coord = entry.runtime_data
    stop = coord.autostop
    scale = Scale(coord, clock)
    await _auto_shot(hass, scale, latency=1.65)
    slow = dict(stop.last)
    tau_slow = stop.model.tau
    assert slow["accepted"] and slow["switch_latency"] == 0.47
    stop.async_reset()

    mock_plug(hass, latency=0.07)
    scale = Scale(coord, clock)
    await _auto_shot(hass, scale, latency=1.25)
    nominal = dict(stop.last)
    tau_nominal = stop.model.tau
    assert nominal["accepted"] and nominal["switch_latency"] == 0.07
    # Same cut (prior model both times), 0.8 g more overshoot.
    assert slow["cut_weight"] == nominal["cut_weight"]
    assert slow["cut_flow"] == nominal["cut_flow"]
    assert abs(slow["overshoot"] - nominal["overshoot"] - 0.8) <= 0.11
    assert abs(tau_slow - tau_nominal) < 1e-6, (tau_slow, tau_nominal)
    # Uncorrected, the slow shot would have taught a clearly larger tau.
    raw = StopModel()
    assert raw.update(slow["cut_flow"], slow["overshoot"])
    assert raw.tau - tau_nominal > 0.15, (raw.tau, tau_nominal)


# --------------------------------------------- measurement edge cases


async def test_measurement_none_when_switch_off_or_unavailable(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Already off, or unavailable: the call is still made, the latency is
    unknown at once (no streak: the switch did not fail to report)."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    stop = entry.runtime_data.autostop
    for state in ("off", "unavailable"):
        hass.states.async_set(PLUG, state, LIVE_PLUG_ATTRS)
        cut = _cut()
        await stop._async_switch_off(PLUG, cut)  # noqa: SLF001
        await hass.async_block_till_done()
        assert not cut.latency_pending and cut.switch_latency is None, state
        assert stop._probe is None  # noqa: SLF001
    assert len(calls) == 2
    assert len(stop.latencies.values) == 0
    assert stop.latencies.unknown_streak == 0


async def test_unknown_after_restart_is_measured(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Briefly ``unknown`` after an HA restart while a shot runs: measured,
    the off report is the answer to the call."""
    await _setup_autostop(hass, entry, PLUG, 38.0, latency=0.09)
    stop = entry.runtime_data.autostop
    hass.states.async_set(PLUG, "unknown", LIVE_PLUG_ATTRS)
    cut = _cut()
    await stop._async_switch_off(PLUG, cut)  # noqa: SLF001
    await hass.async_block_till_done()
    assert not cut.latency_pending and cut.switch_latency == 0.09
    assert list(stop.latencies.values) == [0.09]


async def test_unavailable_at_cut_full_shot(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    events = _events(hass, AUTO_STOP_EVENT)
    hass.states.async_set(PLUG, "unavailable", LIVE_PLUG_ATTRS)
    assert await _auto_shot(hass, Scale(coord, clock)) is not None
    types = [s.attributes["event_type"] for s in events]
    assert types == ["switch_failed", "switch_latency_unknown"], types
    assert coord.autostop.model.count == 0
    assert coord.autostop._probe is None  # noqa: SLF001
    assert coord.brews.last["switch_latency_unknown"] is True
    assert coord.autostop.latencies.unknown_streak == 0


async def test_cup_lifted_while_latency_pending(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Concluded before the switch reported: the conclusion carries no
    latency, the later report still lands in ``last`` and the history."""
    await _setup_autostop(hass, entry, PLUG, 38.0, report="never")
    coord = entry.runtime_data
    events = _events(hass, AUTO_STOP_EVENT)
    assert await _auto_shot(hass, Scale(coord, clock), lift_after_cut=0.5) is not None
    last = coord.autostop.last
    assert last["reason"] == "cup_lifted" and last["switch_latency"] is None, last
    assert events[-1].attributes["event_type"] == "cup_lifted"
    assert events[-1].attributes["switch_latency"] is None
    plug_reports_off(hass, PLUG, 0.3)
    await hass.async_block_till_done()
    assert coord.autostop.last["switch_latency"] == 0.3
    assert coord.autostop.last["reason"] == "cup_lifted"
    assert list(coord.autostop.latencies.values) == [0.3]
    assert coord.autostop.model.count == 0


async def test_second_cut_ends_pending_probe_unknown(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_autostop(hass, entry, PLUG, 38.0, report="never")
    stop = entry.runtime_data.autostop
    hass.states.async_set(PLUG, "on", LIVE_PLUG_ATTRS)
    first = _cut(1.0)
    await stop._async_switch_off(PLUG, first)  # noqa: SLF001
    assert first.latency_pending
    old_probe = stop._probe  # noqa: SLF001
    second = _cut(100.0)
    await stop._async_switch_off(PLUG, second)  # noqa: SLF001
    assert not first.latency_pending and first.switch_latency is None
    assert old_probe.done
    assert second.latency_pending and stop._probe is not old_probe  # noqa: SLF001
    plug_reports_off(hass, PLUG, 0.07)
    await hass.async_block_till_done()
    assert second.switch_latency == 0.07 and first.switch_latency is None


async def test_repair_issue_after_three_unknown_latencies(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Learning would stop silently for a switch that never reports in
    time: after 3 such cuts in a row a repair issue, cleared by the next
    measured latency; kept over a reload."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0, report="never")
    stop = entry.runtime_data.autostop
    registry = ir.async_get(hass)
    issue_id = f"stop_switch_latency_unknown_{entry.entry_id}"

    async def _timed_out_cut(start: float) -> None:
        hass.states.async_set(PLUG, "on", LIVE_PLUG_ATTRS)
        cut = _cut(start)
        await entry.runtime_data.autostop._async_switch_off(PLUG, cut)  # noqa: SLF001
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5.1))
        await hass.async_block_till_done()
        assert cut.switch_latency is None and not cut.latency_pending

    for n in (1, 2):
        await _timed_out_cut(float(n))
        assert registry.async_get_issue(DOMAIN, issue_id) is None
    await _timed_out_cut(3.0)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.translation_key == "stop_switch_latency_unknown"
    assert issue.translation_placeholders == {
        "entity_id": PLUG, "name": "Microbalance", "count": "3",
    }
    assert stop.latencies.unknown_streak == 3
    # Kept over a reload (streak persisted).
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    assert entry.runtime_data.autostop.latencies.unknown_streak == 3
    # The next measured latency clears it.
    mock_plug(hass, latency=0.07)
    hass.states.async_set(PLUG, "on", LIVE_PLUG_ATTRS)
    cut = _cut(4.0)
    await entry.runtime_data.autostop._async_switch_off(PLUG, cut)  # noqa: SLF001
    await hass.async_block_till_done()
    assert cut.switch_latency == 0.07
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert entry.runtime_data.autostop.latencies.unknown_streak == 0
    assert len(calls) == 3
    # Unload clears an open issue too.
    entry.runtime_data.autostop.latencies.unknown_streak = 3
    entry.runtime_data.autostop._async_update_latency_issue()  # noqa: SLF001
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_measurement_failed_call_is_none_even_after_report(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_autostop(hass, entry, PLUG, 38.0)
    stop = entry.runtime_data.autostop

    async def _report_then_raise(call: ServiceCall) -> None:
        plug_reports_off(hass, PLUG, 0.05)
        raise HomeAssistantError("plug offline")

    hass.services.async_register("switch", "turn_off", _report_then_raise)
    hass.states.async_set(PLUG, "on", LIVE_PLUG_ATTRS)
    cut = _cut()
    await stop._async_switch_off(PLUG, cut)  # noqa: SLF001
    await hass.async_block_till_done()
    assert not cut.latency_pending and cut.switch_latency is None
    assert stop._probe is None  # noqa: SLF001


async def test_measurement_report_after_call_return(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_autostop(hass, entry, PLUG, 38.0, report="never")
    stop = entry.runtime_data.autostop
    hass.states.async_set(PLUG, "on", LIVE_PLUG_ATTRS)
    cut = _cut()
    await stop._async_switch_off(PLUG, cut)  # noqa: SLF001
    assert cut.latency_pending  # call returned, no report yet
    # Something else first (attribute update), then the off report.
    hass.states.async_set(PLUG, "on", {**LIVE_PLUG_ATTRS, "power": 1200})
    await hass.async_block_till_done()
    assert cut.latency_pending
    plug_reports_off(hass, PLUG, 0.306)
    await hass.async_block_till_done()
    assert not cut.latency_pending and cut.switch_latency == 0.306
    assert stop.last_switch_latency == 0.306
    assert list(stop.latencies.values) == [0.306]


async def test_unload_during_measurement_drops_listener(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_autostop(hass, entry, PLUG, 38.0, report="never")
    stop = entry.runtime_data.autostop
    hass.states.async_set(PLUG, "on", LIVE_PLUG_ATTRS)
    cut = _cut()
    await stop._async_switch_off(PLUG, cut)  # noqa: SLF001
    probe = stop._probe  # noqa: SLF001
    assert probe is not None and not probe.done
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert probe.done and stop._probe is None  # noqa: SLF001
    plug_reports_off(hass, PLUG, 0.1)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert cut.latency_pending and stop.last_switch_latency is None


# -------------------------------------------------- storage, service


async def test_old_stores_load_without_switch_latency(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """Stores from before 0.5.1: no ``switch_latency`` anywhere."""
    _store_brews(hass_storage, entry)
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    hass_storage[key] = {
        "version": 1, "minor_version": 2, "key": key,
        "data": {
            "model": {"tau": 1.3, "c": 0.3, "p": [[0.5, 0], [0, 1]], "count": 4},
            "last": {"target": 38.0, "accepted": True, "reason": None},
            "settings": {"target_ratio": 0, "target_yield": 38.0, "bean_id": None},
        },
    }
    await _setup(hass, entry)
    coord = entry.runtime_data
    assert coord.autostop.model.count == 4
    assert len(coord.autostop.latencies.values) == 0
    assert coord.autostop.nominal_switch_latency == SWITCH_LATENCY_DEFAULT
    assert _state(hass, LATENCY_SENSOR) == "unknown"
    assert "switch_latency" not in coord.brews.get("01REAL")
    assert "switch_latency_unknown" not in coord.brews.get("01REAL")
    assert _state(hass, "sensor.microbalance_last_yield") == "36.0"


async def test_learn_stop_service_switch_latency(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    key = _store_brews(hass_storage, entry)
    hass_storage[key]["data"]["brews"].extend([
        dict(REAL_BREW, id="01MEASURED", switch_latency=0.459),
        dict(REAL_BREW, id="01UNKNOWN", switch_latency=None,
             switch_latency_unknown=True),
    ])
    await _setup(hass, entry)
    stop = entry.runtime_data.autostop

    async def _learn(**data: Any) -> dict[str, Any]:
        stop.async_reset()
        return await hass.services.async_call(
            DOMAIN, "learn_stop_from_brew", {"cut_offset": 27.83, **data},
            blocking=True, return_response=True,
        )

    # Old brew, no latency: learned as it is (as before 0.5.1).
    old = await _learn(brew_id="01REAL")
    assert old["switch_latency"] is None and old["accepted"]
    assert old["adjusted_overshoot"] == old["overshoot"] == 1.8
    assert old["nominal_switch_latency"] == SWITCH_LATENCY_DEFAULT
    tau_old = stop.model.tau
    # Old brew, latency known from the recorder: corrected.
    given = await _learn(brew_id="01REAL", switch_latency=0.459)
    flow = given["flow_at_cut"]
    assert given["switch_latency"] == 0.459 and given["accepted"]
    assert given["adjusted_overshoot"] == round(1.8 - flow * (0.459 - 0.07), 2)
    assert stop.model.tau < tau_old
    # Stored latency used without a parameter ...
    stored = await _learn(brew_id="01MEASURED")
    assert stored["switch_latency"] == 0.459
    assert stored["adjusted_overshoot"] == given["adjusted_overshoot"]
    # ... and the parameter wins over it.
    override = await _learn(brew_id="01MEASURED", switch_latency=0.07)
    assert override["switch_latency"] == 0.07
    assert override["adjusted_overshoot"] == 1.8
    # Measured without a value (0.5.1+): not learned, like live ...
    refused = await _learn(brew_id="01UNKNOWN")
    assert refused["accepted"] is False
    assert refused["reason"] == "switch_latency_unknown"
    assert refused["switch_latency"] is None and refused["adjusted_overshoot"] is None
    assert stop.model.count == 0
    # ... unless the latency is given.
    given_unknown = await _learn(brew_id="01UNKNOWN", switch_latency=0.459)
    assert given_unknown["accepted"] and stop.model.count == 1
    assert given_unknown["adjusted_overshoot"] == given["adjusted_overshoot"]
    # Out of range (above the measurement timeout): rejected.
    with pytest.raises(Exception):  # noqa: B017 - vol.Invalid wrapped by HA
        await _learn(brew_id="01REAL", switch_latency=6.0)
