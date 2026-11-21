"""Websocket API for the brew card."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .brews import BrewHistory, brew_summary
from .const import DOMAIN, SIGNAL_BREWS_UPDATED, SIGNAL_LIVE
from .coordinator import DifluidConfigEntry

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


@callback
def _resolve_entry(
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
    entry = _resolve_entry(hass, msg)
    return entry.runtime_data.brews if entry else None


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
    """Brew summaries (no profiles), newest first."""
    if (history := _resolve(hass, msg)) is None:
        _not_found(connection, msg["id"])
        return
    connection.send_result(
        msg["id"], {"entry_id": history.entry_id, "brews": history.summaries()}
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
        brew = {k: v for k, v in brew.items() if k != "counted_dose"}
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
    if (entry := _resolve_entry(hass, msg)) is None:
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
    if (entry := _resolve_entry(hass, msg)) is None:
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
