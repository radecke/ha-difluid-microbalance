"""0.7.0b3: the profile definition for the profile preview tile feature
(websocket ``difluid_scale/profile``) and the ``pump`` flag of the throttle
record (the card draws no power band without a pump power entity).

Same harness as ``test_profiles_ha``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402

from test_integration import Scale, mock_plug  # noqa: E402
from test_profiles_ha import MINE, SELECT, _call, _setup_profiles, _tare, tclock  # noqa: E402,F401
from test_throttle_ha import Pump, _shot  # noqa: E402

from custom_components.difluid_scale.const import DOMAIN  # noqa: E402
from custom_components.difluid_scale.profiles import (  # noqa: E402
    BUILTINS,
    POWER_FLOOR_DEFAULT,
    parse_profile,
)
from custom_components.difluid_scale.websocket import profile_definition  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "profile_definitions.json"


def test_card_fixture_matches_the_builtins() -> None:
    """The node test of the feature draws from this file: it must be what
    the websocket sends for the built-ins (and a valid imported profile)."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["floor"] == POWER_FLOOR_DEFAULT
    assert data["language"] == "de"
    assert data["builtins"] == [profile_definition(p, "de") for p in BUILTINS]
    imported = data["imported"]
    raw = {
        "label": imported["label"],
        "phases": [
            {
                "name": p["name"],
                "duration": p["duration"],
                "pump": p["power"],
                "transition": {"type": p["transition"], "duration": p["transition_duration"]},
                "targets": p["targets"],
            }
            for p in imported["phases"]
        ],
    }
    assert profile_definition(parse_profile(raw), "de") == imported


async def _ws(hass: HomeAssistant, hass_ws_client, msg: dict[str, Any]) -> dict[str, Any]:
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/profile", **msg})
    return await ws.receive_json()


async def test_ws_profile_active_label_imported(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="observe", profile="blooming", pump=False)
    registry = er.async_get(hass)
    target = registry.async_get_entity_id(
        "number", DOMAIN, f"{entry.runtime_data.address}_target_shot_time"
    )
    floor = registry.async_get_entity_id(
        "number", DOMAIN, f"{entry.runtime_data.address}_throttle_floor"
    )
    assert target and floor

    # The active profile, found by the select's entity id.
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT})
    assert res["success"], res
    result = res["result"]
    assert result["entry_id"] == entry.entry_id
    assert result["profile"]["label"] == "blooming"
    assert result["profile"]["builtin"] is True and result["profile"]["throttle"] is None
    assert [p["name"] for p in result["profile"]["phases"]] == ["Fill", "Bloom", "Flow"]  # server language (en)
    assert result["profile"]["phases"][2]["flow"] == 2.2
    assert result["profile"]["phases"][2]["transition"] == "linear"
    assert result["profile"]["phases"][0]["targets"] == [
        {"type": "volumetric", "operator": "gte", "value": 1.5}
    ]
    assert result["floor"] == POWER_FLOOR_DEFAULT
    assert result["target_time_entity"] == target
    assert result["floor_entity"] == floor

    # By label (case-insensitive); Zielzeit carries its throttle id.
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT, "label": "zielzeit"})
    assert res["result"]["profile"]["label"] == "target_time"
    assert res["result"]["profile"]["throttle"] == "target_time"

    # An imported profile works the same; the floor follows its setting.
    await _call(hass, "import_profile", {"profile": MINE}, response=False)
    entry.runtime_data.autostop.throttle.set_floor(55)
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT, "label": "Mein Profil"})
    prof = res["result"]["profile"]
    assert prof["builtin"] is False and prof["throttle"] is None
    assert [(p["power"], p["duration"]) for p in prof["phases"]] == [(50.0, 6.0), (100.0, 60.0)]
    assert prof["phases"][1]["transition_duration"] == 2.0
    assert res["result"]["floor"] == 55

    # Also by entry id; any entity of the scale.
    res = await _ws(hass, hass_ws_client, {"entry_id": entry.entry_id})
    assert res["result"]["profile"]["label"] == "blooming"
    res = await _ws(hass, hass_ws_client, {"entity_id": target})
    assert res["success"]


async def test_ws_profile_errors(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="observe")
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT, "label": "Gibt es nicht"})
    assert not res["success"] and res["error"]["code"] == "not_found"
    hass.states.async_set("select.other", "x")
    res = await _ws(hass, hass_ws_client, {"entity_id": "select.other"})
    assert not res["success"] and res["error"]["code"] == "not_found"
    res = await _ws(hass, hass_ws_client, {"entity_id": "select.nope"})
    assert not res["success"] and res["error"]["code"] == "not_found"


