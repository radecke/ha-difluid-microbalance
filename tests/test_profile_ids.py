"""0.7.0b5: built-in pump profiles by id, names in the HA language, the
migration of stored German labels (stop store 1.3 -> 1.4, config entry
1.2 -> 1.3).

Same harness as ``test_profiles_ha``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402

from test_integration import OPTIONS, PLUG_ATTRS, _setup, _state  # noqa: E402
from test_profile_preview import _ws  # noqa: E402
from test_profiles_ha import MINE, SELECT, _call, _setup_profiles, tclock  # noqa: E402,F401
from test_throttle_ha import PLUG, PUMP  # noqa: E402

from custom_components.difluid_scale import profiles as pf  # noqa: E402
from custom_components.difluid_scale.beans import migrate_bean_profiles  # noqa: E402
from custom_components.difluid_scale.const import DOMAIN, STOP_STORAGE_KEY  # noqa: E402

COMPONENT = Path(__file__).parents[1] / "custom_components" / "difluid_scale"
IDS = ["standard", "soft_preinfusion", "blooming", "allonge", "lever", "target_time"]


def test_ids_names_and_legacy_labels() -> None:
    assert [p.label for p in pf.BUILTINS] == IDS
    for old, pid in [("Standard", "standard"), ("Sanfte Vorbrühung", "soft_preinfusion"),
                     ("Blooming", "blooming"), ("Allongé", "allonge"),
                     ("Hebel-Annäherung", "lever"), ("Zielzeit", "target_time"),
                     ("sanfte vorbrühung", "soft_preinfusion"), ("LEVER", "lever")]:
        assert pf.builtin_id(old) == pid
        assert pf.builtin(old).label == pid
    assert pf.builtin_id("Mein Profil") is None
    assert pf.display_name("soft_preinfusion", "de") == "Sanfte Vorbrühung"
    assert pf.display_name("Sanfte Vorbrühung", "en-GB") == "Soft preinfusion"
    assert pf.display_name("Mein Profil", "de") == "Mein Profil"
    blooming = pf.builtin("blooming")
    assert [p.name for p in blooming.phases] == ["fill", "bloom", "flow"]
    assert [pf.phase_display_name(blooming, p.name, "en") for p in blooming.phases] == [
        "Fill", "Bloom", "Flow"]
    assert [pf.phase_display_name(blooming, p.name, "de") for p in blooming.phases] == [
        "Füllen", "Blooming", "Fluss"]
    mine = pf.parse_profile({**MINE, "phases": [{**MINE["phases"][0], "name": "fill"}]})
    assert pf.phase_display_name(mine, "fill", "de") == "fill", "imported: as it is"


def test_translations_match_the_names() -> None:
    """The select's state translations (en/de) are BUILTIN_NAMES; the phase
    keys translate the ``running_phase`` attribute; strings.json = en."""
    strings = json.loads((COMPONENT / "strings.json").read_text())
    for lang in ("en", "de"):
        tr = json.loads((COMPONENT / "translations" / f"{lang}.json").read_text())
        sel = tr["entity"]["select"]["pump_profile"]
        assert sel["state"] == {k: v[lang] for k, v in pf.BUILTIN_NAMES.items()}
        assert sel["state_attributes"]["running_phase"]["state"] == {
            k: v[lang] for k, v in pf.PHASE_NAMES.items()}
        for attr in ("running", "last_outcome_profile", "last_brew_profile"):
            assert sel["state_attributes"][attr]["state"] == sel["state"]
        opts = tr["selector"]["bean_profile"]["options"]
        assert {k: opts[k] for k in pf.BUILTIN_NAMES} == sel["state"]
    assert strings["entity"]["select"]["pump_profile"]["state"] == {
        k: v["en"] for k, v in pf.BUILTIN_NAMES.items()}


def test_migrate_profile_ids() -> None:
    old = {"settings": {}, "profiles": {"active": "Sanfte Vorbrühung", "user": [MINE]}}
    new = pf.migrate_profile_ids(old)
    assert new["profiles"] == {"active": "soft_preinfusion", "user": [MINE]}
    assert old["profiles"]["active"] == "Sanfte Vorbrühung", "input untouched"
    # An imported profile named like a new id is renamed; the active
    # reference follows it.
    clash = {**MINE, "label": "Lever"}
    new = pf.migrate_profile_ids({"profiles": {"active": "lever", "user": [clash]}})
    assert new["profiles"]["user"][0]["label"] == "Lever (imported)"
    assert new["profiles"]["active"] == "Lever (imported)"
    new = pf.migrate_profile_ids({"profiles": {"active": "Mein Profil", "user": [MINE]}})
    assert new["profiles"]["active"] == "Mein Profil"
    assert pf.migrate_profile_ids({"settings": {}}) == {"settings": {}}


def test_migrate_bean_profiles() -> None:
    options = {"beans": [
        {"id": "a", "name": "A", "ratio": 2.0, "profile": "Hebel-Annäherung"},
        {"id": "b", "name": "B", "ratio": 2.0, "profile": "Mein Profil"},
        {"id": "c", "name": "C", "ratio": 2.0},
    ], "x": 1}
    new = migrate_bean_profiles(options)
    assert [b.get("profile") for b in new["beans"]] == ["lever", "Mein Profil", None]
    assert new["x"] == 1
    assert migrate_bean_profiles({"x": 1}) == {"x": 1}


async def test_store_1_3_with_german_labels_migrates(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry, no_ble, lovelace, tclock
) -> None:
    """A stop store written by 0.7.0b4 (1.3, active profile by its German
    label): loaded with the id, written back as 1.4."""
    await _setup_profiles(hass, entry, mode="observe")
    stop = entry.runtime_data.autostop
    data = stop._data_to_save()  # noqa: SLF001
    data["throttle"]["profiles"] = {"active": "Hebel-Annäherung", "user": [MINE]}
    key = STOP_STORAGE_KEY.format(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass_storage[key] = {"version": 1, "minor_version": 3, "key": key, "data": data}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, SELECT) == "lever"
    stop = entry.runtime_data.autostop
    stop.async_schedule_save()
    await stop.async_flush()
    saved = hass_storage[key]
    assert saved["minor_version"] == 4
    assert saved["data"]["throttle"]["profiles"]["active"] == "lever"
    assert saved["data"]["throttle"]["profiles"]["user"] == [MINE]


async def test_entry_1_2_bean_profiles_migrate(
    hass: HomeAssistant, no_ble, lovelace, tclock
) -> None:
    """An entry written by 0.7.0b4 (1.2): bean defaults by German label
    become ids; selecting the bean still selects that profile."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from conftest import ADDR

    hass.config.components.add("bluetooth_adapters")
    hass.config.components.add("bluetooth")
    beans = [
        {"id": "b1", "name": "Brasil", "ratio": 2.0, "profile": "Sanfte Vorbrühung"},
        {"id": "b2", "name": "Kenia", "ratio": 2.2, "profile": "Zielzeit"},
    ]
    old = dict(OPTIONS, stop_switch=PLUG, pump_power=PUMP, beans=beans)
    entry = MockConfigEntry(
        domain=DOMAIN, data={"address": ADDR}, title="Microbalance",
        unique_id=ADDR, options=old, version=1, minor_version=2,
    )
    entry.add_to_hass(hass)
    hass.states.async_set(PLUG, "off", PLUG_ATTRS)
    await _setup(hass, entry)
    assert entry.minor_version == 4  # and 1.3 -> 1.4 (0.7.0b6)
    assert [b["profile"] for b in entry.options["beans"]] == ["soft_preinfusion", "target_time"]
    assert [b["ratios"] for b in entry.options["beans"]] == [
        {"soft_preinfusion": 2.0}, {"target_time": 2.2},
    ]
    assert [b["name"] for b in entry.options["beans"]] == ["Brasil", "Kenia"]
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.microbalance_bean", "option": "Kenia"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert _state(hass, SELECT) == "target_time"


