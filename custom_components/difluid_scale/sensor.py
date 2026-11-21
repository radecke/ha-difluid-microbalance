"""Sensors for the DiFluid Microbalance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .autostop import AutoStop
from .brews import BrewHistory
from .coordinator import DifluidConfigEntry, DifluidData
from .entity import DifluidBrewEntity, DifluidEntity, DifluidStopEntity
from .protocol import DeviceState, WeightUnit

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DifluidSensorEntityDescription(SensorEntityDescription):
    """Sensor description with a value extractor."""

    value_fn: Callable[[DifluidData], float | int | str | None]
    attrs_fn: Callable[[DifluidData], dict[str, Any] | None] = lambda _: None


def _weight(data: DifluidData) -> float | None:
    # Always reported in grams: oz readings are converted (x 28.3495);
    # "gering" has no documented gram conversion and yields None.
    return data.sensor.weight_grams if data.sensor else None


def _weight_attrs(data: DifluidData) -> dict[str, Any] | None:
    if data.sensor is None:
        return None
    try:
        unit = WeightUnit(data.sensor.unit).name.lower()
    except ValueError:
        unit = f"unknown_{data.sensor.unit}"
    return {"scale_unit": unit, "scale_value": data.sensor.weight}


DEVICE_STATE_OPTIONS = [state.name.lower() for state in DeviceState]


def _device_state(data: DifluidData) -> str | None:
    if data.status is None:
        return None
    try:
        return DeviceState(data.status.state).name.lower()
    except ValueError:
        return None  # undocumented value: not a valid enum option


def _status_attrs(data: DifluidData) -> dict[str, Any] | None:
    if data.status is None:
        return None
    return {
        "charging": data.status.charging,
        "device_state": data.status.state_name,
        "raw_status": data.status.raw.hex(" "),
    }


SENSORS: tuple[DifluidSensorEntityDescription, ...] = (
    DifluidSensorEntityDescription(
        key="weight",
        translation_key="weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_weight,
        attrs_fn=_weight_attrs,
    ),
    DifluidSensorEntityDescription(
        key="flow_rate",
        translation_key="flow_rate",
        # Doc: flow rate x10. Unit not stated; g/s assumed (also in oz mode).
        native_unit_of_measurement="g/s",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.sensor.flow_rate if d.sensor else None,
    ),
    DifluidSensorEntityDescription(
        key="timer",
        translation_key="timer",
        # Raw "Time" field (data6-7). DiFluid does not document its unit, so it
        # is exposed unitless until verified against the scale's display.
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.sensor.timer if d.sensor else None,
    ),
    DifluidSensorEntityDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.status.battery if d.status else None,
        attrs_fn=_status_attrs,
    ),
    DifluidSensorEntityDescription(
        key="device_state",
        translation_key="device_state",
        device_class=SensorDeviceClass.ENUM,
        options=DEVICE_STATE_OPTIONS,
        value_fn=_device_state,
    ),
)


@dataclass(frozen=True, kw_only=True)
class DifluidBrewSensorEntityDescription(SensorEntityDescription):
    """Brew sensor description reading from the brew history."""

    value_fn: Callable[[BrewHistory], float | int | None]


def _last(key: str) -> Callable[[BrewHistory], float | int | None]:
    def _value(history: BrewHistory) -> float | int | None:
        brew = history.last
        return brew.get(key) if brew else None

    return _value


BREW_SENSORS: tuple[DifluidBrewSensorEntityDescription, ...] = (
    DifluidBrewSensorEntityDescription(
        key="last_dose",
        translation_key="last_dose",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_last("dose"),
    ),
    DifluidBrewSensorEntityDescription(
        key="last_yield",
        translation_key="last_yield",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_last("yield"),
    ),
    DifluidBrewSensorEntityDescription(
        key="brew_ratio",
        translation_key="brew_ratio",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_last("ratio"),
    ),
    DifluidBrewSensorEntityDescription(
        key="brew_duration",
        translation_key="brew_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_last("duration"),
    ),
    DifluidBrewSensorEntityDescription(
        key="brew_count",
        translation_key="brew_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda h: h.brew_count,
    ),
    DifluidBrewSensorEntityDescription(
        key="coffee_total",
        translation_key="coffee_total",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        value_fn=lambda h: round(h.coffee_total, 1),
    ),
)


@dataclass(frozen=True, kw_only=True)
class DifluidStopSensorEntityDescription(SensorEntityDescription):
    """Adaptive stop sensor description reading from the stop model."""

    value_fn: Callable[[AutoStop], float | int | None]


def _last_deviation(stop: AutoStop) -> float | None:
    return stop.last.get("deviation") if stop.last else None


STOP_SENSORS: tuple[DifluidStopSensorEntityDescription, ...] = (
    DifluidStopSensorEntityDescription(
        key="stop_latency",
        translation_key="stop_latency",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: round(s.model.tau, 3),
    ),
    DifluidStopSensorEntityDescription(
        key="stop_drip",
        translation_key="stop_drip",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: round(s.model.c, 3),
    ),
    DifluidStopSensorEntityDescription(
        key="stop_samples",
        translation_key="stop_samples",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.model.count,
    ),
    DifluidStopSensorEntityDescription(
        key="stop_deviation",
        translation_key="stop_deviation",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_last_deviation,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    async_add_entities(DifluidSensor(coordinator, desc) for desc in SENSORS)
    async_add_entities(
        DifluidBrewSensor(coordinator, desc) for desc in BREW_SENSORS
    )
    async_add_entities(DifluidStopSensor(coordinator, desc) for desc in STOP_SENSORS)


class DifluidSensor(DifluidEntity, SensorEntity):
    """A DiFluid Microbalance sensor."""

    entity_description: DifluidSensorEntityDescription

    @property
    def native_value(self) -> float | int | str | None:
        """Return the current value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes."""
        return self.entity_description.attrs_fn(self.coordinator.data)


class DifluidBrewSensor(DifluidBrewEntity, SensorEntity):
    """A brew result sensor (last brew, totals)."""

    entity_description: DifluidBrewSensorEntityDescription

    @property
    def native_value(self) -> float | int | None:
        """Return the value from the brew history."""
        return self.entity_description.value_fn(self.coordinator.brews)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Brew count only: id and start of the newest brew (small)."""
        if self.entity_description.key != "brew_count":
            return None
        brew = self.coordinator.brews.last
        if brew is None:
            return None
        return {"last_brew_id": brew["id"], "last_brew_start": brew["start"]}


class DifluidStopSensor(DifluidStopEntity, SensorEntity):
    """Learned stop model and the result of the last cut brew."""

    entity_description: DifluidStopSensorEntityDescription

    @property
    def native_value(self) -> float | int | None:
        """Return the value from the stop model."""
        return self.entity_description.value_fn(self.coordinator.autostop)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Deviation only: the last cut brew (cut, prediction, settled yield)."""
        if self.entity_description.key != "stop_deviation":
            return None
        last = self.coordinator.autostop.last
        if last is None:
            return None
        return {k: v for k, v in last.items() if k != "deviation"}
