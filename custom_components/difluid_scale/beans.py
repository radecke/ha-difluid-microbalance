"""Bean profiles: a name, target ratios and an optional note per bean.

The profiles live in the config entry options (edited in the options flow);
which one is active is integration state (``AutoStop.bean_id``, stored with
the targets). The stop learning stays one shared model: the bean only
labels the brews.

0.7.0b6: a bean remembers its pump profile (``profile``: choosing a profile
while the bean is active makes it the bean's) and keeps one target ratio
per pump profile (``ratios``: {profile label: ratio}, e.g. Standard 1:2.7,
Allongé 1:5). Selecting a bean selects its profile, then its ratio for that
profile; choosing another profile loads the bean's ratio for it (missing:
the profile's own ratio target, else the bean's ratio, then remembered);
changing the target ratio writes it back for the active profile. ``ratio``
stays the ratio for the bean's own profile (Standard without one): older
versions read only that, and a bean without ``ratios`` (written by them)
is read as ``{its profile or Standard: ratio}``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import BEAN_NONE, CONF_BEANS
from .profiles import (
    OPERATOR_GTE,
    PROFILE_STANDARD,
    TARGET_RATIO,
    Profile,
    builtin_id,
    label_key,
)

BEAN_ID = "id"
BEAN_NAME = "name"
BEAN_RATIO = "ratio"
BEAN_NOTE = "note"
# Optional target shot time (s) of the pump throttle for this bean (0.6.0);
# missing = the global target shot time.
BEAN_SHOT_TIME = "shot_time"
SHOT_TIME_MIN = 10.0
SHOT_TIME_MAX = 90.0
# Optional default pump profile (label) of this bean (0.7.0): selecting the
# bean selects it (when it exists); missing = the active profile stays.
BEAN_PROFILE = "profile"
PROFILE_LABEL_MAX = 60
# Target ratio per pump profile (0.7.0b6): {profile label: ratio}.
BEAN_RATIOS = "ratios"
# The ``ratio`` as 0.7.0b6+ last wrote it. An older version (after a
# downgrade) changes ``ratio`` only: a ``ratio`` that differs from this
# marker was set there and wins for the bean's own profile.
BEAN_RATIO_WRITTEN = "ratio_written"

# Not allowed as a bean name: the select's "none" option and its labels,
# and the states a dashboard template yields for a missing helper.
RESERVED_NAMES = frozenset({BEAN_NONE, "none", "keine", "unknown", "unavailable"})
# Longer names do not fit a select state (255) and break the layout.
NAME_MAX = 60  # also hard-coded in options.error.bean_name_too_long (strings/translations)

# Name errors (translation keys, options flow and services).
ERR_NAME_EMPTY = "bean_name_empty"
ERR_NAME_TOO_LONG = "bean_name_too_long"
ERR_NAME_RESERVED = "bean_name_reserved"
ERR_NAME_TAKEN = "bean_name_taken"

RATIO_MIN = 0.5
RATIO_MAX = 5.0


def beans_from_options(options: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Valid bean profiles from the options (invalid entries skipped)."""
    beans: list[dict[str, Any]] = []
    for raw in options.get(CONF_BEANS) or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            bean = {
                BEAN_ID: str(raw[BEAN_ID]),
                BEAN_NAME: str(raw[BEAN_NAME]).strip(),
                BEAN_RATIO: float(raw[BEAN_RATIO]),
                BEAN_NOTE: raw.get(BEAN_NOTE) or None,
            }
        except (KeyError, TypeError, ValueError):
            continue
        shot_time = raw.get(BEAN_SHOT_TIME)
        if isinstance(shot_time, (int, float)) and not isinstance(shot_time, bool):
            if SHOT_TIME_MIN <= shot_time <= SHOT_TIME_MAX:
                bean[BEAN_SHOT_TIME] = float(shot_time)
        profile = raw.get(BEAN_PROFILE)
        if isinstance(profile, str) and 0 < len(profile.strip()) <= PROFILE_LABEL_MAX:
            bean[BEAN_PROFILE] = profile.strip()
        bean[BEAN_RATIOS] = _ratios(raw, bean.get(BEAN_PROFILE))
        _mirror(bean)
        if bean[BEAN_NAME]:
            beans.append(bean)
    return beans


def _ratio_ok(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and RATIO_MIN <= value <= RATIO_MAX
    )


