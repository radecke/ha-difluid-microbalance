"""Live brew data for the card (pushed while a brew is active).

Messages (sent as events of ``difluid_scale/subscribe_live``):

- ``live_start``: ``start`` (ISO), ``start_ts`` (epoch s), ``dose`` (dose
  candidate or null), ``points`` (all points so far), plus the current
  values below. A client subscribing mid-brew gets this first, with every
  point recorded so far.
- ``live_points``: ``points`` (only new ``[t, weight, flow]``, t = seconds
  since start), plus current values. Coalesced to the brew tick (0.25 s),
  never one message per BLE frame.
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

from .brew import BrewTracker

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
    ) -> None:
        """Start following the tracker's active brew.

        ``target_fn(start, dose)`` gives the auto-stop target shown with the
        live values (``target``, ``target_source``; None = auto-stop off);
        ``cut_fn(start)`` the seconds to the auto-stop cut (pump off), where
        the brew time stops while the weighing goes on.
        """
        assert tracker.start is not None
        self.start: float = tracker.start
        self.dose = tracker.dose_candidate
        self._target_fn = target_fn
        self._cut_fn = cut_fn
        self.points: list[list[float]] = []
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
        }

    def _collect(self, tracker: BrewTracker) -> list[list[float]]:
        new, self._key = tracker.live_points_since(self._key)
        rounded = [[round(t, 2), round(w, 1), round(f, 1)] for t, w, f in new]
        self.points.extend(rounded)
        return rounded

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
        points = self._collect(tracker)
        current = self._current(tracker, now)
        if not points and current == self._last_current:
            return None
        self._last_current = current
        return {"type": LIVE_POINTS, "points": points, **current}


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
