"""Services: delete a stored brew, learn the auto-stop from a past brew,
add and delete bean profiles (e.g. from a dashboard)."""

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
from homeassistant.util.ulid import ulid_now

from .beans import (
    BEAN_ID,
    BEAN_NAME,
    BEAN_RATIO,
    RATIO_MAX,
    RATIO_MIN,
    find_bean,
    NAME_MAX,
    find_bean_by_name,
    make_bean,
    name_error,
)
from .const import CONF_BEANS, DOMAIN
from .coordinator import DifluidConfigEntry
from .stop_model import plausible_flow, profile_cut
from .websocket import resolve_entry

SERVICE_DELETE_BREW = "delete_brew"
SERVICE_LEARN_STOP = "learn_stop_from_brew"
SERVICE_ADD_BEAN = "add_bean"
SERVICE_DELETE_BEAN = "delete_bean"

ATTR_ENTRY_ID = "entry_id"
ATTR_DEVICE_ID = "device_id"
ATTR_BREW_ID = "brew_id"
ATTR_CUT_OFFSET = "cut_offset"
ATTR_TARGET = "target"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_NAME = "name"
ATTR_RATIO = "ratio"
ATTR_NOTE = "note"
ATTR_ACTIVATE = "activate"
ATTR_BEAN_ID = "bean_id"
ATTR_ACTIVE = "active"

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


ADD_BEAN_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        # None (empty template) is checked in the handler: translated error.
        vol.Required(ATTR_NAME): vol.Any(None, cv.string),
        # Range checked in the handler (translated error).
        vol.Optional(ATTR_RATIO): vol.Coerce(float),
        vol.Optional(ATTR_NOTE): cv.string,
        vol.Optional(ATTR_ACTIVATE, default=True): cv.boolean,
    }
)
# Exactly one of name, bean_id, active: true (checked in the handler, so the
# error is translated and an empty name from a template counts as not given).
DELETE_BEAN_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_NAME): vol.Any(None, cv.string),
        vol.Optional(ATTR_BEAN_ID): vol.Any(None, cv.string),
        vol.Optional(ATTR_ACTIVE, default=False): cv.boolean,
    }
)


def _bean_entry(hass: HomeAssistant, call: ServiceCall) -> DifluidConfigEntry:
    """The entry for the bean services: config_entry_id, or the only one."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entry = next((e for e in entries if e.entry_id == entry_id), None)
        if entry is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="bean_entry_unknown",
                translation_placeholders={"config_entry_id": entry_id},
            )
        return entry
    if len(entries) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="bean_entry_ambiguous" if entries else "bean_no_scale",
        )
    return entries[0]


def _bean_error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
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
        cut_t, weight, flow, clean = cut
        autostop = entry.runtime_data.autostop
        predicted = autostop.model.overshoot(flow if plausible_flow(flow) else 0.0)
        overshoot = final - weight
        # Same acceptance as a live cut: no dropped frame (or level change)
        # just before the cut, overshoot and flow plausible.
        accepted = clean and autostop.async_learn(flow, overshoot)
        target = call.data.get(ATTR_TARGET)
        model = autostop.model
        return {
            "brew_id": brew_id,
            "cut_offset": offset,
            "cut_point_t": cut_t,
            "weight_at_cut": weight,
            "flow_at_cut": round(flow, 3),
            # False: a frame was dropped (or the level changed) just before
            # the cut, the sample is not learned.
            "clean_cut": clean,
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

    @callback
    def _add_bean(call: ServiceCall) -> ServiceResponse:
        """New bean profile, written like the options flow does (no reload)."""
        entry = _bean_entry(hass, call)
        autostop = entry.runtime_data.autostop
        beans = autostop.beans
        name = (call.data[ATTR_NAME] or "").strip()
        if error := name_error(beans, name):
            raise _bean_error(error, name=name, max=str(NAME_MAX))
        ratio = call.data.get(ATTR_RATIO)
        if ratio is None:
            ratio = autostop.ratio  # the current target ratio
            if not RATIO_MIN <= ratio <= RATIO_MAX:
                raise _bean_error(
                    "bean_ratio_missing",
                    ratio=f"{ratio:g}",
                    min=f"{RATIO_MIN:g}",
                    max=f"{RATIO_MAX:g}",
                )
        elif not RATIO_MIN <= ratio <= RATIO_MAX:
            raise _bean_error(
                "bean_ratio_range",
                ratio=f"{ratio:g}",
                min=f"{RATIO_MIN:g}",
                max=f"{RATIO_MAX:g}",
            )
        bean = make_bean(ulid_now(), name, ratio, call.data.get(ATTR_NOTE))
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_BEANS: [*beans, bean]}
        )
        # The first bean is always activated (with beans one is active).
        active = call.data[ATTR_ACTIVATE] or not beans
        if active:
            autostop.select_bean(bean[BEAN_ID])  # the ratio follows
        else:
            autostop.async_beans_changed()
        return {
            "bean_id": bean[BEAN_ID],
            "name": bean[BEAN_NAME],
            "ratio": bean[BEAN_RATIO],
            "active": active,
        }

    @callback
    def _delete_bean(call: ServiceCall) -> ServiceResponse:
        """Delete a bean profile: by id, by name or the active one."""
        entry = _bean_entry(hass, call)
        autostop = entry.runtime_data.autostop
        beans = autostop.beans
        name = (call.data.get(ATTR_NAME) or "").strip()
        bean_id = (call.data.get(ATTR_BEAN_ID) or "").strip()
        active = call.data[ATTR_ACTIVE]
        if sum((bool(name), bool(bean_id), active)) != 1:
            raise _bean_error("bean_delete_which")
        if bean_id:
            bean = find_bean(beans, bean_id)
            if bean is None:
                raise _bean_error("bean_unknown", bean=bean_id)
        elif name:
            bean = find_bean_by_name(beans, name)
            if bean is None:
                raise _bean_error("bean_unknown", bean=name)
        else:
            bean = autostop.active_bean
            if bean is None:
                raise _bean_error("bean_none_active")
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_BEANS: [b for b in beans if b[BEAN_ID] != bean[BEAN_ID]],
            },
        )
        # Same as a delete in the options flow: if it was the active one, the
        # newest remaining bean becomes active (its ratio applies); after the
        # last one none is active and the target ratio stays.
        autostop.async_options_updated()
        return {"bean_id": bean[BEAN_ID], "name": bean[BEAN_NAME]}

    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_BREW, _delete_brew, schema=DELETE_BREW_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_BEAN,
        _add_bean,
        schema=ADD_BEAN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_BEAN,
        _delete_bean,
        schema=DELETE_BEAN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LEARN_STOP,
        _learn_stop,
        schema=LEARN_STOP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
