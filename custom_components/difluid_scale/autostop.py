"""Adaptive stop at a target yield (HA side of ``stop_model``).

While a brew runs (scale timer counting, tracker state ``brewing``) every
weight sample is checked: once ``weight + predicted overshoot`` reaches the
target, the configured stop switch is turned off (fire and forget, the
sample handler does not wait). The scale timer keeps running (the paused
scale sends no weight) until the weight is calm plus ``tail_seconds`` for
late drops, at most TIMER_STOP_MAX after the cut; lifting the cup stops it
at once. The weight then is the final weight: the stored yield, and with
the cut weight one learning sample (when the brew was stored normally).
The brew time ends at the cut (pump off).

Every attempt is reported on the "Auto-stop" event entity: ``cut`` (switch
turned off) or ``switch_failed`` (the service call raised or the switch is
missing/unavailable; reported only, the attempt continues unchanged), then
once the attempt is concluded the conclusion (``learned`` or a reason).

The stop switch latency (turn_off call -> the switch reports ``off``) is
measured per cut (see ``_SwitchProbe``) and taken out of learning: the
sample is corrected to the nominal switch latency (``adjusted_overshoot``),
and a cut whose latency is unknown is not learned. The ``cut`` event fires
when the service call returns, usually before the switch reports, so it
carries no latency; the conclusion events carry ``switch_latency``.

The targets (ratio, yield) and the active bean are integration state kept in
this module's store, so they do not depend on the number entities being
enabled (restore state); see ``async_load`` for the one-time migration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import logging
import math
import time
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    STATE_OFF,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import async_get as async_get_restore
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .beans import (
    BEAN_NAME,
    BEAN_RATIO,
    RATIO_MAX,
    RATIO_MIN,
    beans_from_options,
    find_bean,
    with_bean_ratio,
)
from .brew import BREWING, TAIL_DROP_GRAMS, BrewResult
from .const import (
    CONF_STOP_SWITCH,
    DOMAIN,
    SIGNAL_BREWS_UPDATED,
    STOP_STORAGE_KEY,
    STOP_STORAGE_MINOR_VERSION,
    STOP_STORAGE_VERSION,
)
from .issues import (
    MODE_RUNTIME,
    async_clear_switch_latency_issue,
    async_update_stop_switch_issue,
    async_update_switch_latency_issue,
    stop_switch_ok,
)
from .protocol import DeviceState
from .stop_model import (
    CUT_RESIDUAL_MAX,
    FLOW_MIN_FRAMES,
    FLOW_WINDOW,
    OVERSHOOT_MAX,
    SWITCH_LATENCY_TIMEOUT,
    TIMER_STOP_MAX,
    Cut,
    FlowEstimator,
    IntervalEstimator,
    LearningJournal,
    SampleFilter,
    SettleDetector,
    StopModel,
    SwitchLatencies,
    adjusted_overshoot,
    plausible_flow,
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
# The stop switch did not report ``off`` within SWITCH_LATENCY_TIMEOUT (or
# was not ``on`` at the cut, or the call failed): the overshoot cannot be
# separated from the switch's delay.
REASON_NO_SWITCH_LATENCY = "switch_latency_unknown"

# Event types of the "Auto-stop" event entity.
EVENT_CUT = "cut"
EVENT_SWITCH_FAILED = "switch_failed"
EVENT_LEARNED = "learned"
AUTOSTOP_EVENT_TYPES = [
    EVENT_CUT,
    EVENT_SWITCH_FAILED,
    EVENT_LEARNED,
    REASON_OUTLIER,
    REASON_NOT_SETTLED,
    REASON_DISCARDED,
    REASON_TRUNCATED,
    REASON_CUP_LIFTED,
    REASON_NO_SWITCH_LATENCY,
]

# Settings keys in the store.
SETTING_RATIO = "target_ratio"
SETTING_YIELD = "target_yield"
SETTING_BEAN = "bean_id"

type AutoStopEventListener = Callable[[str, dict[str, Any]], None]


class _SwitchProbe:
    """Measures one cut's stop switch latency: from just before the
    turn_off call is issued until the switch's state becomes ``off``.

    The state listener is set up before the call (a fast report, even one
    that arrives before the call returns, is not missed). The result is
    reported once via ``on_done``: the latency in s (3 decimals) once the
    call returned without error and the ``off`` report is in; None when the
    call failed or no report came within SWITCH_LATENCY_TIMEOUT of the
    start. ``cancel`` (unload) drops listener and timer without a result.
    Never in the way of the switch-off itself (only a listener and a timer).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        switch: str,
        on_done: Callable[[float | None], None],
    ) -> None:
        self._hass = hass
        self._on_done = on_done
        self._t0: float | None = None
        self._reported: float | None = None
        self._call_ok: bool | None = None
        self.done = False
        self._unsub_state: CALLBACK_TYPE | None = async_track_state_change_event(
            hass, [switch], self._state_changed
        )
        self._unsub_timer: CALLBACK_TYPE | None = None

    @callback
    def start(self) -> None:
        """Right before the call is issued: the measurement starts."""
        self._t0 = time.monotonic()
        self._unsub_timer = async_call_later(
            self._hass, SWITCH_LATENCY_TIMEOUT, self._timed_out
        )

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if self._reported is None and new is not None and new.state == STATE_OFF:
            self._reported = time.monotonic()
            self._check()

    @callback
    def call_done(self, ok: bool) -> None:
        """The service call returned (``ok``) or raised."""
        self._call_ok = ok
        self._check()

    @callback
    def _check(self) -> None:
        if self._call_ok is False:
            self.finish(None)
        elif self._call_ok and self._reported is not None and self._t0 is not None:
            latency = self._reported - self._t0
            self.finish(
                round(latency, 3) if latency <= SWITCH_LATENCY_TIMEOUT else None
            )

    @callback
    def _timed_out(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.finish(None)

    @callback
    def finish(self, latency: float | None) -> None:
        """End now with ``latency`` (once; later results are ignored)."""
        if self.done:
            return
        self.cancel()
        self._on_done(latency)

    @callback
    def cancel(self) -> None:
        """Stop listening (idempotent); no result after this."""
        self.done = True
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None


def _iso(start: float) -> str:
    """A brew start (tracker clock, epoch s) as stored with the brew."""
    return datetime.fromtimestamp(start, UTC).isoformat()


class StopStore(Store[dict[str, Any]]):
    """Stop learning store with version migration.

    1.1 -> 1.2 (0.5.0): ``settings`` added. The migration leaves it out;
    ``AutoStop.async_load`` fills it once from the number entities' restore
    state (it needs the entity registry, not available here).
    0.5.1: optional ``switch_latency`` (measured stop switch latencies);
    missing in older data = none measured yet, so no version step.
    0.5.1: optional ``journal`` (``LearningJournal.to_dict``: ``base``
    model and the accepted samples, for deleting a brew from the learning);
    missing = learned before the journal: the model stays, the journal
    starts empty on it, so no version step.
    """

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: Any
    ) -> dict[str, Any]:
        if old_major_version != 1:
            raise NotImplementedError
        return dict(old_data or {})


