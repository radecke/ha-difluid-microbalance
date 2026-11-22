"""Repair issue: the configured stop switch is missing or unavailable.

(Not ``repairs.py``: that name is the platform for fixable issues.)

A missing switch is reported when the entry is set up (once Home Assistant
has started) and when the options change; a switch that becomes really
unavailable later (state listener) and one that is unusable at an auto-stop
cut are reported too. Cleared as soon as the switch is back or the option is
emptied. Not fixable from the repairs dialog: the user either
repairs the switch or picks another one in the options.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_RESTORED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_STOP_SWITCH, MODEL

# When a switch counts as a problem:
# - MODE_MISSING (setup, options change): only when it does not exist. At
#   startup a switch is often a registry placeholder (``restored``) or
#   briefly unavailable until its integration is up.
# - MODE_RUNTIME (state listener): missing, or really unavailable (not a
#   restored placeholder); cleared again when it is back.
# - MODE_CUT (auto-stop cut): missing or unavailable in any form.
MODE_MISSING = "missing"
MODE_RUNTIME = "runtime"
MODE_CUT = "cut"


def stop_switch_issue_id(entry_id: str) -> str:
    """Issue id per config entry."""
    return f"{ISSUE_STOP_SWITCH}_{entry_id}"


@callback
def stop_switch_problem(hass: HomeAssistant, entity_id: str, mode: str) -> bool:
    """True if the switch is unusable in the sense of ``mode``."""
    state = hass.states.get(entity_id)
    if state is None:
        return True
    if mode == MODE_MISSING or state.state != STATE_UNAVAILABLE:
        return False
    if mode == MODE_RUNTIME:
        return not state.attributes.get(ATTR_RESTORED, False)
    return True


@callback
def stop_switch_ok(hass: HomeAssistant, entity_id: str) -> bool:
    """The switch exists and is not unavailable (cut-time check)."""
    return not stop_switch_problem(hass, entity_id, MODE_CUT)


def entry_name(entry: ConfigEntry) -> str:
    """The entry title, unless it is (or contains) the BLE address."""
    address = str(entry.data.get("address", "")).upper()
    if address and address in entry.title.upper():
        return f"DiFluid {MODEL}"
    return entry.title


@callback
def async_update_stop_switch_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    entity_id: str | None,
    mode: str = MODE_CUT,
) -> bool:
    """Create or clear the issue for ``entity_id`` (None = clear).

    Created when the switch is a problem in the sense of ``mode``; cleared
    only when it is really usable (or none is configured), so a lenient
    check (placeholder, startup) never removes a justified issue.
    Returns True when the switch is usable (or none is configured).
    """
    issue_id = stop_switch_issue_id(entry.entry_id)
    if entity_id is None or stop_switch_ok(hass, entity_id):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return True
    if not stop_switch_problem(hass, entity_id, mode):
        return False  # not usable, but not reported in this mode
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_STOP_SWITCH,
        translation_placeholders={"entity_id": entity_id, "name": entry_name(entry)},
    )
    return False


@callback
def async_clear_stop_switch_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the issue (entry unloaded or removed)."""
    ir.async_delete_issue(hass, DOMAIN, stop_switch_issue_id(entry_id))