async def test_services_accept_ids_and_old_labels(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="observe")
    res = await _call(hass, "export_profile", {"label": "Sanfte Vorbrühung"})
    assert res["label"] == "soft_preinfusion" and res["builtin"] is True
    res = await _call(hass, "export_profile", {"label": "soft_preinfusion"})
    assert res["label"] == "soft_preinfusion"
    # Ids and old labels are reserved for imports (any case).
    for label in ("lever", "LEVER", "Hebel-Annäherung", "target_time", "Zielzeit"):
        with pytest.raises(ServiceValidationError):
            await _call(hass, "import_profile", {"profile": {**MINE, "label": label}})
    with pytest.raises(ServiceValidationError):
        await _call(hass, "delete_profile", {"label": "Zielzeit"})
    # The select takes the ids.
    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": "lever"}, blocking=True
    )
    assert _state(hass, SELECT) == "lever"
    assert hass.states.get(SELECT).attributes["options"] == IDS


async def test_ws_names_in_the_language(
    hass: HomeAssistant, hass_ws_client, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="observe", profile="blooming")
    await _call(hass, "import_profile", {"profile": MINE}, response=False)
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT, "language": "en-GB"})
    prof = res["result"]["profile"]
    assert prof["label"] == "blooming" and prof["name"] == "Blooming"
    assert [p["name"] for p in prof["phases"]] == ["Fill", "Bloom", "Flow"]
    assert [p["key"] for p in prof["phases"]] == ["fill", "bloom", "flow"]
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT, "language": "de", "label": "lever"})
    prof = res["result"]["profile"]
    assert prof["name"] == "Hebel-Annäherung"
    assert [p["name"] for p in prof["phases"]] == ["Füllen", "Spitze", "Abfall"]
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT, "language": "de", "label": "Mein Profil"})
    prof = res["result"]["profile"]
    assert prof["name"] == "Mein Profil"
    assert [p["name"] for p in prof["phases"]] == [p["name"] for p in MINE["phases"]]
    # Without a language: the server's.
    hass.config.language = "de"
    res = await _ws(hass, hass_ws_client, {"entity_id": SELECT, "label": "soft_preinfusion"})
    assert res["result"]["profile"]["name"] == "Sanfte Vorbrühung"


