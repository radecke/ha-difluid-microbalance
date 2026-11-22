"""Switches for the DiFluid Microbalance.

Configuration switches (Func 1 settings) and "Prepare brew" (integration
state for the brew card, not a device setting).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DifluidConfigEntry, DifluidCoordinator
from .entity import DifluidEntity, _device_info
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


BREW_READY = SwitchEntityDescription(
    key="brew_ready",
    translation_key="brew_ready",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up setting switches."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(DifluidSettingSwitch(coordinator, desc, cmd) for desc, cmd in SWITCHES),
            DifluidBrewReadySwitch(coordinator, BREW_READY),
        ]
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


class DifluidBrewReadySwitch(SwitchEntity):
    """"Prepare brew": a brew is about to start (card shows an empty chart).

    Integration state kept in the coordinator, so it is available while the
    scale is off (the dashboard can be armed before the scale connects).
    Turns itself off when the brew ends or after READY_TIMEOUT_SECONDS
    without a brew. Not a coordinator entity: writes state only on change.
    Not restored across restarts (starts off).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: DifluidCoordinator, description: SwitchEntityDescription
    ) -> None:
        """Initialize the switch."""
        self.coordinator = coordinator
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Follow the coordinator's brew ready state."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_ready_listener(self.async_write_ha_state)
        )

    @property
    def is_on(self) -> bool:
        """Return True while armed."""
        return self.coordinator.brew_ready

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Arm: the brew card shows an empty live chart."""
        self.coordinator.async_set_brew_ready(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disarm."""
        self.coordinator.async_set_brew_ready(False)
