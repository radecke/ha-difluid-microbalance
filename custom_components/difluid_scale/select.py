"""Weight unit select for the DiFluid Microbalance."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DifluidConfigEntry
from .entity import DifluidEntity
from .protocol import CMD_SETTINGS_WEIGHT_UNIT, WeightUnit

PARALLEL_UPDATES = 1

UNIT_OPTIONS = [unit.name.lower() for unit in WeightUnit]  # gram, ounce, gering

WEIGHT_UNIT = SelectEntityDescription(
    key="weight_unit",
    translation_key="weight_unit",
    entity_category=EntityCategory.CONFIG,
    options=UNIT_OPTIONS,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the weight unit select."""
    async_add_entities([DifluidUnitSelect(entry.runtime_data, WEIGHT_UNIT)])


class DifluidUnitSelect(DifluidEntity, SelectEntity):
    """Display unit (Func 1 Cmd 4); also follows the unit in sensor frames."""

    @property
    def current_option(self) -> str | None:
        """Return the unit last reported by the scale."""
        value = self.coordinator.data.settings.get(CMD_SETTINGS_WEIGHT_UNIT)
        if value is None:
            return None
        try:
            return WeightUnit(value).name.lower()
        except ValueError:
            return None

    async def async_select_option(self, option: str) -> None:
        """Set the unit; the echo updates the state."""
        await self.coordinator.async_set_setting(
            CMD_SETTINGS_WEIGHT_UNIT, WeightUnit[option.upper()].value
        )
