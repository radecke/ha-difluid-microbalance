"""Learning journal (0.5.1): deleting a brew takes it out of the auto-stop
learning (journal + replay), totals stay, the card's service response."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402

from test_integration import (  # noqa: E402
    PLUG,
    REAL_BREW,
    Scale,
    _auto_shot,
    _setup,
    _setup_autostop,
    _state,
    _store_brews,
)

from custom_components.difluid_scale.const import (  # noqa: E402
    STOP_STORAGE_KEY,
)
from custom_components.difluid_scale.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)
from custom_components.difluid_scale.stop_model import (  # noqa: E402
    Cut,
    LearningJournal,
    StopModel,
)

DOMAIN = "difluid_scale"
SAMPLES = "sensor.microbalance_stop_learning_samples"
TAU = "sensor.microbalance_learned_stop_latency"

# stop_learning store as on the live host (2026-11-23, 0.5.1b5): a model
# learned from 11 shots, no journal yet.
HOST_STOP_STORE = {
    "model": {
        "tau": 0.38747342367333987,
        "c": 0.5755018357938158,
        "p": [
            [0.028181263457567066, -0.10629041070026514],
            [-0.10629041070026514, 0.5468506312209026],
        ],
        "count": 11,
    },
    "last": {
        "target": 40.0,
        "target_source": "yield",
        "dose": None,
        "cut_weight": 36.0,
        "cut_flow": 7.98,
        "predicted_overshoot": 3.67,
        "lead": 0.151,
        "final_yield": 38.9,
        "overshoot": 2.9,
        "deviation": -1.1,
        "accepted": False,
        "reason": "brew_discarded",
        "clean_cut": True,
        "bean": "Pacandé El Pital",
        "switch_latency": 0.129,
        "nominal_switch_latency": 0.132,
        "adjusted_overshoot": 2.92,
    },
    "settings": {"target_ratio": 2.7, "target_yield": 40.0, "bean_id": None},
    "switch_latency": {
        "entity_id": PLUG,
        "values": [0.13, 0.132, 0.141, 0.129],
        "unknown_streak": 0,
        "last": 0.129,
    },
}


def _store_host_model(hass_storage: dict[str, Any], entry) -> str:
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 2,
        "key": key,
        "data": {k: v for k, v in HOST_STOP_STORE.items()},
    }
    return key


def _attrs(hass: HomeAssistant) -> dict[str, Any]:
    attrs = hass.states.get(SAMPLES).attributes
    return {k: attrs[k] for k in ("journal_samples", "journal_base_samples")}


async def _delete(hass: HomeAssistant, brew_id: str) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN, "delete_brew", {"brew_id": brew_id},
        blocking=True, return_response=True,
    )
    await hass.async_block_till_done()
    return response


async def test_live_learning_journal_delete_replays(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """Live cuts are journalled with their brew id and the latency-adjusted
    overshoot; deleting one brew replays the model from the rest, sensors
    follow, totals stay; persisted; reset empties the journal."""
    calls = await _setup_autostop(hass, entry, PLUG, 38.0)
    coord = entry.runtime_data
    stop = coord.autostop
    assert _attrs(hass)["journal_samples"] == 0

    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale) is not None
    first = coord.brews.last
    assert stop.model.count == 1 and len(stop.journal) == 1
    entry1 = stop.journal.entries[0]
    # Learned before the brew was stored: id filled in afterwards.
    assert entry1["brew_id"] == first["id"]
    assert entry1["start"] == first["start"]
    # The value fed into the model: the latency-adjusted overshoot.
    assert abs(entry1["overshoot"] - stop.last["adjusted_overshoot"]) < 0.005
    assert abs(entry1["flow"] - stop.last["cut_flow"]) < 0.005
    assert entry1["t"]

    clock.t += 120
    scale = Scale(coord, clock)
    scale.calls = calls
    assert await _auto_shot(hass, scale, flow=2.5) is not None
    second = coord.brews.last
    assert second["id"] != first["id"]
    assert stop.model.count == 2 and len(stop.journal) == 2
    assert stop.journal.entries[1]["brew_id"] == second["id"]
    # Unchanged journal: replay == incremental, exactly.
    assert stop.journal.replay().to_dict() == stop.model.to_dict()
    assert _state(hass, SAMPLES) == "2"
    assert _attrs(hass) == {"journal_samples": 2, "journal_base_samples": 0}
    totals = (coord.brews.brew_count, coord.brews.coffee_total)

    # Delete the first brew: the model is the second sample alone, learned
    # from the prior.
    expected = StopModel()
    assert expected.update(
        stop.journal.entries[1]["flow"], stop.journal.entries[1]["overshoot"]
    )
    tau_before = _state(hass, TAU)
    response = await _delete(hass, first["id"])
    assert response == {
        "brew_id": first["id"],
        "removed_from_learning": True,
        "model": {
            "tau": round(expected.tau, 3),
            "c": round(expected.c, 3),
            "count": 1,
        },
    }
    assert stop.model.to_dict() == expected.to_dict()
    assert [e["brew_id"] for e in stop.journal.entries] == [second["id"]]
    assert (coord.brews.brew_count, coord.brews.coffee_total) == totals
    assert [b["id"] for b in coord.brews.brews] == [second["id"]]
    # Sensors follow the replay.
    assert _state(hass, SAMPLES) == "1"
    assert _attrs(hass)["journal_samples"] == 1
    assert _state(hass, TAU) == str(round(expected.tau, 3))
    assert _state(hass, TAU) != tau_before
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["auto_stop"]["journal"]["samples"] == 1

    # Persisted with the model; restored after a reload.
    model = stop.model.to_dict()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    data = hass_storage[key]["data"]
    assert data["model"] == model
    assert data["journal"]["base"] == StopModel().to_dict()
    assert [e["brew_id"] for e in data["journal"]["entries"]] == [second["id"]]
    stop = entry.runtime_data.autostop
    assert stop.model.to_dict() == model and len(stop.journal) == 1

    # Deleting the newest (last remaining) brew: back to the prior.
    response = await _delete(hass, second["id"])
    assert response["removed_from_learning"] is True
    assert stop.model.to_dict() == StopModel().to_dict()
    assert _state(hass, SAMPLES) == "0" and _state(hass, TAU) == "1.0"
    assert entry.runtime_data.brews.brews == []
    assert entry.runtime_data.brews.brew_count == totals[0]

    # Reset clears the journal too.
    stop.journal.add(LearningJournal.entry(2.0, 2.0, "x", None))
    stop.model.update(2.0, 2.0)
    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.microbalance_reset_stop_learning"}, blocking=True,
    )
    assert stop.model.count == 0 and len(stop.journal) == 0
    assert _attrs(hass) == {"journal_samples": 0, "journal_base_samples": 0}


async def test_learn_from_brew_replaces_journal_entry_and_delete(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """learn_stop_from_brew for a brew already journalled replaces its
    sample (no double counting); a rejected relearn changes nothing."""
    _store_brews(hass_storage, entry)
    await _setup(hass, entry)
    stop = entry.runtime_data.autostop

    async def learn(offset: float) -> dict[str, Any]:
        return await hass.services.async_call(
            DOMAIN, "learn_stop_from_brew",
            {"brew_id": "01REAL", "cut_offset": offset},
            blocking=True, return_response=True,
        )

    first = await learn(27.83)
    assert first["accepted"] and first["model"]["count"] == 1
    assert len(stop.journal) == 1
    assert stop.journal.entries[0]["brew_id"] == "01REAL"
    assert stop.journal.entries[0]["start"] == REAL_BREW["start"]
    assert abs(stop.journal.entries[0]["overshoot"] - first["adjusted_overshoot"]) < 0.005
    once = stop.model.to_dict()

    # Same brew again: replaced, still one sample, same model.
    again = await learn(27.83)
    assert again["accepted"] and again["model"]["count"] == 1
    assert len(stop.journal) == 1
    assert stop.model.to_dict() == once

    # Another cut point of the same brew: replaced by the new sample.
    other = await learn(28.04)
    assert other["accepted"] and other["model"]["count"] == 1
    expected = StopModel()
    assert expected.update(
        stop.journal.entries[0]["flow"], stop.journal.entries[0]["overshoot"]
    )
    assert stop.model.to_dict() == expected.to_dict()
    replaced = stop.model.to_dict()

    # Rejected (overshoot > 5 g): the journalled sample stays.
    rejected = await learn(25.5)
    assert rejected["accepted"] is False
    assert stop.model.to_dict() == replaced and len(stop.journal) == 1

    # Deleting the brew takes it out: prior again.
    response = await _delete(hass, "01REAL")
    assert response["removed_from_learning"] is True
    assert response["model"] == {"tau": 1.0, "c": 0.3, "count": 0}
    assert len(stop.journal) == 0


async def test_migrated_model_without_journal(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """A store from before the journal (live host data): the model stays,
    the journal is empty on it; deleting an unjournalled brew leaves the
    model alone; a sample learned now can be taken out again exactly."""
    _store_brews(hass_storage, entry)
    key = _store_host_model(hass_storage, entry)
    await _setup(hass, entry)
    stop = entry.runtime_data.autostop
    host = HOST_STOP_STORE["model"]
    assert stop.model.to_dict() == host
    assert len(stop.journal) == 0
    assert _state(hass, SAMPLES) == "11"
    assert _attrs(hass) == {"journal_samples": 0, "journal_base_samples": 11}

    response = await _delete(hass, "01OTHER")
    assert response == {
        "brew_id": "01OTHER",
        "removed_from_learning": False,
        "model": {"tau": round(host["tau"], 3), "c": round(host["c"], 3), "count": 11},
    }
    assert stop.model.to_dict() == host

    learned = await hass.services.async_call(
        DOMAIN, "learn_stop_from_brew", {"brew_id": "01REAL", "cut_offset": 27.83},
        blocking=True, return_response=True,
    )
    assert learned["accepted"] and learned["model"]["count"] == 12
    assert _attrs(hass) == {"journal_samples": 1, "journal_base_samples": 11}
    response = await _delete(hass, "01REAL")
    assert response["removed_from_learning"] is True
    assert stop.model.to_dict() == host
    assert _state(hass, SAMPLES) == "11"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    data = hass_storage[key]["data"]
    assert data["model"] == host
    assert data["journal"] == {"base": host, "entries": []}
    # The other keys are kept as they were.
    assert data["switch_latency"]["values"] == [0.13, 0.132, 0.141, 0.129]


async def test_delete_refused_while_brew_busy(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    entry,
    no_ble,
    lovelace,
    clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brew whose auto-stop cut is not concluded (learning pending) or
    that is still running cannot be deleted."""
    _store_brews(hass_storage, entry)
    await _setup(hass, entry)
    coord = entry.runtime_data
    start = datetime.fromisoformat(REAL_BREW["start"]).timestamp()
    assert datetime.fromtimestamp(start, UTC).isoformat() == REAL_BREW["start"]
    coord.autostop._cut = Cut(  # noqa: SLF001
        start=start, t=start + 27.0, weight=36.0, flow=2.0, target=38.0, predicted=2.3
    )
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "delete_brew", {"brew_id": "01REAL"}, blocking=True
        )
    assert err.value.translation_key == "brew_busy"
    assert coord.brews.get("01REAL") is not None
    coord.autostop._cut = None  # noqa: SLF001

    # Running (e.g. continued after a pause): the tracker's brew start.
    with monkeypatch.context() as patch:
        patch.setattr(type(coord.brew_tracker), "start", property(lambda _s: start))
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "delete_brew", {"brew_id": "01REAL"}, blocking=True
            )
    # Otherwise fine.
    await _delete(hass, "01REAL")
    assert coord.brews.get("01REAL") is None


