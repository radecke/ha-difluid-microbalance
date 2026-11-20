"""Binary sensors for the DiFluid Microbalance."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DifluidConfigEntry
from .entity import DifluidEntity

PARALLEL_UPDATES = 0

CONNECTED = BinarySensorEntityDescription(
    key="connected",
    translation_key="connected",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    async_add_entities([DifluidConnectedSensor(entry.runtime_data, CONNECTED)])


class DifluidConnectedSensor(DifluidEntity, BinarySensorEntity):
    """GATT connection state; always available."""

    @property
    def available(self) -> bool:
        """Always available so it can report 'disconnected'."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True while connected."""
        return self.coordinator.connected
