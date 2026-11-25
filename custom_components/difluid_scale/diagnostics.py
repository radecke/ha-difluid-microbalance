"""Diagnostics for the DiFluid Microbalance (BLE address redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .coordinator import INFO_SERIAL, DifluidConfigEntry
from .issues import entry_name

TO_REDACT = {CONF_ADDRESS, INFO_SERIAL}
LAST_BREWS = 5
# Shot timing of the last brews with their profiles: enough to backtest the
# shot-time prediction without a copy of the store (0.6.0).
TIMING_BREWS = 10
REDACTED = "**REDACTED**"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DifluidConfigEntry
) -> dict[str, Any]:
    """Options, stop model, last auto-stop result, device, beans, brews."""
    coordinator = entry.runtime_data
    autostop = coordinator.autostop
    tracker = coordinator.brew_tracker
    model = autostop.model
    status = coordinator.data.status
    return {
        "entry": {
            # The fallback title of a manual setup is the BLE address.
            "title": entry.title if entry_name(entry) == entry.title else REDACTED,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "device": {
            "info": async_redact_data(dict(coordinator.device_info), TO_REDACT),
            "connected": coordinator.connected,
            "status": None
            if status is None
            else {
                "state": status.state_name,
                "battery": status.battery,
                "charging": status.charging,
            },
            "settings": {str(k): v for k, v in coordinator.data.settings.items()},
        },
        "brew_tracker": {"state": tracker.state, "brew_ready": coordinator.brew_ready},
        "auto_stop": {
            "enabled": autostop.enabled,
            "stop_switch": autostop.switch_entity,
            "settings": autostop.settings(),
            "active_bean": autostop.active_bean_name,
            "model": {
                "tau": model.tau,
                "c": model.c,
                "p": model.p,
                "count": model.count,
            },
            "journal": {
                "samples": len(autostop.journal),
                "base_count": autostop.journal.base.count,
                "entries": autostop.journal.entries,
            },
            "last": autostop.last,
            "switch_latency": {
                "last": autostop.last_switch_latency,
                "nominal": autostop.nominal_switch_latency,
                **autostop.latencies.to_dict(),
            },
        },
        "throttle": _throttle(coordinator),
        "beans": autostop.beans,
        "brews": {
            "brew_count": coordinator.brews.brew_count,
            "coffee_total": round(coordinator.brews.coffee_total, 1),
            "last": coordinator.brews.summaries()[:LAST_BREWS],
            "shot_timing": [
                {
                    "start": brew.get("start"),
                    "duration": brew.get("duration"),
                    "yield": brew.get("yield"),
                    "target": brew.get("target"),
                    "first_drop": (brew.get("metrics") or {}).get("first_drop"),
                    "relay_offset": brew.get("relay_offset"),
                    "first_drop_shot": brew.get("first_drop_shot"),
                    "shot_time": brew.get("shot_time"),
                    "since_last_relay_off": brew.get("since_last_relay_off"),
                    "throttle": brew.get("throttle"),
                    "profile": brew.get("profile"),
                }
                for brew in list(reversed(coordinator.brews.brews))[:TIMING_BREWS]
            ],
        },
    }


def _throttle(coordinator: Any) -> dict[str, Any]:
    throttle = coordinator.autostop.throttle
    pump = throttle.pump_entity
    state = coordinator.hass.states.get(pump) if pump else None
    return {
        "pump_entity": pump,
        "pump_state": None
        if state is None
        else {
            "state": state.state,
            "min": state.attributes.get("min"),
            "max": state.attributes.get("max"),
            "step": state.attributes.get("step"),
        },
        "settings": throttle.to_store()["settings"],
        "effective_target_time": throttle.effective_target_time(),
        "model": throttle.model.to_dict(),
        "journal": {
            "samples": len(throttle.journal),
            "base_count": throttle.journal.base.count,
            "entries": throttle.journal.entries,
        },
        "prediction_exponent": {
            "value": throttle.prediction_exponent,
            "samples": throttle.exponent.to_list(),
            "throttled_samples": throttle.exponent_throttled.to_list(),
            "recent": throttle.exponent_recent,
        },
        "last": throttle.last,
        "last_relay_off": throttle.last_relay_off,
        "throttled_deviations": coordinator.autostop.throttled_deviations,
        # Pump profiles (0.7.0): only the active label and the count of
        # imported profiles (their contents are the user's).
        "profiles": {
            "active": throttle.profiles.active.label,
            "user_profiles": len(throttle.profiles.library.user),
        },
    }

