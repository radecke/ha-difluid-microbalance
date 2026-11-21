"""Adaptive stop at a target yield (HA side of ``stop_model``).

While a brew runs (scale timer counting, tracker state ``brewing``) every
weight sample is checked: once ``weight + predicted overshoot`` reaches the
target, the configured stop switch is turned off (fire and forget, the
sample handler does not wait) and the scale timer is stopped right after;
the tracker's capture tail (``tail_seconds``) still counts the drips into
the brew. The cut weight, flow and time are kept; when the weight has
settled and the brew was stored normally, the real overshoot is one
learning sample for the model.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .brew import BREWING, TAIL_DROP_GRAMS, BrewResult
from .const import CONF_STOP_SWITCH, STOP_STORAGE_KEY, STOP_STORAGE_VERSION
from .stop_model import (
    OVERSHOOT_MAX,
    SETTLE_TIMEOUT,
    TIMER_STOP_MAX,
    Cut,
    FlowEstimator,
    IntervalEstimator,
    SettleDetector,
    StopModel,
)

if TYPE_CHECKING:
    from .coordinator import DifluidCoordinator

_LOGGER = logging.getLogger(__name__)

SWITCH_DOMAIN = "switch"

# Why the last cut brew taught the model nothing (``last["reason"]``).
REASON_OUTLIER = "outlier"
REASON_NOT_SETTLED = "not_settled"
REASON_DISCARDED = "brew_discarded"
REASON_TRUNCATED = "brew_truncated"
REASON_CUP_LIFTED = "cup_lifted"


class AutoStop:
    """Cuts brews at the target and learns the overshoot model."""

    def __init__(
        self, hass: HomeAssistant, coordinator: DifluidCoordinator, entry_id: str
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.coordinator = coordinator
        self.model = StopModel()
        self.target = 0.0
        self.ratio = 0.0
        # Last cut brew (cut values, settled yield, accepted or why not).
        self.last: dict[str, Any] | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STOP_STORAGE_VERSION, STOP_STORAGE_KEY.format(entry_id)
        )
        self._dirty = False
        self._flow = FlowEstimator()
        # Sample intervals of the active brew (cut anticipation).
        self._intervals = IntervalEstimator()
        self._interval_start: float | None = None
        # Start of the brew that was cut (once per brew) and of the brew in
        # which the prediction was seen below the target (crossing check).
        self._cut_start: float | None = None
        self._below_start: float | None = None
        self._cut: Cut | None = None
        self._settle: SettleDetector | None = None
        self._settled: float | None = None
        self._brew_ok = False
        self._timer_stopped = True  # nothing pending
        self._listeners: list[CALLBACK_TYPE] = []

    # ------------------------------------------------------------ config

    @property
    def switch_entity(self) -> str | None:
        """The configured stop switch (options flow), None = feature off."""
        return self.coordinator.config_entry.options.get(CONF_STOP_SWITCH) or None

    @property
    def enabled(self) -> bool:
        """A switch is configured and a target (ratio or yield) is set."""
        return self.switch_entity is not None and (self.target > 0 or self.ratio > 0)

    @callback
    def set_target(self, target: float) -> None:
        """Fixed target yield in grams (0 = off); the fallback without a dose."""
        self.target = max(0.0, float(target))

    @callback
    def set_ratio(self, ratio: float) -> None:
        """Target brew ratio (yield / dose, 0 = off)."""
        self.ratio = max(0.0, float(ratio))

    def effective_target(self) -> tuple[float, str, float | None]:
        """Target for the running brew: ratio x detected dose, else the
        fixed target yield. Returns (grams, source, dose)."""
        dose = self.coordinator.brew_tracker.dose_candidate
        if self.ratio > 0 and dose is not None and dose > 0:
            return round(self.ratio * dose, 1), "ratio", dose
        return self.target, "yield", None

    # ----------------------------------------------------------- samples

    @callback
    def feed_sample(self, t: float, weight: float) -> None:
        """Every weight sample, after the brew tracker has seen it."""
        flow = self._flow.feed(t, weight)
        if self._cut is not None:
            self._observe(t, weight)
        tracker = self.coordinator.brew_tracker
        if tracker.state != BREWING:
            return
        start = tracker.start
        if self._interval_start != start:
            self._intervals.clear()
            self._interval_start = start
        self._intervals.feed(t)
        if not self.enabled:
            return
        if self._cut_start == start:
            return  # once per brew
        target, source, dose = self.effective_target()
        if target <= 0:
            return
        if weight > target + OVERSHOOT_MAX:
            # Far above the target: not coffee in a tared cup (cup placed
            # without taring); never cut on that.
            return
        lead = self._intervals.lead
        if not self.model.should_cut(weight, flow, target, lead):
            self._below_start = start
            return
        if self._below_start != start:
            # The prediction was never below the target in this brew (cup
            # or weight already there when it started): no crossing.
            return
        self._fire(start, t, weight, flow, target, lead, source, dose)

    @callback
    def _fire(
        self,
        start: float,
        t: float,
        weight: float,
        flow: float,
        target: float,
        lead: float,
        source: str = "yield",
        dose: float | None = None,
    ) -> None:
        switch = self.switch_entity
        predicted = self.model.overshoot(flow)
        self._cut_start = start
        self._cut = Cut(
            start=start, t=t, weight=weight, flow=flow, target=target,
            predicted=predicted, lead=lead, source=source, dose=dose,
        )
        self._settle = SettleDetector()
        self._settle.feed(t, weight)
        self._settled = None
        self._brew_ok = False
        self._timer_stopped = False
        # Switch now (eager: the service call is issued before this
        # returns, not awaited). The scale timer keeps running until the
        # weight has settled: while it is paused the scale sends no weight,
        # so the drips would be missed (see _observe).
        self.hass.async_create_task(
            self._async_switch_off(switch), "difluid_scale autostop switch"
        )
        _LOGGER.debug(
            "%s: auto-stop at %.1f g, flow %.2f g/s, predicted +%.2f g, "
            "lead %.2f s (target %.1f)",
            self.coordinator.address, weight, flow, predicted, lead, target,
        )

    async def _async_switch_off(self, switch: str | None) -> None:
        if switch is None:
            return
        try:
            await self.hass.services.async_call(
                SWITCH_DOMAIN,
                SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: switch},
                blocking=True,
            )
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.warning("Auto-stop could not turn off %s: %s", switch, err)

    async def _async_stop_timer(self) -> None:
        try:
            await self.coordinator.async_stop_timer()
        except HomeAssistantError as err:
            _LOGGER.debug("Auto-stop could not stop the scale timer: %s", err)

    @callback
    def _observe(self, t: float, weight: float) -> None:
        """Samples after the cut: wait for the weight to settle."""
        cut = self._cut
        assert cut is not None and self._settle is not None
        if self._settled is None and (
            weight < 0 or weight < cut.weight - TAIL_DROP_GRAMS
        ):
            # Cup lifted before the drips settled: stop the timer at once so
            # the brew tracker cuts the brew at the lift; nothing to learn.
            self._stop_timer_once()
            self._conclude(None, REASON_CUP_LIFTED)
            return
        if self._settled is None:
            if t - cut.t > SETTLE_TIMEOUT:
                self._stop_timer_once()
                self._conclude(None, REASON_NOT_SETTLED)
                return
            self._settled = self._settle.feed(t, weight)
        if self._settled is not None or t - cut.t >= TIMER_STOP_MAX:
            self._stop_timer_once()
        self._maybe_learn()

    @callback
    def _stop_timer_once(self) -> None:
        """Stop the scale timer once per cut (drips are in by now)."""
        if self._timer_stopped:
            return
        self._timer_stopped = True
        self.hass.async_create_task(
            self._async_stop_timer(), "difluid_scale autostop timer"
        )

    @callback
    def brew_finished(self, result: BrewResult | None, discarded: bool) -> None:
        """The tracker stored (``result``) or discarded a run."""
        cut = self._cut
        if cut is None:
            return
        if result is not None:
            if result.start != cut.start:
                return
            if result.truncated:
                self._conclude(self._settled, REASON_TRUNCATED)
                return
            self._brew_ok = True
            self._maybe_learn()
        elif discarded:
            self._conclude(self._settled, REASON_DISCARDED)

    @callback
    def _maybe_learn(self) -> None:
        cut = self._cut
        if cut is None or self._settled is None or not self._brew_ok:
            return
        overshoot = self._settled - cut.weight
        accepted = self.model.update(cut.flow, overshoot)
        self._conclude(self._settled, None if accepted else REASON_OUTLIER)

    @callback
    def _conclude(self, final: float | None, reason: str | None) -> None:
        cut = self._cut
        assert cut is not None
        self._cut = None
        self._settle = None
        self.last = {
            "target": round(cut.target, 1),
            "target_source": cut.source,
            "dose": cut.dose,
            "cut_weight": round(cut.weight, 1),
            "cut_flow": round(cut.flow, 2),
            "predicted_overshoot": round(cut.predicted, 2),
            "lead": round(cut.lead, 3),
            "final_yield": None if final is None else round(final, 1),
            "overshoot": None if final is None else round(final - cut.weight, 2),
            "deviation": None if final is None else round(final - cut.target, 1),
            "accepted": reason is None,
            "reason": reason,
        }
        _LOGGER.debug("%s: auto-stop result %s", self.coordinator.address, self.last)
        self._async_changed()

    @callback
    def async_learn(self, flow: float, overshoot: float) -> bool:
        """Learn one sample from outside the live cut (past brew); persisted.

        Same acceptance as a live sample (overshoot within the plausible
        range); False if it was rejected.
        """
        accepted = self.model.update(flow, overshoot)
        if accepted:
            self._async_changed()
        return accepted

    @callback
    def async_reset(self) -> None:
        """Forget the learned model (the last result stays visible)."""
        self.model.reset()
        self._async_changed()

    # ------------------------------------------------------- persistence

    async def async_load(self) -> None:
        """Load the learned model."""
        data = await self._store.async_load()
        if not data:
            return
        self.model = StopModel.from_dict(data.get("model"))
        last = data.get("last")
        self.last = last if isinstance(last, dict) else None

    def _data_to_save(self) -> dict[str, Any]:
        self._dirty = False
        return {"model": self.model.to_dict(), "last": self.last}

    async def async_flush(self) -> None:
        """Write a pending delayed save now (unload)."""
        if self._dirty:
            await self._store.async_save(self._data_to_save())

    async def async_remove(self) -> None:
        """Delete the storage file (config entry removed)."""
        await self._store.async_remove()

    # --------------------------------------------------------- listeners

    @callback
    def _async_changed(self) -> None:
        self._dirty = True
        self._store.async_delay_save(self._data_to_save, 1)
        for listener in list(self._listeners):
            listener()

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> Callable[[], None]:
        """Call ``listener`` when the model or the last result changes."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove
