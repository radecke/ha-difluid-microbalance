"""Numbers: dose correction, auto-stop target ratio and target yield."""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import DifluidConfigEntry
from .entity import DifluidBrewEntity, DifluidStopEntity, DifluidThrottleEntity
from .throttle import FLOOR_MAX, FLOOR_MIN, TARGET_TIME_MAX, TARGET_TIME_MIN
from .throttle_model import POWER_MAX

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
    native_min_value=0.0,
    native_max_value=5.0,
    native_step=0.1,
    mode=NumberMode.BOX,
)

TARGET_SHOT_TIME = NumberEntityDescription(
    key="target_shot_time",
    translation_key="target_shot_time",
    device_class=NumberDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.SECONDS,
    native_min_value=TARGET_TIME_MIN,
    native_max_value=TARGET_TIME_MAX,
    native_step=1.0,
    mode=NumberMode.BOX,
)

THROTTLE_FLOOR = NumberEntityDescription(
    key="throttle_floor",
    translation_key="throttle_floor",
    native_unit_of_measurement=PERCENTAGE,
    native_min_value=FLOOR_MIN,
    native_max_value=FLOOR_MAX,
    native_step=1.0,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)

STANDARD_PUMP_POWER = NumberEntityDescription(
    key="standard_pump_power",
    translation_key="standard_pump_power",
    native_unit_of_measurement=PERCENTAGE,
    native_max_value=POWER_MAX,
    native_step=1.0,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
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
            DifluidTargetShotTimeNumber(coordinator, TARGET_SHOT_TIME),
            DifluidThrottleFloorNumber(coordinator, THROTTLE_FLOOR),
            DifluidStandardPowerNumber(coordinator, STANDARD_PUMP_POWER),
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


class DifluidTargetYieldNumber(DifluidStopEntity, NumberEntity):
    """Settled yield to stop the brew at (0 = auto-stop off).

    The value is integration state (stored with the stop model), so the
    auto-stop keeps its target while this entity is disabled."""

    @property
    def native_value(self) -> float:
        """Current target in grams."""
        return self.coordinator.autostop.target

    async def async_set_native_value(self, value: float) -> None:
        """Set a new target (takes effect on the next sample)."""
        self.coordinator.autostop.set_target(value)


class DifluidTargetRatioNumber(DifluidStopEntity, NumberEntity):
    """Target brew ratio (yield / dose, 0 = off); integration state.

    With a dose detected for the running brew the auto-stop aims at
    ratio x dose; without one it falls back to the target yield. With a
    bean active, a new ratio is written back to the bean's profile."""

    @property
    def native_value(self) -> float:
        """Current target ratio."""
        return self.coordinator.autostop.ratio

    async def async_set_native_value(self, value: float) -> None:
        """Set a new ratio (takes effect on the next sample)."""
        self.coordinator.autostop.set_ratio(value)


class DifluidTargetShotTimeNumber(DifluidThrottleEntity, NumberEntity):
    """Pump throttle: target shot time (relay on -> pump off), global.

    A bean profile can set its own; the attribute ``effective`` shows the
    one the next brew gets."""

    @property
    def native_value(self) -> float:
        """Global target shot time (s)."""
        return self.coordinator.autostop.throttle.target_time

    @property
    def extra_state_attributes(self) -> dict[str, float]:
        """The target shot time for the active bean."""
        return {"effective": self.coordinator.autostop.throttle.effective_target_time()}

    async def async_set_native_value(self, value: float) -> None:
        """Set a new global target shot time."""
        self.coordinator.autostop.throttle.set_target_time(value)


class DifluidThrottleFloorNumber(DifluidThrottleEntity, NumberEntity):
    """Pump throttle: lowest power it sets (% of the pump entity's range)."""

    @property
    def native_value(self) -> float:
        """Floor in %."""
        return self.coordinator.autostop.throttle.floor

    async def async_set_native_value(self, value: float) -> None:
        """Set a new floor."""
        self.coordinator.autostop.throttle.set_floor(value)


class DifluidStandardPowerNumber(DifluidThrottleEntity, NumberEntity):
    """Power of the pump profile Standard (0.7.0b4): % of the pump
    entity's range, from the throttle floor to 100 (100 = full power, as
    before). Global, not per bean; takes effect at the next relay start.
    Without a pump power entity nothing is set (observe: only recorded)."""

    @property
    def native_min_value(self) -> float:
        """The floor: lower powers run at the floor anyway."""
        return self.coordinator.autostop.throttle.floor

    @property
    def native_value(self) -> float:
        """The power Standard runs at (at least the floor)."""
        throttle = self.coordinator.autostop.throttle
        return max(throttle.floor, throttle.standard_power)

    async def async_set_native_value(self, value: float) -> None:
        """Set the power."""
        self.coordinator.autostop.throttle.set_standard_power(value)
