"""Pump throttle, Home Assistant side (0.6.0; maths in ``throttle_model``).

Shot time: the pump relay is the auto-stop's stop switch. Its ``off -> on``
state change marks shot time 0. The time is taken with ``time.monotonic()``
when the state-changed event is received: the listener runs as a callback
in the same event loop turn in which the state was written, so receiving
it is the write time within well under a millisecond, and all later shot
times come from the same monotonic clock (``time_fired`` is wall-clock
time; turning it into monotonic time would mix in clock steps, e.g. NTP).
What neither can see is the delay before HA (Zigbee report of the relay).
A relay that was ``unavailable``/``unknown`` before (or had no state) is no
pump start: its start time is not known.

A brew (scale timer) matches a relay start that lies at most
RELAY_BEFORE before and RELAY_AFTER after the brew start; then for every
brew, in every mode (also ``off``), the brew stores ``relay_offset``
(relay on -> profile t = 0), ``first_drop_shot`` (relay on -> first drop),
``shot_time`` (relay on -> end of the brew time) and
``since_last_relay_off`` (seconds since the stop switch last went
``off`` before this relay start; remembered across restarts in the store,
None until one was seen). It only sees the relay: flushes started with the
machine's own button and steam are invisible to it.

The throttle itself works only while a stop target is active (auto-stop
enabled), a brew is detected, the relay start matched and the cup was
tared (first drop). Without a relay start (brew started with the machine's
button) it stays inactive for that brew and logs why. Descaling and
flushing via the relay are no brews (no scale timer) or start long before
it, so they are never throttled.

Mode ``observe``: the prediction and the trigger are computed, logged,
stored with the brew and reported, with the first power it would have set
(``would_set``, ``would_need_floor``); the pump entity is never written.
``throttle``: the power is set (``number.set_value``; one worker per pump,
the newest value wins, errors only logged: the auto-stop never waits for or
fails on it). Powers are in % of the entity's maximum (``PowerGrid``: any
min/max/step). After a command the command counts as the power in effect
(the entity may confirm late or never; warned once after 2 s).

Every shot starts at full power (decision 0052): in ``throttle`` mode the
pump is set to its maximum when the relay goes on, when the throttle is
set up (and the options change) and when a shot ends (auto-stop cut, relay
off, brew stored or discarded, BLE lost mid-shot, unload) - each time only
if the throttle changed it in this brew OR it stands below its maximum.
After an auto-stop cut that restore is held back (``hold_restore``) until
the relay reports ``off`` (or the cut's call succeeded and HOLD_BACKSTOP
ran out, the next relay start, unload): the pump keeps its throttled power
while it still runs, so the drip after the cut is that of the throttled
shot (learning samples of throttled shots stay consistent) and a failed
cut never leaves the machine running at full power. A
pump left lower (failed restore, restart mid-shot) would otherwise slow the
next shot before any prediction. Never in ``observe``/``off``. The pump
entity is taken at the brew start: an options change mid-shot still resets
the pump that was throttled.

Pump profiles (0.7.0, ``profile_control``/``profiles``): the throttle is
the built-in profile Zielzeit. The active profile is taken at the relay
start: Zielzeit -> this throttle (unchanged), any other profile -> a
profile run (``ProfileControl``) and this throttle stays inactive for the
brew (reason ``profile``). Both write the pump only through this class's
queue and restore rules (``request``/``restore_value``): one writer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import logging
import math
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .beans import BEAN_SHOT_TIME
from .brew import BREWING, FirstDropDetector
from .const import CONF_PUMP_POWER
from .profile_control import ProfileControl
from .profiles import PROFILE_STANDARD, PROFILE_TARGET_TIME
from .stop_model import SWITCH_LATENCY_TIMEOUT, LearningJournal
from .throttle_model import (
    EVENT_CORRECTED,
    PowerGrid,
    EVENT_ENDED,
    EVENT_TRIGGERED,
    MODE_DEFAULT,
    MODE_OFF,
    MODE_THROTTLE,
    MODES,
    POWER_FLOOR_DEFAULT,
    POWER_MAX,
    POWER_STEP_DEFAULT,
    TARGET_TIME_DEFAULT,
    ExponentModel,
    ShotThrottle,
    ThrottleModel,
    fit_exponent,
    parse_throttle_journal,
    throttle_journal,
)

if TYPE_CHECKING:
    from .autostop import AutoStop
    from .coordinator import DifluidCoordinator

_LOGGER = logging.getLogger(__name__)

NUMBER_DOMAIN = "number"
SERVICE_SET_VALUE = "set_value"

# Relay start relative to the brew (scale timer) start that still belongs
# to the brew: the scale's BLE timer start was seen ~10 s late
# (2026-11-20); a timer started by hand just before the pump is allowed too.
RELAY_BEFORE = 20.0  # s
RELAY_AFTER = 5.0  # s
# The predicted shot time sensor is written at most this often.
PREDICTION_WRITE_INTERVAL = 1.0  # s
# Learnable brews looked back on for the exponent lock-in rule.
EXPONENT_RECENT = 10
# After a successful auto-stop ``turn_off`` call the pump restore waits for
# the relay's ``off`` report, at most this long (the switch latency
# measurement's timeout; a relay that never reports its state).
HOLD_BACKSTOP = SWITCH_LATENCY_TIMEOUT  # s
# Unload: wait at most this long for the pump to be set back.
RESTORE_TIMEOUT = 2.0  # s
# A pump entity that has not shown the commanded value this long after the
# command is warned about once (the command keeps counting).
CONFIRM_TIMEOUT = 2.0  # s
# A set_value call that has not returned by then is given up (logged): the
# worker and a restore after it must never hang on one entity.
PUMP_CALL_TIMEOUT = 5.0  # s
# A pump left low is set back at most this often by the pending-restore
# retry (setup, back from unavailable): never a tight loop on an entity
# that does not report or keeps showing a low value.
RESTORE_RETRY_INTERVAL = 30.0  # s

TARGET_TIME_MIN, TARGET_TIME_MAX = 10.0, 90.0
FLOOR_MIN, FLOOR_MAX = 10.0, 100.0

# Why the throttle was not active in a brew (``reason``).
REASON_NO_TARGET = "no_target"
REASON_NO_RELAY = "no_relay_start"
REASON_NO_DROP = "no_first_drop"  # cup not tared
REASON_LATE_TIMER = "timer_after_first_drop"
REASON_PUMP_UNAVAILABLE = "pump_unavailable"
REASON_NO_PUMP = "no_pump"
# The shot ran another pump profile than Zielzeit (0.7.0): no throttle.
REASON_PROFILE = "profile"

SETTING_MODE = "mode"
SETTING_LEARNING = "learning"
SETTING_TARGET_TIME = "target_time"
SETTING_FLOOR = "floor"
# 0.7.0b4: the power profile "Standard" runs at (%, global, not per bean).
SETTING_STANDARD_POWER = "standard_power"
STANDARD_POWER_DEFAULT = 100.0

type ThrottleEventListener = Callable[[str, dict[str, Any]], None]


def default_store() -> dict[str, Any]:
    """The throttle part of the stop store for 1.3 (migration, fresh)."""
    model = ThrottleModel()
    return {
        "settings": {
            SETTING_MODE: MODE_DEFAULT,
            SETTING_LEARNING: False,
            SETTING_TARGET_TIME: TARGET_TIME_DEFAULT,
            SETTING_FLOOR: POWER_FLOOR_DEFAULT,
            SETTING_STANDARD_POWER: STANDARD_POWER_DEFAULT,
        },
        "model": model.to_dict(),
        "journal": throttle_journal(model).to_dict(),
        "last": None,
        "last_relay_off": None,
        "exponent": [],
        "profiles": {"active": PROFILE_STANDARD, "user": []},
    }


def pump_grid(state: Any) -> PowerGrid:
    """The power grid of a pump entity state (min/max/step attributes)."""
    attrs = state.attributes if state is not None else {}
    low = _number(attrs.get("min"))
    high = _number(attrs.get("max"))
    step = _number(attrs.get("step"))
    return PowerGrid(
        0.0 if low is None else low,
        100.0 if high is None else high,
        1.0 if step is None else step,
    )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()



def _finite(value: Any) -> float:
    """A setting's new value as a finite float (NaN/inf rejected)."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"not a finite number: {value!r}")
    return number

