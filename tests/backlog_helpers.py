"""Test helper (0.7.0b6): the profile the tracker records from frames."""

from __future__ import annotations

from custom_components.difluid_scale.brew import BREWING, BrewTracker


def profile_from_frames(
    frames: list[tuple[float, float, float, int | None]], base: int = 0
) -> list[tuple[float, float, float]]:
    """Profile points of a running brew from ``(arrival s since start,
    weight, flow, timer)`` in arrival order, as the tracker records them
    (placed by ``BacklogClock``, one point per profile tick)."""
    tracker = BrewTracker()
    tracker.state = BREWING
    tracker._t0 = 0.0
    tracker._base = base
    for rel, weight, flow, timer in frames:
        tracker._record_frame(rel, weight, flow, timer, rel)
    return [p for _, p in sorted(tracker._buckets.items())]
