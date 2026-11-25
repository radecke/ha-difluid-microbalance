"""Pump profiles, Home Assistant side (0.7.0; maths in ``profiles``).

One profile run per pump relay start (shot time 0 = relay ``off -> on``,
the throttle's monotonic clock) of a real brew: only if the integration's
own "Prepare brew" switch (``coordinator.brew_ready``) is on at the relay
``off -> on`` (0.7.0b2). Flushes, rinsing and a cleaning program are not
prepared and run at full power (reason ``not_prepared``). The switch is
read only at the relay start: turning it off mid-shot (it turns itself off
at the brew's end) never ends the run. It runs from the relay start to the
shot's end: relay off (or unavailable/unknown), auto-stop cut, unload, a
change of the stop switch or pump entity in the options, or a change of
the throttle mode mid-shot (bean, ratio or profile edits do not end it). It
does not need the scale: durations and transitions run on a clock tick
(BREW_TICK_INTERVAL, 0.25 s: the resolution of a ramp, not a delay) and on
every scale frame. Weight and flow (exits, flow phases) come from every
accepted frame (outlier filter passed) from the relay start on, whether
the scale timer runs or not: the cup's tare is the reference. Without
fresh frames (scale silent) weight/flow exits are skipped and flow phases
hold their power (logged once per shot). An error ends the run (pump back
to full power).

Pending restores (``ThrottleControl.restore_pending``) and the restore
hold after an auto-stop cut are described in ``throttle``.

Single writer: ``ThrottleControl`` owns the pump. Profile runs write
through its command queue (one worker per pump, newest value wins, errors
logged) and its restore rules. A run starts only for a profile without
``x_throttle``; the 0.6.0 throttle (``ShotThrottle``) runs only for the
Zielzeit profile (``x_throttle: target_time``), decided once at the relay
start. So exactly one controller writes the pump per shot.

Modes are global (the throttle select): ``off`` = no run (pump untouched),
``observe`` = the run is computed and recorded (power trace, phases), the
pump is never written, ``throttle`` = written. In ``throttle`` mode a run
that changed the pump sets it back to its maximum at the shot's end (as
the throttle does, decision 0052).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import BREW_TICK_INTERVAL
from .issues import async_update_profile_issues
from .protocol import CMD_SETTINGS_AUTO_STOP_TIMING
from .profiles import (
    PROFILE_STANDARD,
    TARGET_FLOW,
    TARGET_RATIO,
    TARGET_VOLUMETRIC,
    Profile,
    ProfileLibrary,
    ProfileRun,
    label_key,
)
from .throttle_model import MODE_OFF, MODE_THROTTLE, POWER_MAX, PowerGrid

if TYPE_CHECKING:
    from .throttle import ThrottleControl, _Brew

_LOGGER = logging.getLogger(__name__)

# A frame older than this is no scale data for a clock tick (the brew
# tracker's SAMPLE_SILENCE_SECONDS: a silent link).
SCALE_STALE = 1.0  # s

REASON_NO_SCALE = "no_scale_data"
# "Prepare brew" was off at the relay start: no brew (flush, rinse,
# cleaning program), no profile run. Brews stored by 0.7.0b1 may carry the
# older reason "no_tare" (its tare rule, removed in 0.7.0b2).
REASON_NOT_PREPARED = "not_prepared"


class _Run:
    """One profile shot (by relay start)."""

    def __init__(self, relay_mono: float, profile: Profile, mode: str) -> None:
        self.relay_mono = relay_mono
        self.profile = profile
        self.mode = mode
        self.apply = mode == MODE_THROTTLE
        self.pump: str | None = None
        self.grid = PowerGrid()
        self.run: ProfileRun | None = None
        self.touched = False
        self.command_value: float | None = None
        self.ended = False
        self.end_reason: str | None = None
        self.end_shot_time: float | None = None
        self.brew_start: float | None = None  # matched brew (tracker start)
        self.relay_offset: float | None = None
        self.frame: tuple[float, float, float] | None = None  # (mono, w, q)
        self.no_scale_logged = False
        self.ignored_logged: set[tuple[int, float]] = set()
        self.reason: str | None = None
        # The pump entity cannot go to 0 % (its min > 0): a bloom phase
        # runs at the entity's minimum instead (warned once, recorded).
        self.bloom_not_possible = False
        # The scale's own "auto stop timing" is on: it may stop its timer
        # during a bloom (no flow), which ends the brew there (warned).
        self.scale_auto_stop_timing = False
        self.started_wall: float | None = None


class ProfileControl:
    """Profile library, the active profile and the running profile shot."""

    def __init__(self, hass: HomeAssistant, throttle: ThrottleControl) -> None:
        self.hass = hass
        self.throttle = throttle
        self.library = ProfileLibrary()
        self.current: _Run | None = None
        self.last_run: _Run | None = None
        self._unsub_tick: CALLBACK_TYPE | None = None
        # (relay start, reason) of the last relay start without a run.
        self.last_skip: tuple[float, str] | None = None
        # Outcome of the last relay start (select attributes): profile,
        # "applied" | "observed" | "not_prepared", wall time.
        self.last_outcome: dict[str, Any] | None = None
        # The same, only for relay starts matched to a scale brew (a
        # flush or the cleaning program never overwrites it).
        self.last_brew_outcome: dict[str, Any] | None = None
        self._issue_key: tuple | None = None
        # The first skip for "not prepared" in throttle mode is logged at
        # info level (once per setup), later ones at debug.
        self._not_prepared_logged = False

    # ------------------------------------------------------------ library

    @property
    def active(self) -> Profile:
        return self.library.active

    @property
    def throttle_profile(self) -> bool:
        """The active profile is the 0.6.0 throttle (Zielzeit)."""
        return self.active.throttle is not None

    # --------------------------------------------------------------- run

    @callback
    def relay_on(self, relay_mono: float) -> None:
        """Pump relay off -> on: a new shot; runs the active profile unless
        mode off or the profile is the throttle's."""
        self.end("relay_on")  # a run left over (relay off missed)
        mode = self.throttle.mode
        profile = self._with_standard_power(self.active)
        if mode == MODE_OFF or profile.throttle is not None:
            return
        if not self.throttle.coordinator.brew_ready:
            # No brew prepared: the pump stays at full power (Standard).
            # Debug only: a cleaning program pulses the relay many times.
            self.last_skip = (relay_mono, REASON_NOT_PREPARED)
            self.last_outcome = {
                "profile": profile.label, "outcome": REASON_NOT_PREPARED,
                "at": self.throttle.wall(),
            }
            if mode == MODE_THROTTLE and not self._not_prepared_logged:
                # Once per setup at info level: a user without a "Prepare
                # brew" automation learns why the profile never runs.
                self._not_prepared_logged = True
                _LOGGER.info(
                    "%s: pump profile %s skipped: 'Prepare brew' was off when "
                    "the pump relay went on. Profiles run only for prepared "
                    "brews (turn the switch on before the shot, e.g. from an "
                    "automation); further skips are logged at debug level",
                    self.throttle.coordinator.address, profile.label,
                )
            else:
                _LOGGER.debug(
                    "%s: profile %s not run: 'Prepare brew' was off at the "
                    "relay start",
                    self.throttle.coordinator.address, profile.label,
                )
            self.throttle.notify()
            return
        run = _Run(relay_mono, profile, mode)
        run.started_wall = self.throttle.wall()
        pump = self.throttle.pump_entity
        run.pump = pump
        state = self.hass.states.get(pump) if pump else None
        if profile.has_bloom and state is not None and _number(state.state) is not None:
            grid = self.throttle.grid_of(state)
            if grid.low > 0:
                run.bloom_not_possible = True
                _LOGGER.warning(
                    "%s: profile %s has a 0 %% phase, but pump %s cannot go "
                    "below %s (its min): that phase runs at the minimum, the "
                    "pump is not off",
                    self.throttle.coordinator.address, profile.label, pump, grid.low,
                )
        if profile.has_bloom and self._scale_auto_stop_timing():
            run.scale_auto_stop_timing = True
            _LOGGER.warning(
                "%s: profile %s has a 0 %% phase and the scale's 'auto stop "
                "timing' is on: the scale may stop its timer during the bloom, "
                "which ends the brew (and the auto-stop) there; turn it off",
                self.throttle.coordinator.address, profile.label,
            )
        if run.apply:
            if pump is None or state is None or _number(state.state) is None:
                run.apply = False
                run.reason = "no_pump" if pump is None else "pump_unavailable"
                _LOGGER.warning(
                    "%s: throttle mode, but the pump power entity is %s: "
                    "profile %s is only observed",
                    self.throttle.coordinator.address,
                    "not configured" if pump is None else f"unavailable ({pump})",
                    profile.label,
                )
            else:
                run.grid = self.throttle.grid_of(state)
        run.run = ProfileRun(
            profile,
            floor=self.throttle.floor,
            grid=run.grid,
            model=self.throttle.model,
            dose=self._dose(),
        )
        self.current = run
        self.last_outcome = {
            "profile": profile.label,
            "outcome": "applied" if run.apply else "observed",
            "at": run.started_wall,
        }
        _LOGGER.debug(
            "%s: profile %s starts (%s)",
            self.throttle.coordinator.address, profile.label, run.mode,
        )
        self._unsub_tick = async_track_time_interval(
            self.hass, self._tick, BREW_TICK_INTERVAL
        )
        self._safe_update(run, 0.0, None, None)

    def _with_standard_power(self, profile: Profile) -> Profile:
        """The built-in Standard at the *Standard pump power* setting
        (0.7.0b4, read here at the relay start: a change takes effect with
        the next shot); every other profile as it is. Clamped to the floor
        by the run like any profile power."""
        power = self.throttle.standard_power
        if not profile.builtin or profile.key != label_key(PROFILE_STANDARD):
            return profile
        if power >= POWER_MAX:
            return profile
        return dataclasses.replace(
            profile,
            phases=tuple(dataclasses.replace(p, power=power) for p in profile.phases),
        )

    def _scale_auto_stop_timing(self) -> bool:
        try:
            data = self.throttle.coordinator.data
            return data.settings.get(CMD_SETTINGS_AUTO_STOP_TIMING) == 1
        except Exception:  # noqa: BLE001 - informational only
            return False

    def _dose(self) -> float | None:
        tracker = self.throttle.coordinator.brew_tracker
        try:
            if tracker.active:
                return tracker.dose_candidate
            return tracker.next_dose(self.throttle.wall())
        except Exception:  # noqa: BLE001 - the dose is optional
            return None

    @callback
    def _tick(self, _now: Any = None) -> None:
        self.tick()

    @callback
    def tick(self) -> None:
        """Clock tick: advance the running shot (last frame if fresh)."""
        run = self.current
        if run is None or run.ended or run.run is None:
            return
        now = self.throttle.mono()
        weight = flow = None
        if run.frame is not None and now - run.frame[0] <= SCALE_STALE:
            weight, flow = run.frame[1], run.frame[2]
        self._safe_update(run, now - run.relay_mono, weight, flow)

    @callback
    def sample(self, weight: float, flow: float) -> None:
        """Every accepted weight frame (outlier filter passed), whether or
        not the scale timer runs: the cup's tare is the reference, the
        scale shows the weight net since it."""
        run = self.current
        if run is None or run.ended or run.run is None:
            return
        now = self.throttle.mono()
        run.frame = (now, weight, flow)
        self._safe_update(run, now - run.relay_mono, weight, flow)

    @callback
    def frame(self, brew: _Brew) -> None:
        """A frame of a brew after its relay match: links the brew (its
        record) to the run."""
        run = self.current
        if run is None or run.run is None:
            return
        if brew.relay_mono is None or brew.relay_mono != run.relay_mono:
            return
        run.brew_start = brew.start
        run.relay_offset = brew.relay_offset

    def _safe_update(
        self, run: _Run, t: float, weight: float | None, flow: float | None
    ) -> None:
        """``_update``; an error ends the run (pump back to full power):
        never stuck at the last command, e.g. a 0 % bloom."""
        try:
            self._update(run, t, weight, flow)
        except Exception:
            _LOGGER.exception(
                "%s: error in pump profile %s: run ended, pump back to full power",
                self.throttle.coordinator.address, run.profile.label,
            )
            self.end("error")

    def _needs_scale(self, run: _Run) -> bool:
        phase = run.run.phase if run.run else None
        if phase is None:
            return False
        return phase.flow is not None or any(
            t.type in (TARGET_VOLUMETRIC, TARGET_FLOW, TARGET_RATIO)
            for t in phase.targets
        )

    def _update(
        self, run: _Run, t: float, weight: float | None, flow: float | None
    ) -> None:
        prun = run.run
        assert prun is not None
        if prun.dose is None:
            prun.dose = self._dose()
        if weight is None and not run.no_scale_logged and self._needs_scale(run):
            run.no_scale_logged = True
            _LOGGER.info(
                "%s: profile %s: no scale data (no brew, cup not tared or "
                "scale silent): weight/flow exits are skipped, flow phases "
                "hold their power",
                self.throttle.coordinator.address, run.profile.label,
            )
        power = self._power_in_effect(run)
        phases = len(prun.phases)
        new = prun.update(t, weight, flow, power)
        if new is None and len(prun.phases) != phases:
            self.throttle.notify()  # a phase change (select attributes)
        for item in prun.ignored:
            if item not in run.ignored_logged:
                run.ignored_logged.add(item)
                _LOGGER.info(
                    "%s: profile %s, phase %d: ratio target %s ignored (no dose)",
                    self.throttle.coordinator.address, run.profile.label,
                    item[0] + 1, item[1],
                )
        if new is None:
            return
        if run.apply:
            self._set(run, new)
        self.throttle.last_power = new
        self.throttle.notify()

    def _power_in_effect(self, run: _Run) -> float:
        """Apply: the last command (it counts until the entity shows
        another); before one, the entity's state. Observe: 100 %."""
        if not run.apply:
            return POWER_MAX
        if run.command_value is not None:
            return run.grid.percent(run.command_value)
        state = self.hass.states.get(run.pump) if run.pump else None
        value = _number(state.state) if state is not None else None
        if value is None:
            return POWER_MAX
        return min(POWER_MAX, max(0.0, run.grid.percent(value)))

    def _set(self, run: _Run, percent: float) -> None:
        pump = run.pump
        if pump is None:
            return
        value = run.grid.value(percent)
        current = run.command_value
        if current is None:
            state = self.hass.states.get(pump)
            current = _number(state.state) if state is not None else None
        if current is not None and abs(current - value) < 1e-6:
            return
        if self.throttle.restore_held:
            return  # held back after a cut: not sent, so not in effect
        run.touched = True
        run.command_value = value
        self.throttle.request(pump, value)

    @callback
    def command_failed(self, pump: str, value: float) -> None:
        """A set failed: the state counts again, not the command."""
        run = self.current
        if run is not None and run.pump == pump and run.command_value == value:
            run.command_value = None

    @callback
    def end(self, reason: str) -> Any:
        """The shot ended: final phase mark, pump back to its maximum
        (throttle mode, if the run changed it or it stands lower)."""
        run = self.current
        if run is None or run.ended:
            return None
        run.ended = True
        run.end_reason = reason
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None
        t = self.throttle.mono() - run.relay_mono
        run.end_shot_time = round(t, 2)
        if run.run is not None:
            run.run.finish(t)
        task = None
        if run.mode == MODE_THROTTLE and run.pump is not None:
            # Already commanded back to full power (the profile's end) and
            # shown by the entity: nothing to resend; else the throttle's
            # restore rule (changed in this shot, or below its maximum).
            state = self.hass.states.get(run.pump)
            high = self.throttle.grid_of(state).high
            done = (
                run.command_value is not None
                and abs(run.command_value - high) < 1e-6
                and state is not None
                and _number(state.state) == run.command_value
            )
            value = self.throttle.restore_value(run.pump, run.touched and not done)
            if value is not None:
                run.command_value = value
                task = self.throttle.request(run.pump, value)
                if run.touched and run.run is not None and run.run.trace:
                    if run.run.trace[-1][1] != POWER_MAX:
                        run.run.trace.append([round(t, 2), POWER_MAX])
        self.last_run = run
        self.current = None
        _LOGGER.debug(
            "%s: profile %s ended (%s) at shot time %.1f s",
            self.throttle.coordinator.address, run.profile.label, reason, t,
        )
        self.throttle.notify()
        return task

    @property
    def running(self) -> bool:
        """A profile shot runs and has changed the pump."""
        run = self.current
        return run is not None and not run.ended and run.touched

    # ----------------------------------------------------------- results

    def attributes(self) -> dict[str, Any]:
        """Attributes of the profile select: the active profile's first
        drop deadline, the running shot's (shot time and ISO time) and the
        last relay start's outcome."""
        attrs: dict[str, Any] = {
            "first_drop_deadline": self.active.first_drop_deadline,
            # 0.7.0b3: the active profile's definition revision (the profile
            # preview tile feature refetches when it changes).
            "profile_revision": self.active.revision,
            # 0.7.0b4: the pump power is really controlled (mode throttle,
            # usable pump entity); the profile preview greys the Standard
            # power out otherwise.
            "pump_controllable": self.throttle.pump_controllable,
            # 0.7.0b4: the auto-stop ends the shot at a target (stop switch
            # and a target ratio or yield): the preview's end chip.
            "auto_stop_target": self.throttle.autostop.enabled,
            "running": None,
            "running_phase": None,
            "running_first_drop_deadline": None,
            "running_first_drop_deadline_at": None,
            "last_outcome": None,
            "last_outcome_profile": None,
            "bloom_not_possible": None,
        }
        run = self.current
        if run is not None and not run.ended and run.run is not None:
            phase = run.run.phase
            deadline = run.profile.first_drop_deadline
            attrs.update(
                {
                    "running": run.profile.label,
                    "running_phase": phase.name if phase is not None else None,
                    "running_first_drop_deadline": deadline,
                    "running_first_drop_deadline_at": None
                    if deadline is None or run.started_wall is None
                    else _iso(run.started_wall + deadline),
                    "bloom_not_possible": run.bloom_not_possible,
                }
            )
        if self.last_outcome is not None:
            attrs["last_outcome"] = self.last_outcome["outcome"]
            attrs["last_outcome_profile"] = self.last_outcome["profile"]
        attrs["last_brew_outcome"] = None
        attrs["last_brew_profile"] = None
        if self.last_brew_outcome is not None:
            attrs["last_brew_outcome"] = self.last_brew_outcome["outcome"]
            attrs["last_brew_profile"] = self.last_brew_outcome["profile"]
        return attrs

    @callback
    def brew_matched(self, brew: _Brew) -> None:
        """A relay start was matched to a scale brew: its profile outcome
        (applied / observed / not_prepared / throttle / off) becomes the last
        brew's."""
        run = self.run_for(brew)
        if run is not None:
            outcome = "applied" if run.apply else "observed"
            label = run.profile.label
        elif self.last_skip is not None and self.last_skip[0] == brew.relay_mono:
            outcome, label = REASON_NOT_PREPARED, brew.profile_label
        elif brew.mode == MODE_OFF:
            outcome, label = "off", brew.profile_label
        elif not brew.throttle_allowed or brew.profile_label is None:
            outcome, label = "not_run", brew.profile_label
        else:
            outcome, label = "throttle", brew.profile_label
        self.last_brew_outcome = {"profile": label, "outcome": outcome}
        self.throttle.notify()

    @callback
    def check_issues(self) -> None:
        """Repair issues for a bloom that cannot work as planned (0 %
        phase, mode not off): the scale's auto stop timing is on; the pump
        entity's min is above 0. Only calls the registry on a change."""
        profile = self.active
        relevant = profile.has_bloom and self.throttle.mode != MODE_OFF
        pump = self.throttle.pump_entity
        state = self.hass.states.get(pump) if pump else None
        pump_min = None
        if state is not None and _number(state.state) is not None:
            pump_min = self.throttle.grid_of(state).low
        key = (
            profile.label if relevant else None,
            relevant and self._scale_auto_stop_timing(),
            pump,
            pump_min if relevant else None,
        )
        if key == self._issue_key:
            return
        self._issue_key = key
        async_update_profile_issues(
            self.hass, self.throttle.coordinator.config_entry, key[0], key[1], pump, key[3]
        )

    def run_for(self, brew: _Brew | None) -> _Run | None:
        """The run of that brew (its matched relay start)."""
        if brew is None or brew.relay_mono is None:
            return None
        for run in (self.current, self.last_run):
            if run is not None and run.relay_mono == brew.relay_mono:
                return run
        return None

    def changed_pump(self, brew: _Brew | None) -> bool:
        """The run of that brew really set the pump below 100 %."""
        run = self.run_for(brew)
        return bool(
            run is not None
            and run.apply
            and run.touched
            and run.run is not None
            and any(p[1] < POWER_MAX for p in run.run.trace)
        )

    def record_part(self, brew: _Brew) -> dict[str, Any]:
        """Profile fields of the throttle record of a brew."""
        run = self.run_for(brew)
        label = run.profile.label if run is not None else brew.profile_label
        if label is None:
            label = brew.start_profile  # no relay start (0.7.0b6)
        data: dict[str, Any] = {"profile": label}
        if brew.relay_mono is not None and not brew.throttle_allowed:
            data["target_time"] = None  # no throttle: no target shot time
        if run is None or run.run is None:
            skip = self.last_skip
            if skip is not None and brew.relay_mono == skip[0]:
                data["profile_reason"] = skip[1]
            return data
        offset = brew.relay_offset or 0.0
        prun = run.run
        data.update(
            {
                "applied": bool(run.apply and run.touched),
                # 0.7.0b3: the run controlled the pump (throttle mode, usable
                # pump entity), whether or not a value had to be written.
                "controlled": bool(run.apply),
                "power": [[round(t - offset, 2), p] for t, p in prun.trace],
                "phases": [
                    [round(p[0] - offset, 2), p[1], p[2], p[3]] for p in prun.phases
                ],
                "profile_end_reason": run.end_reason,
                "profile_ignored_targets": [list(i) for i in prun.ignored],
                "first_drop_deadline": run.profile.first_drop_deadline,
                "bloom_not_possible": run.bloom_not_possible,
                "scale_auto_stop_timing": run.scale_auto_stop_timing,
            }
        )
        if run.reason is not None:
            data["reason"] = run.reason
        return data

    def live_part(self, brew: _Brew) -> dict[str, Any]:
        """Profile fields of the live info."""
        data = self.record_part(brew)
        run = self.run_for(brew)
        if run is not None and run.run is not None:
            phase = run.run.phase
            data["phase"] = phase.name if phase is not None else None
            data.pop("phases", None)
            data.pop("profile_ignored_targets", None)
            data.pop("profile_end_reason", None)
            data["first_drop_deadline"] = run.profile.first_drop_deadline
        return data

    # ------------------------------------------------------- persistence

    def load(self, data: Any, default_active: str = PROFILE_STANDARD) -> None:
        skipped = self.library.load(data, default_active)
        if skipped:
            _LOGGER.warning(
                "%s: %d stored pump profile(s) are invalid and were skipped",
                self.throttle.coordinator.address, skipped,
            )

    def to_store(self) -> dict[str, Any]:
        return self.library.to_store()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None
