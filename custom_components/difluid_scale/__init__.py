"""The DiFluid Microbalance integration."""

from __future__ import annotations

from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .beans import migrate_bean_profiles, migrate_bean_ratios
from .brews import BrewHistory, brew_config_from_options
from .const import (
    DOMAIN,
    LEGACY_CONF_TARE_WINDOW,
    STOP_STORAGE_KEY,
    STOP_STORAGE_VERSION,
)
from .coordinator import DifluidConfigEntry, DifluidCoordinator
from .frontend import async_register_card
from .issues import (
    async_clear_cut_failed_issue,
    async_clear_profile_issues,
    async_clear_stop_switch_issue,
    async_clear_switch_latency_issue,
)
from .services import async_register_services
from .websocket import async_register_websocket

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the websocket API, the services and the brew card once."""
    async_register_websocket(hass)
    async_register_services(hass)
    await async_register_card(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: DifluidConfigEntry) -> bool:
    """1.1 -> 1.2 (0.7.0b2): drop the removed option ``tare_window`` (the
    pump profiles' tare rule, replaced by "Prepare brew"); everything else
    is kept. 1.2 -> 1.3 (0.7.0b5): a bean's default pump profile naming a
    built-in by its old German label names it by its id. 1.3 -> 1.4
    (0.7.0b6): a bean's ratio becomes its ratio for its pump profile
    (Standard without one) in ``ratios`` (``ratio`` stays). A newer major
    version is refused (downgrade)."""
    if entry.version > 1:
        return False
    if entry.version == 1 and entry.minor_version < 2:
        options = {
            k: v for k, v in entry.options.items() if k != LEGACY_CONF_TARE_WINDOW
        }
        hass.config_entries.async_update_entry(
            entry, options=options, minor_version=2
        )
    if entry.version == 1 and entry.minor_version < 3:
        hass.config_entries.async_update_entry(
            entry, options=migrate_bean_profiles(entry.options), minor_version=3
        )
    if entry.version == 1 and entry.minor_version < 4:
        hass.config_entries.async_update_entry(
            entry, options=migrate_bean_ratios(entry.options), minor_version=4
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DifluidConfigEntry) -> bool:
    """Set up a DiFluid Microbalance from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    coordinator = DifluidCoordinator(hass, entry, address)
    await coordinator.brews.async_load()
    await coordinator.autostop.async_load()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_restore_brew()
    entry.async_on_unload(coordinator.async_listen_ha_stop())
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    # Start listening only after entities exist so the first connect is seen.
    entry.async_on_unload(coordinator.async_start())

    # Stop switch check (repair issue: only a switch that does not exist)
    # once all integrations are up.
    @callback
    def _check_switch(_hass: HomeAssistant) -> None:
        coordinator.autostop.async_watch_switch()

    entry.async_on_unload(async_at_started(hass, _check_switch))
    entry.async_on_unload(coordinator.autostop.async_stop_watching)
    # A reload keeps the issue (the setup check only reports a missing
    # switch, so an issue from a failed cut would otherwise be lost while the
    # switch is still unusable); disabling the entry clears it, removing it
    # too (async_remove_entry).
    @callback
    def _clear_issue_if_disabled() -> None:
        if entry.disabled_by is not None:
            async_clear_stop_switch_issue(hass, entry.entry_id)
            async_clear_cut_failed_issue(hass, entry.entry_id)
            async_clear_profile_issues(hass, entry.entry_id)

    entry.async_on_unload(_clear_issue_if_disabled)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: DifluidConfigEntry
) -> None:
    """Apply new options live (no reload, keeps the BLE link): brew
    thresholds, bean profiles, stop switch (re-checked)."""
    coordinator = entry.runtime_data
    coordinator.brew_tracker.set_config(brew_config_from_options(dict(entry.options)))
    coordinator.autostop.async_options_updated()


async def async_unload_entry(hass: HomeAssistant, entry: DifluidConfigEntry) -> bool:
    """Unload a config entry and disconnect from the scale."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: DifluidConfigEntry) -> None:
    """Delete stored brews and the learned stop model when the entry is removed."""
    async_clear_stop_switch_issue(hass, entry.entry_id)
    async_clear_switch_latency_issue(hass, entry.entry_id)
    async_clear_cut_failed_issue(hass, entry.entry_id)
    async_clear_profile_issues(hass, entry.entry_id)
    await BrewHistory(hass, entry.entry_id, lambda: None).async_remove()
    await Store(
        hass, STOP_STORAGE_VERSION, STOP_STORAGE_KEY.format(entry.entry_id)
    ).async_remove()
