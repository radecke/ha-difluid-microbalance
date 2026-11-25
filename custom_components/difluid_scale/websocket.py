"""Websocket API for the brew card."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .brews import BrewHistory, brew_summary
from .const import DOMAIN, SIGNAL_BREWS_UPDATED, SIGNAL_LIVE
from .coordinator import DifluidConfigEntry
from .profiles import (
    PROFILE_STANDARD,
    Profile,
    display_name,
    label_key,
    phase_display_name,
)

_TARGET = {
    vol.Optional("entry_id"): str,
    vol.Optional("device_id"): str,
}


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the commands (once, from async_setup)."""
    websocket_api.async_register_command(hass, ws_brews)
    websocket_api.async_register_command(hass, ws_brew)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_subscribe_live)
    websocket_api.async_register_command(hass, ws_profile)


@callback
def resolve_entry(
    hass: HomeAssistant, msg: dict[str, Any]
) -> DifluidConfigEntry | None:
    """Find the loaded entry for an entry_id / device_id (or the only one)."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    entry_id = msg.get("entry_id")
    if device_id := msg.get("device_id"):
        device = dr.async_get(hass).async_get(device_id)
        if device is None:
            return None
        ids = device.config_entries
        entries = [e for e in entries if e.entry_id in ids]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    elif not msg.get("device_id") and len(entries) != 1:
        return None
    return entries[0] if entries else None


@callback
def _resolve(hass: HomeAssistant, msg: dict[str, Any]) -> BrewHistory | None:
    """Find the brew history for an entry_id / device_id (or the only one)."""
    entry = resolve_entry(hass, msg)
    return entry.runtime_data.brews if entry else None


def _flag_learned(
    hass: HomeAssistant, history: BrewHistory, brew: dict[str, Any]
) -> dict[str, Any]:
    """``brew`` plus ``learned``: its sample is in the auto-stop learning
    journal (deleting it takes the sample out of the model)."""
    entry = hass.config_entries.async_get_entry(history.entry_id)
    coordinator = getattr(entry, "runtime_data", None) if entry else None
    learned = coordinator is not None and coordinator.autostop.learned(brew)
    return {**brew, "learned": learned}


def _not_found(connection: websocket_api.ActiveConnection, msg_id: int) -> None:
    connection.send_error(
        msg_id,
        websocket_api.ERR_NOT_FOUND,
        "No loaded DiFluid scale matches; pass entry_id or device_id",
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/brews", **_TARGET})
@callback
def ws_brews(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Brew summaries (no profiles), newest first; each with ``learned``."""
    if (history := _resolve(hass, msg)) is None:
        _not_found(connection, msg["id"])
        return
    connection.send_result(
        msg["id"],
        {
            "entry_id": history.entry_id,
            "brews": [_flag_learned(hass, history, b) for b in history.summaries()],
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/brew",
        vol.Optional("brew_id"): str,
        **_TARGET,
    }
)
@callback
def ws_brew(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """One brew including its profile (newest if no brew_id)."""
    if (history := _resolve(hass, msg)) is None:
        _not_found(connection, msg["id"])
        return
    brew_id = msg.get("brew_id")
    brew = history.get(brew_id) if brew_id else history.last
    if brew is None and brew_id:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown brew")
        return
    if brew is not None:
        brew = _flag_learned(
            hass, history, {k: v for k, v in brew.items() if k != "counted_dose"}
        )
    connection.send_result(msg["id"], {"entry_id": history.entry_id, "brew": brew})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_brews", **_TARGET}
)
@callback
def ws_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Push a message whenever the brew history changes.

    Unlike subscribe_events this also works for non-admin users.
    """
    if (entry := resolve_entry(hass, msg)) is None:
        _not_found(connection, msg["id"])
        return
    msg_id = msg["id"]

    @callback
    def _changed() -> None:
        # Resolved at send time: a reload replaces the history object.
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is None:
            return
        last = coordinator.brews.last
        connection.send_message(
            websocket_api.event_message(
                msg_id,
                {
                    "entry_id": entry.entry_id,
                    "last": brew_summary(last) if last else None,
                },
            )
        )

    connection.subscriptions[msg_id] = async_dispatcher_connect(
        hass, SIGNAL_BREWS_UPDATED.format(entry.entry_id), _changed
    )
    connection.send_result(msg_id)


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_live", **_TARGET}
)
@callback
def ws_subscribe_live(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Push live data of the active brew (see live.py for the messages).

    The first event is ``live_start`` with all points so far when a brew is
    active, otherwise ``live_idle``; then ``live_ready``. Survives a reload of the entry (the
    signal is per entry id).
    """
    if (entry := resolve_entry(hass, msg)) is None:
        _not_found(connection, msg["id"])
        return
    msg_id = msg["id"]

    @callback
    def _forward(message: dict[str, Any]) -> None:
        connection.send_message(
            websocket_api.event_message(msg_id, {"entry_id": entry.entry_id, **message})
        )

    connection.subscriptions[msg_id] = async_dispatcher_connect(
        hass, SIGNAL_LIVE.format(entry.entry_id), _forward
    )
    connection.send_result(msg_id)
    coordinator = entry.runtime_data
    _forward(coordinator.live_snapshot())
    _forward(coordinator.ready_snapshot())