def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class _Brew:
    """Throttle state of one brew (by tracker start)."""

    def __init__(self, start: float) -> None:
        self.start = start
        self.relay_mono: float | None = None  # matched relay start
        self.relay_offset: float | None = None  # shot time at profile t = 0
        self.since_last_relay_off: float | None = None
        self.drop = FirstDropDetector()
        self.drop_t: float | None = None
        self.shot: ShotThrottle | None = None
        self.mode = MODE_OFF
        self.reason: str | None = None
        self.reason_logged = False
        self.target_time: float | None = None
        self.touched = False  # the pump was written in this brew
        self.ended = False
        self.end_reason: str | None = None
        self.end_shot_time: float | None = None
        self.learned = False
        self.sample: dict[str, float] | None = None
        self.sample_reason: str | None = None
        self.pump: str | None = None  # pump entity at the brew start
        # Pump profile at the matched relay start (0.7.0); the throttle
        # runs only for the Zielzeit profile.
        self.profile_label: str | None = None
        # Pump profile selected when the brew started (0.7.0b6): stored and
        # sent live with the brew, also without a relay start or with the
        # throttle off, so the card names it - with a stop switch or a pump
        # power entity configured only (None otherwise).
        self.start_profile: str | None = None
        self.throttle_allowed = True
        self.grid: PowerGrid | None = None
        self.command_value: float | None = None  # last entity value set
        self.command_mono: float | None = None


