"""The DiFluid Microbalance integration."""

from __future__ import annotations

from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .coordinator import DifluidConfigEntry, DifluidCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: DifluidConfigEntry) -> bool:
    """Set up a DiFluid Microbalance from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    coordinator = DifluidCoordinator(hass, entry, address)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Start listening only after entities exist so the first connect is seen.
    entry.async_on_unload(coordinator.async_start())
    return True




async def async_unload_entry(hass: HomeAssistant, entry: DifluidConfigEntry) -> bool:
    """Unload a config entry and disconnect from the scale."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