def test_migrate_profile_ids_unique_names() -> None:
    """The renamed label is unique: "lever" next to an existing "Lever
    (imported)" becomes "lever (imported 2)"; an imported profile named
    like a built-in's English name ("Target time", allowed before 0.7.0b5)
    is renamed too, and the active reference follows the right one."""
    lever = {**MINE, "label": "lever"}
    taken = {**MINE, "label": "Lever (imported)"}
    new = pf.migrate_profile_ids({"profiles": {"active": "lever", "user": [lever, taken]}})
    labels = [u["label"] for u in new["profiles"]["user"]]
    assert labels == ["lever (imported 2)", "Lever (imported)"]
    assert new["profiles"]["active"] == "lever (imported 2)"
    tt = {**MINE, "label": "Target time"}
    new = pf.migrate_profile_ids({"profiles": {"active": "Zielzeit", "user": [tt]}})
    assert new["profiles"]["user"][0]["label"] == "Target time (imported)"
    assert new["profiles"]["active"] == "target_time", "the built-in, by its old label"
    # The renamed profiles load (nothing skipped).
    library = pf.ProfileLibrary()
    assert library.load(new["profiles"]) == 0
    assert library.active.label == "target_time"
    assert [p.label for p in library.user] == ["Target time (imported)"]


def test_import_reserves_names_in_every_language() -> None:
    for label in ("Soft preinfusion", "soft PREINFUSION", "Lever approximation",
                  "Target time", "Sanfte Vorbrühung", "hebel-annäherung", "Zielzeit",
                  "Allongé", "standard", "target_time"):
        assert pf.reserved_label(label), label
        with pytest.raises(pf.LibraryError):
            pf.ProfileLibrary().add(pf.parse_profile({**MINE, "label": label}))
    assert not pf.reserved_label("Mein Profil")
    assert not pf.reserved_label("Lever (imported)")


async def test_import_service_refuses_display_names(
    hass: HomeAssistant, entry, no_ble, lovelace, tclock
) -> None:
    await _setup_profiles(hass, entry, mode="observe")
    for label in ("Soft preinfusion", "TARGET TIME", "Lever approximation"):
        with pytest.raises(ServiceValidationError):
            await _call(hass, "import_profile", {"profile": {**MINE, "label": label}})
