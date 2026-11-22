"""The DiFluid Microbalance integration."""

from __future__ import annotations

from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .brews import BrewHistory, brew_config_from_options
from .const import DOMAIN, STOP_STORAGE_KEY, STOP_STORAGE_VERSION
from .coordinator import DifluidConfigEntry, DifluidCoordinator
from .frontend import async_register_card
from .issues import MODE_MISSING, MODE_RUNTIME, async_clear_stop_switch_issue
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

    # Stop switch check (repair issue) once all integrations are up. During
    # HA startup only a missing switch counts (placeholders, still loading);
    # set up while HA runs (reload), a really unavailable one counts too.
    mode = MODE_RUNTIME if hass.state is CoreState.running else MODE_MISSING

    @callback
    def _check_switch(_hass: HomeAssistant) -> None:
        coordinator.autostop.async_watch_switch(mode)

    entry.async_on_unload(async_at_started(hass, _check_switch))
    entry.async_on_unload(coordinator.autostop.async_stop_watching)
    entry.async_on_unload(
        lambda: async_clear_stop_switch_issue(hass, entry.entry_id)
    )
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
    await BrewHistory(hass, entry.entry_id, lambda: None).async_remove()
    await Store(
        hass, STOP_STORAGE_VERSION, STOP_STORAGE_KEY.format(entry.entry_id)
    ).async_remove()
