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
        "beans": autostop.beans,
        "brews": {
            "brew_count": coordinator.brews.brew_count,
            "coffee_total": round(coordinator.brews.coffee_total, 1),
            "last": coordinator.brews.summaries()[:LAST_BREWS],
        },
    }