class ThrottleControl:
    """Relay timing, prediction and pump control for the running brew."""

    def __init__(
        self, hass: HomeAssistant, coordinator: DifluidCoordinator, autostop: AutoStop
    ) -> None:
        """Initialize (settings and model come from the stop store)."""
        self.hass = hass
        self.coordinator = coordinator
        self.autostop = autostop
        self.mode = MODE_DEFAULT
        self.learning = False
        self.target_time = TARGET_TIME_DEFAULT
        self.floor = POWER_FLOOR_DEFAULT
        self.standard_power = STANDARD_POWER_DEFAULT
        self.model = ThrottleModel()
        self.journal = throttle_journal()
        # The installation's power-law exponent (prediction).
        self.exponent = ExponentModel()
        # Exponents of really throttled brews (their profile up to the
        # trigger only: an early window, systematically other values) are
        # kept apart; they count only in the lock-in case (see
        # ``prediction_exponent``).
        self.exponent_throttled = ExponentModel()
        # Last EXPONENT_RECENT learnable brews: {"start", "unthrottled"}.
        self.exponent_recent: list[dict[str, Any]] = []
        # Throttle record of the last brew that had a relay start.
        self.last: dict[str, Any] | None = None
        self.last_prediction: float | None = None
        self.last_power: float | None = None
        # Relay: last off -> on (monotonic, wall), last on -> off (wall).
        self._relay_on_mono: float | None = None
        self._relay_on_wall: float | None = None
        self._relay_since_off: float | None = None
        self._relay_off_mono: float | None = None
        self.last_relay_off: float | None = None  # epoch s, stored
        self._unsub_relay: CALLBACK_TYPE | None = None
        self._brew: _Brew | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self._event_listeners: list[ThrottleEventListener] = []
        self._prediction_written_t: float | None = None
        self._tasks: set[asyncio.Task] = set()
        # Pump commands: newest value per entity, one worker sending them.
        self._pump_queue: dict[str, float] = {}
        # Pump entities already warned about for not showing a command.
        self._unconfirmed_warned: set[str | None] = set()
        self._worker: asyncio.Task | None = None
        # Pump profiles (0.7.0): library, active profile, running shot.
        # Writes go through this class's pump queue (single writer).
        self.profiles = ProfileControl(hass, self)
        self._relay_profile: str | None = None
        self._relay_throttle = True
        # Pumps this integration set below their maximum and has not seen
        # back at it yet (stored: survives a restart). Retried whenever the
        # entity shows a number again and at setup, in every mode.
        self.restore_pending: set[str] = set()
        # Last value queued per pump (the pending flag clears only when the
        # entity shows its maximum AND that was the last value asked for).
        self._last_requested: dict[str, float] = {}
        self._retry_mono: dict[str, float] = {}
        self._retry_timers: dict[str, CALLBACK_TYPE] = {}
        # Scale settings (auto stop timing) for the profile repair issues.
        self._unsub_coordinator: CALLBACK_TYPE | None = None
        self._unsub_pumps: CALLBACK_TYPE | None = None
        self._tracked_pumps: frozenset[str] = frozenset()
        # (stop switch, pump entity) last set up by ``async_watch``: only a
        # change of one of them ends a running profile shot (an options save
        # for beans, ratios or profiles does not; audit B4).
        self._watched: tuple[str | None, str | None] | None = None
        # Restore hold (audit B2/B3): from an auto-stop cut until the relay
        # reports off (or the cut's call succeeded and the backstop ran out,
        # or the next relay start / unload) no pump value is sent; restores
        # asked for meanwhile wait here (newest per pump).
        self._hold = False
        self._held: dict[str, float] = {}
        self._unsub_hold_backstop: CALLBACK_TYPE | None = None

    # ---------------------------------------------------------- settings

    @property
    def pump_entity(self) -> str | None:
        """The configured pump power entity (options), None = none."""
        return self.coordinator.config_entry.options.get(CONF_PUMP_POWER) or None

    @callback
    def set_mode(self, mode: str) -> None:
        """off / observe / throttle (takes effect at the next brew)."""
        if mode not in MODES:
            raise ValueError(mode)
        run = self.profiles.current
        if run is not None and not run.ended and run.mode != mode:
            # A running profile shot does not go on in another mode (e.g.
            # throttle -> off must not leave the pump low): it ends here,
            # the pump back to full power if it changed it.
            self.profiles.end("mode")
        self.mode = mode
        self._changed()

    @callback
    def set_learning(self, on: bool) -> None:
        """Learn the power model from throttled shots."""
        self.learning = bool(on)
        self._changed()

    @callback
    def set_target_time(self, value: float) -> None:
        """Global target shot time (s); a bean profile may override it."""
        self.target_time = min(TARGET_TIME_MAX, max(TARGET_TIME_MIN, _finite(value)))
        self._changed()

    @callback
    def set_floor(self, value: float) -> None:
        """Lowest pump power the throttle sets (%)."""
        self.floor = min(FLOOR_MAX, max(FLOOR_MIN, _finite(value)))
        self._changed()

    @callback
    def set_standard_power(self, value: float) -> None:
        """Power of the profile Standard (%, 0.7.0b4); read at the relay
        start, so a change takes effect with the next shot. Below the floor
        it runs at the floor (like every profile power)."""
        self.standard_power = min(POWER_MAX, max(FLOOR_MIN, _finite(value)))
        self._changed()

    @property
    def pump_controllable(self) -> bool:
        """The pump power is really controlled (0.7.0b4): mode throttle and
        a configured pump power entity that exists and shows a number. The
        profile preview greys the Standard power out otherwise."""
        if self.mode != MODE_THROTTLE or not self.pump_entity:
            return False
        state = self.hass.states.get(self.pump_entity)
        return state is not None and _number(state.state) is not None

    def effective_target_time(self) -> float:
        """The active bean's target shot time, else the global one."""
        bean = self.autostop.active_bean
        value = _number(bean.get(BEAN_SHOT_TIME)) if bean else None
        return value if value is not None and value > 0 else self.target_time

    # ------------------------------------------------------------- relay

    @callback
    def async_watch(self, switch: str | None) -> None:
        """Follow the stop switch (the pump relay).

        Called at setup and on every options save: a running profile shot
        ends (pump back to full power) only when the stop switch or the
        pump entity changed, not for bean, ratio or profile edits."""
        previous = self._watched
        self.async_stop_watching()
        key = (switch, self.pump_entity)
        if previous is not None and key != previous:
            self.profiles.end("options")  # relay or pump replaced
            # The old relay is no longer followed: nothing waits for its
            # off report any more.
            self.release_restore("options")
        self._watched = key
        if previous is not None and key != previous:
            # The throttle entities show it (the profile select's
            # ``pump_controllable``, 0.7.0b4).
            self._notify()
        self._ensure_full_power()
        self._track_pumps()
        for pump in sorted(self.restore_pending):
            self._retry_restore(pump)
        if self._unsub_coordinator is None:
            self._unsub_coordinator = self.coordinator.async_add_listener(
                self.profiles.check_issues
            )
        self.profiles.check_issues()
        if switch is None:
            return
        self._unsub_relay = async_track_state_change_event(
            self.hass, [switch], self._relay_changed
        )

    @callback
    def async_stop_watching(self) -> None:
        """Stop following the relay."""
        self._watched = None
        if self._unsub_relay is not None:
            self._unsub_relay()
            self._unsub_relay = None

    @callback
    def _relay_changed(self, event: Event[EventStateChangedData]) -> None:
        if self.coordinator.unloading:
            return
        old, new = event.data["old_state"], event.data["new_state"]
        if new is None or new.state != STATE_ON:
            # Off, unavailable, unknown or removed: a profile shot ends
            # (pump back to full power); never held low on a lost relay.
            self.profiles.end(
                "relay_off" if new is not None and new.state == STATE_OFF
                else "relay_lost"
            )
        if new is None or new.state not in (STATE_ON, STATE_OFF):
            brew = self._brew  # the throttle (Zielzeit) too
            if brew is not None and not brew.ended and brew.shot is not None:
                self._end(brew, "relay_lost", self._shot_time(brew))
        if (
            new is not None
            and new.state == STATE_ON
            and (old is None or old.state != STATE_ON)
            and not self.autostop.cut_retry_pending
        ):
            # The relay is on again (a new shot, or back from unavailable)
            # and no failed cut waits for it: nothing to hold back any more.
            self.release_restore("relay_on")
        if new is None or old is None:
            return
        if new.state == STATE_ON and old.state == STATE_OFF:
            # A new shot: whatever the last cut held back goes out now.
            self.release_restore("relay_on")
            # Every shot starts at full power.
            self._ensure_full_power()
            now_wall = time.time()
            self._relay_on_mono = time.monotonic()
            self._relay_on_wall = now_wall
            self._relay_off_mono = None
            self._relay_since_off = (
                round(now_wall - self.last_relay_off, 1)
                if self.last_relay_off is not None
                and now_wall >= self.last_relay_off
                else None
            )
            # The profile is decided once per shot: the throttle (Zielzeit)
            # or a profile run, never both.
            self._relay_profile = self.profiles.active.label
            self._relay_throttle = self.profiles.throttle_profile
            self.profiles.relay_on(self._relay_on_mono)
        elif new.state == STATE_OFF and old.state == STATE_ON:
            self._relay_off_mono = time.monotonic()
            self.last_relay_off = time.time()
            self.autostop.async_schedule_save()
            self.profiles.end("relay_off")
            brew = self._brew
            if brew is not None and not brew.ended and brew.shot is not None:
                self._end(brew, "relay_off", self._shot_time(brew))
        if new.state == STATE_OFF:
            # The pump is off now (from on or from unavailable): a restore
            # held back since the cut can go out.
            self.release_restore("relay_off")

    # ------------------------------------------------------------ frames

    def _shot_time(self, brew: _Brew) -> float | None:
        if brew.relay_mono is None:
            return None
        return time.monotonic() - brew.relay_mono

    def _match_relay(self, brew: _Brew) -> None:
        """Take the last relay start if it belongs to this brew.

        Brew start and relay start are compared at the receipt of the
        newest frame (wall clock of the tracker vs. the monotonic clock,
        both read now): a frame the outlier filter held back carries an
        older ``t`` and must not shift the offset."""
        relay = self._relay_on_mono
        if relay is None or brew.relay_mono is not None:
            return
        sample = self.coordinator.brew_tracker.last_sample
        if sample is None:
            return
        now = time.monotonic()
        brew_start_mono = now - (sample[0] - brew.start)
        delta = relay - brew_start_mono  # < 0: relay first
        if self._relay_off_mono is not None and self._relay_off_mono < brew_start_mono:
            return  # pump already off before this brew started
        if -RELAY_BEFORE <= delta <= RELAY_AFTER:
            brew.relay_mono = relay
            brew.relay_offset = round(-delta, 2)
            brew.since_last_relay_off = self._relay_since_off
            brew.profile_label = self._relay_profile
            brew.throttle_allowed = self._relay_throttle
            self.profiles.brew_matched(brew)

    @callback
    def feed(
        self,
        t: float,
        weight: float,
        flow: float,
        *,
        target: float,
        lead: float,
        disturbed_t: float | None,
        flow_count: int,
    ) -> None:
        """An accepted frame (after the cut check). ``target`` g, 0 = no
        stop target active."""
        tracker = self.coordinator.brew_tracker
        start = tracker.start
        if tracker.state != BREWING or start is None:
            return
        brew = self._brew
        if brew is None or brew.start != start:
            brew = self._brew = _Brew(start)
            brew.mode = self.mode
            brew.target_time = self.effective_target_time()
            brew.pump = self.pump_entity
            # Only where pump profiles matter at all: a stop switch or a
            # pump power entity (a scale-only setup shows no profile).
            if self.autostop.switch_entity or self.pump_entity:
                brew.start_profile = self.profiles.active.label
        if brew.ended:
            return
        rel = t - start
        self._match_relay(brew)
        drop = brew.drop.feed(rel, weight)
        if drop is not None and brew.drop_t is None:
            brew.drop_t = drop
        self.profiles.frame(brew)
        if brew.mode == MODE_OFF:
            return
        if brew.relay_mono is None:
            if drop is not None and rel >= drop + 3.0:
                self._inactive(brew, REASON_NO_RELAY)
            return
        if not brew.throttle_allowed:
            self._inactive(brew, REASON_PROFILE)
            return
        if target <= 0:
            self._inactive(brew, REASON_NO_TARGET)
            return
        if brew.drop.untared:
            self._inactive(
                brew, REASON_LATE_TIMER if brew.drop.late_start else REASON_NO_DROP
            )
            return
        shot_time = self._shot_time(brew)
        assert shot_time is not None
        if brew.shot is None:
            brew.shot = self._new_shot(brew)
        shot = brew.shot
        power = self._power_in_effect(brew, shot)
        was_triggered = shot.triggered
        new = shot.feed(
            rel,
            weight,
            flow,
            shot_time,
            drop=drop,
            target=target,
            lead=lead,
            power=power,
            disturbed_t=None if disturbed_t is None else disturbed_t - start,
            flow_count=flow_count,
        )
        self._learn_if_measured(brew)
        self._write_prediction(shot, t)
        if new is None:
            return
        event = EVENT_TRIGGERED if not was_triggered else EVENT_CORRECTED
        if shot.apply:
            if not self._set_pump(brew, new):
                return  # same entity value as in effect: nothing to do
        self.last_power = new
        _LOGGER.debug(
            "%s: throttle %s (%s) at shot time %.1f s: power %s %%, predicted "
            "end %.1f s, target %.1f s, wanted flow %.2f g/s",
            self.coordinator.address, event, brew.mode, shot_time, new,
            shot.predicted_at_trigger if shot.prediction is None else shot.prediction,
            shot.target_time, shot.target_flow or 0.0,
        )
        self._fire(event, brew, shot_time)
        self._notify()

    def _new_shot(self, brew: _Brew) -> ShotThrottle:
        apply = brew.mode == MODE_THROTTLE
        pump = brew.pump
        grid = PowerGrid(0.0, 100.0, POWER_STEP_DEFAULT)
        if apply:
            state = self.hass.states.get(pump) if pump else None
            if pump is None:
                apply = False
                brew.reason = REASON_NO_PUMP
            elif state is None or _number(state.state) is None:
                apply = False
                brew.reason = REASON_PUMP_UNAVAILABLE
            if not apply:
                _LOGGER.warning(
                    "%s: throttle mode, but the pump power entity is %s: "
                    "this brew is only observed",
                    self.coordinator.address,
                    "not configured" if pump is None else f"unavailable ({pump})",
                )
            else:
                grid = pump_grid(state)
        brew.grid = grid
        return ShotThrottle(
            target_time=brew.target_time or self.target_time,
            floor=self.floor,
            model=self.model,
            stop_model=self.autostop.model,
            grid=grid,
            apply=apply,
            exponent=self.prediction_exponent,
            learn=self.learning,
        )

    def _power_in_effect(self, brew: _Brew, shot: ShotThrottle) -> float:
        """Power in effect (% of max). After a command, the command until
        the entity shows it (a pump without feedback is warned about once,
        CONFIRM_TIMEOUT after the command); before, the entity's state.
        100 % when only observing (the pump is never changed then)."""
        if not shot.apply:
            return POWER_MAX
        grid = brew.grid or PowerGrid()
        state = self.hass.states.get(brew.pump) if brew.pump else None
        value = _number(state.state) if state is not None else None
        if brew.command_value is not None:
            tolerance = grid.step / 2 if grid.step else 1e-3
            if value is not None and abs(value - brew.command_value) <= tolerance:
                return grid.percent(value)
            if (
                brew.pump not in self._unconfirmed_warned
                and brew.command_mono is not None
                and time.monotonic() - brew.command_mono >= CONFIRM_TIMEOUT
            ):
                self._unconfirmed_warned.add(brew.pump)
                _LOGGER.warning(
                    "%s: pump power entity %s does not show the commanded %s "
                    "(state %s); the throttle goes on with the command",
                    self.coordinator.address, brew.pump, brew.command_value,
                    None if state is None else state.state,
                )
            return grid.percent(brew.command_value)
        if value is None:
            return shot.command if shot.command is not None else POWER_MAX
        return min(POWER_MAX, max(0.0, grid.percent(value)))

    def _inactive(self, brew: _Brew, reason: str) -> None:
        brew.reason = reason
        if not brew.reason_logged:
            brew.reason_logged = True
            _LOGGER.debug(
                "%s: pump throttle inactive for this brew: %s",
                self.coordinator.address,
                reason,
            )

    def _write_prediction(self, shot: ShotThrottle, t: float) -> None:
        pred = shot.prediction
        if pred is None or not math.isfinite(pred):
            return
        value = round(pred, 1)
        if value == self.last_prediction:
            return
        last = self._prediction_written_t
        if last is not None and 0 <= t - last < PREDICTION_WRITE_INTERVAL:
            return
        self._prediction_written_t = t
        self.last_prediction = value
        self._notify()

    def _learn_if_measured(self, brew: _Brew) -> None:
        shot = brew.shot
        if shot is None or brew.sample is not None or shot.sample is None:
            if shot is not None and shot.sample_reason and brew.sample_reason is None:
                brew.sample_reason = shot.sample_reason
            return
        brew.sample = dict(shot.sample)
        if not (self.learning and shot.apply and brew.touched):
            return
        if self.model.update(**shot.sample):
            self.journal.add(
                LearningJournal.make(
                    dict(shot.sample), None, _iso(brew.start), dt_util.utcnow().isoformat()
                )
            )
            brew.learned = True
            self.autostop.learned_changed()
            self._changed()

    # -------------------------------------------------------------- pump

    def _set_pump(self, brew: _Brew, percent: float) -> bool:
        """Command ``percent``; False if that is the entity value in effect
        already (no call, no event)."""
        pump = brew.pump
        if pump is None:
            return False
        grid = brew.grid or pump_grid(self.hass.states.get(pump))
        value = grid.value(percent)
        state = self.hass.states.get(pump)
        current = brew.command_value
        if current is None and state is not None:
            current = _number(state.state)
        if current is not None and abs(current - value) < 1e-6:
            return False
        if self._hold:
            # Held back after a cut: nothing goes out, so nothing may count
            # as the power in effect either.
            return False
        brew.touched = True
        brew.command_value = value
        brew.command_mono = time.monotonic()
        self._request(pump, value)
        return True

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = self.coordinator.config_entry.async_create_background_task(
            self.hass, coro, "difluid_scale throttle pump"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _request(self, pump: str, value: float) -> asyncio.Task | None:
        """Queue a pump value: one worker sends them in order, the newest
        value per pump wins (a late correction never overtakes a restore).
        A value below the maximum marks the pump ``restore_pending``.

        During a restore hold (after an auto-stop cut, until the relay is
        off) the value is kept back instead (newest per pump) and sent when
        the hold is released; None then."""
        if self._hold:
            self._held[pump] = value
            return None
        high = pump_grid(self.hass.states.get(pump)).high
        self._last_requested[pump] = value
        if value < high - 1e-6 and pump not in self.restore_pending:
            self.restore_pending.add(pump)
            self._track_pumps()
            self.autostop.async_schedule_save()
        self._pump_queue[pump] = value
        if self._worker is None or self._worker.done():
            self._worker = self._spawn(self._async_pump_worker())
        return self._worker

    async def _async_pump_worker(self) -> None:
        while self._pump_queue:
            pump = next(iter(self._pump_queue))
            value = self._pump_queue.pop(pump)
            if not await self._async_set_pump(pump, value):
                self._command_failed(pump, value)
            elif (
                pump in self.restore_pending
                and pump not in self._pump_queue
                and self._last_requested.get(pump) == value
                and self._at_max(pump, value)
                and self._at_max(pump, None)
            ):
                # The maximum was sent and the entity shows it already (it
                # did not change, so no state event will come). Only for a
                # value AT the maximum: after a low value the entity's state
                # is still the old one until it reports (ESPHome, MQTT).
                self._clear_pending(pump)

    @callback
    def _command_failed(self, pump: str, value: float) -> None:
        """A set failed: the pump still runs at its old value, so the
        state counts again, not the command."""
        brew = self._brew
        if brew is not None and brew.pump == pump and brew.command_value == value:
            brew.command_value = None
            brew.command_mono = None
        self.profiles.command_failed(pump, value)
        if pump in self.restore_pending and self._at_max(pump, value):
            # A failed restore does not use up the retry interval: the next
            # "back from unavailable" retries at once.
            self._retry_mono.pop(pump, None)

    async def _async_set_pump(self, pump: str, value: float) -> bool:
        """Set the pump power; never raises (logged)."""
        try:
            async with asyncio.timeout(PUMP_CALL_TIMEOUT):
                await self.hass.services.async_call(
                    NUMBER_DOMAIN,
                    SERVICE_SET_VALUE,
                    {ATTR_ENTITY_ID: pump, "value": value},
                    blocking=True,
                )
        except Exception as err:  # noqa: BLE001 - the shot must never fail on it
            _LOGGER.warning(
                "Pump throttle could not set %s to %s: %s", pump, value, err
            )
            return False
        return True

    def _restore_needed(self, pump: str | None, touched: bool) -> float | None:
        """Entity value to set back (its max = 100 %), None = leave it."""
        if pump is None:
            return None
        state = self.hass.states.get(pump)
        if state is None:
            return None
        high = pump_grid(state).high
        value = _number(state.state)
        if touched or (value is not None and value < high - 1e-6):
            return high
        return None

    @callback
    def _ensure_full_power(self) -> None:
        """Throttle mode: a pump below its maximum is set to it (every
        shot starts at full power). Not while a throttled shot runs."""
        if self.mode != MODE_THROTTLE or self.coordinator.unloading:
            return
        brew = self._brew
        if brew is not None and not brew.ended and brew.touched:
            return
        if self.profiles.running:
            return
        pump = self.pump_entity
        value = self._restore_needed(pump, False)
        if pump is not None and value is not None:
            _LOGGER.debug("%s: pump %s back to full power", self.coordinator.address, pump)
            self._request(pump, value)

    def _restore(self, brew: _Brew) -> tuple[bool, asyncio.Task | None]:
        """End of a shot: its pump back to full power (throttle mode).
        Returns (a restore was asked for, the worker task; None while the
        restore is held back after a cut)."""
        if brew.mode != MODE_THROTTLE and self.mode != MODE_THROTTLE:
            return False, None
        if self.profiles.running:
            return False, None  # a profile shot still runs: it restores at its end
        pump = brew.pump if brew.pump is not None else self.pump_entity
        value = self._restore_needed(pump, brew.touched)
        if pump is None or value is None:
            return False, None
        brew.command_value = value
        return True, self._request(pump, value)

    # ------------------------------------------------- pending restores

    @callback
    def _track_pumps(self) -> None:
        """Follow the configured pump and every pump left low."""
        pumps = frozenset(
            p for p in (*self.restore_pending, self.pump_entity) if p
        )
        if pumps == self._tracked_pumps and self._unsub_pumps is not None:
            return
        if self._unsub_pumps is not None:
            self._unsub_pumps()
            self._unsub_pumps = None
        self._tracked_pumps = pumps
        if pumps:
            self._unsub_pumps = async_track_state_change_event(
                self.hass, sorted(pumps), self._pump_changed
            )

    def _shot_controls(self, pump: str) -> bool:
        """A running shot (throttle or profile) set this pump on purpose."""
        run = self.profiles.current
        if run is not None and not run.ended and run.touched and run.pump == pump:
            return True
        brew = self._brew
        return (
            brew is not None and not brew.ended and brew.touched and brew.pump == pump
        )

    def _at_max(self, pump: str, value: float | None) -> bool:
        """``value`` (None: the entity's state) is the pump's maximum.

        Tolerance: the smaller of half a grid step and 0.5 % of the range
        (99.9 of 100 counts, an entity reporting on a finer grid), so the
        next lower grid value (e.g. 90 on a step-10 grid) never counts."""
        state = self.hass.states.get(pump)
        if value is None:
            value = _number(state.state) if state is not None else None
            if value is None:
                return False
        grid = pump_grid(state)
        tol = (grid.high - grid.low) * 0.005
        if grid.step:
            tol = min(tol, grid.step / 2)
        return value >= grid.high - max(tol, 1e-6)

    @callback
    def _clear_pending(self, pump: str) -> None:
        if pump in self.restore_pending:
            self.restore_pending.discard(pump)
            self._retry_mono.pop(pump, None)
            self._cancel_retry_timer(pump)
            self.autostop.async_schedule_save()

    @callback
    def _cancel_retry_timer(self, pump: str | None = None) -> None:
        for key in [pump] if pump is not None else list(self._retry_timers):
            cancel = self._retry_timers.pop(key, None)
            if cancel is not None:
                cancel()

    @callback
    def _pump_changed(self, event: Event[EventStateChangedData]) -> None:
        """A followed pump's state changed: clears the pending flag when it
        shows its maximum after a maximum was asked for; retries when a
        pending pump becomes a number again (back from unavailable). Never
        on its own commands' echoes: no tight loop."""
        if self.coordinator.unloading:
            return
        old, new = event.data["old_state"], event.data["new_state"]
        if event.data["entity_id"] == self.pump_entity and (
            (old is not None and _number(old.state) is not None)
            != (new is not None and _number(new.state) is not None)
        ):
            # Usable <-> unavailable/removed: ``pump_controllable`` changed.
            self._notify()
        if new is not None and new.entity_id == self.pump_entity:
            self.profiles.check_issues()  # its min may have changed
        if new is None or new.entity_id not in self.restore_pending:
            return
        pump = new.entity_id
        value = _number(new.state)
        if value is None:
            return
        last = self._last_requested.get(pump)
        if self._at_max(pump, value):
            if (last is None or self._at_max(pump, last)) and not self._shot_controls(pump):
                self._clear_pending(pump)
            return
        if old is None or _number(old.state) is None:
            self._retry_restore(pump)  # back from unavailable/unknown, low

    @callback
    def _retry_restore(self, pump: str) -> None:
        """Pending pump (setup, or back from unavailable): at its maximum
        and nothing lower asked for -> cleared; lower and no shot controls
        it -> set to its maximum (any mode), at most once per
        RESTORE_RETRY_INTERVAL."""
        if pump not in self.restore_pending:
            return
        state = self.hass.states.get(pump)
        if state is None or _number(state.state) is None:
            return  # unavailable: retried when it shows a number
        last = self._last_requested.get(pump)
        if self._at_max(pump, None):
            if last is None or self._at_max(pump, last):
                self._clear_pending(pump)
            return
        if self._shot_controls(pump):
            return
        now = time.monotonic()
        before = self._retry_mono.get(pump)
        if before is not None and now - before < RESTORE_RETRY_INTERVAL:
            # Limited: one deferred retry at the end of the interval (never
            # more than one pending timer per pump; no loop, never lost).
            if pump not in self._retry_timers:
                delay = RESTORE_RETRY_INTERVAL - (now - before) + 0.1

                @callback
                def _deferred(_now: Any) -> None:
                    self._retry_timers.pop(pump, None)
                    if not self.coordinator.unloading:
                        self._retry_restore(pump)

                self._retry_timers[pump] = async_call_later(self.hass, delay, _deferred)
            return
        self._retry_mono[pump] = now
        high = pump_grid(state).high
        _LOGGER.info(
            "%s: pump %s was left at %s by this integration: back to full power",
            self.coordinator.address, pump, state.state,
        )
        self._request(pump, high)

    # --------------------------------------------------------------- end

    @callback
    def _end(
        self, brew: _Brew, reason: str, shot_time: float | None
    ) -> asyncio.Task | None:
        """The shot is over: final verdict, event, pump back to 100 %."""
        if brew.ended:
            return None
        brew.ended = True
        brew.end_reason = reason
        if shot_time is not None:
            brew.end_shot_time = round(shot_time, 2)
        shot = brew.shot
        if shot is not None:
            shot.finish(shot_time)
        touched = brew.touched
        asked, task = self._restore(brew)
        if asked:
            if shot is not None and shot.apply and shot.power_trace and touched:
                end_t = shot.power_trace[-1][0]
                if shot_time is not None and brew.relay_offset is not None:
                    end_t = max(end_t, round(shot_time - brew.relay_offset, 2))
                shot.power_trace.append([end_t, POWER_MAX])
        if shot is not None and shot.triggered:
            self._fire(EVENT_ENDED, brew, shot_time)
        self.last = self.record(brew)
        self._changed()
        return task

    @callback
    def _close(self, brew: _Brew) -> None:
        """End a brew in which the throttle never ran (inactive or off):
        its record (with the reason) becomes ``last``."""
        brew.ended = True
        self._restore(brew)
        record = self.record(brew)
        if record is not None:
            self.last = record
            self._changed()

    @callback
    def hold_restore(self) -> None:
        """An auto-stop cut is being issued: no pump value goes out until
        the relay is off (audit B3: the pump went to 100 % in the same loop
        turn as ``turn_off``, i.e. while the pump still ran, which raised
        the overshoot of throttled shots and biased their learning
        samples). A failed cut keeps the hold (audit B2): the pump is not
        set back to full power while it still runs."""
        self._hold = True
        self._cancel_hold_backstop()

    @callback
    def cut_switched(self, switch: str) -> None:
        """The cut's ``turn_off`` call succeeded: release when the relay
        reports ``off``; at once if it shows ``off`` already, else after
        the backstop (a relay that does not report its state)."""
        if not self._hold:
            return
        state = self.hass.states.get(switch)
        if state is not None and state.state == STATE_OFF:
            self.release_restore("relay_off")
            return
        self._cancel_hold_backstop()

        @callback
        def _backstop(_now: Any) -> None:
            self._unsub_hold_backstop = None
            if not self.coordinator.unloading:
                self.release_restore("backstop")

        self._unsub_hold_backstop = async_call_later(
            self.hass, HOLD_BACKSTOP, _backstop
        )

    @callback
    def release_restore(self, why: str) -> None:
        """End the restore hold: the restores asked for meanwhile go out."""
        self._cancel_hold_backstop()
        if not self._hold:
            return
        self._hold = False
        held, self._held = self._held, {}
        if held:
            _LOGGER.debug(
                "%s: pump restore released (%s): %s",
                self.coordinator.address, why, held,
            )
        for pump, value in held.items():
            self._request(pump, value)

    @property
    def restore_held(self) -> bool:
        """A cut holds the pump restores back (until the relay is off)."""
        return self._hold

    @callback
    def _cancel_hold_backstop(self) -> None:
        if self._unsub_hold_backstop is not None:
            self._unsub_hold_backstop()
            self._unsub_hold_backstop = None

    @callback
    def on_cut(self, start: float) -> None:
        """The auto-stop cut this brew (pump off)."""
        self.profiles.end("cut")
        brew = self._brew
        if brew is not None and brew.start == start:
            if brew.shot is None:
                self._close(brew)  # nothing to finish; keep relay data
                return
            self._end(brew, "cut", self._shot_time(brew))

    @callback
    def brew_finished(self, start: float | None) -> None:
        """The tracker stored or discarded the brew that started at ``start``."""
        brew = self._brew
        if brew is None or start is None or brew.start != start:
            return
        if not brew.ended:
            if brew.shot is None:
                self._close(brew)
            else:
                self._end(brew, "brew_end", self._shot_time(brew))

    @callback
    def brew_discarded(self) -> None:
        """The tracker discarded the run the throttle follows."""
        brew = self._brew
        if brew is not None and not brew.ended:
            self.brew_finished(brew.start)

    @callback
    def async_ble_lost(self, reason: str = "ble_lost") -> None:
        """No weight any more (the scale disconnected, or ``no_weight``: it
        shows a unit without a gram conversion): a throttled pump goes back
        to 100 % now. A pump profile goes on (it runs without the scale)."""
        brew = self._brew
        if brew is not None and not brew.ended and brew.shot is not None:
            self._end(brew, reason, self._shot_time(brew))

    async def async_shutdown(self) -> None:
        """Unload: end a running shot, set the pump back (bounded wait)."""
        # Nothing of this instance waits for the relay any more: restores
        # held back since a cut go out now.
        self.release_restore("unloaded")
        self.async_stop_watching()
        self._cancel_retry_timer()
        if self._unsub_coordinator is not None:
            self._unsub_coordinator()
            self._unsub_coordinator = None
        if self._unsub_pumps is not None:
            self._unsub_pumps()
            self._unsub_pumps = None
            self._tracked_pumps = frozenset()
        brew = self._brew
        task = self.profiles.end("unloaded")
        if brew is not None and not brew.ended and brew.shot is not None:
            task = self._end(brew, "unloaded", self._shot_time(brew)) or task
        worker = task or self._worker
        if worker is not None and not worker.done():
            try:
                await asyncio.wait_for(asyncio.shield(worker), RESTORE_TIMEOUT)
            except TimeoutError:
                _LOGGER.warning("Pump throttle: restoring the pump timed out")

    # ---------------------------------------------------------- results

    def applied_for(self, start: float | None) -> bool:
        """The pump was really throttled in the brew that started at ``start``."""
        brew = self._brew
        if brew is None or start is None or brew.start != start:
            return False
        if self.profiles.changed_pump(brew):
            return True  # a profile run set it below 100 %
        return brew.touched and brew.shot is not None and brew.shot.changed

    def relay_for(self, start: float | None) -> dict[str, Any] | None:
        """Relay timing of that brew (None without a matched relay start)."""
        brew = self._brew
        if brew is None or start is None or brew.start != start:
            return None
        if brew.relay_offset is None:
            return None
        return {
            "relay_offset": brew.relay_offset,
            "since_last_relay_off": brew.since_last_relay_off,
        }

    def record(self, brew: _Brew | None) -> dict[str, Any] | None:
        """The throttle part stored with a brew (None: no brew seen). Mode
        off: only ``profile`` (0.7.0b6; no throttle fields made up)."""
        if brew is None:
            return None
        if brew.mode == MODE_OFF:
            return self._profile_only(brew)
        shot = brew.shot
        data: dict[str, Any] = {
            "mode": brew.mode,
            "active": shot is not None,
            "reason": brew.reason,
            "target_time": brew.target_time,
            "applied": bool(shot is not None and shot.apply and brew.touched),
            # 0.7.0b3: the throttle controlled the pump (throttle mode, usable
            # pump entity), whether or not it lowered it; the card's line
            # style (dashed = not controlled), the same live and stored.
            "controlled": bool(shot is not None and shot.apply),
            # A pump power entity was configured at the brew start: without
            # one the power trace is an assumption, not set or read (0.7.0b3).
            "pump": brew.pump is not None,
            "triggered": bool(shot and shot.triggered),
            "too_late": bool(shot and shot.too_late),
            "trigger_shot_time": None,
            "predicted_at_trigger": None,
            "unreachable": bool(shot and shot.unreachable),
            "unreachable_reason": None if shot is None else shot.unreachable_reason,
            "would_set": None if shot is None else shot.would_set,
            "would_need_floor": bool(shot and shot.would_need_floor),
            "grind_finer": bool(shot and shot.grind_finer),
            "end_shot_time": brew.end_shot_time,
            "end_reason": brew.end_reason,
            "power": [list(p) for p in shot.power_trace] if shot else [],
            "learned": brew.learned,
            # Exponent the prediction used (running shot's, shrunk to the
            # installation's); frozen at the trigger.
            "prediction_exponent": None if shot is None else round(shot.exponent_used, 3),
            "sample": brew.sample,
            "sample_reason": brew.sample_reason,
        }
        if shot is not None and shot.triggered:
            data["trigger_shot_time"] = round(shot.trigger_shot_time or 0.0, 2)
            pred = shot.predicted_at_trigger
            data["predicted_at_trigger"] = (
                round(pred, 2) if pred is not None and math.isfinite(pred) else None
            )
        data.update(self.profiles.record_part(brew))
        return data

    def brew_record(self, start: float | None) -> dict[str, Any] | None:
        """``record`` of the brew that started at ``start``."""
        brew = self._brew
        if brew is None or start is None or brew.start != start:
            return None
        return self.record(brew)

    def live_info(self, start: float) -> dict[str, Any] | None:
        """Live values for the card (power trace, target time, offset)."""
        brew = self._brew
        if brew is None or brew.start != start:
            return None
        if brew.relay_offset is None and brew.shot is None:
            return self._profile_only(brew)
        shot = brew.shot
        info: dict[str, Any] = {
            "relay_offset": brew.relay_offset,
            "first_drop_shot": None
            if brew.drop_t is None or brew.relay_offset is None
            else round(brew.drop_t + brew.relay_offset, 1),
            "mode": brew.mode,
            "target_time": brew.target_time if brew.mode != MODE_OFF else None,
            "pump": brew.pump is not None,
        }
        if shot is not None:
            pred = shot.prediction
            info.update(
                {
                    "power": [list(p) for p in shot.power_trace],
                    "applied": shot.apply,
                    "controlled": bool(shot.apply),
                    "triggered": shot.triggered,
                    "unreachable": shot.unreachable,
                    "unreachable_reason": shot.unreachable_reason,
                    "would_set": shot.would_set,
                    "would_need_floor": shot.would_need_floor,
                    "grind_finer": shot.grind_finer,
                    "prediction": round(pred, 1)
                    if pred is not None and math.isfinite(pred)
                    else None,
                }
            )
        if brew.mode != MODE_OFF:
            info.update(self.profiles.live_part(brew))
        if not info.get("profile") and brew.start_profile:
            info["profile"] = brew.start_profile
        return info

    @staticmethod
    def _profile_only(brew: _Brew) -> dict[str, Any] | None:
        """Only the brew's pump profile (0.7.0b6: the card always shows it),
        None without one."""
        label = brew.profile_label or brew.start_profile
        return {"profile": label} if label else None

    # ------------------------------------------------------------ events

    def _fire(self, event_type: str, brew: _Brew, shot_time: float | None) -> None:
        shot = brew.shot
        assert shot is not None
        pred = shot.prediction
        data = {
            "mode": brew.mode,
            "shot_time": None if shot_time is None else round(shot_time, 1),
            "target_time": shot.target_time,
            "predicted_shot_time": round(pred, 1)
            if pred is not None and math.isfinite(pred)
            else None,
            "predicted_at_trigger": None
            if shot.predicted_at_trigger is None
            else round(shot.predicted_at_trigger, 1),
            "power": shot.command,
            "applied": shot.apply,
            "target_flow": None
            if shot.target_flow is None
            else round(shot.target_flow, 2),
            "target_weight": None
            if shot.target_now is None
            else round(shot.target_now, 1),
            "unreachable": shot.unreachable,
            "unreachable_reason": shot.unreachable_reason,
            "would_set": shot.would_set,
            "would_need_floor": shot.would_need_floor,
            "grind_finer": shot.grind_finer,
            "bean": self.autostop.brew_bean(brew.start),
        }
        if event_type == EVENT_ENDED:
            data["end_reason"] = brew.end_reason
        for listener in list(self._event_listeners):
            try:
                listener(event_type, data)
            except Exception:
                _LOGGER.exception("Error in a throttle event listener")

    @callback
    def async_add_event_listener(
        self, listener: ThrottleEventListener
    ) -> Callable[[], None]:
        """Call ``listener(event_type, data)`` for every throttle event."""
        self._event_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._event_listeners:
                self._event_listeners.remove(listener)

        return _remove

    # ----------------------------------------------------- model/journal

    def learned(self, brew: dict[str, Any]) -> bool:
        """The brew has a sample in the throttle learning journal."""
        return self.journal.index_of(brew.get("id"), brew.get("start")) is not None

    def brew_stored(self, brew: dict[str, Any]) -> bool:
        """Give the journal entry learned just before the brew's id, and
        learn the installation's power-law exponent from the brew (not from
        a really throttled one: its curve is the throttle's). True if
        something to save changed."""
        changed = self.journal.set_brew_id(brew["start"], brew["id"])
        # Only espresso shots of this setup: started with the relay and cut
        # at an auto-stop target (no pour-over, no shot by the machine's
        # button). A really throttled shot counts up to its trigger only
        # (after it the curve is the throttle's); without that, a machine
        # whose shots are always throttled would never learn its exponent.
        record = brew.get("throttle") or {}
        offset = brew.get("relay_offset")
        end = brew.get("duration")
        if record.get("applied"):
            trigger = record.get("trigger_shot_time")
            end = None if trigger is None or offset is None else trigger - offset
        if offset is not None and brew.get("target") is not None and end is not None:
            try:
                p = fit_exponent(
                    brew.get("profile") or [],
                    (brew.get("metrics") or {}).get("first_drop"),
                    end,
                )
            except (TypeError, ValueError, IndexError):
                p = None
            pool = self.exponent_throttled if record.get("applied") else self.exponent
            if pool.add(p, brew["id"], brew["start"]):
                self.exponent_recent = [
                    r for r in self.exponent_recent if r.get("start") != brew["start"]
                ]
                self.exponent_recent.append(
                    {"start": brew["start"], "unthrottled": not record.get("applied")}
                )
                del self.exponent_recent[:-EXPONENT_RECENT]
                changed = True
                self._notify()
        return changed

    @property
    def prediction_exponent(self) -> float:
        """The installation's exponent for the prediction: from unthrottled
        brews; only when none of the last EXPONENT_RECENT learnable brews
        was unthrottled (every shot gets throttled: lock-in) the throttled
        brews' samples count too, in their order."""
        if any(r.get("unthrottled") for r in self.exponent_recent) or not (
            self.exponent_throttled.samples
        ):
            return self.exponent.value
        merged = sorted(
            [*self.exponent.samples, *self.exponent_throttled.samples],
            key=lambda x: x.get("start") or "",
        )
        return ExponentModel(merged).value

    @callback
    def async_forget_brew(self, brew_id: str, start: str | None) -> bool:
        """Take a deleted brew's samples out (power model replayed,
        exponent recomputed)."""
        exponent = self.exponent.remove(brew_id, start)
        exponent = self.exponent_throttled.remove(brew_id, start) or exponent
        self.exponent_recent = [
            r for r in self.exponent_recent if start is None or r.get("start") != start
        ]
        if not self.journal.remove(brew_id, start):
            if exponent:
                self._changed()
            return False  # True only for a learning sample of the power model
        self.model = self.journal.replay()
        self._changed()
        return True

    @callback
    def async_reset(self) -> None:
        """Forget the learned power model, its journal and the learned
        exponent (back to its start value)."""
        had = len(self.journal) > 0
        self.model = ThrottleModel()
        self.journal = throttle_journal()
        self.exponent = ExponentModel()
        self.exponent_throttled = ExponentModel()
        self.exponent_recent = []
        self._changed()
        if had:
            self.autostop.learned_changed()

    # ------------------------------------------------------- persistence

    def load(self, data: Any) -> None:
        """Restore from the stop store's ``throttle`` part."""
        if not isinstance(data, dict):
            data = default_store()
        settings = data.get("settings")
        if isinstance(settings, dict):
            mode = settings.get(SETTING_MODE)
            self.mode = mode if mode in MODES else MODE_DEFAULT
            self.learning = settings.get(SETTING_LEARNING) is True
            value = _number(settings.get(SETTING_TARGET_TIME))
            if value is not None:
                self.target_time = min(TARGET_TIME_MAX, max(TARGET_TIME_MIN, value))
            value = _number(settings.get(SETTING_FLOOR))
            if value is not None:
                self.floor = min(FLOOR_MAX, max(FLOOR_MIN, value))
            value = _number(settings.get(SETTING_STANDARD_POWER))
            # Absent before 0.7.0b4: full power, as before.
            self.standard_power = (
                STANDARD_POWER_DEFAULT
                if value is None
                else min(POWER_MAX, max(FLOOR_MIN, value))
            )
        model = ThrottleModel.parse(data.get("model"))
        journal = parse_throttle_journal(data.get("journal"))
        if journal is None:
            self.model = model if model is not None else ThrottleModel()
            self.journal = throttle_journal(self.model)
        else:
            self.model = model if model is not None else journal.replay()
            self.journal = journal
        last = data.get("last")
        self.last = last if isinstance(last, dict) else None
        self.last_relay_off = _number(data.get("last_relay_off"))
        self.exponent = ExponentModel.parse(data.get("exponent"))
        self.exponent_throttled = ExponentModel.parse(data.get("exponent_throttled"))
        recent = data.get("exponent_recent")
        self.exponent_recent = [
            {"start": r.get("start"), "unthrottled": r.get("unthrottled") is True}
            for r in (recent if isinstance(recent, list) else [])
            if isinstance(r, dict)
        ][-EXPONENT_RECENT:]
        pending = data.get("restore_pending")
        self.restore_pending = {
            p for p in (pending if isinstance(pending, list) else []) if isinstance(p, str) and p
        }
        self._track_pumps()  # retried when a pending pump shows a number
        # A 0.6.0 store (throttle settings, no profiles): Zielzeit keeps its
        # behaviour; a new store starts at Standard.
        self.profiles.load(
            data.get("profiles"),
            PROFILE_TARGET_TIME if "profiles" not in data else PROFILE_STANDARD,
        )

    def to_store(self) -> dict[str, Any]:
        """The ``throttle`` part of the stop store."""
        return {
            "settings": {
                SETTING_MODE: self.mode,
                SETTING_LEARNING: self.learning,
                SETTING_TARGET_TIME: self.target_time,
                SETTING_FLOOR: self.floor,
                SETTING_STANDARD_POWER: self.standard_power,
            },
            "model": self.model.to_dict(),
            "journal": self.journal.to_dict(),
            "last": self.last,
            "last_relay_off": self.last_relay_off,
            "exponent": self.exponent.to_list(),
            "exponent_throttled": self.exponent_throttled.to_list(),
            "exponent_recent": list(self.exponent_recent),
            "profiles": self.profiles.to_store(),
            "restore_pending": sorted(self.restore_pending),
        }

    # ------------------------------------------- shared with the profiles

    def mono(self) -> float:
        """The shot clock (monotonic)."""
        return time.monotonic()

    def wall(self) -> float:
        """Wall-clock time (epoch s, the brew tracker's clock)."""
        return time.time()

    @staticmethod
    def grid_of(state: Any) -> PowerGrid:
        """The power grid of a pump entity state."""
        return pump_grid(state)

    def request(self, pump: str, value: float) -> asyncio.Task | None:
        """Queue a pump value (the one pump writer, see ``_request``)."""
        return self._request(pump, value)

    def restore_value(self, pump: str | None, touched: bool) -> float | None:
        """Entity value to set back at a shot's end, None = leave it."""
        return self._restore_needed(pump, touched)

    @callback
    def select_profile(self, label: str, chosen: bool = True) -> None:
        """Make a pump profile active (takes effect at the next relay start).

        ``chosen``: the user chose it (select, card, service); with a bean
        active it becomes the bean's profile (0.7.0b6). False for the
        profile a bean selection sets."""
        profile = self.profiles.library.select(label)
        if chosen:
            self.autostop.profile_chosen(profile.label)
        self._changed()

    @callback
    def profiles_changed(self) -> None:
        """The profile library changed (import, delete): save, update."""
        self._changed()

    @callback
    def notify(self) -> None:
        """Live values changed."""
        self._notify()

    # --------------------------------------------------------- listeners

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @callback
    def _changed(self) -> None:
        self.autostop.async_schedule_save()
        self.profiles.check_issues()  # profile or mode may have changed
        self._notify()

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> Callable[[], None]:
        """Call ``listener`` when settings, model or live values change."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove
