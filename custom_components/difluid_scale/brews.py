"""Brew history: persistence, totals, bus event, change notifications."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util.ulid import ulid_now

from .brew import BrewConfig, BrewResult, compute_ratio, correct_dose
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
    EVENT_BREW_COMPLETED,
    MAX_STORED_BREWS,
    SIGNAL_BREWS_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

DOSE_AUTO = "auto"


def brew_config_from_options(options: dict[str, Any]) -> BrewConfig:
    """Build the tracker config from config entry options."""
    opts = {**DEFAULT_OPTIONS, **options}
    return BrewConfig(
        tail_seconds=float(opts[CONF_TAIL_SECONDS]),
        min_duration=float(opts[CONF_MIN_DURATION]),
        min_yield=float(opts[CONF_MIN_YIELD]),
        dose_min=float(opts[CONF_DOSE_MIN]),
        dose_max=float(opts[CONF_DOSE_MAX]),
        stability_tolerance=float(opts[CONF_STABILITY_TOLERANCE]),
        stability_seconds=float(opts[CONF_STABILITY_SECONDS]),
        dose_lookback_minutes=float(opts[CONF_DOSE_LOOKBACK]),
    )


def brew_summary(brew: dict[str, Any]) -> dict[str, Any]:
    """A stored brew without its profile."""
    summary = {k: v for k, v in brew.items() if k not in ("profile", "counted_dose")}
    summary["points"] = len(brew.get("profile", ()))
    return summary


class BrewHistory:
    """Keeps the last brews (with profiles) and running totals per entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        device_id_fn: Callable[[], str | None],
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry_id = entry_id
        self._device_id_fn = device_id_fn
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY.format(entry_id)
        )
        self.brews: list[dict[str, Any]] = []  # oldest first
        self.brew_count = 0
        self.coffee_total = 0.0
        self._listeners: list[CALLBACK_TYPE] = []
        self._dirty = False
        # A brew in progress handed over across an unload/reload.
        self._active: dict[str, Any] | None = None

    async def async_load(self) -> None:
        """Load stored brews and totals."""
        data = await self._store.async_load()
        if not data:
            return
        self.brews = list(data.get("brews", []))[-MAX_STORED_BREWS:]
        for brew in self.brews:
            # Stored before 0.3.0b2: what was added to the total so far.
            brew.setdefault("counted_dose", brew.get("dose") or 0.0)
        self.brew_count = int(data.get("brew_count", len(self.brews)))
        self.coffee_total = float(data.get("coffee_total", 0.0))
        self._active = data.get("active")

    def take_active(self) -> dict[str, Any] | None:
        """The brew in progress saved on the last unload (once)."""
        active, self._active = self._active, None
        if active is not None:
            self._dirty = True
            self._store.async_delay_save(self._data_to_save, 1)
        return active

    def set_active(self, active: dict[str, Any] | None) -> None:
        """Hand a brew in progress to the next instance (saved on flush)."""
        if active is not None or self._active is not None:
            self._dirty = True
        self._active = active

    async def async_flush(self) -> None:
        """Write a pending delayed save now (unload/reload within 1 s)."""
        if self._dirty:
            await self._store.async_save(self._data_to_save())

    async def async_remove(self) -> None:
        """Delete the storage file (config entry removed)."""
        await self._store.async_remove()

    @property
    def last(self) -> dict[str, Any] | None:
        """The newest brew, if any."""
        return self.brews[-1] if self.brews else None

    def get(self, brew_id: str) -> dict[str, Any] | None:
        """A stored brew by id."""
        return next((b for b in self.brews if b["id"] == brew_id), None)

    def summaries(self) -> list[dict[str, Any]]:
        """Brew summaries, newest first."""
        return [brew_summary(b) for b in reversed(self.brews)]

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``listener`` whenever the history changes; return remover."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _async_changed(self) -> None:
        self._dirty = True
        self._store.async_delay_save(self._data_to_save, 1)
        for listener in list(self._listeners):
            listener()
        async_dispatcher_send(self.hass, SIGNAL_BREWS_UPDATED.format(self.entry_id))

    def _data_to_save(self) -> dict[str, Any]:
        self._dirty = False
        data: dict[str, Any] = {
            "brews": self.brews,
            "brew_count": self.brew_count,
            "coffee_total": round(self.coffee_total, 1),
        }
        if self._active is not None:
            data["active"] = self._active
        return data

    @callback
    def async_add_result(self, result: BrewResult) -> dict[str, Any]:
        """Store a finished brew, update totals, fire the bus event.

        A continued brew (``result.updated``, e.g. pour-over pause) replaces
        the stored brew with the same start: same id, brew count unchanged,
        the coffee total only grows if the dose grew.
        """
        start = datetime.fromtimestamp(result.start, UTC).isoformat()
        if result.updated and (old := self._find_start(start)) is not None:
            return self._async_replace(old, result)
        brew = {
            "id": ulid_now(),
            "start": start,
            "duration": result.duration,
            "dose": result.dose,
            "dose_source": DOSE_AUTO if result.dose is not None else None,
            "yield": result.yield_g,
            "ratio": result.ratio,
            # Most this brew ever added to coffee_total (dose corrections).
            "counted_dose": result.dose or 0.0,
            "profile": [list(p) for p in result.profile],
        }
        self.brews.append(brew)
        del self.brews[:-MAX_STORED_BREWS]
        self.brew_count += 1
        if result.dose is not None:
            self.coffee_total += result.dose
        self._async_completed(brew, updated=False)
        return brew

    def _find_start(self, start: str) -> dict[str, Any] | None:
        return next((b for b in reversed(self.brews) if b["start"] == start), None)

    @callback
    def _async_replace(
        self, brew: dict[str, Any], result: BrewResult
    ) -> dict[str, Any]:
        brew["duration"] = result.duration
        brew["yield"] = result.yield_g
        brew["profile"] = [list(p) for p in result.profile]
        if brew.get("dose_source") != "manual" and result.dose is not None:
            counted = brew.get("counted_dose") or 0.0
            self.coffee_total += max(0.0, result.dose - counted)
            brew["dose"] = result.dose
            brew["dose_source"] = DOSE_AUTO
            brew["counted_dose"] = round(max(counted, result.dose), 1)
        brew["ratio"] = compute_ratio(brew.get("dose"), brew["yield"])
        self._async_completed(brew, updated=True)
        return brew

    @callback
    def _async_completed(self, brew: dict[str, Any], updated: bool) -> None:
        _LOGGER.debug(
            "brew %s%s: dose %s g, yield %s g, ratio %s, %s s, %s points",
            brew["id"],
            " (updated)" if updated else "",
            brew["dose"],
            brew["yield"],
            brew["ratio"],
            brew["duration"],
            len(brew["profile"]),
        )
        self.hass.bus.async_fire(
            EVENT_BREW_COMPLETED,
            {
                "entry_id": self.entry_id,
                "device_id": self._device_id_fn(),
                "brew_id": brew["id"],
                "start": brew["start"],
                "dose": brew["dose"],
                "yield": brew["yield"],
                "ratio": brew["ratio"],
                "duration": brew["duration"],
                "updated": updated,
            },
        )
        self._async_changed()

    @callback
    def async_delete(self, brew_id: str) -> dict[str, Any]:
        """Remove a stored brew and take it out of the totals.

        No brew-completed event; listeners, the dispatcher signal (card) and
        the store are updated. Raises LookupError for an unknown id.
        """
        brew = self.get(brew_id)
        if brew is None:
            raise LookupError(brew_id)
        self.brews.remove(brew)
        self.brew_count = max(0, self.brew_count - 1)
        counted = brew.get("counted_dose")
        if counted is None:
            counted = brew.get("dose") or 0.0
        self.coffee_total = max(0.0, round(self.coffee_total - counted, 1))
        self._async_changed()
        return brew

    @callback
    def async_set_last_dose(self, dose: float) -> None:
        """Correct the newest brew's dose (recomputes its ratio)."""
        brew = self.last
        if brew is None:
            raise LookupError("no brew")
        self.coffee_total += correct_dose(brew, dose)
        self._async_changed()
