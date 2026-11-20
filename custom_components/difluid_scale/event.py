"""Physical key presses on the DiFluid Microbalance as an event entity."""

from __future__ import annotations

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
    EventEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DifluidConfigEntry
from .entity import DifluidEntity
from .protocol import KEY_EVENT_TYPES

PARALLEL_UPDATES = 0

KEY_PRESS = EventEntityDescription(
    key="key_press",
    translation_key="key_press",
    device_class=EventDeviceClass.BUTTON,
    event_types=list(KEY_EVENT_TYPES),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the key event entity."""
    async_add_entities([DifluidKeyEvent(entry.runtime_data, KEY_PRESS)])


class DifluidKeyEvent(DifluidEntity, EventEntity):
    """Fires when a key is pressed on the scale itself (not app echoes)."""

    async def async_added_to_hass(self) -> None:
        """Subscribe to key events from the coordinator."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_key_listener(self._async_handle_key)
        )

    @callback
    def _async_handle_key(self, event_type: str) -> None:
        self._trigger_event(event_type)
        self.async_write_ha_state()
