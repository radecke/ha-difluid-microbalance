"""Serve the brew card and register it as a Lovelace resource."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.start import async_at_started
from homeassistant.loader import async_get_integration

from .const import CARD_FILENAME, CARD_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card JS and add it to Lovelace resources (storage mode)."""
    integration = await async_get_integration(hass, DOMAIN)
    url = f"{CARD_URL}?v={integration.version}"
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                CARD_URL,
                str(Path(__file__).parent / "www" / CARD_FILENAME),
                cache_headers=False,
            )
        ]
    )

    async def _register_resource(hass: HomeAssistant) -> None:
        try:
            await _async_ensure_resource(hass, url)
        except Exception:  # noqa: BLE001 - never break setup over the card
            _LOGGER.warning(
                "Could not register the brew card as a Lovelace resource; "
                "add %s (JavaScript module) manually",
                url,
                exc_info=True,
            )

    async_at_started(hass, _register_resource)


async def _async_ensure_resource(hass: HomeAssistant, url: str) -> None:
    lovelace: Any = hass.data.get("lovelace")
    if lovelace is None:
        _LOGGER.info("Lovelace not loaded; add %s as a resource manually", url)
        return
    if isinstance(lovelace, dict):  # older Home Assistant
        mode = lovelace.get("mode")
        resources = lovelace.get("resources")
    else:
        mode = getattr(lovelace, "resource_mode", getattr(lovelace, "mode", None))
        resources = getattr(lovelace, "resources", None)
    if mode != "storage" or resources is None:
        _LOGGER.info(
            "Lovelace resources are in YAML mode; add this to your resources: "
            "url: %s, type: module",
            url,
        )
        return
    if not getattr(resources, "loaded", True):
        await resources.async_load()
        resources.loaded = True
    for item in resources.async_items():
        current: str = item.get("url", "")
        if current.split("?", 1)[0] != CARD_URL:
            continue
        if current != url:
            await resources.async_update_item(
                item["id"], {"res_type": "module", "url": url}
            )
            _LOGGER.debug("Updated Lovelace resource to %s", url)
        return
    await resources.async_create_item({"res_type": "module", "url": url})
    _LOGGER.debug("Registered Lovelace resource %s", url)
