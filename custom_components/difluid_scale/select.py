"""Selects: weight unit of the scale, active bean profile."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .beans import BEAN_ID, BEAN_NAME, find_bean_by_name
from .const import BEAN_NONE, DOMAIN
from .coordinator import DifluidConfigEntry
from .entity import DifluidEntity, DifluidStopEntity
from .protocol import CMD_SETTINGS_WEIGHT_UNIT, WeightUnit

PARALLEL_UPDATES = 1

UNIT_OPTIONS = [unit.name.lower() for unit in WeightUnit]  # gram, ounce, gering

WEIGHT_UNIT = SelectEntityDescription(
    key="weight_unit",
    translation_key="weight_unit",
    entity_category=EntityCategory.CONFIG,
    options=UNIT_OPTIONS,
)

BEAN = SelectEntityDescription(key="bean", translation_key="bean")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the weight unit and bean selects."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            DifluidUnitSelect(coordinator, WEIGHT_UNIT),
            DifluidBeanSelect(coordinator, BEAN),
        ]
    )


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


class DifluidBeanSelect(DifluidStopEntity, SelectEntity):
    """Active bean profile; integration state.

    Options are the bean names; "none" only while there are no profiles
    (with profiles one is always active). Selecting a bean sets the target
    ratio to the bean's ratio.
    """

    @property
    def options(self) -> list[str]:
        """The bean names, or only "none" without profiles."""
        names = [b[BEAN_NAME] for b in self.coordinator.autostop.beans]
        return names or [BEAN_NONE]

    @property
    def current_option(self) -> str:
        """Name of the active bean, or "none"."""
        return self.coordinator.autostop.active_bean_name or BEAN_NONE

    async def async_select_option(self, option: str) -> None:
        """Activate a bean ("none" only without profiles: nothing to do)."""
        autostop = self.coordinator.autostop
        if option == BEAN_NONE:
            if autostop.beans:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="bean_required"
                )
            return
        bean = find_bean_by_name(autostop.beans, option)
        if bean is not None:
            autostop.select_bean(bean[BEAN_ID])