def profile_definition(profile: Profile, language: str | None = None) -> dict[str, Any]:
    """A validated profile as the profile preview feature draws it: the
    parsed phases (defaults filled in, imported and built-in alike).
    ``label`` is the identity (a built-in's id); ``name`` and the phases'
    ``name`` are for people: built-ins in ``language`` (en/de, 0.7.0b5),
    imported ones as they are; ``key`` is a phase's stored name."""
    return {
        "label": profile.label,
        "name": display_name(profile.label, language),
        "revision": profile.revision,
        "builtin": profile.builtin,
        # 0.7.0b4: the built-in Standard (its power is a setting).
        "standard": profile.builtin and profile.key == label_key(PROFILE_STANDARD),
        # "target_time": the adaptive throttle (Zielzeit), no fixed course.
        "throttle": profile.throttle,
        "phases": [
            {
                "name": phase_display_name(profile, p.name, language),
                "key": p.name,
                "duration": p.duration,
                "power": p.power,
                "flow": p.flow,
                "transition": p.transition,
                "transition_duration": p.transition_duration,
                "targets": [
                    {"type": t.type, "operator": t.operator, "value": t.value}
                    for t in p.targets
                ],
            }
            for p in profile.phases
        ],
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/profile",
        vol.Optional("entity_id"): cv.entity_id,
        vol.Optional("label"): str,
        # The frontend's language for the names (0.7.0b5); default: the
        # server's.
        vol.Optional("language"): str,
        **_TARGET,
    }
)
@callback
def ws_profile(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """A pump profile's definition for the profile preview tile feature
    (0.7.0b3): the active one, or ``label``. The scale is found by
    ``entity_id`` (an entity of this integration, e.g. the profile select)
    or entry_id / device_id. With the throttle floor (phase powers are
    clamped to it) and the target shot time number of the same entry."""
    registry = er.async_get(hass)
    entry = None
    if entity_id := msg.get("entity_id"):
        reg = registry.async_get(entity_id)
        if reg is not None and reg.platform == DOMAIN and reg.config_entry_id:
            entry = next(
                (
                    e
                    for e in hass.config_entries.async_loaded_entries(DOMAIN)
                    if e.entry_id == reg.config_entry_id
                ),
                None,
            )
    else:
        entry = resolve_entry(hass, msg)
    if entry is None:
        _not_found(connection, msg["id"])
        return
    coordinator = entry.runtime_data
    throttle = coordinator.autostop.throttle
    profiles = throttle.profiles
    label = msg.get("label")
    profile = profiles.library.get(label) if label else profiles.active
    if profile is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown profile")
        return
    target_entity = registry.async_get_entity_id(
        "number", DOMAIN, f"{coordinator.address}_target_shot_time"
    )
    floor_entity = registry.async_get_entity_id(
        "number", DOMAIN, f"{coordinator.address}_throttle_floor"
    )
    standard_entity = registry.async_get_entity_id(
        "number", DOMAIN, f"{coordinator.address}_standard_pump_power"
    )
    ratio_entity = registry.async_get_entity_id(
        "number", DOMAIN, f"{coordinator.address}_target_ratio"
    )
    connection.send_result(
        msg["id"],
        {
            "entry_id": entry.entry_id,
            "profile": profile_definition(profile, msg.get("language") or hass.config.language),
            "floor": throttle.floor,
            "floor_entity": floor_entity,
            "target_time_entity": target_entity,
            "standard_power_entity": standard_entity,
            "ratio_entity": ratio_entity,
            # The pump power is really controlled (mode throttle, usable
            # pump entity): else the Standard power has no effect.
            "pump_controllable": throttle.pump_controllable,
            "auto_stop_target": coordinator.autostop.enabled,
        },
    )
