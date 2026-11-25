"""Repair issues: the configured stop switch is missing or unavailable;
or it did not report ``off`` in time at several auto-stop cuts in a row.

(Not ``repairs.py``: that name is the platform for fixable issues.)

A missing switch (no state at all) is reported when the entry is set up
(once Home Assistant has started), reloaded, and when the options change; a
switch that is missing or unavailable at an auto-stop cut is reported too.
A switch merely going unavailable later is not reported: a relay powered
from the machine's own socket drops off whenever the machine is switched
off, when no brew can run anyway. The state listener only clears the issue,
as soon as the switch is back; emptying the option or choosing another
switch clears it too, and so do disabling and removing the entry (a reload
keeps it). Not fixable from the repairs dialog: the user either
repairs the switch or picks another one in the options.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    ISSUE_CUT_FAILED,
    ISSUE_PROFILE_BLOOM_PUMP,
    ISSUE_PROFILE_BLOOM_SCALE,
    ISSUE_STOP_SWITCH,
    ISSUE_SWITCH_LATENCY,
    MODEL,
    SWITCH_LATENCY_ISSUE_STREAK,
)
from .profiles import display_name

# When a switch counts as a problem:
# - MODE_MISSING (setup, reload, options change): only when it does not
#   exist. An unavailable switch is not reported then: at startup it is
#   often a registry placeholder (``restored``) or still loading, and while
#   HA runs it may just be powered off together with the machine.
# - MODE_CUT (auto-stop cut): missing or unavailable in any form.
# The state listener never creates the issue (async_clear_stop_switch_issue_if_ok).
MODE_MISSING = "missing"
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
    if mode == MODE_MISSING:
        return False
    return state.state == STATE_UNAVAILABLE


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
        # Not usable, but not reported in this mode: keep an issue for this
        # switch, drop one that names another (the option was changed).
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        if issue is not None and (issue.translation_placeholders or {}).get(
            "entity_id"
        ) != entity_id:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        return False
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
def async_clear_stop_switch_issue_if_ok(
    hass: HomeAssistant, entry: ConfigEntry, entity_id: str
) -> None:
    """Clear the issue once the switch is really usable; never create it
    (state listener: going unavailable later is no reason to report)."""
    if stop_switch_ok(hass, entity_id):
        ir.async_delete_issue(hass, DOMAIN, stop_switch_issue_id(entry.entry_id))


@callback
def async_clear_stop_switch_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the issue (entry unloaded or removed)."""
    ir.async_delete_issue(hass, DOMAIN, stop_switch_issue_id(entry_id))


# Translation keys of the cut-failed issue (one issue id, the text changes).
ISSUE_STAGE_RETRYING = f"{ISSUE_CUT_FAILED}_retrying"
ISSUE_STAGE_GAVE_UP = ISSUE_CUT_FAILED
ISSUE_STAGE_GAVE_UP_PUMP = f"{ISSUE_CUT_FAILED}_pump"


def cut_failed_issue_id(entry_id: str) -> str:
    """Give-up issue id per config entry."""
    return f"{ISSUE_CUT_FAILED}_{entry_id}"


@callback
def async_create_cut_failed_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    entity_id: str,
    attempts: int,
    stage: str = ISSUE_STAGE_RETRYING,
) -> None:
    """The auto-stop could not turn ``entity_id`` off (the machine may still
    run): created at the first failure (``retrying``), its text replaced on
    giving up (``gave_up``; ``gave_up_pump`` when a throttled pump was set
    back to full power). Cleared by the switch reporting ``off``, the next
    successful cut, disabling or removing the entry."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        cut_failed_issue_id(entry.entry_id),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=stage,
        translation_placeholders={
            "entity_id": entity_id,
            "name": entry_name(entry),
            "attempts": str(attempts),
        },
    )


@callback
def async_clear_cut_failed_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the give-up issue."""
    ir.async_delete_issue(hass, DOMAIN, cut_failed_issue_id(entry_id))


def switch_latency_issue_id(entry_id: str) -> str:
    """Latency issue id per config entry."""
    return f"{ISSUE_SWITCH_LATENCY}_{entry_id}"


@callback
def async_update_switch_latency_issue(
    hass: HomeAssistant, entry: ConfigEntry, entity_id: str | None, streak: int
) -> None:
    """Create the issue once ``streak`` auto-stop cuts in a row ended without
    an ``off`` report in time (nothing learned from them); clear it on the
    next measured latency (streak 0), without a switch or another switch."""
    issue_id = switch_latency_issue_id(entry.entry_id)
    if entity_id is None or streak < SWITCH_LATENCY_ISSUE_STREAK:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_SWITCH_LATENCY,
        translation_placeholders={
            "entity_id": entity_id,
            "name": entry_name(entry),
            "count": str(streak),
        },
    )


@callback
def async_clear_switch_latency_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the latency issue (entry unloaded or removed)."""
    ir.async_delete_issue(hass, DOMAIN, switch_latency_issue_id(entry_id))


def profile_issue_id(key: str, entry_id: str) -> str:
    """Pump profile issue id per config entry."""
    return f"{key}_{entry_id}"


@callback
def async_update_profile_issues(
    hass: HomeAssistant,
    entry: ConfigEntry,
    profile: str | None,
    scale_auto_stop: bool,
    pump: str | None,
    pump_min: float | None,
) -> None:
    """Pump profiles (0.7.0): ``profile`` = the active profile's label if
    it has a 0 % phase and profiles run (mode not off), else None. Creates
    or clears one issue per cause: the scale's auto stop timing is on; the
    pump entity's min is above 0 (``pump_min``)."""
    for key, active, placeholders in (
        (ISSUE_PROFILE_BLOOM_SCALE, profile is not None and scale_auto_stop, {}),
        (
            ISSUE_PROFILE_BLOOM_PUMP,
            profile is not None and pump is not None and (pump_min or 0) > 0,
            {"entity_id": pump or "", "min": f"{pump_min or 0:g}"},
        ),
    ):
        issue_id = profile_issue_id(key, entry.entry_id)
        if not active:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders={
                "name": entry_name(entry),
                # A built-in by its name in the server's language (ids are
                # not for people), an imported one by its label.
                "profile": display_name(profile, hass.config.language) if profile else "",
                **placeholders,
            },
        )


@callback
def async_clear_profile_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the pump profile issues (entry removed or disabled)."""
    for key in (ISSUE_PROFILE_BLOOM_SCALE, ISSUE_PROFILE_BLOOM_PUMP):
        ir.async_delete_issue(hass, DOMAIN, profile_issue_id(key, entry_id))
