"""Bean profiles: a name, a target ratio and an optional note per bean.

The profiles live in the config entry options (edited in the options flow);
which one is active is integration state (``AutoStop.bean_id``, stored with
the targets). Selecting a bean sets the target ratio to the bean's ratio;
changing the target ratio while a bean is active writes it back to the bean.
The stop learning stays one shared model: the bean only labels the brews.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import BEAN_NONE, CONF_BEANS

BEAN_ID = "id"
BEAN_NAME = "name"
BEAN_RATIO = "ratio"
BEAN_NOTE = "note"

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
        if bean[BEAN_NAME]:
            beans.append(bean)
    return beans


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
    bean_id: str, name: str, ratio: float, note: str | None = None
) -> dict[str, Any]:
    """A bean profile as stored in the options (options flow, service)."""
    note = str(note or "").strip()
    return {
        BEAN_ID: bean_id,
        BEAN_NAME: str(name).strip(),
        BEAN_RATIO: round(float(ratio), 2),
        BEAN_NOTE: note or None,
    }


def with_bean_ratio(
    options: Mapping[str, Any], bean_id: str, ratio: float
) -> dict[str, Any]:
    """New options with the ratio of one bean replaced."""
    beans = [
        {**b, BEAN_RATIO: round(ratio, 2)} if b.get(BEAN_ID) == bean_id else dict(b)
        for b in options.get(CONF_BEANS) or []
        if isinstance(b, Mapping)
    ]
    return {**options, CONF_BEANS: beans}
