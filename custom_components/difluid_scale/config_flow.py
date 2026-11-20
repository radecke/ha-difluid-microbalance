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
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_DOSE_LOOKBACK,
    CONF_DOSE_MAX,
    CONF_DOSE_MIN,
    CONF_MIN_DURATION,
    CONF_MIN_YIELD,
    CONF_STABILITY_SECONDS,
    CONF_STABILITY_TOLERANCE,
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
    """Brew detection thresholds."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_DOSE_MIN] >= user_input[CONF_DOSE_MAX]:
                errors["base"] = "dose_range"
            else:
                return self.async_create_entry(data=user_input)
        values = {**DEFAULT_OPTIONS, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, values),
            errors=errors,
        )
