"""0.7.0b6: a bean remembers its pump profile and keeps a ratio per profile.

Pure helpers first, then the Home Assistant level: choosing a profile with
a bean active, switching beans, the ratio written back per profile, no
self-echo, no reload per choice, the 1.3 -> 1.4 migration and the options
flow on the new format (the old one still read).
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.difluid_scale.beans import (
    BEAN_RATIOS,
    bean_ratio_for,
    beans_from_options,
    make_bean,
    migrate_bean_ratios,
    profile_ratio,
    with_bean_update,
    without_profile,
)
from custom_components.difluid_scale.profiles import builtin, parse_profile

OLD = {
    "beans": [
        {"id": "b1", "name": "Pacandé", "ratio": 2.7, "note": None},
        {"id": "b2", "name": "Kenia", "ratio": 2.2, "note": None, "profile": "allonge"},
    ]
}
WITH_RATIO = {
    "label": "Lungo",
    "phases": [
        {"name": "pre", "duration": 8, "pump": 60},
        {"name": "brew", "duration": 60, "pump": 100,
         "targets": [{"type": "ratio", "operator": "gte", "value": 3.5}]},
    ],
}


def test_old_format_read_as_ratio_of_its_profile() -> None:
    beans = beans_from_options(OLD)
    assert beans[0][BEAN_RATIOS] == {"standard": 2.7}
    assert beans[1][BEAN_RATIOS] == {"allonge": 2.2}
    assert bean_ratio_for(beans[0], "standard") == 2.7
    assert bean_ratio_for(beans[0], "Standard") == 2.7  # label case-insensitive
    assert bean_ratio_for(beans[0], "allonge") is None


def test_migration_keeps_everything() -> None:
    new = migrate_bean_ratios(OLD)
    assert [b["ratio"] for b in new["beans"]] == [2.7, 2.2]  # old readers
    assert new["beans"][0]["ratios"] == {"standard": 2.7}
    assert new["beans"][1]["ratios"] == {"allonge": 2.2}
    assert migrate_bean_ratios(new) == new  # idempotent
    assert migrate_bean_ratios({"x": 1}) == {"x": 1}


def test_update_writes_profile_and_ratio_and_mirrors() -> None:
    opts = migrate_bean_ratios(OLD)
    opts = with_bean_update(opts, "b1", profile="allonge", ratio=5.0, ratio_profile="allonge")
    bean = opts["beans"][0]
    assert bean["profile"] == "allonge"
    assert bean["ratios"] == {"standard": 2.7, "allonge": 5.0}
    assert bean["ratio"] == 5.0  # = the ratio for its own profile
    # A ratio for another profile does not touch the mirror.
    opts = with_bean_update(opts, "b1", ratio=2.8, ratio_profile="standard")
    assert opts["beans"][0]["ratio"] == 5.0
    assert opts["beans"][0]["ratios"]["standard"] == 2.8
    assert opts["beans"][1] == migrate_bean_ratios(OLD)["beans"][1]


def test_make_bean_keeps_other_ratios() -> None:
    bean = make_bean("b1", "Pacandé", 4.8, profile="allonge", ratios={"standard": 2.7, "Allonge": 5.0})
    assert bean["ratios"] == {"standard": 2.7, "allonge": 4.8}
    assert make_bean("b2", "X", 2.0)["ratios"] == {"standard": 2.0}


def test_ratio_changed_by_an_older_version_wins() -> None:
    """After a downgrade an older version changes only ``ratio`` (it keeps
    ``ratios`` and the marker ``ratio_written``): back on 0.7.0b6 that
    ratio counts for the bean's profile. Without such a change (marker
    equal) nothing is overridden."""
    bean = {"id": "b1", "name": "P", "ratio": 2.5, "ratio_written": 5.0,
            "profile": "allonge", "ratios": {"standard": 2.7, "allonge": 5.0}}
    parsed = beans_from_options({"beans": [bean]})[0]
    assert parsed["ratios"] == {"standard": 2.7, "allonge": 2.5}
    assert parsed["ratio_written"] == 2.5
    # Without a profile: the Standard entry.
    bean = {"id": "b2", "name": "Q", "ratio": 2.4, "ratio_written": 2.7,
            "ratios": {"standard": 2.7, "allonge": 5.0}}
    assert beans_from_options({"beans": [bean]})[0]["ratios"] == {"standard": 2.4, "allonge": 5.0}
    # As 0.7.0b6 writes it: nothing changes.
    opts = with_bean_update(migrate_bean_ratios(OLD), "b1", ratio=2.9, ratio_profile="allonge")
    assert beans_from_options(opts)[0]["ratios"] == {"standard": 2.7, "allonge": 2.9}


def test_deleting_a_profile_keeps_the_standard_ratio() -> None:
    """{standard 2.7, allonge 5.0} with Allongé as its profile: Allongé
    deleted -> the bean has no profile, ``ratio`` mirrors Standard 2.7, the
    Standard ratio is not overwritten by the Allongé one."""
    opts = migrate_bean_ratios(OLD)
    opts = with_bean_update(opts, "b1", profile="allonge", ratio=5.0, ratio_profile="allonge")
    new = without_profile(opts, "Allonge")
    bean = new["beans"][0]
    assert "profile" not in bean and bean["ratio"] == 2.7 and bean["ratio_written"] == 2.7
    parsed = beans_from_options(new)[0]
    assert parsed["ratios"] == {"standard": 2.7, "allonge": 5.0}
    # Kenia had Allongé too, with only that ratio: it keeps it as ``ratio``.
    kenia = beans_from_options(new)[1]
    assert "profile" not in kenia and kenia["ratio"] == 2.2
    assert without_profile(new, "lever") is None


def test_profile_ratio_from_its_json() -> None:
    assert profile_ratio(parse_profile(WITH_RATIO)) == 3.5
    top = {**WITH_RATIO, "label": "Top", "targets": [{"type": "ratio", "value": 4.0}]}
    assert profile_ratio(parse_profile(top)) == 4.0
    assert profile_ratio(builtin("allonge")) is None
    assert profile_ratio(None) is None


pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from test_integration import OPTIONS, PLUG, PLUG_ATTRS, _options_step, _setup, _state

PROFILE = "select.microbalance_pump_profile"
BEAN = "select.microbalance_bean"
RATIO = "number.microbalance_target_ratio"


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    await hass.services.async_call(
        "select", "select_option", {"entity_id": entity_id, "option": option}, blocking=True
    )
    await hass.async_block_till_done()


async def _set_ratio(hass: HomeAssistant, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": RATIO, "value": value}, blocking=True
    )
    await hass.async_block_till_done()


def _bean(entry, bean_id: str) -> dict[str, Any]:
    return next(b for b in entry.options["beans"] if b["id"] == bean_id)


async def _setup_beans(hass: HomeAssistant, entry, beans: list, minor: int = 4) -> None:
    hass.states.async_set(PLUG, "on", PLUG_ATTRS)
    hass.config_entries.async_update_entry(
        entry, options=dict(OPTIONS, stop_switch=PLUG, beans=beans), minor_version=minor
    )
    await _setup(hass, entry)


class _Writes:
    """Counts option writes (each would be applied by the update listener)."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.count = 0
        orig = hass.config_entries.async_update_entry

        def _count(e, **kwargs):
            if e is entry and "options" in kwargs:
                self.count += 1
            return orig(e, **kwargs)

        hass.config_entries.async_update_entry = _count  # type: ignore[method-assign]