def _ratios(raw: Mapping[str, Any], profile: str | None) -> dict[str, float]:
    """A stored bean's ratios per profile; the old format (``ratio``
    only) as the ratio of its profile (Standard without one)."""
    ratios: dict[str, float] = {}
    stored = raw.get(BEAN_RATIOS)
    if isinstance(stored, Mapping):
        for label, value in stored.items():
            if (
                isinstance(label, str)
                and 0 < len(label.strip()) <= PROFILE_LABEL_MAX
                and _ratio_ok(value)
            ):
                ratios[label.strip()] = round(float(value), 2)
    own = profile or PROFILE_STANDARD
    ratio = raw.get(BEAN_RATIO)
    if _ratio_ok(ratio):
        ratio = round(float(ratio), 2)
        written = raw.get(BEAN_RATIO_WRITTEN)
        if not ratios:
            ratios[own] = ratio  # the old format
        elif _ratio_ok(written) and round(float(written), 2) != ratio:
            # Changed by an older version after a downgrade (it keeps
            # ``ratios`` and the marker, changes only ``ratio``): it wins
            # for the bean's own profile.
            ratios = _set_ratio(ratios, own, ratio)
    return ratios


def _mirror(bean: dict[str, Any]) -> dict[str, Any]:
    """Set ``ratio`` to the bean's ratio for its own profile (Standard
    without one) where it has one, and the marker to it."""
    own = bean.get(BEAN_PROFILE) or PROFILE_STANDARD
    mirrored = bean_ratio_for(bean, own)
    if mirrored is not None:
        bean[BEAN_RATIO] = mirrored
    if _ratio_ok(bean.get(BEAN_RATIO)):
        bean[BEAN_RATIO_WRITTEN] = round(float(bean[BEAN_RATIO]), 2)
    return bean


def bean_ratio_for(bean: Mapping[str, Any], label: str) -> float | None:
    """The bean's own target ratio for the pump profile ``label`` (None:
    none remembered)."""
    ratios = bean.get(BEAN_RATIOS)
    if not isinstance(ratios, Mapping):
        ratios = _ratios(bean, bean.get(BEAN_PROFILE))
    key = label_key(label)
    return next(
        (float(v) for k, v in ratios.items() if label_key(k) == key and _ratio_ok(v)),
        None,
    )


def profile_ratio(profile: Profile | None) -> float | None:
    """A pump profile's own target ratio, if its JSON has one: a ``ratio``
    target at the top level, else the ``gte`` ratio exit of its last phase
    (where the auto-stop ends the shot); None otherwise."""
    if profile is None:
        return None
    raw = profile.raw if isinstance(profile.raw, Mapping) else {}
    for target in raw.get("targets") or []:
        if (
            isinstance(target, Mapping)
            and target.get("type") == TARGET_RATIO
            and target.get("operator", OPERATOR_GTE) == OPERATOR_GTE
            and _ratio_ok(target.get("value"))
        ):
            return round(float(target["value"]), 2)
    if profile.phases:
        for target in profile.phases[-1].targets:
            if (
                target.type == TARGET_RATIO
                and target.operator == OPERATOR_GTE
                and _ratio_ok(target.value)
            ):
                return round(float(target.value), 2)
    return None


def find_bean(
    beans: list[dict[str, Any]], bean_id: str | None
) -> dict[str, Any] | None:
    """The bean with ``bean_id`` (None if unknown)."""
    if bean_id is None:
        return None
    return next((b for b in beans if b[BEAN_ID] == bean_id), None)


def find_bean_by_name(
    beans: list[dict[str, Any]], name: str
) -> dict[str, Any] | None:
    """The bean called ``name`` (case-insensitive)."""
    key = name.strip().casefold()
    return next((b for b in beans if b[BEAN_NAME].casefold() == key), None)


def name_error(
    beans: list[dict[str, Any]], name: str | None, own_id: str | None = None
) -> str | None:
    """Why ``name`` cannot be a bean name (error key), or None.

    Empty (also None), longer than NAME_MAX, reserved (the select's "none"
    option, as translated "None"/"Keine", "unknown"/"unavailable"), or taken
    by another bean (case-insensitive).
    """
    name = (name or "").strip()
    if not name:
        return ERR_NAME_EMPTY
    if len(name) > NAME_MAX:
        return ERR_NAME_TOO_LONG
    if name.casefold() in RESERVED_NAMES:
        return ERR_NAME_RESERVED
    other = find_bean_by_name(beans, name)
    if other is not None and other[BEAN_ID] != own_id:
        return ERR_NAME_TAKEN
    return None


def make_bean(
    bean_id: str,
    name: str,
    ratio: float,
    note: str | None = None,
    shot_time: float | None = None,
    profile: str | None = None,
    ratios: Mapping[str, float] | None = None,
    write_ratio: bool = True,
) -> dict[str, Any]:
    """A bean profile as stored in the options (options flow, service).
    ``shot_time``: the throttle's target shot time for this bean,
    ``profile``: its pump profile label (both left out when None).
    ``ratios``: its ratios per profile so far (edit); ``ratio`` is set as
    the one for ``profile`` (Standard without one). ``write_ratio`` False:
    ``ratios`` stay as they are (the form's ratio was not touched while its
    profile changed); ``ratio`` is then the one the bean has for
    ``profile``, if any."""
    note = str(note or "").strip()
    bean: dict[str, Any] = {
        BEAN_ID: bean_id,
        BEAN_NAME: str(name).strip(),
        BEAN_RATIO: round(float(ratio), 2),
        BEAN_NOTE: note or None,
    }
    if shot_time is not None:
        bean[BEAN_SHOT_TIME] = round(float(shot_time), 1)
    if profile is not None and str(profile).strip():
        bean[BEAN_PROFILE] = str(profile).strip()
    own = bean.get(BEAN_PROFILE) or PROFILE_STANDARD
    if write_ratio:
        bean[BEAN_RATIOS] = _set_ratio(dict(ratios or {}), own, bean[BEAN_RATIO])
    else:
        bean[BEAN_RATIOS] = dict(ratios or {})
    return _mirror(bean)


