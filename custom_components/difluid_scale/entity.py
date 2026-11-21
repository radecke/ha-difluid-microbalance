"""Base entities for the DiFluid Microbalance."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import DifluidCoordinator


def _device_info(coordinator: DifluidCoordinator) -> DeviceInfo:
    address = coordinator.address
    return DeviceInfo(
        identifiers={(DOMAIN, address)},
        connections={(CONNECTION_BLUETOOTH, address)},
        manufacturer=MANUFACTURER,
        model=MODEL,
        name=coordinator.config_entry.title,
    )


class DifluidEntity(CoordinatorEntity[DifluidCoordinator]):
    """Common device info and availability."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: DifluidCoordinator, description: EntityDescription
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    @property
    def available(self) -> bool:
        """Available only while the scale is connected."""
        return self.coordinator.connected


class DifluidBrewEntity(Entity):
    """Brew result entity: backed by stored history, always available.

    Not a coordinator entity, so it writes state only when the history
    changes, not on every sensor frame; the scale being off does not matter.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: DifluidCoordinator, description: EntityDescription
    ) -> None:
        """Initialize the entity."""
        self.coordinator = coordinator
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Follow the brew history."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.brews.async_add_listener(self.async_write_ha_state)
        )


class DifluidStopEntity(Entity):
    """Adaptive stop entity: integration state, always available.

    Writes state only when the learned model or the last cut brew changes.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: DifluidCoordinator, description: EntityDescription
    ) -> None:
        """Initialize the entity."""
        self.coordinator = coordinator
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Follow the stop model."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.autostop.async_add_listener(self.async_write_ha_state)
        )