async def test_bean_remembers_profile_and_ratio_per_profile(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    beans = migrate_bean_ratios(OLD)["beans"]
    beans[0] = {**beans[0], "profile": "standard"}
    await _setup_beans(hass, entry, beans)
    coord = entry.runtime_data
    client = coord._client
    autostop = coord.autostop
    await _select(hass, BEAN, "Pacandé")
    assert _state(hass, PROFILE) == "standard" and _state(hass, RATIO) == "2.7"
    writes = _Writes(hass, entry)
    # A profile chosen with the bean active: the bean's profile from now
    # on; no ratio for it yet -> the bean's ratio as a start, remembered.
    await _select(hass, PROFILE, "allonge")
    assert writes.count == 1  # one write for profile + ratio
    assert _bean(entry, "b1")["profile"] == "allonge"
    assert _bean(entry, "b1")["ratios"] == {"standard": 2.7, "allonge": 2.7}
    assert _state(hass, RATIO) == "2.7"
    # The ratio changed: written for the active profile only.
    await _set_ratio(hass, 5.0)
    assert writes.count == 2
    assert _bean(entry, "b1")["ratios"] == {"standard": 2.7, "allonge": 5.0}
    assert autostop.ratio == 5.0
    # Back to Standard: its own ratio.
    await _select(hass, PROFILE, "standard")
    assert _state(hass, RATIO) == "2.7" and _bean(entry, "b1")["profile"] == "standard"
    await _select(hass, PROFILE, "allonge")
    assert _state(hass, RATIO) == "5.0"
    # Switching beans: the other bean's profile, then its ratio for it.
    before = writes.count
    await _select(hass, BEAN, "Kenia")
    assert _state(hass, PROFILE) == "allonge" and _state(hass, RATIO) == "2.2"
    # No echo: the profile set by the bean is not written back (Kenia had
    # it and its ratio for it), and nothing else changed.
    assert writes.count == before
    assert _bean(entry, "b2") == beans[1]
    await _select(hass, BEAN, "Pacandé")
    assert _state(hass, PROFILE) == "allonge" and _state(hass, RATIO) == "5.0"
    assert writes.count == before
    # No reload and no reconnect for any of this: same coordinator, same
    # BLE client, still loaded.
    assert entry.runtime_data is coord and coord._client is client
    assert entry.state.name == "LOADED"


async def test_profile_without_bean_ratio_uses_its_own_target(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_beans(hass, entry, migrate_bean_ratios(OLD)["beans"])
    await _select(hass, BEAN, "Pacandé")
    await hass.services.async_call(
        "difluid_scale", "import_profile", {"profile": WITH_RATIO}, blocking=True,
        return_response=True,
    )
    await _select(hass, PROFILE, "Lungo")
    assert _state(hass, RATIO) == "3.5"
    assert _bean(entry, "b1")["ratios"]["Lungo"] == 3.5
    assert _bean(entry, "b1")["profile"] == "Lungo"


async def test_bean_without_profile_keeps_the_active_one(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_beans(hass, entry, migrate_bean_ratios(OLD)["beans"])
    await _select(hass, BEAN, "Kenia")
    assert _state(hass, PROFILE) == "allonge"
    await _select(hass, BEAN, "Pacandé")  # no profile of its own
    assert _state(hass, PROFILE) == "allonge"
    assert "profile" not in _bean(entry, "b1")
    # Its ratio as the start for that profile, remembered for it.
    assert _state(hass, RATIO) == "2.7"
    assert _bean(entry, "b1")["ratios"] == {"standard": 2.7, "allonge": 2.7}


async def test_profile_choice_without_bean_writes_nothing(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    await _setup_beans(hass, entry, [])
    await _set_ratio(hass, 2.3)
    writes = _Writes(hass, entry)
    await _select(hass, PROFILE, "allonge")
    assert writes.count == 0 and _state(hass, RATIO) == "2.3"


async def test_entry_1_3_migrates_and_old_format_still_read(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """An entry of 0.7.0b5 (1.3): ratios added, nothing lost. A bean in
    the old format (written by an older version after a downgrade) is
    still read."""
    await _setup_beans(hass, entry, OLD["beans"], minor=3)
    assert entry.minor_version == 4
    assert [b["ratios"] for b in entry.options["beans"]] == [{"standard": 2.7}, {"allonge": 2.2}]
    assert [b["ratio"] for b in entry.options["beans"]] == [2.7, 2.2]
    # Old format at 1.4 (no "ratios"): read as the ratio of its profile.
    hass.config_entries.async_update_entry(entry, options=dict(entry.options, beans=OLD["beans"]))
    await hass.async_block_till_done()
    await _select(hass, BEAN, "Kenia")
    assert _state(hass, PROFILE) == "allonge" and _state(hass, RATIO) == "2.2"


async def test_options_flow_edits_the_ratio_of_the_beans_profile(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    beans = migrate_bean_ratios(OLD)["beans"]
    beans[0] = {**beans[0], "profile": "allonge", "ratio": 5.0,
                "ratios": {"standard": 2.7, "allonge": 5.0}}
    await _setup_beans(hass, entry, beans)
    result = await _options_step(hass, entry, "edit_bean")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"id": "b1"})
    assert result["step_id"] == "edit_bean_details"
    suggested = {
        str(k): k.description.get("suggested_value")
        for k in result["data_schema"].schema
        if k.description
    }
    assert suggested["ratio"] == 5.0 and suggested["profile"] == "allonge"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Pacandé", "ratio": 4.8, "profile": "allonge"}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    bean = _bean(entry, "b1")
    assert bean["ratios"] == {"standard": 2.7, "allonge": 4.8}  # Standard kept
    assert bean["ratio"] == 4.8


async def _shot_with(hass: HomeAssistant, entry, clock, action) -> dict[str, Any]:
    """One auto-stopped shot (18 g dose); ``action`` runs once mid-brew."""
    from unittest.mock import patch

    from test_integration import Scale, _auto_shot, mock_plug

    from custom_components.difluid_scale.brew import BREWING

    coord = entry.runtime_data
    calls = mock_plug(hass)
    original = coord.autostop.feed_sample
    done = False

    def _feed(t: float, weight: float, placed: float | None = None) -> None:
        nonlocal done
        if not done and coord.brew_tracker.state == BREWING and weight > 5:
            done = True
            hass.async_create_task(action())
        original(t, weight, placed)

    scale = Scale(coord, clock)
    scale.calls = calls
    with patch.object(coord.autostop, "feed_sample", _feed):
        assert await _auto_shot(hass, scale, dose=18.0) is not None
    assert done
    return coord.autostop.last


async def test_choice_during_a_brew_waits_for_the_next(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """A profile or bean chosen while a brew runs does not change its cut
    target; the bean's ratio applies after it. A ratio set by hand still
    applies at once (as in 0.7.0b5)."""
    beans = migrate_bean_ratios(OLD)["beans"]
    beans[0] = {**beans[0], "profile": "standard", "ratio": 2.0,
                "ratios": {"standard": 2.0, "allonge": 3.0}}
    await _setup_beans(hass, entry, beans)
    autostop = entry.runtime_data.autostop
    await _select(hass, BEAN, "Pacandé")
    assert autostop.ratio == 2.0
    last = await _shot_with(hass, entry, clock, lambda: _select(hass, PROFILE, "allonge"))
    assert last["target"] == 36.0  # 2.0 x 18 g: the ratio the brew started with
    assert _state(hass, PROFILE) == "allonge"
    assert autostop.ratio == 3.0  # the bean's ratio for Allongé, after the brew
    assert _bean(entry, "b1")["profile"] == "allonge"
    # A bean chosen mid-brew: the same.
    last = await _shot_with(hass, entry, clock, lambda: _select(hass, BEAN, "Kenia"))
    assert last["target"] == 54.0  # 3.0 x 18 g
    assert autostop.ratio == 2.2
    # By hand mid-brew: at once (the cut uses it).
    await _select(hass, BEAN, "Pacandé")
    assert autostop.ratio == 3.0
    last = await _shot_with(hass, entry, clock, lambda: _set_ratio(hass, 2.1))
    assert last["target"] == 37.8  # 2.1 x 18 g
    assert autostop.ratio == 2.1


async def test_options_flow_profile_change_keeps_other_ratios(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Changing only the profile in the bean form: the ratio shown (the old
    profile's) is not written to the new profile; no ratio of another
    profile is overwritten. Changing the ratio too writes it for the new
    profile only."""
    beans = migrate_bean_ratios(OLD)["beans"]
    beans[0] = {**beans[0], "profile": "standard", "ratio": 2.7,
                "ratios": {"standard": 2.7, "allonge": 5.0}}
    await _setup_beans(hass, entry, beans)

    async def _edit(data: dict) -> None:
        result = await _options_step(hass, entry, "edit_bean")
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"id": "b1"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], data)
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()

    await _edit({"name": "Pacandé", "ratio": 2.7, "profile": "allonge"})
    bean = _bean(entry, "b1")
    assert bean["profile"] == "allonge"
    assert bean["ratios"] == {"standard": 2.7, "allonge": 5.0}  # untouched
    assert bean["ratio"] == 5.0  # the mirror: its ratio for Allongé
    # A new profile without a ratio yet, ratio left as shown: none written.
    await _edit({"name": "Pacandé", "ratio": 5.0, "profile": "lever"})
    assert _bean(entry, "b1")["ratios"] == {"standard": 2.7, "allonge": 5.0}
    # Profile and ratio changed: the ratio for the new profile only.
    await _edit({"name": "Pacandé", "ratio": 2.9, "profile": "standard"})
    assert _bean(entry, "b1")["ratios"] == {"standard": 2.9, "allonge": 5.0}


async def test_ratio_by_hand_after_a_profile_choice_mid_brew(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    """Profile chosen mid-brew, then the ratio set by hand: the hand ratio
    drives this brew and is stored for the profile the brew runs with; the
    newly chosen profile keeps its own ratio, which applies after the brew."""
    beans = migrate_bean_ratios(OLD)["beans"]
    beans[0] = {**beans[0], "profile": "standard", "ratio": 2.0,
                "ratios": {"standard": 2.0, "allonge": 3.0}}
    await _setup_beans(hass, entry, beans)
    autostop = entry.runtime_data.autostop
    await _select(hass, BEAN, "Pacandé")

    async def _both() -> None:
        # As the select and the number entity do, back to back.
        autostop.throttle.select_profile("allonge")
        autostop.set_ratio(2.1)

    last = await _shot_with(hass, entry, clock, _both)
    assert last["target"] == 37.8  # 2.1 x 18 g, by hand
    assert _bean(entry, "b1")["ratios"] == {"standard": 2.1, "allonge": 3.0}
    assert autostop.ratio == 3.0  # Allongé's own ratio after the brew


async def test_options_flow_ratio_mid_brew_waits_newest_wins(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    beans = migrate_bean_ratios(OLD)["beans"]
    beans[0] = {**beans[0], "profile": "standard", "ratio": 2.0,
                "ratios": {"standard": 2.0, "allonge": 3.0}}
    await _setup_beans(hass, entry, beans)
    autostop = entry.runtime_data.autostop
    await _select(hass, BEAN, "Pacandé")

    async def _choose_then_edit() -> None:
        autostop.throttle.select_profile("allonge")  # defers 3.0
        # Then the bean's Allongé ratio edited in the options: newest wins.
        new = with_bean_update(entry.options, "b1", ratio=3.4, ratio_profile="allonge")
        hass.config_entries.async_update_entry(entry, options=new)
        await hass.async_block_till_done()

    last = await _shot_with(hass, entry, clock, _choose_then_edit)
    assert last["target"] == 36.0  # the brew keeps 2.0 x 18 g
    assert autostop.ratio == 3.4


async def test_deferred_ratio_after_a_restart_follows_bean_and_profile(
    hass: HomeAssistant, entry, no_ble, lovelace, clock
) -> None:
    beans = migrate_bean_ratios(OLD)["beans"]
    beans[0] = {**beans[0], "profile": "allonge", "ratio": 3.0,
                "ratios": {"standard": 2.0, "allonge": 3.0}}
    await _setup_beans(hass, entry, beans)
    autostop = entry.runtime_data.autostop
    await _select(hass, BEAN, "Pacandé")
    # As if stopped mid-brew with Allongé's ratio waiting, the brew's 2.0
    # still the target.
    autostop.ratio = 2.0
    autostop._deferred_ratio = 3.0
    assert autostop.settings()["ratio_deferred"] is True
    autostop.async_schedule_save()
    await autostop.async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.autostop.ratio == 3.0
    assert "ratio_deferred" not in entry.runtime_data.autostop.settings()
