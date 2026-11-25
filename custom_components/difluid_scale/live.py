"""Live brew data for the card (pushed while a brew is active).

Messages (sent as events of ``difluid_scale/subscribe_live``):

- ``live_start``: ``start`` (ISO), ``start_ts`` (epoch s), ``dose`` (dose
  candidate or null), ``points`` (all points so far), plus the current
  values below. A client subscribing mid-brew gets this first, with every
  point recorded so far.
- ``live_points``: ``points`` (only new ``[t, weight, flow]``, t = seconds
  since start), plus current values. Coalesced to the brew tick (0.25 s),
  never one message per BLE frame. ``replace_from`` (0.7.0b6, seconds or
  null): frames that came late (a backlog, or a bunch) were placed back
  at their scale time after points from there had been sent; the client
  first drops its points with ``t >= replace_from``, then appends
  ``points`` (which then start at ``replace_from``). Only for that:
  nothing else is changed after it was sent.
- ``live_end``: ``brew_id`` + ``brew`` (summary) when stored, otherwise
  ``reason`` (``too_short`` / ``low_yield`` / ``no_samples`` / ``unloaded``)
  and an English ``detail``.
- ``live_idle``: sent once on subscribe when no brew is active.
- ``live_ready``: ``ready`` (bool), the state of the "Prepare brew" switch,
  ``dose`` (dose the next brew would get, or None; resent when it changes)
  (brew about to start: the card shows an empty live chart). Sent on
  subscribe right after ``live_start`` / ``live_idle``, and whenever the
  switch changes. When a brew ends, ``ready: false`` comes just before its
  ``live_end``.

Current values: ``weight``, ``flow``, ``timer`` (scale), ``elapsed`` (s since
start; frozen at the stop once the timer stopped), ``phase`` (``brewing`` /
``tail`` / ``parked``), ``stop`` (s since start of the timer stop, or null).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .brew import PROFILE_RESOLUTION, BrewTracker

LIVE_START = "live_start"
LIVE_POINTS = "live_points"
LIVE_END = "live_end"
LIVE_IDLE = "live_idle"
LIVE_READY = "live_ready"


class LiveBrew:
    """Points and current values of the active brew, as sent so far."""

    def __init__(
        self,
        tracker: BrewTracker,
        target_fn: Callable[[float, float | None], dict[str, Any] | None]
        | None = None,
        cut_fn: Callable[[float], float | None] | None = None,
        throttle_fn: Callable[[float], dict[str, Any] | None] | None = None,
    ) -> None:
        """Start following the tracker's active brew.

        ``target_fn(start, dose)`` gives the auto-stop target shown with the
        live values (``target``, ``target_source``; None = auto-stop off);
        ``cut_fn(start)`` the seconds to the auto-stop cut (pump off), where
        the brew time stops while the weighing goes on; ``throttle_fn(start)``
        the pump throttle's live values (``throttle``: relay offset, first
        drop in shot time, target time, power trace in profile time, ...;
        None without a relay start).
        """
        assert tracker.start is not None
        self.start: float = tracker.start
        self.dose = tracker.dose_candidate
        self._target_fn = target_fn
        self._cut_fn = cut_fn
        self._throttle_fn = throttle_fn
        self.points: list[list[float]] = []
        self._keys: list[int] = []  # bucket key of each point
        self._key = -1
        self._last_current: dict[str, Any] | None = None

    def _current(self, tracker: BrewTracker, now: float) -> dict[str, Any]:
        sample = tracker.last_sample
        stop = tracker.stop_elapsed
        cut = self._cut_fn(self.start) if self._cut_fn else None
        if cut is not None:
            elapsed = cut  # brew time ends at the cut (pump off)
        else:
            elapsed = stop if stop is not None else max(0.0, now - self.start)
        weight = flow = timer = None
        if sample is not None and sample[0] >= self.start - 10:
            _, weight, flow, timer = sample
            weight = round(weight, 1)
            flow = round(flow, 1)
        target = self._target_fn(self.start, self.dose) if self._target_fn else None
        return {
            "weight": weight,
            "flow": flow,
            "timer": timer,
            "elapsed": round(elapsed, 1),
            "phase": tracker.state,
            "stop": stop,
            "cut": cut,
            "target": target["target"] if target else None,
            "target_source": target["target_source"] if target else None,
            "throttle": self._throttle_fn(self.start) if self._throttle_fn else None,
        }

    def _collect(
        self, tracker: BrewTracker
    ) -> tuple[list[list[float]], float | None]:
        """New points, and ``replace_from`` when sent points were revised."""
        replace_from: float | None = None
        revised = tracker.pop_revision()
        if revised is not None and revised <= self._key:
            # Drop by the rounded t the client compares: a kept point must
            # round below the cut, so step back over points that round onto it.
            key = revised
            while any(
                k < key and p[0] >= round(key * PROFILE_RESOLUTION, 2)
                for k, p in zip(self._keys, self.points)
            ):
                key -= 1
            replace_from = round(key * PROFILE_RESOLUTION, 2)
            keep = [i for i, p in enumerate(self.points) if p[0] < replace_from]
            self.points = [self.points[i] for i in keep]
            self._keys = [self._keys[i] for i in keep]
            self._key = key - 1
        new, self._key = tracker.live_points_since(self._key)
        rounded = [[round(t, 2), round(w, 1), round(f, 1)] for t, w, f in new]
        self.points.extend(rounded)
        self._keys.extend(int(t / PROFILE_RESOLUTION) for t, _, _ in new)
        return rounded, replace_from

    def start_message(self, tracker: BrewTracker, now: float) -> dict[str, Any]:
        """``live_start`` with everything recorded so far (collects first)."""
        self._collect(tracker)
        current = self._current(tracker, now)
        self._last_current = current
        return {
            "type": LIVE_START,
            "start": datetime.fromtimestamp(self.start, UTC).isoformat(),
            "start_ts": self.start,
            "dose": self.dose,
            "points": list(self.points),
            **current,
        }

    def snapshot(self, tracker: BrewTracker, now: float) -> dict[str, Any]:
        """``live_start`` for a late subscriber (does not advance the stream)."""
        return {
            "type": LIVE_START,
            "start": datetime.fromtimestamp(self.start, UTC).isoformat(),
            "start_ts": self.start,
            "dose": self.dose,
            "points": list(self.points),
            **(self._last_current or self._current(tracker, now)),
        }

    def update_message(
        self, tracker: BrewTracker, now: float
    ) -> dict[str, Any] | None:
        """``live_points`` with new points, or None if nothing changed."""
        points, replace_from = self._collect(tracker)
        current = self._current(tracker, now)
        if not points and replace_from is None and current == self._last_current:
            return None
        self._last_current = current
        return {
            "type": LIVE_POINTS,
            "points": points,
            "replace_from": replace_from,
            **current,
        }


def end_message(
    brew: dict[str, Any] | None = None,
    reason: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """``live_end``: the stored brew, or why nothing was stored."""
    return {
        "type": LIVE_END,
        "brew_id": brew["id"] if brew else None,
        "brew": brew,
        "reason": reason,
        "detail": detail,
    }


def ready_message(
    ready: bool,
    dose: float | None = None,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``live_ready``: whether a brew is about to start (switch state), with
    the dose the next brew would get (None when no dose plateau is known)
    and the auto-stop target it would get (None when the auto-stop is off)."""
    target = target if ready else None
    return {
        "type": LIVE_READY,
        "ready": ready,
        "dose": dose if ready else None,
        "target": target["target"] if target else None,
        "target_source": target["target_source"] if target else None,
    }