class AutoStop:
    """Cuts brews at the target and learns the overshoot model."""

    def __init__(
        self, hass: HomeAssistant, coordinator: DifluidCoordinator, entry_id: str
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.coordinator = coordinator
        self._entry_id = entry_id
        self.model = StopModel()
        # Accepted samples of the model (replayed when one is taken out).
        self.journal = LearningJournal()
        self.target = 0.0
        self.ratio = 0.0
        # Active bean profile (id into the options' bean list), None = none.
        self.bean_id: str | None = None
        # Ratio of the active bean last applied as the target: an options
        # save only moves the target when the bean's ratio really changed.
        self._bean_ratio_applied: float | None = None
        # Bean of the running brew, taken once at its start.
        self._brew_bean_start: float | None = None
        self._brew_bean: str | None = None
        # (brew start, target, source) of the last cut: the stored target.
        self._last_cut_target: tuple[float, float, str, float] | None = None
        # (brew start, measured switch latency) of the last cut whose
        # measurement finished: the stored brew's ``switch_latency``.
        self._last_cut_latency: tuple[float, float | None] | None = None
        # Brew start of the cut ``last`` describes (this run only).
        self._last_start: float | None = None
        # Last cut brew (cut values, settled yield, accepted or why not).
        self.last: dict[str, Any] | None = None
        self._store = StopStore(
            hass,
            STOP_STORAGE_VERSION,
            STOP_STORAGE_KEY.format(entry_id),
            minor_version=STOP_STORAGE_MINOR_VERSION,
        )
        self._dirty = False
        # Single bad frames are held back before anything below sees them.
        self._filter = SampleFilter()
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
        # When the weight settled, the weight at the end of the tail after
        # that (the final weight), the newest weight since the cut.
        self._settled_t: float | None = None
        self._final: float | None = None
        self._last_weight: float | None = None
        self._brew_ok = False
        self._timer_stopped = True  # nothing pending
        self._listeners: list[CALLBACK_TYPE] = []
        self._event_listeners: list[AutoStopEventListener] = []
        self._unsub_switch: CALLBACK_TYPE | None = None
        # Stop switch latencies of the last cuts (nominal = their median)
        # and the latest cut's measurement (None: unknown, see _SwitchProbe).
        self.latencies = SwitchLatencies()
        self.last_switch_latency: float | None = None
        self._probe: _SwitchProbe | None = None

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
        self._async_changed()

    @callback
    def set_ratio(self, ratio: float) -> None:
        """Target brew ratio (yield / dose, 0 = off).

        With a bean active a ratio inside the bean form's range is written
        back to its profile; others (0 = off, 0.3) stay a live value only.
        """
        self.ratio = max(0.0, float(ratio))
        bean = self.active_bean
        if (
            bean is not None
            and RATIO_MIN <= self.ratio <= RATIO_MAX
            and bean[BEAN_RATIO] != self.ratio
        ):
            entry = self.coordinator.config_entry
            new_options = with_bean_ratio(entry.options, bean["id"], self.ratio)
            self._bean_ratio_applied = round(self.ratio, 2)
            self.hass.config_entries.async_update_entry(entry, options=new_options)
        self._async_changed()

    # ------------------------------------------------------------- beans

    @property
    def beans(self) -> list[dict[str, Any]]:
        """Bean profiles from the options."""
        return beans_from_options(self.coordinator.config_entry.options)

    @property
    def active_bean(self) -> dict[str, Any] | None:
        """The active bean profile, if any."""
        return find_bean(self.beans, self.bean_id)

    @property
    def active_bean_name(self) -> str | None:
        """Name of the active bean (stored with each brew)."""
        bean = self.active_bean
        return bean[BEAN_NAME] if bean else None

    @callback
    def select_bean(self, bean_id: str | None) -> None:
        """Make a bean active; its ratio becomes the target.

        With bean profiles one of them is always active: an unknown id or
        None then activates the newest bean (see ``_ensure_active_bean``).
        """
        bean = find_bean(self.beans, bean_id)
        self.bean_id = bean["id"] if bean else None
        if bean is not None:
            self.ratio = bean[BEAN_RATIO]
        self._bean_ratio_applied = bean[BEAN_RATIO] if bean else None
        self._ensure_active_bean()
        self._async_changed()

    def _ensure_active_bean(self) -> bool:
        """With bean profiles, one is always active.

        If none is (none stored yet, or the active one was deleted) the
        newest bean becomes active and its ratio the target, like selecting
        it. "Newest" = the last profile in the options list: profiles are
        appended when created and keep their place when edited. Without
        profiles no bean is active (the ratio stays). Returns True when the
        active bean changed.
        """
        beans = self.beans
        if not beans:
            changed = self.bean_id is not None
            self.bean_id = None
            self._bean_ratio_applied = None
            return changed
        if find_bean(beans, self.bean_id) is not None:
            return False
        newest = beans[-1]
        self.bean_id = newest["id"]
        self.ratio = newest[BEAN_RATIO]
        self._bean_ratio_applied = newest[BEAN_RATIO]
        return True

    @callback
    def async_beans_changed(self) -> None:
        """Bean profiles were changed outside the options flow (service):
        update the entities (select options) right away."""
        self._async_changed()

    @callback
    def brew_bean(self, start: float | None) -> str | None:
        """Bean of the brew that started at ``start``.

        Taken on the first call for that start (the coordinator calls it at
        the brew start), so the stored brew and the auto-stop events of a
        brew name the same bean even if the selection changes meanwhile.
        """
        if start is None:
            return self.active_bean_name
        if self._brew_bean_start != start:
            self._brew_bean_start = start
            self._brew_bean = self.active_bean_name
        return self._brew_bean

    @callback
    def async_options_updated(self) -> None:
        """Options changed: follow bean edits, re-check the stop switch.

        The target ratio only follows the active bean when that bean's ratio
        changed; any other options save keeps a ratio set by hand.
        """
        bean = self.active_bean
        if bean is None:
            # Active one deleted (or first bean added): newest becomes active.
            self._ensure_active_bean()
        elif bean[BEAN_RATIO] != self._bean_ratio_applied:
            self.ratio = bean[BEAN_RATIO]  # edited in the options flow
            self._bean_ratio_applied = bean[BEAN_RATIO]
        self.async_watch_switch()
        self._async_changed()

    # ------------------------------------------------------ stop switch

    @callback
    def async_watch_switch(self, mode: str = MODE_RUNTIME) -> None:
        """Check the stop switch now and follow its state.

        ``mode`` MODE_MISSING only for the check right after Home Assistant
        started (switches may still be placeholders or loading); otherwise
        (options change, setup or reload while running) a really unavailable
        switch counts too. The issue is cleared only once the switch is
        usable or the option is empty.
        """
        if self._unsub_switch is not None:
            self._unsub_switch()
            self._unsub_switch = None
        if self.coordinator.unloading:
            return
        switch = self.switch_entity
        async_update_stop_switch_issue(
            self.hass, self.coordinator.config_entry, switch, mode
        )
        self._async_update_latency_issue()
        if switch is None:
            return

        @callback
        def _changed(event: Event[EventStateChangedData]) -> None:
            if self.coordinator.unloading:
                return
            async_update_stop_switch_issue(
                self.hass, self.coordinator.config_entry, switch, MODE_RUNTIME
            )

        self._unsub_switch = async_track_state_change_event(
            self.hass, [switch], _changed
        )

    @callback
    def async_stop_watching(self) -> None:
        """Stop following the switch and drop a running latency
        measurement (unload)."""
        if self._unsub_switch is not None:
            self._unsub_switch()
            self._unsub_switch = None
        if self._probe is not None:
            self._probe.cancel()
            self._probe = None
        async_clear_switch_latency_issue(
            self.hass, self.coordinator.config_entry.entry_id
        )

    @callback
    def _async_update_latency_issue(self) -> None:
        """Repair issue while the configured switch's last cuts all ended
        without an ``off`` report in time (learning has stopped)."""
        switch = self.switch_entity
        async_update_switch_latency_issue(
            self.hass,
            self.coordinator.config_entry,
            switch,
            self.latencies.unknown_streak
            if switch is not None and switch == self.latencies.entity_id
            else 0,
        )

    @property
    def nominal_switch_latency(self) -> float:
        """Nominal stop switch latency (s) of the configured switch."""
        return self.latencies.nominal(self.switch_entity)

    def switch_latency_for(self, start: float | None) -> float | None:
        """Measured stop switch latency of the cut in the brew that started
        at ``start`` (None: no cut there, not measured (yet), or unknown)."""
        cut = self._cut
        if cut is not None and start is not None and cut.start == start:
            return cut.switch_latency
        last = self._last_cut_latency
        if last is not None and start is not None and last[0] == start:
            return last[1]
        return None

    def switch_latency_unknown_for(self, start: float | None) -> bool:
        """The brew that started at ``start`` was cut and its switch latency
        measurement ended without a value (False: no cut, measured, or
        still pending)."""
        last = self._last_cut_latency
        return (
            last is not None
            and start is not None
            and last[0] == start
            and last[1] is None
        )

    def effective_target(self) -> tuple[float, str, float | None]:
        """Target for the running brew: ratio x detected dose, else the
        fixed target yield. Returns (grams, source, dose)."""
        dose = self.coordinator.brew_tracker.dose_candidate
        if self.ratio > 0 and dose is not None and dose > 0:
            return round(self.ratio * dose, 1), "ratio", dose
        return self.target, "yield", None

    def target_for_dose(self, dose: float | None) -> dict[str, Any] | None:
        """The target a brew with ``dose`` gets (card, stored brew).

        ``{"target": g, "target_source": "ratio"|"yield"}``; None while the
        auto-stop is off (no stop switch, no target).
        """
        if not self.enabled:
            return None
        if self.ratio > 0 and dose is not None and dose > 0:
            return {"target": round(self.ratio * dose, 1), "target_source": "ratio"}
        if self.target > 0:
            return {"target": round(self.target, 1), "target_source": "yield"}
        return None

    def brew_target(self, start: float | None, dose: float | None) -> dict[str, Any] | None:
        """Target shown for the brew that started at ``start``.

        The cut target once the auto-stop fired in that brew (what it aimed
        at), else the effective target: ratio x the brew's dose (the dose
        the live view shows), or the fixed target yield.
        """
        cut = self._last_cut_target
        if cut is not None and start is not None and cut[0] == start:
            return {"target": cut[1], "target_source": cut[2]}
        return self.target_for_dose(dose)

    def cut_offset(self, start: float | None) -> float | None:
        """Seconds from the start of that brew to its auto-stop cut (pump
        off), None without a cut in that brew."""
        cut = self._last_cut_target
        if cut is None or start is None or cut[0] != start:
            return None
        return round(cut[3] - start, 1)

    # ----------------------------------------------------------- samples

    @callback
    def feed_sample(self, t: float, weight: float) -> None:
        """Every weight sample, after the brew tracker has seen it.

        Passed through the outlier filter first: a frame that jumps further
        than a shot can pour is held back and decided by the next frame, so
        it never drives a cut, the flow or the settle/lift check on its own.
        """
        for ft, fw, shift in self._filter.feed(t, weight):
            self._feed_accepted(ft, fw, shift)

    @callback
    def _feed_accepted(self, t: float, weight: float, shift: bool) -> None:
        flow = self._flow.feed(t, weight, shift)
        tracker = self.coordinator.brew_tracker
        if self._cut is not None:
            if tracker.start == self._cut.start:
                self._observe(t, weight)
            elif tracker.start is not None:
                # A new brew while an old attempt is still open (its brew
                # ended without an answer): close it, never touch this brew.
                self._conclude(self._final_weight(), REASON_NOT_SETTLED)
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
        # An implausible flow never drives the cut: then only the weight
        # (plus the drip c) counts, as a backstop at the target.
        cut_flow = flow if plausible_flow(flow) else 0.0
        if self._flow.residual() > CUT_RESIDUAL_MAX:
            # This frame jumped above the line through the window: it does
            # not cut on its own, the next frame decides.
            return
        if not self.model.should_cut(weight, cut_flow, target, lead):
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
        predicted = self.model.overshoot(flow if plausible_flow(flow) else 0.0)
        disturbed = self._filter.level_t
        self._cut_start = start
        self._last_cut_target = (start, round(target, 1), source, t)
        self._cut = Cut(
            start=start, t=t, weight=weight, flow=flow, target=target,
            predicted=predicted, lead=lead, source=source, dose=dose,
            bean=self.brew_bean(start),
            clean=self._flow.count >= FLOW_MIN_FRAMES
            and (disturbed is None or disturbed < t - FLOW_WINDOW),
            nominal_latency=self.latencies.nominal(switch),
            latency_pending=switch is not None,
        )
        self._settle = SettleDetector()
        self._settle.feed(t, weight)
        self._settled = None
        self._settled_t = None
        self._final = None
        self._last_weight = weight
        self._brew_ok = False
        self._timer_stopped = False
        # Switch now (eager: the service call is issued before this
        # returns, not awaited). The scale timer keeps running until the
        # weight has settled: while it is paused the scale sends no weight,
        # so the drips would be missed (see _observe).
        self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self._async_switch_off(switch, self._cut),
            "difluid_scale autostop switch",
        )
        _LOGGER.debug(
            "%s: auto-stop at %.1f g, flow %.2f g/s, predicted +%.2f g, "
            "lead %.2f s (target %.1f)",
            self.coordinator.address, weight, flow, predicted, lead, target,
        )

    async def _async_switch_off(self, switch: str | None, cut: Cut) -> None:
        if switch is None:
            return
        # Checked first: a missing or unavailable entity is skipped by the
        # service call without an error.
        ok = stop_switch_ok(self.hass, switch)
        # Latency measured unless the switch is off already, unavailable or
        # missing. ``unknown`` is measured too: the plug is briefly unknown
        # after a Home Assistant restart while a shot evidently runs, and
        # its ``off`` report then is the answer to this call.
        state = self.hass.states.get(switch)
        probe: _SwitchProbe | None = None
        if state is not None and state.state not in (STATE_OFF, STATE_UNAVAILABLE):
            if self._probe is not None:
                # A previous cut still waits: it ends unknown, never pending
                # forever.
                self._probe.finish(None)
            probe = self._probe = _SwitchProbe(
                self.hass,
                switch,
                lambda latency: self._switch_latency_known(
                    cut, switch, latency, probed=True
                ),
            )
            # Measurement start: right before the call is issued. This task
            # is started eagerly from the sample handler (_fire), so this is
            # the cut decision plus only the checks above.
            probe.start()
        # Shielded: an unload cancelling this task mid-cut must never abort
        # switching the machine off (only what follows is skipped).
        call = self.hass.async_create_task(
            self.hass.services.async_call(
                SWITCH_DOMAIN,
                SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: switch},
                blocking=True,
            ),
            "difluid_scale autostop turn_off",
        )
        try:
            await asyncio.shield(call)
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.warning("Auto-stop could not turn off %s: %s", switch, err)
            ok = False
            call_ok = False
        else:
            call_ok = True
            if not ok:
                _LOGGER.warning(
                    "Auto-stop: stop switch %s is missing or unavailable", switch
                )
        if self.coordinator.unloading:
            return  # unloaded meanwhile: no issue, no event from this instance
        async_update_stop_switch_issue(
            self.hass, self.coordinator.config_entry, None if ok else switch
        )
        self._fire_event(EVENT_CUT if ok else EVENT_SWITCH_FAILED, cut)
        if probe is not None:
            probe.call_done(call_ok)
        else:
            self._switch_latency_known(cut, switch, None)

    @callback
    def _switch_latency_known(
        self, cut: Cut, switch: str, latency: float | None, probed: bool = False
    ) -> None:
        """A cut's latency measurement ended (``latency`` None: unknown;
        ``probed``: a measurement ran, i.e. the switch did not report in
        time or the call failed, rather than it being off/unavailable)."""
        if self.coordinator.unloading or not cut.latency_pending:
            return
        if self._probe is not None and self._probe.done:
            self._probe = None
        cut.latency_pending = False
        cut.switch_latency = latency
        self.last_switch_latency = latency
        self._last_cut_latency = (cut.start, latency)
        if latency is not None or probed:
            self.latencies.add(switch, latency)
            self._async_update_latency_issue()
        if latency is not None:
            _LOGGER.debug(
                "%s: stop switch %s reported off after %.3f s (nominal %.3f s)",
                self.coordinator.address, switch, latency, cut.nominal_latency,
            )
        else:
            _LOGGER.debug(
                "%s: stop switch %s latency unknown (off or unavailable at "
                "the cut, call failed or no off report within %.0f s)",
                self.coordinator.address, switch, SWITCH_LATENCY_TIMEOUT,
            )
        # A brew stored before the measurement ended gets it now.
        self.coordinator.brews.async_set_switch_latency(cut.start, latency)
        if self._cut is cut:
            self._maybe_learn()
        elif self.last is not None and self._last_start == cut.start:
            # Concluded before the measurement ended (not learned anyway).
            self.last["switch_latency"] = latency
        self._async_changed()

    async def _async_stop_timer(self) -> None:
        """Stop the scale timer, but only while it is timing.

        The stop command is a DLink press: in another state (timer already
        stopped by hand, reset) it would do something else. Without a status
        answer the brew tracker's view (timer counting) decides.
        """
        try:
            state = await self.coordinator.async_request_status()
            if state is None:
                timing = self.coordinator.brew_tracker.state == BREWING
            else:
                timing = state == DeviceState.TIMING
            if not timing:
                _LOGGER.debug(
                    "%s: auto-stop leaves the timer alone (state %s)",
                    self.coordinator.address,
                    state,
                )
                return
            # The drips are in (calm + tail): the brew ends at this stop,
            # without the tracker's capture tail (nothing more to weigh).
            self.coordinator.brew_tracker.end_without_tail()
            await self.coordinator.async_stop_timer()
        except HomeAssistantError as err:
            # Timer still running (e.g. BLE drop): a later hand stop keeps
            # the normal capture tail.
            self.coordinator.brew_tracker.keep_tail()
            _LOGGER.debug("Auto-stop could not stop the scale timer: %s", err)

    @property
    def _tail_seconds(self) -> float:
        return self.coordinator.brew_tracker.config.tail_seconds

    @callback
    def _observe(self, t: float, weight: float) -> None:
        """Samples after the cut: wait until the weight is calm, then
        ``tail_seconds`` more (late drops); the weight then is the final
        weight and the timer is stopped. Lifting the cup stops it at once."""
        cut = self._cut
        assert cut is not None and self._settle is not None
        if self._final is not None:
            return  # timer stopped, waiting for the stored brew
        # Order below: decide (conclude / final) before stopping the timer.
        # The stop task starts eagerly and its status answer can finish the
        # brew (brew_finished) before _stop_timer_once returns.
        if weight < 0 or weight < cut.weight - TAIL_DROP_GRAMS:
            if self._settled is None:
                # Lifted before the drips settled: nothing to learn.
                self._conclude(None, REASON_CUP_LIFTED)
            else:
                # Lifted during the tail: the weight before the lift counts.
                self._final = self._last_weight
            self._stop_timer_once()  # the brew tracker cuts at the lift
            self._maybe_learn()
            return
        self._last_weight = weight
        if self._settled is None:
            self._settled = self._settle.feed(t, weight)
            if self._settled is not None:
                self._settled_t = t
        if (
            self._settled_t is not None
            and t - self._settled_t >= self._tail_seconds
        ):
            self._final = weight
        if self._final is None and t - cut.t >= TIMER_STOP_MAX:
            # Backstop: stop the timer now (the paused scale sends no
            # weight, so nothing more can be learned afterwards).
            if self._settled is None:
                self._conclude(None, REASON_NOT_SETTLED)
                self._stop_timer_once()
                return
            self._final = weight
        if self._final is not None:
            self._stop_timer_once()
        self._maybe_learn()

    def _final_weight(self) -> float | None:
        """Final weight for the conclusion: the end of the tail, or (timer
        stopped earlier, e.g. by hand) the newest weight once settled."""
        if self._final is not None:
            return self._final
        return self._last_weight if self._settled is not None else None

    @callback
    def _stop_timer_once(self) -> None:
        """Stop the scale timer once per cut (drips are in by now).

        The pending stop is cleared either way; the command itself is only
        sent while the scale reports TIMING (see ``_async_stop_timer``).
        """
        if self._timer_stopped:
            return
        self._timer_stopped = True
        self.coordinator.config_entry.async_create_background_task(
            self.hass, self._async_stop_timer(), "difluid_scale autostop timer"
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
                self._conclude(self._final_weight(), REASON_TRUNCATED)
                return
            self._brew_ok = True
            if self._final is None:
                if self._settled is None:
                    # Timer stopped (by hand) before the weight was calm: the
                    # paused scale sends no more weight, nothing to learn.
                    self._conclude(None, REASON_NOT_SETTLED)
                    return
                self._final = self._last_weight  # stopped before the tail ended
            self._maybe_learn()
        elif discarded:
            self._conclude(self._final_weight(), REASON_DISCARDED)

    @callback
    def _maybe_learn(self) -> None:
        cut = self._cut
        if cut is None or self._final is None or not self._brew_ok:
            return
        if cut.latency_pending:
            return  # learned once the switch latency is known (<= timeout)
        if cut.switch_latency is None:
            # The switch's share of the overshoot is unknown: not learned.
            self._conclude(self._final, REASON_NO_SWITCH_LATENCY)
            return
        overshoot = adjusted_overshoot(
            self._final - cut.weight,
            cut.flow,
            cut.switch_latency,
            cut.nominal_latency,
        )
        # A dropped frame or level change just before the cut, or a flow
        # or (adjusted) overshoot outside the plausible range: an outlier,
        # nothing learned.
        accepted = cut.clean and self.model.update(cut.flow, overshoot)
        if accepted:
            self._journal_add(cut.flow, overshoot, _iso(cut.start))
        self._conclude(self._final, None if accepted else REASON_OUTLIER)

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
            # False: dropped frame, level change or gap just before the cut.
            "clean_cut": cut.clean,
            "bean": cut.bean,
            # Measured stop switch latency (None: unknown or, concluded
            # within the measurement, not yet known), the nominal one the
            # sample was corrected to, and the corrected overshoot.
            "switch_latency": cut.switch_latency,
            "nominal_switch_latency": round(cut.nominal_latency, 3),
            "adjusted_overshoot": None
            if final is None or cut.switch_latency is None
            else round(
                adjusted_overshoot(
                    final - cut.weight,
                    cut.flow,
                    cut.switch_latency,
                    cut.nominal_latency,
                ),
                2,
            ),
        }
        self._last_start = cut.start
        _LOGGER.debug("%s: auto-stop result %s", self.coordinator.address, self.last)
        self._fire_event(
            EVENT_LEARNED if reason is None else reason, cut, final=final
        )
        self._async_changed()

    @callback
    def _fire_event(
        self, event_type: str, cut: Cut, final: float | None = None
    ) -> None:
        """Report an attempt to the event entity."""
        data: dict[str, Any] = {
            "target": round(cut.target, 1),
            "target_source": cut.source,
            "dose": cut.dose,
            "cut_weight": round(cut.weight, 1),
            "cut_flow": round(cut.flow, 2),
            "predicted_overshoot": round(cut.predicted, 2),
            "final_yield": None if final is None else round(final, 1),
            "deviation": None if final is None else round(final - cut.target, 1),
            "bean": cut.bean,
        }
        if event_type not in (EVENT_CUT, EVENT_SWITCH_FAILED):
            # Conclusions only: the cut event fires when the call returns,
            # usually before the switch reported off.
            data["switch_latency"] = cut.switch_latency
        for listener in list(self._event_listeners):
            try:
                listener(event_type, data)
            except Exception:
                _LOGGER.exception("Error in an auto-stop event listener")

    @callback
    def async_add_event_listener(
        self, listener: AutoStopEventListener
    ) -> Callable[[], None]:
        """Call ``listener(event_type, data)`` for every attempt event."""
        self._event_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._event_listeners:
                self._event_listeners.remove(listener)

        return _remove

    def _journal_add(
        self,
        flow: float,
        overshoot: float,
        start: str | None,
        brew_id: str | None = None,
    ) -> None:
        """Journal a sample just learned by ``self.model.update``."""
        if brew_id is None and start is not None:
            # Live cuts are usually learned just before their brew is
            # stored: the id follows in async_brew_stored.
            brew = self.coordinator.brews.find_start(start)
            brew_id = brew["id"] if brew is not None else None
        self.journal.add(
            LearningJournal.entry(
                flow, overshoot, brew_id, start, dt_util.utcnow().isoformat()
            )
        )
        self._learned_changed()

    @callback
    def _learned_changed(self) -> None:
        """Which brews are learned changed: the card reloads the brew list
        (its ``learned`` flags) on the brews signal."""
        async_dispatcher_send(
            self.hass, SIGNAL_BREWS_UPDATED.format(self._entry_id)
        )

    def learned(self, brew: dict[str, Any]) -> bool:
        """The brew has a sample in the learning journal (deleting it takes
        that sample out of the model)."""
        return self.journal.index_of(brew.get("id"), brew.get("start")) is not None

    @callback
    def async_brew_stored(self, brew: dict[str, Any]) -> None:
        """A brew was stored: give its journal entry (learned just before,
        by start) the brew id."""
        if self.journal.set_brew_id(brew["start"], brew["id"]):
            self._dirty = True
            self._store.async_delay_save(self._data_to_save, 1)

    def learning_pending_for(self, start: str) -> bool:
        """The cut of the brew starting at ``start`` (ISO) is not concluded
        yet (its sample may still be learned)."""
        cut = self._cut
        return cut is not None and _iso(cut.start) == start

    @callback
    def async_learn(
        self,
        flow: float,
        overshoot: float,
        brew_id: str | None = None,
        start: str | None = None,
    ) -> bool:
        """Learn one sample from outside the live cut (past brew); persisted.

        Same acceptance as a live sample (overshoot and flow within the
        plausible range); False if it was rejected (nothing changes). A brew
        still in the journal has its sample replaced in place and the model
        is replayed (not counted twice); otherwise the sample is learned
        incrementally and journalled. Exception: a brew whose sample is no
        longer in the journal (folded into its base after 50 newer samples,
        or learned before the journal existed) is counted again.
        """
        if not (
            math.isfinite(flow)
            and math.isfinite(overshoot)
            and StopModel.plausible(overshoot, flow)
        ):
            return False
        index = self.journal.index_of(brew_id, start)
        if index is None:
            if not self.model.update(flow, overshoot):
                return False
            self._journal_add(flow, overshoot, start, brew_id)
        else:
            self.journal.remove(brew_id, start)
            self.journal.entries.insert(
                index,
                LearningJournal.entry(
                    flow, overshoot, brew_id, start, dt_util.utcnow().isoformat()
                ),
            )
            self.model = self.journal.replay()
        self._async_changed()
        return True

    @callback
    def async_forget_brew(self, brew_id: str, start: str | None) -> bool:
        """Take a (deleted) brew's samples out of the learning: drop its
        journal entries and replay the model. False (model untouched) if
        the brew is not in the journal."""
        if not self.journal.remove(brew_id, start):
            return False
        self.model = self.journal.replay()
        self._async_changed()
        return True

    @callback
    def async_reset(self) -> None:
        """Forget the learned model and its journal (the last result stays
        visible)."""
        had = len(self.journal) > 0
        self.model.reset()
        self.journal = LearningJournal()
        self._async_changed()
        if had:
            self._learned_changed()

    # ------------------------------------------------------- persistence

    async def async_load(self) -> None:
        """Load the learned model, the targets and the active bean."""
        data = await self._store.async_load() or {}
        model = StopModel.parse(data.get("model"))
        journal = LearningJournal.parse(data.get("journal"))
        if journal is None:
            # No (valid) journal, e.g. learned before 0.5.1: it starts
            # empty on the stored model.
            self.model = model if model is not None else StopModel()
            self.journal = LearningJournal(self.model)
        else:
            if model is None:
                # Model missing or damaged: rebuilt from the journal.
                _LOGGER.warning(
                    "%s: stored stop model unreadable, rebuilt from its "
                    "learning journal (%s samples)",
                    self.coordinator.address,
                    len(journal),
                )
                model = journal.replay()
            self.model = model
            self.journal = journal
        latency = data.get("switch_latency")
        if isinstance(latency, Mapping):
            self.latencies = SwitchLatencies.from_dict(dict(latency))
            last = latency.get("last")
            self.last_switch_latency = (
                float(last) if isinstance(last, (int, float)) else None
            )
        last = data.get("last")
        self.last = last if isinstance(last, dict) else None
        settings = data.get("settings")
        if isinstance(settings, Mapping):
            self._apply_settings(settings)
            bean = self.active_bean
            self._bean_ratio_applied = bean[BEAN_RATIO] if bean else None
            # Beans but no (valid) active one: the newest becomes active and
            # its ratio applies. With the stored bean still there the stored
            # ratio stays as it was (also one set by hand).
            if self._ensure_active_bean():
                self._dirty = True
                self._store.async_delay_save(self._data_to_save, 1)
            return
        # Before 0.5.0 the targets lived only in the number entities'
        # restore state (lost while an entity is disabled). Taken over once.
        self.target = self._restored_number("target_yield")
        self.ratio = self._restored_number("target_ratio")
        _LOGGER.debug(
            "%s: targets taken over from restore state: ratio %s, yield %s",
            self.coordinator.address,
            self.ratio,
            self.target,
        )
        self._ensure_active_bean()
        self._dirty = True
        self._store.async_delay_save(self._data_to_save, 1)

    def _apply_settings(self, settings: Mapping[str, Any]) -> None:
        def _float(key: str) -> float:
            try:
                return max(0.0, float(settings.get(key) or 0.0))
            except (TypeError, ValueError):
                return 0.0

        self.ratio = _float(SETTING_RATIO)
        self.target = _float(SETTING_YIELD)
        bean_id = settings.get(SETTING_BEAN)
        self.bean_id = str(bean_id) if bean_id else None

    def _restored_number(self, key: str) -> float:
        """Last value of one of this scale's number entities (restore state)."""
        unique_id = f"{self.coordinator.address}_{key}"
        entity_id = er.async_get(self.hass).async_get_entity_id(
            Platform.NUMBER, DOMAIN, unique_id
        )
        if entity_id is None:
            return 0.0
        restore = async_get_restore(self.hass)
        # HA 2026.11 replaced RestoreStateData.last_states with
        # async_get_stored_state(); support both.
        getter = getattr(restore, "async_get_stored_state", None)
        if getter is not None:
            stored = getter(entity_id)
        else:
            stored = getattr(restore, "last_states", {}).get(entity_id)
        if stored is None:
            return 0.0
        value: Any = None
        if stored.extra_data is not None:
            value = stored.extra_data.as_dict().get("native_value")
        if value is None:
            value = stored.state.state
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0  # unknown / unavailable

    def settings(self) -> dict[str, Any]:
        """Targets and active bean as stored."""
        return {
            SETTING_RATIO: self.ratio,
            SETTING_YIELD: self.target,
            SETTING_BEAN: self.bean_id,
        }

    def _data_to_save(self) -> dict[str, Any]:
        self._dirty = False
        return {
            "model": self.model.to_dict(),
            "journal": self.journal.to_dict(),
            "last": self.last,
            "settings": self.settings(),
            "switch_latency": {
                **self.latencies.to_dict(),
                "last": self.last_switch_latency,
            },
        }

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
