"""Services: delete a stored brew, learn the auto-stop from a past brew."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import DifluidConfigEntry
from .stop_model import profile_cut
from .websocket import resolve_entry

SERVICE_DELETE_BREW = "delete_brew"
SERVICE_LEARN_STOP = "learn_stop_from_brew"

ATTR_ENTRY_ID = "entry_id"
ATTR_DEVICE_ID = "device_id"
ATTR_BREW_ID = "brew_id"
ATTR_CUT_OFFSET = "cut_offset"
ATTR_TARGET = "target"

# Same targeting as the websocket API: entry_id or device_id, or neither
# when exactly one scale is set up.
_TARGET = {
    vol.Optional(ATTR_ENTRY_ID): cv.string,
    vol.Optional(ATTR_DEVICE_ID): cv.string,
}
DELETE_BREW_SCHEMA = vol.Schema({**_TARGET, vol.Required(ATTR_BREW_ID): cv.string})
LEARN_STOP_SCHEMA = vol.Schema(
    {
        **_TARGET,
        vol.Required(ATTR_BREW_ID): cv.string,
        vol.Required(ATTR_CUT_OFFSET): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional(ATTR_TARGET): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)


def _entry(hass: HomeAssistant, call: ServiceCall) -> DifluidConfigEntry:
    entry = resolve_entry(hass, call.data)
    if entry is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_scale"
        )
    return entry


def _brew(entry: DifluidConfigEntry, brew_id: str) -> dict[str, Any]:
    brew = entry.runtime_data.brews.get(brew_id)
    if brew is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_brew",
            translation_placeholders={"brew_id": brew_id},
        )
    return brew


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the services (once, from async_setup)."""

    @callback
    def _delete_brew(call: ServiceCall) -> None:
        entry = _entry(hass, call)
        brew_id = call.data[ATTR_BREW_ID]
        _brew(entry, brew_id)
        entry.runtime_data.brews.async_delete(brew_id)

    @callback
    def _learn_stop(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call)
        brew_id = call.data[ATTR_BREW_ID]
        brew = _brew(entry, brew_id)
        offset = call.data[ATTR_CUT_OFFSET]
        cut = profile_cut(brew.get("profile") or [], offset)
        final = brew.get("yield")
        if cut is None or final is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="cut_outside_profile",
                translation_placeholders={"cut_offset": str(offset)},
            )
        cut_t, weight, flow = cut
        autostop = entry.runtime_data.autostop
        predicted = autostop.model.overshoot(flow)
        overshoot = final - weight
        accepted = autostop.async_learn(flow, overshoot)
        target = call.data.get(ATTR_TARGET)
        model = autostop.model
        return {
            "brew_id": brew_id,
            "cut_offset": offset,
            "cut_point_t": cut_t,
            "weight_at_cut": weight,
            "flow_at_cut": round(flow, 3),
            "final_yield": final,
            "overshoot": round(overshoot, 2),
            "predicted_overshoot_before": round(predicted, 2),
            "target": target,
            "deviation": None if target is None else round(final - target, 1),
            "accepted": accepted,
            "reason": None if accepted else "outlier",
            "model": {
                "tau": round(model.tau, 3),
                "c": round(model.c, 3),
                "count": model.count,
            },
        }

    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_BREW, _delete_brew, schema=DELETE_BREW_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LEARN_STOP,
        _learn_stop,
        schema=LEARN_STOP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
