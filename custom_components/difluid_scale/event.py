"""Event entities: physical key presses, auto-stop attempts."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
    EventEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .autostop import AUTOSTOP_EVENT_TYPES
from .coordinator import DifluidConfigEntry, DifluidCoordinator
from .entity import DifluidEntity, _device_info
from .protocol import KEY_EVENT_TYPES

PARALLEL_UPDATES = 0

KEY_PRESS = EventEntityDescription(
    key="key_press",
    translation_key="key_press",
    device_class=EventDeviceClass.BUTTON,
    event_types=list(KEY_EVENT_TYPES),
)

AUTO_STOP = EventEntityDescription(
    key="auto_stop",
    translation_key="auto_stop",
    event_types=list(AUTOSTOP_EVENT_TYPES),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the key and auto-stop event entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            DifluidKeyEvent(coordinator, KEY_PRESS),
            DifluidAutoStopEvent(coordinator, AUTO_STOP),
        ]
    )


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


class DifluidAutoStopEvent(EventEntity):
    """One event per auto-stop step: ``cut`` / ``switch_failed`` when the
    switch is turned off, then the conclusion (``learned`` or why not).

    Integration state, available while the scale is off; not a coordinator
    entity (writes state only when an event fires).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: DifluidCoordinator, description: EventEntityDescription
    ) -> None:
        """Initialize the entity."""
        self.coordinator = coordinator
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Subscribe to the auto-stop events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.autostop.async_add_event_listener(self._async_handle)
        )

    @callback
    def _async_handle(self, event_type: str, data: dict[str, Any]) -> None:
        self._trigger_event(event_type, data)
        self.async_write_ha_state()
