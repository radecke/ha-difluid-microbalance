"""Configuration switches (Func 1 settings) for the DiFluid Microbalance."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DifluidConfigEntry, DifluidCoordinator
from .entity import DifluidEntity
from .protocol import (
    CMD_SETTINGS_AUTO_DETECT_TIMING,
    CMD_SETTINGS_AUTO_STOP_TIMING,
)

PARALLEL_UPDATES = 1

# description key -> Func 1 Cmd
SWITCHES: tuple[tuple[SwitchEntityDescription, int], ...] = (
    (
        SwitchEntityDescription(
            key="auto_detect_timing",
            translation_key="auto_detect_timing",
            entity_category=EntityCategory.CONFIG,
        ),
        CMD_SETTINGS_AUTO_DETECT_TIMING,
    ),
    (
        SwitchEntityDescription(
            key="auto_stop_timing",
            translation_key="auto_stop_timing",
            entity_category=EntityCategory.CONFIG,
        ),
        CMD_SETTINGS_AUTO_STOP_TIMING,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up setting switches."""
    coordinator = entry.runtime_data
    async_add_entities(
        DifluidSettingSwitch(coordinator, desc, cmd) for desc, cmd in SWITCHES
    )


class DifluidSettingSwitch(DifluidEntity, SwitchEntity):
    """A boolean device setting; state follows the device's responses."""

    def __init__(
        self,
        coordinator: DifluidCoordinator,
        description: SwitchEntityDescription,
        cmd: int,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, description)
        self._cmd = cmd

    @property
    def is_on(self) -> bool | None:
        """Return the last value reported by the scale."""
        value = self.coordinator.data.settings.get(self._cmd)
        return None if value is None else value == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the setting; the echo updates the state."""
        await self.coordinator.async_set_setting(self._cmd, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the setting; the echo updates the state."""
        await self.coordinator.async_set_setting(self._cmd, 0)
