"""Buttons (simulated key presses) for the DiFluid Microbalance."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import DifluidConfigEntry, DifluidCoordinator
from .entity import DifluidEntity
from .protocol import (
    CMD_TARE,
    CMD_TIMER_START,
    CMD_TIMER_STOP,
    DeviceState,
)

PARALLEL_UPDATES = 1

# Observed timer cycle (2026-11-20, device-reported state + display check):
#   IDLE --click--> TIMER_READY --click--> TIMING --press--> TIMER_PAUSE
#   TIMER_PAUSE --click--> IDLE (timer back to 0)
# "click" = DLink single click (CMD_TIMER_START), "press" = DLink press
# (CMD_TIMER_STOP).
_MAX_CLICKS = 4


async def _send_frame(coordinator: DifluidCoordinator, frame: bytes) -> None:
    await coordinator.async_send(frame)


async def _click_until(
    coordinator: DifluidCoordinator, target: DeviceState
) -> None:
    """Click the DLink key until the device reports ``target``."""
    state = await coordinator.async_request_status()
    for _ in range(_MAX_CLICKS):
        if state == target:
            return
        await coordinator.async_send(CMD_TIMER_START)
        await asyncio.sleep(0.3)
        state = await coordinator.async_request_status()
    if state != target:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="timer_state",
            translation_placeholders={"state": str(state)},
        )


async def _timer_start(coordinator: DifluidCoordinator) -> None:
    await _click_until(coordinator, DeviceState.TIMING)


async def _timer_reset(coordinator: DifluidCoordinator) -> None:
    state = await coordinator.async_request_status()
    if state == DeviceState.TIMING:
        await coordinator.async_send(CMD_TIMER_STOP)
        await asyncio.sleep(0.3)
    await _click_until(coordinator, DeviceState.IDLE)


@dataclass(frozen=True, kw_only=True)
class DifluidButtonEntityDescription(ButtonEntityDescription):
    """Button description carrying the action to run."""

    press_fn: Callable[[DifluidCoordinator], Awaitable[None]]


BUTTONS: tuple[DifluidButtonEntityDescription, ...] = (
    # Func 3 Cmd 2 data 1: Power button single click (= tare)
    DifluidButtonEntityDescription(
        key="tare",
        translation_key="tare",
        press_fn=lambda c: _send_frame(c, CMD_TARE),
    ),
    DifluidButtonEntityDescription(
        key="timer_start", translation_key="timer_start", press_fn=_timer_start
    ),
    # Func 3 Cmd 1 data 0: DLink press -> TIMER_PAUSE (display keeps the time)
    DifluidButtonEntityDescription(
        key="timer_stop",
        translation_key="timer_stop",
        press_fn=lambda c: _send_frame(c, CMD_TIMER_STOP),
    ),
    DifluidButtonEntityDescription(
        key="timer_reset", translation_key="timer_reset", press_fn=_timer_reset
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DifluidConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up buttons."""
    coordinator = entry.runtime_data
    async_add_entities(DifluidButton(coordinator, desc) for desc in BUTTONS)


class DifluidButton(DifluidEntity, ButtonEntity):
    """Runs a key-press action on the scale."""

    entity_description: DifluidButtonEntityDescription

    async def async_press(self) -> None:
        """Run the action; raises HomeAssistantError when not connected."""
        await self.entity_description.press_fn(self.coordinator)
