"""Numbers: dose correction, auto-stop target ratio and target yield."""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import DifluidConfigEntry
from .entity import DifluidBrewEntity, DifluidStopEntity

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

TARGET_YIELD = NumberEntityDescription(
    key="target_yield",
    translation_key="target_yield",
    icon="mdi:target",
    device_class=NumberDeviceClass.WEIGHT,
    native_unit_of_measurement=UnitOfMass.GRAMS,
    native_min_value=0.0,
    native_max_value=100.0,
    native_step=0.1,
    mode=NumberMode.BOX,
)

TARGET_RATIO = NumberEntityDescription(
    key="target_ratio",
    translation_key="target_ratio",
    icon="mdi:scale-balance",
    native_min_value=0.0,
    native_max_value=5.0,
    native_step=0.1,
    mode=NumberMode.BOX,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the dose correction and target yield numbers."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            DifluidDoseNumber(coordinator, DOSE_CORRECTION),
            DifluidTargetYieldNumber(coordinator, TARGET_YIELD),
            DifluidTargetRatioNumber(coordinator, TARGET_RATIO),
        ]
    )


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


class DifluidTargetYieldNumber(DifluidStopEntity, RestoreNumber):
    """Settled yield to stop the brew at (0 = auto-stop off); restored."""

    async def async_added_to_hass(self) -> None:
        """Restore the last target."""
        await super().async_added_to_hass()
        data = await self.async_get_last_number_data()
        if data is not None and data.native_value is not None:
            self.coordinator.autostop.set_target(data.native_value)

    @property
    def native_value(self) -> float:
        """Current target in grams."""
        return self.coordinator.autostop.target

    async def async_set_native_value(self, value: float) -> None:
        """Set a new target (takes effect on the next sample)."""
        self.coordinator.autostop.set_target(value)
        self.async_write_ha_state()


class DifluidTargetRatioNumber(DifluidStopEntity, RestoreNumber):
    """Target brew ratio (yield / dose, 0 = off); restored.

    With a dose detected for the running brew the auto-stop aims at
    ratio x dose; without one it falls back to the target yield."""

    async def async_added_to_hass(self) -> None:
        """Restore the last ratio."""
        await super().async_added_to_hass()
        data = await self.async_get_last_number_data()
        if data is not None and data.native_value is not None:
            self.coordinator.autostop.set_ratio(data.native_value)

    @property
    def native_value(self) -> float:
        """Current target ratio."""
        return self.coordinator.autostop.ratio

    async def async_set_native_value(self, value: float) -> None:
        """Set a new ratio (takes effect on the next sample)."""
        self.coordinator.autostop.set_ratio(value)
        self.async_write_ha_state()
