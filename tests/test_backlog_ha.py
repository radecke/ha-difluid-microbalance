"""0.7.0b6: auto-stop learning and held-back scale data (HA side).

The real brew of 2026-11-25 (fixture ``backlog_2026-11-25.json``): an
event-loop stall in the middle of the shot, the cut later on fresh data:
learned. The same shot with the stall moved onto the cut: the cut acted on
held-back data, ``delayed_data``, not learned. And the stored profile and
metrics put the backlog at its scale time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from test_integration import (
    PLUG,
    Scale,
    _auto_shot,
    _setup_autostop,
    sensor_frame,
)

from custom_components.difluid_scale.autostop import REASON_DELAYED
from custom_components.difluid_scale.protocol import DeviceState
from custom_components.difluid_scale.stop_model import FlowEstimator, SampleFilter

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "backlog_2026-11-25.json").read_text()
)
STATES = {"timing": DeviceState.TIMING, "timer_pause": DeviceState.TIMER_PAUSE, "idle": DeviceState.IDLE}
# Target for a cut at about 40 g with the prior model (cut at 40.2 g, 3.3 g/s,
# as on 2026-11-25).
TARGET_G = 44.0


async def _replay(hass: HomeAssistant, scale: Scale, frames: list) -> None:
    coord = scale.coord
    clock = scale.clock
    base = clock.t + 20.0  # brew start; the preroll frames lie before it
    events = [(t, "frame", (w, f, timer)) for t, w, f, timer, *_ in frames]
    events += [(t, "state", st) for t, st in FIXTURE["device_state"]]
    events.sort(key=lambda e: e[0])
    for t, kind, data in [*events, (45.0, "end", None)]:
        clock.t = round(base + t, 4)
        scale._tick()
        if kind == "frame":
            w, f, timer = data
            coord._on_notification(None, bytearray(sensor_frame(w, f, timer)))
        elif kind == "state":
            scale.status(STATES[data])
        await hass.async_block_till_done()


def _b5_flow(at: float) -> float:
    """The flow 0.7.0b5 computed at the frame arriving at ``at`` (every
    frame at its arrival)."""
    filt, est = SampleFilter(), FlowEstimator()
    flow = 0.0
    for t, w, *_ in FIXTURE["frames"]:
        if t > at + 1e-6:
            break
        for ft, fw, shift in filt.feed(t, w):
            flow = est.feed(ft, fw, shift)
    return round(flow, 2)


def _bunched_at_cut(frames: list) -> list:
    """The real shot with the stall moved onto the cut: the frames of
    21.0-24.0 s held back, delivered within milliseconds at 24.1 s."""
    out = []
    held = []
    for fr in frames:
        if 21.0 < fr[0] < 24.1:
            held.append(fr)
        else:
            if fr[0] >= 24.1 and held:
                out += [[round(24.1 + 0.003 * i, 3), *h[1:]] for i, h in enumerate(held)]
                held = []
            out.append(fr)
    return out


async def test_real_backlog_mid_shot_is_learned(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, TARGET_G)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    await _replay(hass, scale, FIXTURE["frames"])
    assert len(calls) == 1  # one cut
    last = coord.autostop.last
    assert last is not None, "no conclusion"
    assert last["delayed_data"] is False, last
    assert last["reason"] != REASON_DELAYED, last
    assert last["accepted"], sorted(last.items())
    # On fresh frames (prior model, so not the real 40.2 g cut): the same
    # frame and flow as without backlog handling (0.7.0b5: arrival times),
    # since no backlog lies in the flow window.
    assert last["cut_weight"] == 38.0, str(sorted(last.items()))
    assert abs(last["cut_flow"] - _b5_flow(23.211)) < 0.01, last
    assert coord.brews.brew_count == 1
    brew = coord.brews.last
    # Stored at the scale time: first drop at about 8-9 s (0.7.0b5: 15.6).
    assert 8.0 <= brew["metrics"]["first_drop"] <= 9.5, brew["metrics"]
    assert brew["metrics"]["flow_cv"] < 0.3, brew["metrics"]
    assert len(brew["profile"]) >= 80
    assert len([p for p in brew["profile"] if 6.6 < p[0] < 15.5]) >= 20


async def test_backlog_at_the_cut_is_not_learned(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, TARGET_G)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    await _replay(hass, scale, _bunched_at_cut(FIXTURE["frames"]))
    assert len(calls) == 1
    last = coord.autostop.last
    assert last is not None
    assert last["delayed_data"] is True, last
    assert last["reason"] == REASON_DELAYED and not last["accepted"], last
    assert coord.autostop.model.count == 0
    # The flow at the cut came from the frames' scale times, not from the
    # bunch (several g within milliseconds would read as > 100 g/s).
    assert last["cut_flow"] < 8.0, last
    # The brew itself is stored normally, the bunch at its scale time.
    assert coord.brews.brew_count == 1
    profile = coord.brews.last["profile"]
    assert len([p for p in profile if 21.0 < p[0] < 24.0]) >= 8


class _LateScale(Scale):
    """Every frame reaches HA ``lag`` s after the scale took it (its count
    lags the arrival by that much)."""

    lag = 0.3

    def timer(self) -> int:
        if self.frozen is not None:
            return self.frozen
        if self.timer_start is None:
            return 0
        return max(0, int(self.clock.t - self.timer_start - self.lag))


async def test_steady_delay_changes_nothing_at_the_cut(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """A normal shot whose frames all arrive 0.3 s late (BLE latency): no
    frame is late in the backlog sense, the cut rule sees the same times,
    flow and weight as without the delay (0.7.0b5 behaviour)."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    results = []
    for scale_type in (Scale, _LateScale):
        coord.autostop.async_reset()
        scale = scale_type(coord, clock)
        scale.calls = calls
        assert await _auto_shot(hass, scale) is not None
        last = coord.autostop.last
        results.append((last["cut_weight"], last["cut_flow"], last["accepted"]))
        assert coord.brew_tracker.finished_backlog is not None
        assert coord.brew_tracker.finished_backlog[1] == []
    assert results[0] == results[1], results
    assert results[0][2] is True


def _on_time_bunch_near_cut(frames: list) -> list:
    """The real shot with frames 22.98-23.21 s held ~0.4 s (BLE coalescing)
    and delivered within ms together with the 23.515 s frame: a bunch over
    counts 22/23, none late."""
    out = []
    for fr in frames:
        if 22.9 < fr[0] < 23.5:
            fr = [round(23.509 + 0.002 * (fr[0] > 23.0), 3), *fr[1:]]
        out.append(fr)
    out.sort(key=lambda f: f[0])
    return out


async def test_on_time_bunch_before_the_cut_is_learned(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    calls = await _setup_autostop(hass, entry, PLUG, TARGET_G)
    coord = entry.runtime_data
    scale = Scale(coord, clock)
    scale.calls = calls
    await _replay(hass, scale, _on_time_bunch_near_cut(FIXTURE["frames"]))
    last = coord.autostop.last
    assert last is not None and len(calls) == 1
    assert last["delayed_data"] is False and last["reason"] != REASON_DELAYED, last
