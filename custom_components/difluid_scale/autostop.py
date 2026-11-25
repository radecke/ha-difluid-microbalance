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
missing/unavailable), then once the attempt is concluded the conclusion
(``learned`` or a reason). A failed cut is retried for CUT_RETRY_WINDOW
(timed, and at once whenever the switch shows ``on`` again); a successful
retry fires ``cut`` with ``attempt`` > 1, giving up fires ``cut_failed``
and raises a repair issue. From the cut decision until the relay reports
``off`` (or the cut is given up) the pump throttle sends no pump value
(``ThrottleControl.hold_restore``): a throttled pump is never set back to
full power while it still runs.

Cut rule: the prediction must cross the target within the brew (never cut
a cup that was not tared: a weight more than OVERSHOOT_MAX above the
target never cuts). A brew that was below its cut point and not cut yet
cuts at once when its weight comes back past the target after a gap in the
scale data (same brew resumed, or a new tracker brew after the old one was
parked by that gap, see BELOW_CARRY_SECONDS); such a late cut is never
learned.

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
    STATE_ON,
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
    BEAN_PROFILE,
    BEAN_RATIO,
    bean_ratio_for,
    profile_ratio,
    with_bean_update,
    RATIO_MAX,
    RATIO_MIN,
    beans_from_options,
    find_bean,
)
from .brew import (
    BACKLOG_LATE_SECONDS,
    BREWING,
    PARKED,
    TAIL_DROP_GRAMS,
    BrewResult,
)
from .const import (
    CONF_STOP_SWITCH,
    DOMAIN,
    SIGNAL_BREWS_UPDATED,
    STOP_STORAGE_KEY,
    STOP_STORAGE_MINOR_VERSION,
    STOP_STORAGE_VERSION,
)
from .issues import (
    MODE_MISSING,
    async_clear_cut_failed_issue,
    ISSUE_STAGE_GAVE_UP,
    ISSUE_STAGE_GAVE_UP_PUMP,
    ISSUE_STAGE_RETRYING,
    async_create_cut_failed_issue,
    async_clear_stop_switch_issue_if_ok,
    async_clear_switch_latency_issue,
    async_update_stop_switch_issue,
    async_update_switch_latency_issue,
    stop_switch_ok,
)
from .profiles import migrate_profile_ids
from .protocol import DeviceState
from .throttle import ThrottleControl, default_store
from .throttle_model import MODE_THROTTLE
from .stop_model import (
    CUT_RESIDUAL_MAX,
    FLOW_MIN_FRAMES,
    FLOW_WINDOW,
    JUMP_GRAMS,
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
# The scale data at the cut came in a backlog (0.7.0b6): frames held back
# (stalled event loop or BLE stack) within FLOW_WINDOW before the cut, or
# the cut frame itself arrived late. The cut then acted on old data: not
# learned. A backlog earlier in the brew (the cut on fresh data) or after
# the cut (it cannot have changed the cut) does not matter.
REASON_DELAYED = "delayed_data"

# Event types of the "Auto-stop" event entity.
EVENT_CUT = "cut"
EVENT_SWITCH_FAILED = "switch_failed"
# The cut could not be made within CUT_RETRY_WINDOW (retries given up).
EVENT_CUT_FAILED = "cut_failed"
EVENT_LEARNED = "learned"
AUTOSTOP_EVENT_TYPES = [
    EVENT_CUT,
    EVENT_SWITCH_FAILED,
    EVENT_CUT_FAILED,
    EVENT_LEARNED,
    REASON_OUTLIER,
    REASON_NOT_SETTLED,
    REASON_DISCARDED,
    REASON_TRUNCATED,
    REASON_CUP_LIFTED,
    REASON_NO_SWITCH_LATENCY,
    REASON_DELAYED,
]

# A failed cut (call raised, switch missing or unavailable) is retried
# (audit B2) for CUT_RETRY_WINDOW after the first failure: every
# CUT_RETRY_INTERVAL for the first CUT_RETRY_FAST attempts, then every
# CUT_RETRY_INTERVAL_SLOW, and at once whenever the switch shows ``on``
# again (a Zigbee/ESPHome relay reconnecting takes 10-60 s). Never a tight
# loop, one attempt at a time. A switch reporting ``off`` ends the retries
# (pump off); at the end of the window the auto-stop gives up: event
# ``cut_failed``, a repair issue, and the pump restore is released.
CUT_RETRY_INTERVAL = 1.0  # s
CUT_RETRY_FAST = 10
CUT_RETRY_INTERVAL_SLOW = 5.0  # s
CUT_RETRY_WINDOW = 120.0  # s
# A brew that saw the prediction below the target and was not cut hands
# that over to a new brew only if it was parked by a gap in the scale data
# (BLE drop or silence; it was closed as truncated or is still parked), the
# stop switch stayed ``on`` throughout (no off report, so no new relay
# start, never unavailable/unknown in between), the new brew's first weight
# continues the old one (not less than TAIL_DROP_GRAMS below it, not more
# than JUMP_GRAMS + CARRY_FLOW_FACTOR x the last flow (at least
# CARRY_FLOW_MIN) x the time between: the shot keeps its flow, it does not
# jump to a full mug) and it comes at most this long after the last frame
# (audit B1): the machine still pumps the same shot. A brew that ended
# regularly (timer stopped, stored or discarded without a gap) hands
# nothing over. A matching window, not a delay.
BELOW_CARRY_SECONDS = 120.0
CARRY_FLOW_FACTOR = 1.5
CARRY_FLOW_MIN = 1.0  # g/s

# Stop deviations of throttled brews kept for their mean (sensor attribute).
THROTTLED_DEVIATIONS_MAX = 15

# Settings keys in the store.
SETTING_RATIO = "target_ratio"
SETTING_YIELD = "target_yield"
SETTING_BEAN = "bean_id"
SETTING_DEFERRED = "ratio_deferred"

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


class _Retry:
    """A failed cut being retried."""

    def __init__(self, cut: Cut, switch: str) -> None:
        self.cut = cut
        self.switch = switch
        self.attempts = 1  # made so far (the first one failed)
        self.inflight = False  # an attempt's call is running
        # The window ran out while an attempt was in flight: its result
        # decides (success -> cut, failure -> give up).
        self.window_over = False


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
    1.2 -> 1.3 (0.6.0): ``throttle`` added (pump throttle settings at their
    defaults, the power model at its prior with an empty journal). The stop
    model, its journal and everything else are kept unchanged.
    1.3 -> 1.4 (0.7.0b5): built-in pump profiles have ids: the active
    profile stored by its German label becomes the id; an imported profile
    whose label is now a built-in id is renamed ("<label> (imported)", the
    active reference follows).
    """

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: Any
    ) -> dict[str, Any]:
        if old_major_version != 1:
            raise NotImplementedError
        data = dict(old_data or {})
        if old_minor_version < 3 and not isinstance(data.get("throttle"), dict):
            data["throttle"] = default_store()
        if old_minor_version < 4 and isinstance(data.get("throttle"), dict):
            data["throttle"] = migrate_profile_ids(data["throttle"])
        return data


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
        # A bean's ratio chosen (bean or pump profile) while a brew runs:
        # it becomes the target once that brew has ended (0.7.0b6); the
        # running brew keeps the target it started with.
        self._deferred_ratio: float | None = None
        # Bean of the running brew, taken once at its start.
        self._brew_bean_start: float | None = None
        self._brew_bean: str | None = None
        # Bean id and pump profile the running brew started with (0.7.0b6):
        # a ratio set by hand during it is the ratio for those.
        self._brew_bean_id: str | None = None
        self._brew_profile: str | None = None
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
        self._arrival: float | None = None  # arrival of the newest sample
        self._flow = FlowEstimator()
        # Sample intervals of the active brew (cut anticipation).
        self._intervals = IntervalEstimator()
        self._interval_start: float | None = None
        # Start of the brew that was cut (once per brew) and of the brew in
        # which the prediction was seen below the target (crossing check).
        self._cut_start: float | None = None
        self._below_start: float | None = None
        # (brew start, tracker t, weight, flow) of the last frame whose prediction
        # was below the target; cleared by a cut, the switch reporting off,
        # or that brew ending without a gap. ``_below_parked``: that brew
        # was parked (gap in the scale data) and not resumed since.
        self._below_last: tuple[float, float, float, float] | None = None
        self._below_parked = False
        self._carried_start: float | None = None
        # A failed cut being retried (until success, ``off`` or give-up).
        self._retry: _Retry | None = None
        self._unsub_retry: CALLBACK_TYPE | None = None
        self._unsub_retry_window: CALLBACK_TYPE | None = None
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
        # Stop deviations (final - target, g) of the last throttled cuts:
        # watched on their own, the stop model stays shared (0.6.0).
        # [{"start": ISO, "deviation": g}], oldest first.
        self.throttled_deviations: list[dict[str, Any]] = []
        # Pump throttle (relay timing, prediction, pump power).
        self.throttle = ThrottleControl(hass, coordinator, self)
        self._throttle_error_t: float | None = None

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
        back to the bean, as its ratio for the active pump profile
        (0.7.0b6); others (0 = off, 0.3) stay a live value only. During a
        brew it applies at once (as before) and is the ratio of the bean and
        profile that brew runs with; a bean or profile chosen during the
        brew keeps its own ratio for after it.
        """
        self.ratio = max(0.0, float(ratio))
        bean = self.active_bean
        label = self._active_profile_label()
        if self._brew_running():
            bean = find_bean(self.beans, self._brew_bean_id)
            label = self._brew_profile or label
            if self.bean_id == self._brew_bean_id and (
                self._active_profile_label() == label
            ):
                self._deferred_ratio = None  # nothing else chosen meanwhile
        else:
            self._deferred_ratio = None
        if (
            bean is not None
            and label is not None
            and RATIO_MIN <= self.ratio <= RATIO_MAX
            and bean_ratio_for(bean, label) != round(self.ratio, 2)
        ):
            if bean["id"] == self.bean_id and label == self._active_profile_label():
                # The active bean's ratio for the active profile: no echo.
                self._bean_ratio_applied = round(self.ratio, 2)
            self._write_bean(bean, ratio=self.ratio, ratio_profile=label)
        self._async_changed()

    def _brew_running(self) -> bool:
        """A brew is pending (brewing, tail, parked) and its bean/profile
        were taken at its start."""
        tracker = self.coordinator.brew_tracker
        return tracker.active and tracker.start == self._brew_bean_start

    def _active_profile_label(self) -> str | None:
        """Label of the active pump profile (None before the throttle is
        set up)."""
        try:
            return self.throttle.profiles.active.label
        except AttributeError:
            return None

    def _write_bean(self, bean: dict[str, Any], **changes: Any) -> None:
        """Write one bean's profile/ratio back to the options. Applied live
        by the update listener (no reload, the scale stays connected); the
        caller has set ``_bean_ratio_applied`` first, so the listener does
        not apply it again."""
        entry = self.coordinator.config_entry
        self.hass.config_entries.async_update_entry(
            entry, options=with_bean_update(entry.options, bean["id"], **changes)
        )

    def _bean_ratio(self, bean: dict[str, Any], label: str | None) -> tuple[float, bool]:
        """The bean's ratio for profile ``label`` and whether the bean has
        it: its own, else the profile's own ratio target, else the bean's
        ratio (``ratio``) as a starting value."""
        if label is not None:
            own = bean_ratio_for(bean, label)
            if own is not None:
                return own, True
            library = getattr(getattr(self.throttle, "profiles", None), "library", None)
            from_profile = profile_ratio(library.get(label)) if library else None
            if from_profile is not None:
                return from_profile, False
        return bean[BEAN_RATIO], False

    @callback
    def _apply_bean_ratio(self, bean: dict[str, Any], profile: str | None = None) -> None:
        """Target ratio = the bean's ratio for the active profile; one it
        did not have yet is remembered (with ``profile``: the bean's pump
        profile too), in one options write."""
        label = self._active_profile_label()
        ratio, known = self._bean_ratio(bean, label)
        if self.coordinator.brew_tracker.active:
            # Not for the running brew: it keeps its target (0.7.0b6).
            self._deferred_ratio = ratio
        else:
            self.ratio = ratio
            self._deferred_ratio = None
        self._bean_ratio_applied = round(ratio, 2)
        changes: dict[str, Any] = {}
        if profile is not None and profile != bean.get(BEAN_PROFILE):
            changes["profile"] = profile
        if not known and label is not None and RATIO_MIN <= ratio <= RATIO_MAX:
            changes["ratio"] = ratio
            changes["ratio_profile"] = label
        if changes:
            self._write_bean(bean, **changes)

    @callback
    def profile_chosen(self, label: str) -> None:
        """A pump profile was chosen by the user (select, card, service):
        with a bean active it becomes the bean's profile and the bean's
        ratio for it the target (0.7.0b6). Not called for the profile a
        bean selection sets (no echo)."""
        bean = self.active_bean
        if bean is None:
            return
        self._apply_bean_ratio(bean, profile=label)
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
            # Its pump profile first, then its ratio for that profile.
            self._select_bean_profile(bean)
            self._apply_bean_ratio(bean)
        else:
            self._bean_ratio_applied = None
        self._ensure_active_bean()
        self._async_changed()

    def _select_bean_profile(self, bean: dict[str, Any]) -> None:
        """The bean's default pump profile becomes active (0.7.0); an unknown
        one (deleted) is logged and the active profile stays."""
        label = bean.get(BEAN_PROFILE)
        if not label:
            return
        library = self.throttle.profiles.library
        if library.get(label) is None:
            _LOGGER.warning(
                "%s: bean %s: pump profile %s does not exist; the active "
                "profile stays", self.coordinator.address, bean.get("name"), label,
            )
            return
        # Set by the bean: not a user choice (no write back).
        self.throttle.select_profile(label, chosen=False)

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
        self._select_bean_profile(newest)  # like selecting it
        self._apply_bean_ratio(newest)
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
            self._brew_bean_id = self.bean_id
            self._brew_profile = self._active_profile_label()
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
        else:
            ratio, _ = self._bean_ratio(bean, self._active_profile_label())
            if round(ratio, 2) != self._bean_ratio_applied:
                # Edited in the options flow (our own writes set
                # _bean_ratio_applied first: no echo). During a brew it
                # waits for its end like a bean choice; the newest wins.
                if self.coordinator.brew_tracker.active:
                    self._deferred_ratio = ratio
                else:
                    self.ratio = ratio
                self._bean_ratio_applied = round(ratio, 2)
        self.async_watch_switch()
        self._async_changed()

    # ------------------------------------------------------ stop switch

    @callback
    def async_watch_switch(self) -> None:
        """Check the stop switch now and follow its state.

        Here (setup, reload, options change) only a switch that does not
        exist is reported; an unavailable one is reported by the cut that
        cannot switch it off (it may just be powered off with the machine).
        Following its state only clears the issue, once the switch is usable;
        an emptied option clears it too.
        """
        if self._unsub_switch is not None:
            self._unsub_switch()
            self._unsub_switch = None
        if self.coordinator.unloading:
            return
        switch = self.switch_entity
        async_update_stop_switch_issue(
            self.hass, self.coordinator.config_entry, switch, MODE_MISSING
        )
        self._async_update_latency_issue()
        self.throttle.async_watch(switch)
        if switch is None:
            return

        @callback
        def _changed(event: Event[EventStateChangedData]) -> None:
            if self.coordinator.unloading:
                return
            async_clear_stop_switch_issue_if_ok(
                self.hass, self.coordinator.config_entry, switch
            )
            new = event.data["new_state"]
            if new is not None and new.state == STATE_OFF:
                # The pump is off: no cut to retry, no shot to carry over.
                self._below_last = None
                if self._retry is None or not self._retry.inflight:
                    # (An attempt in flight sees the off itself: its call
                    # worked, it reports the cut.)
                    self._cancel_retry("switch off")
                async_clear_cut_failed_issue(
                    self.hass, self.coordinator.config_entry.entry_id
                )
            elif new is not None and new.state == STATE_ON:
                old = event.data["old_state"]
                if old is None or old.state != STATE_ON:
                    # Back from unavailable/unknown: what ran before is not
                    # known to be the same relay start; nothing carries over.
                    self._below_last = None
                    self._retry_cut()  # usable again: retry a failed cut now

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
        self._cancel_retry(None)
        self.throttle.async_stop_watching()
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
    def feed_sample(
        self, t: float, weight: float, placed: float | None = None
    ) -> None:
        """Every weight sample, after the brew tracker has seen it.

        ``t`` is its arrival, ``placed`` its time on the scale's clock when
        it came in a backlog (0.7.0b6, see ``BacklogClock``): the flow, the
        cut rule and the settle check use ``placed``, so a bunch of held-back
        frames is not read as a steep rise; the cut time (pump off) stays
        the arrival.

        Passed through the outlier filter first: a frame that jumps further
        than a shot can pour is held back and decided by the next frame, so
        it never drives a cut, the flow or the settle/lift check on its own.
        """
        self._arrival = t
        if placed is None:
            placed = t
        for ft, fw, shift in self._filter.feed(placed, weight):
            self._feed_accepted(ft, fw, shift)

    @callback
    def _feed_accepted(self, t: float, weight: float, shift: bool) -> None:
        flow = self._flow.feed(t, weight, shift)
        tracker = self.coordinator.brew_tracker
        if self._cut is not None:
            if tracker.start == self._cut.start:
                self._check_delayed(self._cut)
            if self._cut is not None and tracker.start == self._cut.start:
                self._observe(t, weight)
            elif tracker.start is not None:
                # A new brew while an old attempt is still open (its brew
                # ended without an answer): close it, never touch this brew.
                self._conclude(self._final_weight(), REASON_NOT_SETTLED)
        if tracker.state != BREWING:
            self._profile_sample(weight, flow)
            return
        start = tracker.start
        if self._interval_start != start:
            self._intervals.clear()
            self._interval_start = start
        self._intervals.feed(t)
        self._check_cut(start, t, weight, flow)
        # After the cut decision: a pump command never delays a cut.
        self._profile_sample(weight, flow)
        # After the cut decision: the throttle never delays a cut, and an
        # error in it never reaches the cut.
        try:
            target = self.effective_target()[0] if self.enabled else 0.0
            self.throttle.feed(
                t,
                weight,
                flow,
                target=target if self._cut_start != start else 0.0,
                lead=self._intervals.lead,
                disturbed_t=self._filter.disturbed_t,
                flow_count=self._flow.count,
            )
        except Exception:
            now = time.monotonic()
            if self._throttle_error_t is None or now - self._throttle_error_t > 60:
                self._throttle_error_t = now
                _LOGGER.exception(
                    "%s: error in the pump throttle", self.coordinator.address
                )

    @callback
    def _profile_sample(self, weight: float, flow: float) -> None:
        """Pump profiles get every accepted frame, scale timer or not
        (0.7.0); an error there never reaches the auto-stop."""
        try:
            self.throttle.profiles.sample(weight, flow)
        except Exception:
            _LOGGER.exception("%s: error in the pump profile", self.coordinator.address)

    @callback
    def _check_cut(self, start: float, t: float, weight: float, flow: float) -> None:
        """The auto-stop rule for one accepted frame of the running brew."""
        if not self.enabled:
            return
        if self._cut_start == start:
            return  # once per brew
        target, source, dose = self.effective_target()
        if target <= 0:
            return
        if self._below_start != start and self._carry_below(start, t, weight):
            self._below_start = start
            self._carried_start = start
        below = self._below_start == start
        if weight > target + OVERSHOOT_MAX and not below:
            # Far above the target and never below it in this brew: not
            # coffee in a tared cup (cup placed without taring); never cut on
            # that. A brew that WAS below the cut point (resumed after a BLE
            # gap, or carried over) cuts at once instead (audit B1).
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
            if self._below_last is None or self._below_last[0] != start:
                self._below_parked = False
            self._below_last = (start, t, weight, cut_flow)
            return
        if not below:
            # The prediction was never below the target in this brew (cup
            # or weight already there when it started): no crossing.
            return
        self._fire(start, t, weight, flow, target, lead, source, dose)

    def _carry_below(self, start: float, t: float, weight: float) -> bool:
        """A new brew continues an uncut shot (see BELOW_CARRY_SECONDS)."""
        last = self._below_last
        if last is None or last[0] == start or self._cut_start == last[0]:
            return False
        if not self._below_parked:
            return False  # the last brew ended regularly, not by a gap
        elapsed = t - last[1]
        if not 0.0 <= elapsed <= BELOW_CARRY_SECONDS:
            return False
        flow = max(last[3], CARRY_FLOW_MIN) * CARRY_FLOW_FACTOR
        if not (
            last[2] - TAIL_DROP_GRAMS
            <= weight
            <= last[2] + JUMP_GRAMS + flow * elapsed
        ):
            return False  # another cup (lifted, or one not tared)
        switch = self.switch_entity
        state = self.hass.states.get(switch) if switch else None
        if state is None or state.state != STATE_ON:
            return False
        _LOGGER.info(
            "%s: auto-stop: a new brew while the last one was below its "
            "target and not cut, the stop switch still on: same shot",
            self.coordinator.address,
        )
        return True

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
        # Far past the target when cut, or a shot carried over from an
        # uncut brew (a gap in the scale data): the overshoot from here says
        # nothing about the model; never learned.
        late = weight > target + OVERSHOOT_MAX or self._carried_start == start
        if late:
            _LOGGER.info(
                "%s: auto-stop: late cut at %.1f g (target %.1f g) after a "
                "gap in the scale data; not learned",
                self.coordinator.address, weight, target,
            )
        self._cut_start = start
        self._below_last = None
        self._last_cut_target = (
            start, round(target, 1), source, self._arrival if self._arrival is not None else t
        )
        self._cut = Cut(
            start=start, t=t, weight=weight, flow=flow, target=target,
            predicted=predicted, lead=lead, source=source, dose=dose,
            bean=self.brew_bean(start),
            clean=self._flow.count >= FLOW_MIN_FRAMES
            and (disturbed is None or disturbed < t - FLOW_WINDOW)
            and not late,
            late=late,
            nominal_latency=self.latencies.nominal(switch),
            latency_pending=switch is not None,
            throttled=self.throttle.applied_for(start),
        )
        lateness = self.coordinator.brew_tracker.last_lateness
        if lateness is not None and lateness > BACKLOG_LATE_SECONDS:
            self._cut.delayed = True
        self._check_delayed(self._cut)
        self._settle = SettleDetector()
        self._settle.feed(t, weight)
        self._settled = None
        self._settled_t = None
        self._final = None
        self._last_weight = weight
        self._brew_ok = False
        self._timer_stopped = False
        self._cancel_retry(None)  # an older cut's retry: this cut takes over
        # No pump value goes out from here until the relay is off (the
        # restores of the throttle/profile ending below are held back).
        self.throttle.hold_restore()
        # Switch now (eager: the service call is issued before this
        # returns, not awaited). The scale timer keeps running until the
        # weight has settled: while it is paused the scale sends no weight,
        # so the drips would be missed (see _observe).
        self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self._async_switch_off(switch, self._cut),
            "difluid_scale autostop switch",
        )
        # Pump off: the throttle and a profile run end (their restore to
        # 100 % waits for the relay's off report), after the switch-off call
        # has been issued.
        try:
            self.throttle.on_cut(start)
        except Exception:
            _LOGGER.exception("%s: error ending the pump throttle", self.coordinator.address)
        _LOGGER.debug(
            "%s: auto-stop at %.1f g, flow %.2f g/s, predicted +%.2f g, "
            "lead %.2f s (target %.1f)",
            self.coordinator.address, weight, flow, predicted, lead, target,
        )

    async def _async_switch_off(
        self, switch: str | None, cut: Cut, attempt: int = 1
    ) -> None:
        """Turn the stop switch off for ``cut``. ``attempt`` > 1: a retry
        of a failed cut (no latency measurement then: the first attempt
        already ended it, and a late cut is never learned)."""
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
        if (
            attempt == 1
            and state is not None
            and state.state not in (STATE_OFF, STATE_UNAVAILABLE)
        ):
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
            if attempt == 1:
                _LOGGER.warning("Auto-stop could not turn off %s: %s", switch, err)
            else:
                _LOGGER.debug(
                    "Auto-stop retry %d could not turn off %s: %s",
                    attempt, switch, err,
                )
            ok = False
            call_ok = False
        else:
            call_ok = True
            if not ok and attempt == 1:
                _LOGGER.warning(
                    "Auto-stop: stop switch %s is missing or unavailable", switch
                )
        if self.coordinator.unloading:
            return  # unloaded meanwhile: no issue, no event from this instance
        retry = self._retry
        if attempt > 1 and (retry is None or retry.cut is not cut):
            return  # retries ended meanwhile (off, give-up, a new cut)
        async_update_stop_switch_issue(
            self.hass, self.coordinator.config_entry, None if ok else switch
        )
        if ok:
            async_clear_cut_failed_issue(self.hass, self.coordinator.config_entry.entry_id)
            # The pump relay was told to go off: the held pump restore goes
            # out at its off report (or the backstop).
            self.throttle.cut_switched(switch)
            if attempt > 1:
                _LOGGER.info(
                    "%s: auto-stop: %s turned off on retry %d",
                    self.coordinator.address, switch, attempt,
                )
                self._end_retry()
                self._fire_event(EVENT_CUT, cut, attempt=attempt)
        elif retry is not None and attempt > 1:
            retry.inflight = False
            retry.attempts = attempt
            if retry.window_over:
                self._give_up()
            else:
                self._schedule_retry()
        else:
            self._start_retry(cut, switch)
        if attempt > 1:
            return
        if not ok:
            self._fire_event(EVENT_SWITCH_FAILED, cut, attempt=attempt)
        else:
            self._fire_event(EVENT_CUT, cut, attempt=attempt)
        if probe is not None:
            probe.call_done(call_ok)
        else:
            self._switch_latency_known(cut, switch, None)

    @callback
    def _start_retry(self, cut: Cut, switch: str) -> None:
        """The cut's first attempt failed: retry within CUT_RETRY_WINDOW.
        The pump restore stays held meanwhile (the pump may still run)."""
        self._end_retry()
        self._retry = _Retry(cut, switch)
        self._unsub_retry_window = async_call_later(
            self.hass, CUT_RETRY_WINDOW, self._retry_window_over
        )
        # Reported at once (a cup overflows within 20-40 s at 2-3 g/s; the
        # event alone reaches only users with an automation): the issue
        # says it keeps trying, and goes when the cut succeeds or the switch
        # reports off; giving up only changes its text.
        async_create_cut_failed_issue(
            self.hass, self.coordinator.config_entry, switch, 1, ISSUE_STAGE_RETRYING
        )
        self._schedule_retry()

    @callback
    def _schedule_retry(self) -> None:
        """Next timed attempt (unless the switch already reports off)."""
        retry = self._retry
        if retry is None:
            return
        state = self.hass.states.get(retry.switch)
        if state is not None and state.state == STATE_OFF:
            self._end_retry()
            self.throttle.release_restore("relay_off")
            return
        if self._unsub_retry is None:
            delay = (
                CUT_RETRY_INTERVAL
                if retry.attempts < CUT_RETRY_FAST
                else CUT_RETRY_INTERVAL_SLOW
            )
            self._unsub_retry = async_call_later(self.hass, delay, self._retry_timer)

    @callback
    def _retry_timer(self, _now: datetime) -> None:
        self._unsub_retry = None
        self._retry_cut()

    @property
    def cut_retry_pending(self) -> bool:
        """A failed cut is still being retried."""
        return self._retry is not None

    @callback
    def _retry_cut(self) -> None:
        """Retry a failed cut now (timer, or the switch is ``on`` again)."""
        retry = self._retry
        if retry is None or retry.inflight or self.coordinator.unloading:
            return
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None
        if retry.switch != self.switch_entity:
            self._end_retry()  # the stop switch was replaced meanwhile
            self.throttle.release_restore("options")
            return
        retry.inflight = True
        self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self._async_switch_off(retry.switch, retry.cut, retry.attempts + 1),
            "difluid_scale autostop switch retry",
        )

    @callback
    def _retry_window_over(self, _now: datetime) -> None:
        """CUT_RETRY_WINDOW after the first failure: give up (audit B2); an
        attempt still in flight decides first."""
        self._unsub_retry_window = None
        retry = self._retry
        if retry is None or self.coordinator.unloading:
            return
        if retry.inflight:
            retry.window_over = True
            if self._unsub_retry is not None:
                self._unsub_retry()
                self._unsub_retry = None
            return
        self._give_up()

    @callback
    def _give_up(self) -> None:
        retry = self._retry
        if retry is None:
            return
        self._end_retry()
        pump = self.throttle.pump_entity is not None and self.throttle.mode == MODE_THROTTLE
        _LOGGER.warning(
            "%s: auto-stop could not turn off %s within %.0f s (%d attempts); "
            "the machine may still run%s",
            self.coordinator.address, retry.switch, CUT_RETRY_WINDOW,
            retry.attempts, "; the pump goes back to full power" if pump else "",
        )
        async_create_cut_failed_issue(
            self.hass,
            self.coordinator.config_entry,
            retry.switch,
            retry.attempts,
            ISSUE_STAGE_GAVE_UP_PUMP if pump else ISSUE_STAGE_GAVE_UP,
        )
        self._fire_event(EVENT_CUT_FAILED, retry.cut, attempt=retry.attempts)
        # Nothing waits for the cut any more: the normal restore rules apply
        # (a throttled pump back to full power), never a pump held forever.
        self.throttle.release_restore("cut_failed")

    @callback
    def _end_retry(self) -> None:
        self._retry = None
        for name in ("_unsub_retry", "_unsub_retry_window"):
            unsub = getattr(self, name)
            if unsub is not None:
                unsub()
                setattr(self, name, None)

    @callback
    def _cancel_retry(self, why: str | None) -> None:
        if self._retry is not None and why is not None:
            _LOGGER.debug(
                "%s: auto-stop retry ended (%s)", self.coordinator.address, why
            )
        self._end_retry()

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
        # The sample is corrected to the switch's nominal latency as it is
        # now, when the sample is learned: the same rule as
        # ``learn_stop_from_brew`` (audit B9).
        cut.nominal_latency = self.latencies.nominal(switch)
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
    def _track_below(self, result: BrewResult | None, discarded: bool) -> None:
        """Keep the uncut "below the target" brew only while it is parked
        by a gap; a regular end (timer stopped, stored, discarded) or a
        resume clears what a later brew could inherit."""
        last = self._below_last
        if last is None:
            return
        tracker = self.coordinator.brew_tracker
        if tracker.start == last[0]:
            if tracker.state == PARKED:
                self._below_parked = True
            elif tracker.state == BREWING:
                self._below_parked = False  # resumed: the same brew goes on
        ended = (result is not None and result.start == last[0]) or discarded
        if ended and not self._below_parked:
            self._below_last = None

    @callback
    def brew_finished(self, result: BrewResult | None, discarded: bool) -> None:
        """The tracker stored (``result``) or discarded a run; called on
        every tracker step (``result`` None, ``discarded`` False otherwise)."""
        self._track_below(result, discarded)
        cut = self._cut
        if cut is None:
            return
        self._check_delayed(cut)  # frames placed for good at the brew end
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
    def _check_delayed(self, cut: Cut) -> None:
        """Mark the cut when a backlog of its brew lies around it (frames
        placed back in time can show up a few frames later)."""
        if cut.delayed:
            return
        tracker = self.coordinator.brew_tracker
        if tracker.backlog_near(cut.start, cut.t - FLOW_WINDOW, cut.t):
            cut.delayed = True

    @callback
    def apply_deferred_ratio(self) -> None:
        """After a tracker step (the brew stored): once no brew is pending
        any more, a bean's ratio chosen during it becomes the target."""
        if (
            self._deferred_ratio is not None
            and not self.coordinator.brew_tracker.active
        ):
            self.ratio, self._deferred_ratio = self._deferred_ratio, None
            self._async_changed()

    @callback
    def _maybe_learn(self) -> None:
        cut = self._cut
        if cut is None or self._final is None or not self._brew_ok:
            return
        if cut.latency_pending:
            return  # learned once the switch latency is known (<= timeout)
        if cut.delayed:
            # Cut on held-back data or sent late: says nothing about the model.
            self._conclude(self._final, REASON_DELAYED)
            return
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
            self._journal_add(
                cut.flow, overshoot, _iso(cut.start), throttled=cut.throttled
            )
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
            # The data at the cut came in a backlog (0.7.0b6).
            "delayed_data": cut.delayed,
            "bean": cut.bean,
            # The pump throttle slowed this brew down (0.6.0).
            "throttled": cut.throttled,
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
        if cut.throttled and final is not None:
            self.throttled_deviations = [
                *self.throttled_deviations,
                {"start": _iso(cut.start), "deviation": round(final - cut.target, 1)},
            ][-THROTTLED_DEVIATIONS_MAX:]
        _LOGGER.debug("%s: auto-stop result %s", self.coordinator.address, self.last)
        self._fire_event(
            EVENT_LEARNED if reason is None else reason, cut, final=final
        )
        self._async_changed()

    @callback
    def _fire_event(
        self,
        event_type: str,
        cut: Cut,
        final: float | None = None,
        attempt: int | None = None,
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
        if attempt is not None:
            # Cut attempts only: 1 = the cut itself, > 1 = a retry after a
            # failed one (the ``cut`` event of a successful retry).
            data["attempt"] = attempt
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
        throttled: bool = False,
    ) -> None:
        """Journal a sample just learned by ``self.model.update``."""
        if brew_id is None and start is not None:
            # Live cuts are usually learned just before their brew is
            # stored: the id follows in async_brew_stored.
            brew = self.coordinator.brews.find_start(start)
            brew_id = brew["id"] if brew is not None else None
        self.journal.add(
            LearningJournal.entry(
                flow,
                overshoot,
                brew_id,
                start,
                dt_util.utcnow().isoformat(),
                throttled=throttled,
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

    @callback
    def learned_changed(self) -> None:
        """Which brews are learned changed (also the throttle model)."""
        self._learned_changed()

    @callback
    def async_schedule_save(self) -> None:
        """Save the store soon (throttle settings and model)."""
        self._dirty = True
        self._store.async_delay_save(self._data_to_save, 1)

    def learned(self, brew: dict[str, Any]) -> bool:
        """The brew has a sample in a learning journal (stop or pump
        throttle model; deleting it takes that sample out of the model)."""
        return (
            self.journal.index_of(brew.get("id"), brew.get("start")) is not None
            or self.throttle.learned(brew)
        )

    @callback
    def async_brew_stored(self, brew: dict[str, Any]) -> None:
        """A brew was stored: give its journal entries (learned just before,
        by start) the brew id."""
        stop = self.journal.set_brew_id(brew["start"], brew["id"])
        if self.throttle.brew_stored(brew) or stop:
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
        throttled: bool = False,
    ) -> bool:
        """Learn one sample from outside the live cut (past brew); persisted.

        ``throttled``: the pump was throttled in that brew; a sample still
        in the journal keeps its own flag as well (audit B9: it was lost).

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
            self._journal_add(flow, overshoot, start, brew_id, throttled=throttled)
        else:
            throttled = throttled or bool(
                self.journal.entries[index].get("throttled")
            )
            self.journal.remove(brew_id, start)
            self.journal.entries.insert(
                index,
                LearningJournal.entry(
                    flow,
                    overshoot,
                    brew_id,
                    start,
                    dt_util.utcnow().isoformat(),
                    throttled=throttled,
                ),
            )
            self.model = self.journal.replay()
        self._async_changed()
        return True

    @callback
    def async_forget_brew(self, brew_id: str, start: str | None) -> bool:
        """Take a (deleted) brew's samples out of the learning: drop its
        journal entries and replay the model, the same for the pump
        throttle's power model. False (models untouched) if the brew is in
        neither journal."""
        throttle = self.throttle.async_forget_brew(brew_id, start)
        before = len(self.throttled_deviations)
        self.throttled_deviations = [
            d for d in self.throttled_deviations if d.get("start") != start
        ]
        if len(self.throttled_deviations) != before:
            self._async_changed()
        if not self.journal.remove(brew_id, start):
            return throttle
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
        self.throttle.load(data.get("throttle"))
        deviations = data.get("throttled_deviations")
        self.throttled_deviations = [
            {"start": v.get("start"), "deviation": float(v["deviation"])}
            for v in (deviations if isinstance(deviations, list) else [])
            if isinstance(v, dict)
            and isinstance(v.get("deviation"), (int, float))
            and not isinstance(v.get("deviation"), bool)
        ][-THROTTLED_DEVIATIONS_MAX:]
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
            # The stored target ratio stays (also one set by hand): only
            # what the bean has now counts as applied.
            self._bean_ratio_applied = (
                round(self._bean_ratio(bean, self._active_profile_label())[0], 2)
                if bean
                else None
            )
            if settings.get(SETTING_DEFERRED) and bean is not None:
                # Stopped while a bean's ratio waited for a brew end: the
                # brew is gone, the bean's ratio for the active profile
                # applies (not the one the brew ran with).
                self.ratio = self._bean_ratio(bean, self._active_profile_label())[0]
                self._dirty = True
                self._store.async_delay_save(self._data_to_save, 1)
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
        data = {
            SETTING_RATIO: self.ratio,
            SETTING_YIELD: self.target,
            SETTING_BEAN: self.bean_id,
        }
        if self._deferred_ratio is not None:
            # A ratio waiting for the brew end (0.7.0b6): after a restart
            # the ratio is derived from the bean and profile again.
            data[SETTING_DEFERRED] = True
        return data

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
            "throttle": self.throttle.to_store(),
            "throttled_deviations": self.throttled_deviations,
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
