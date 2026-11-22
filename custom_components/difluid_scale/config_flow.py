"""Config flow for the DiFluid Microbalance."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, UnitOfMass, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util.ulid import ulid_now

from .beans import (
    BEAN_ID,
    BEAN_NAME,
    BEAN_NOTE,
    BEAN_RATIO,
    RATIO_MAX,
    RATIO_MIN,
    beans_from_options,
    find_bean,
    make_bean,
    name_error,
)

from .const import (
    CONF_BEANS,
    CONF_DOSE_LOOKBACK,
    CONF_DOSE_MAX,
    CONF_DOSE_MIN,
    CONF_MIN_DURATION,
    CONF_MIN_YIELD,
    CONF_STABILITY_SECONDS,
    CONF_STABILITY_TOLERANCE,
    CONF_STOP_SWITCH,
    CONF_TAIL_SECONDS,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOCAL_NAME_PREFIX,
    SERVICE_UUID,
)


def _number(
    minimum: float, maximum: float, step: float, unit: str
) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=NumberSelectorMode.BOX,
        )
    )


_SECONDS = UnitOfTime.SECONDS
_GRAMS = UnitOfMass.GRAMS
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TAIL_SECONDS): _number(0, 30, 0.5, _SECONDS),
        vol.Required(CONF_MIN_DURATION): _number(0, 120, 1, _SECONDS),
        vol.Required(CONF_MIN_YIELD): _number(0, 100, 0.5, _GRAMS),
        vol.Required(CONF_DOSE_MIN): _number(1, 100, 0.5, _GRAMS),
        vol.Required(CONF_DOSE_MAX): _number(1, 100, 0.5, _GRAMS),
        vol.Required(CONF_STABILITY_TOLERANCE): _number(0.1, 5, 0.1, _GRAMS),
        vol.Required(CONF_STABILITY_SECONDS): _number(1, 30, 0.5, _SECONDS),
        vol.Required(CONF_DOSE_LOOKBACK): _number(1, 240, 1, UnitOfTime.MINUTES),
        # Adaptive stop: turned off at the target yield; empty = feature off.
        vol.Optional(CONF_STOP_SWITCH): EntitySelector(
            EntitySelectorConfig(domain="switch")
        ),
    }
)


# Keys the detection form owns; everything else in the options (bean
# profiles) is kept when it is saved.
DETECTION_KEYS = {str(key) for key in OPTIONS_SCHEMA.schema}

BEAN_SCHEMA = vol.Schema(
    {
        vol.Required(BEAN_NAME): TextSelector(),
        vol.Required(BEAN_RATIO): NumberSelector(
            NumberSelectorConfig(
                min=RATIO_MIN, max=RATIO_MAX, step=0.1, mode=NumberSelectorMode.BOX
            )
        ),
        vol.Optional(BEAN_NOTE): TextSelector(TextSelectorConfig(multiline=True)),
    }
)


def _is_supported(info: BluetoothServiceInfoBleak) -> bool:
    return SERVICE_UUID in info.service_uuids or (info.name or "").startswith(
        LOCAL_NAME_PREFIX
    )


class DifluidConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DiFluid Microbalance."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow (brew detection thresholds)."""
        return DifluidOptionsFlow()

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not _is_supported(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device."""
        assert self._discovery_info is not None
        name = self._discovery_info.name or self._discovery_info.address
        if user_input is not None:
            return self.async_create_entry(
                title=name, data={CONF_ADDRESS: self._discovery_info.address}
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders={"name": name}
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick a discovered scale."""
        if user_input is not None:
            address: str = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._discovered.get(address, address),
                data={CONF_ADDRESS: address},
            )

        current = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current or info.address in self._discovered:
                continue
            if _is_supported(info):
                self._discovered[info.address] = info.name or info.address

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            addr: f"{name} ({addr})"
                            for addr, name in self._discovered.items()
                        }
                    )
                }
            ),
        )



class DifluidOptionsFlow(OptionsFlow):
    """Menu: brew detection and stop switch, bean profiles."""

    def __init__(self) -> None:
        """Initialize."""
        self._bean_id: str | None = None

    @property
    def _beans(self) -> list[dict[str, Any]]:
        return beans_from_options(self.config_entry.options)

    def _save_beans(self, beans: list[dict[str, Any]]) -> ConfigFlowResult:
        return self.async_create_entry(
            data={**self.config_entry.options, CONF_BEANS: beans}
        )

    def _bean_select(self) -> SelectSelector:
        return SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=b[BEAN_ID], label=b[BEAN_NAME])
                    for b in self._beans
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """What to change."""
        menu = ["detection", "add_bean"]
        if self._beans:
            menu += ["edit_bean", "delete_bean"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_detection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Brew detection thresholds and the stop switch."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_DOSE_MIN] >= user_input[CONF_DOSE_MAX]:
                errors["base"] = "dose_range"
            else:
                kept = {
                    k: v
                    for k, v in self.config_entry.options.items()
                    if k not in DETECTION_KEYS
                }
                return self.async_create_entry(data={**kept, **user_input})
        values = {**DEFAULT_OPTIONS, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="detection",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, values),
            errors=errors,
        )

    def _bean_errors(self, user_input: dict[str, Any], own_id: str | None) -> dict:
        error = name_error(self._beans, user_input.get(BEAN_NAME), own_id)
        return {BEAN_NAME: error} if error else {}

    @staticmethod
    def _bean_from_input(bean_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
        return make_bean(
            bean_id,
            user_input[BEAN_NAME],
            user_input[BEAN_RATIO],
            user_input.get(BEAN_NOTE),
        )

    async def async_step_add_bean(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """New bean profile."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._bean_errors(user_input, None)
            if not errors:
                bean = self._bean_from_input(ulid_now(), user_input)
                return self._save_beans([*self._beans, bean])
        return self.async_show_form(
            step_id="add_bean",
            data_schema=self.add_suggested_values_to_schema(
                BEAN_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_edit_bean(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the bean profile to edit."""
        if user_input is not None:
            self._bean_id = user_input[BEAN_ID]
            return await self.async_step_edit_bean_details()
        return self.async_show_form(
            step_id="edit_bean",
            data_schema=vol.Schema({vol.Required(BEAN_ID): self._bean_select()}),
        )

    async def async_step_edit_bean_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit name, ratio and note of the chosen bean."""
        bean = find_bean(self._beans, self._bean_id)
        if bean is None:
            return self.async_abort(reason="unknown_bean")
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._bean_errors(user_input, bean[BEAN_ID])
            if not errors:
                new = self._bean_from_input(bean[BEAN_ID], user_input)
                return self._save_beans(
                    [new if b[BEAN_ID] == bean[BEAN_ID] else b for b in self._beans]
                )
        values = {k: v for k, v in bean.items() if v is not None}
        return self.async_show_form(
            step_id="edit_bean_details",
            data_schema=self.add_suggested_values_to_schema(
                BEAN_SCHEMA, {**values, **(user_input or {})}
            ),
            errors=errors,
            description_placeholders={"name": bean[BEAN_NAME]},
        )

    async def async_step_delete_bean(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete a bean profile (stored brews keep its name)."""
        if user_input is not None:
            bean_id = user_input[BEAN_ID]
            return self._save_beans([b for b in self._beans if b[BEAN_ID] != bean_id])
        return self.async_show_form(
            step_id="delete_bean",
            data_schema=vol.Schema({vol.Required(BEAN_ID): self._bean_select()}),
        )