def _set_ratio(ratios: dict[str, float], label: str, ratio: float) -> dict[str, float]:
    """``ratios`` with the one for ``label`` set (an existing spelling of
    the same label replaced)."""
    key = label_key(label)
    out = {k: v for k, v in ratios.items() if label_key(k) != key}
    out[label] = round(float(ratio), 2)
    return out


def with_bean_update(
    options: Mapping[str, Any],
    bean_id: str,
    *,
    profile: str | None = None,
    ratio: float | None = None,
    ratio_profile: str | None = None,
) -> dict[str, Any]:
    """New options with one bean changed: its pump profile (``profile``)
    and/or its ratio for ``ratio_profile`` (default: its own profile, or
    Standard). ``ratio`` mirrors the ratio for its own profile (older
    versions read only that)."""
    beans = []
    for raw in options.get(CONF_BEANS) or []:
        if not isinstance(raw, Mapping):
            continue
        bean = dict(raw)
        if bean.get(BEAN_ID) == bean_id:
            own = bean.get(BEAN_PROFILE)
            ratios = _ratios(bean, own if isinstance(own, str) else None)
            if profile is not None:
                bean[BEAN_PROFILE] = own = profile
            if ratio is not None:
                ratios = _set_ratio(
                    ratios, ratio_profile or own or PROFILE_STANDARD, ratio
                )
            bean[BEAN_RATIOS] = ratios
            _mirror(bean)
        beans.append(bean)
    return {**options, CONF_BEANS: beans}


def without_profile(options: Mapping[str, Any], label: str) -> dict[str, Any] | None:
    """New options with the pump profile ``label`` (deleted) removed from
    the beans that had it as their profile; their ``ratio`` then mirrors
    their Standard ratio. Their ratio for it is kept (should a profile of
    that label come back). None when no bean had it."""
    key = label_key(label)
    changed = False
    beans = []
    for raw in options.get(CONF_BEANS) or []:
        if (
            isinstance(raw, Mapping)
            and isinstance(raw.get(BEAN_PROFILE), str)
            and label_key(raw[BEAN_PROFILE]) == key
        ):
            bean = dict(raw)
            bean[BEAN_RATIOS] = _ratios(bean, bean[BEAN_PROFILE])
            del bean[BEAN_PROFILE]
            raw = _mirror(bean)
            changed = True
        beans.append(raw)
    return {**options, CONF_BEANS: beans} if changed else None


def with_bean_ratio(
    options: Mapping[str, Any],
    bean_id: str,
    ratio: float,
    profile: str | None = None,
) -> dict[str, Any]:
    """New options with one bean's ratio for ``profile`` (default: its own
    profile, or Standard) replaced."""
    return with_bean_update(options, bean_id, ratio=ratio, ratio_profile=profile)


def migrate_bean_ratios(options: Mapping[str, Any]) -> dict[str, Any]:
    """1.3 -> 1.4 (0.7.0b6): every bean's ``ratio`` becomes its ratio for
    its pump profile (Standard without one) in ``ratios``; ``ratio`` and
    everything else stay (older versions keep working)."""
    if CONF_BEANS not in options:
        return dict(options)
    beans = []
    for raw in options.get(CONF_BEANS) or []:
        if isinstance(raw, dict) and not isinstance(raw.get(BEAN_RATIOS), Mapping):
            own = raw.get(BEAN_PROFILE)
            raw = _mirror(
                {**raw, BEAN_RATIOS: _ratios(raw, own if isinstance(own, str) else None)}
            )
        beans.append(raw)
    return {**options, CONF_BEANS: beans}


def migrate_bean_profiles(options: Mapping[str, Any]) -> dict[str, Any]:
    """Options with every bean's default pump profile that names a built-in
    by its pre-0.7.0b5 German label (or another spelling) set to the id;
    other labels (imported profiles) unchanged."""
    beans = []
    for raw in options.get(CONF_BEANS) or []:
        if isinstance(raw, dict) and isinstance(raw.get(BEAN_PROFILE), str):
            pid = builtin_id(raw[BEAN_PROFILE])
            if pid is not None:
                raw = {**raw, BEAN_PROFILE: pid}
        beans.append(raw)
    if CONF_BEANS not in options:
        return dict(options)
    return {**options, CONF_BEANS: beans}
