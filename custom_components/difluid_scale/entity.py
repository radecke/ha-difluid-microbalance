"""Base entity for the DiFluid Microbalance."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import DifluidCoordinator


class DifluidEntity(CoordinatorEntity[DifluidCoordinator]):
    """Common device info and availability."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: DifluidCoordinator, description: EntityDescription
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        address = coordinator.address
        self._attr_unique_id = f"{address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=coordinator.config_entry.title,
        )

    @property
    def available(self) -> bool:
        """Available only while the scale is connected."""
        return self.coordinator.connected
