"""Manual dose correction for the DiFluid Microbalance."""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import DifluidConfigEntry
from .entity import DifluidBrewEntity

PARALLEL_UPDATES = 0

DOSE_CORRECTION = NumberEntityDescription(
    key="dose_correction",
    translation_key="dose_correction",
    device_class=NumberDeviceClass.WEIGHT,
    native_unit_of_measurement=UnitOfMass.GRAMS,
    native_min_value=1.0,
    native_max_value=60.0,
    native_step=0.1,
    mode=NumberMode.BOX,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the dose correction number."""
    async_add_entities([DifluidDoseNumber(entry.runtime_data, DOSE_CORRECTION)])


class DifluidDoseNumber(DifluidBrewEntity, NumberEntity):
    """Shows and corrects the dose of the newest brew."""

    @property
    def native_value(self) -> float | None:
        """Dose of the newest brew (None if unknown or no brew yet)."""
        brew = self.coordinator.brews.last
        return brew.get("dose") if brew else None

    async def async_set_native_value(self, value: float) -> None:
        """Correct the newest brew's dose; the ratio follows."""
        try:
            self.coordinator.brews.async_set_last_dose(value)
        except LookupError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="no_brew"
            ) from err