# --------------------------------------------------- pump flag (0.7.0b3)


async def _brew(hass: HomeAssistant, entry, tclock, *, pump: bool, seconds: float) -> Any:
    """Observe mode, Blooming; a shot of ``seconds``; the coordinator."""
    await _setup_profiles(hass, entry, mode="observe", profile="blooming", pump=pump)
    mock_plug(hass)
    if pump:
        Pump(hass)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    _tare(scale)
    await _shot(hass, scale, tclock, pump=None, seconds=seconds)
    return coord


@pytest.mark.parametrize("pump", [False, True])
async def test_live_pump_flag(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock, pump: bool
) -> None:
    """The live throttle info says whether a pump power entity is set up."""
    coord = await _brew(hass, entry, tclock, pump=pump, seconds=5.0)
    live = coord.live_snapshot().get("throttle")
    assert live is not None and live["pump"] is pump


@pytest.mark.parametrize("pump", [False, True])
async def test_record_pump_flag(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock, pump: bool
) -> None:
    """The stored record too. Without a pump power entity the trace is an
    assumption (the card draws no band); with one it is what would have
    been set (observe)."""
    coord = await _brew(hass, entry, tclock, pump=pump, seconds=50.0)
    record = coord.brews.last["throttle"]
    assert record["mode"] == "observe" and record["applied"] is False
    assert record["pump"] is pump
    assert record["profile"] == "blooming" and record["power"]


# ------------------------------------------- fix round 1 (0.7.0b3 review)


@pytest.mark.parametrize(("mode", "pump", "controlled"), [
    ("observe", True, False),
    ("throttle", True, True),
    ("throttle", False, False),  # no pump entity: only observed (no_pump)
])
async def test_controlled_flag_live_and_stored(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock, mode: str, pump: bool, controlled: bool
) -> None:
    """``controlled``: the engine controlled the pump (the card's line
    style), the same live and stored, whether or not a value had to be
    written (Sanfte Vorbrühung in throttle mode writes 40 %)."""
    await _setup_profiles(hass, entry, mode=mode, profile="soft_preinfusion", pump=pump)
    mock_plug(hass)
    if pump:
        Pump(hass)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    _tare(scale)
    await _shot(hass, scale, tclock, pump=None, seconds=5.0)
    live = coord.live_snapshot()["throttle"]
    assert live["controlled"] is controlled


async def test_controlled_without_a_write_is_solid(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    """Standard in throttle mode never has to write the pump (100 %):
    ``applied`` false (nothing written), ``controlled`` true — stored and
    live the same."""
    await _setup_profiles(hass, entry, mode="throttle", profile="standard")
    mock_plug(hass)
    pump = Pump(hass)
    coord = entry.runtime_data
    scale = Scale(coord, tclock)
    _tare(scale)
    await _shot(hass, scale, tclock, pump=pump, seconds=50.0)
    record = coord.brews.last["throttle"]
    assert record["profile"] == "standard"
    assert record["applied"] is False and record["controlled"] is True


async def test_profile_revision_follows_an_import_over_it(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, tclock
) -> None:
    """The select carries the active profile's revision; importing a
    profile of the same label over it changes it (the preview refetches);
    the websocket sends the same value."""
    await _setup_profiles(hass, entry, mode="observe")
    await _call(hass, "import_profile", {"profile": MINE, "activate": True}, response=False)
    await hass.async_block_till_done()
    rev1 = hass.states.get(SELECT).attributes["profile_revision"]
    assert isinstance(rev1, str) and len(rev1) == 12
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT})
    assert res["result"]["profile"]["revision"] == rev1
    changed = {**MINE, "phases": [{**MINE["phases"][0], "pump": 60}, MINE["phases"][1]]}
    await _call(hass, "import_profile", {"profile": changed, "overwrite": True}, response=False)
    await hass.async_block_till_done()
    rev2 = hass.states.get(SELECT).attributes["profile_revision"]
    assert rev2 != rev1
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT})
    assert res["result"]["profile"]["revision"] == rev2
    assert res["result"]["profile"]["phases"][0]["power"] == 60.0


async def test_ws_profile_rejects_an_invalid_entity_id(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="observe")
    res = await _ws(hass, hass_ws_client, {"entity_id": "not an entity"})
    assert not res["success"] and res["error"]["code"] == "invalid_format"