async def test_learn_from_brew_refused_while_cut_pending(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """learn_stop_from_brew raises brew_busy while the brew's live cut is not
    concluded (it would be learned twice); afterwards it works."""
    _store_brews(hass_storage, entry)
    await _setup(hass, entry)
    stop = entry.runtime_data.autostop
    start = datetime.fromisoformat(REAL_BREW["start"]).timestamp()
    stop._cut = Cut(  # noqa: SLF001
        start=start, t=start + 27.0, weight=36.0, flow=2.0, target=38.0, predicted=2.3
    )
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "learn_stop_from_brew", {"brew_id": "01REAL", "cut_offset": 27.83},
            blocking=True, return_response=True,
        )
    assert err.value.translation_key == "brew_busy"
    assert stop.model.count == 0 and len(stop.journal) == 0
    stop._cut = None  # noqa: SLF001
    response = await hass.services.async_call(
        DOMAIN, "learn_stop_from_brew", {"brew_id": "01REAL", "cut_offset": 27.83},
        blocking=True, return_response=True,
    )
    assert response["accepted"] is True


async def test_websocket_learned_flag_follows_journal(
    hass: HomeAssistant, hass_storage: dict[str, Any], hass_ws_client, entry,
    no_ble, lovelace, clock,
) -> None:
    """Brew summaries and the full brew carry ``learned``; learn and reset
    push a brews update so the card reloads the flags."""
    _store_brews(hass_storage, entry)
    await _setup(hass, entry)
    ws = await hass_ws_client(hass)
    msg_id = 0

    async def call(**msg: Any) -> Any:
        nonlocal msg_id
        msg_id += 1
        await ws.send_json({"id": msg_id, **msg})
        reply = await ws.receive_json()
        assert reply["success"], reply
        return reply["result"]

    async def flags() -> dict[str, bool]:
        brews = (await call(type=f"{DOMAIN}/brews"))["brews"]
        return {b["id"]: b["learned"] for b in brews}

    assert await flags() == {"01OTHER": False, "01REAL": False}
    sub = await call(type=f"{DOMAIN}/subscribe_brews")
    assert sub is None
    sub_id = msg_id

    await hass.services.async_call(
        DOMAIN, "learn_stop_from_brew", {"brew_id": "01REAL", "cut_offset": 27.83},
        blocking=True, return_response=True,
    )
    pushed = await ws.receive_json()
    assert pushed["id"] == sub_id and pushed["type"] == "event"
    assert await flags() == {"01OTHER": False, "01REAL": True}
    full = (await call(type=f"{DOMAIN}/brew", brew_id="01REAL"))["brew"]
    assert full["learned"] is True and "profile" in full

    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.microbalance_reset_stop_learning"}, blocking=True,
    )
    pushed = await ws.receive_json()
    assert pushed["id"] == sub_id
    assert await flags() == {"01OTHER": False, "01REAL": False}


async def test_load_rebuilds_damaged_model_from_journal(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, clock
) -> None:
    """Valid journal, missing or damaged model: rebuilt by replaying the
    journal from its base (not the prior)."""
    host = HOST_STOP_STORE["model"]
    journal = LearningJournal(StopModel.from_dict(host))
    expected = StopModel.from_dict(host)
    for i, (flow, y) in enumerate([(2.0, 1.9), (2.4, 2.2), (1.8, 1.7)]):
        assert expected.update(flow, y)
        journal.add(LearningJournal.entry(flow, y, f"b{i}", None))
    key = _store_host_model(hass_storage, entry)
    data = hass_storage[key]["data"]
    data["journal"] = journal.to_dict()
    data["model"] = {"tau": "nan?", "c": None}
    await _setup(hass, entry)
    stop = entry.runtime_data.autostop
    assert stop.model.to_dict() == expected.to_dict()
    assert stop.model.count == 14 and len(stop.journal) == 3
    assert _state(hass, SAMPLES) == "14"
