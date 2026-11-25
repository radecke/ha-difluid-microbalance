"""Pump profiles (0.7.0): a GaggiMate profile subset, built-ins, executor.

Pure maths and validation, no HA imports (like ``throttle_model``).

Format: a SUBSET of the GaggiMate profile JSON
(https://docs.gaggimate.eu/docs/profiles/, fields as parsed by
``src/display/models/profile.h``). Stage 1 has no pressure sensor: the only
control variable is the pump power (% of the pump entity's maximum, see
``PowerGrid``), the only feedback the scale (cup weight, scale flow g/s,
which lags the pump by ~1-1.5 s plus its 2 s flow window).

Profile: ``label`` (required, the name; unique, case-insensitive),
``type`` ("standard" | "pro", default "pro"), ``description``,
``temperature`` (accepted, ignored: no temperature control), ``phases``
(required, 1-PHASES_MAX). Phase: ``name``, ``phase`` ("preinfusion" |
"brew", default "brew"), ``valve`` (must be 1 or absent: the machine's
three-way valve is not controllable; 0 is rejected), ``duration``
(required, the phase's maximum in s), ``temperature`` (ignored), ``pump``
(required), ``transition`` ``{type: instant|linear|ease-in|ease-out|
ease-in-out, duration, target: "time"}``, ``targets`` ``[{type, operator:
gte|lte (default gte), value}]``.

``pump`` as a number = power in % (0-100). As an object ``{"target":
"flow", "flow": g/s}`` = closed loop on the SCALE flow (slow controller,
below). ``{"target": "pressure"}``, target types ``pressure`` and
``pumped`` and transitions on ``volumetric``/``pumped`` are rejected with a
clear error (they need a pressure sensor or a pumped-volume measurement;
reserved for stage 2). Target types: ``volumetric`` (cup weight, g, net as
the scale shows it), ``flow`` (scale flow, g/s), ``ratio`` (x the brew's
dose, converted to a weight when the dose is known, else that target is
ignored and reported; GaggiMate's parser has it too: ``TARGET_TYPE_RATIO``).
Weight and flow targets count only when met for TARGET_HOLD (1 s) on end:
pump vibration moves an empty cup by 0.1-1.0 g. Flow targets count only
after the first drops (DROP_GRAMS held DROP_HOLD, the first-drop rule of
the brew metrics): before them the scale flow is ~0. Unknown fields are
kept on export (round trip) but ignored. Custom extensions start with
``x_``; ``x_throttle`` is reserved for the built-in "Zielzeit" (the 0.6.0
throttle, decision 0052).

Execution (``ProfileRun``): t = shot time (s since the pump relay went on,
monotonic). A phase ends when its duration has elapsed OR the first of its
targets is met (targets that need the scale are skipped without scale
data). After the last phase the pump goes back to 100 %: profiles only
shape a shot, the shot is ended by the auto-stop (or by hand).

Power: a phase's power is clamped to [floor, 100] (``floor`` = the pump
throttle floor setting); only an explicit 0 % (a bloom: pump off, the
pressure stays in the puck behind the closed valve) goes below the floor.
A transition interpolates from the power commanded at the phase start to
the phase's power (``linear``/``ease-*`` over ``transition.duration``;
``instant``/0 s: at once). Values between 0 and the floor are raised to the
floor.

Flow phases: the start power is the learned pump model's prediction for
the target flow (``ThrottleModel.power_for(power, target / flow)``, only
once the model has learned at least one step and the scale shows a flow),
else the power commanded before (after a 0 % bloom: 100 %). The
transition runs to that start power; then the controller corrects at most
once per FLOW_WAIT (the scale's flow window must hold only frames from
after the last power change plus the pump/puck lag, the same principle as
the throttle, decision 0052), by a partial relative step through the
pump model, bounded by FLOW_STEP_MAX and clamped to [floor, 100]. Gains
are UNTUNED starting values, chosen in the simulation only
(``tests/throttle_sim.py``), never on a machine. Before the first drops
(DROP_GRAMS held DROP_HOLD, the first-drop rule) the start power is held.

``Profile.first_drop_deadline``: shot time (s) after which "no coffee" is
plausible, for "no coffee -> abort" automations (see the property).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any

from .brew import FIRST_DROP_GRAMS, FIRST_DROP_SUSTAIN
from .stop_model import FLOW_WINDOW
from .throttle_model import (
    POWER_FLOOR_DEFAULT,
    POWER_MAX,
    PowerGrid,
    ThrottleModel,
)

# ------------------------------------------------------------------ format

TYPE_STANDARD = "standard"
TYPE_PRO = "pro"
PROFILE_TYPES = (TYPE_STANDARD, TYPE_PRO)
PHASE_PREINFUSION = "preinfusion"
PHASE_BREW = "brew"
PHASE_KINDS = (PHASE_PREINFUSION, PHASE_BREW)
TRANSITION_INSTANT = "instant"
TRANSITION_LINEAR = "linear"
TRANSITION_EASE_IN = "ease-in"
TRANSITION_EASE_OUT = "ease-out"
TRANSITION_EASE_IN_OUT = "ease-in-out"
TRANSITIONS = (
    TRANSITION_INSTANT,
    TRANSITION_LINEAR,
    TRANSITION_EASE_IN,
    TRANSITION_EASE_OUT,
    TRANSITION_EASE_IN_OUT,
)
TARGET_VOLUMETRIC = "volumetric"
TARGET_FLOW = "flow"
TARGET_RATIO = "ratio"
TARGET_TYPES = (TARGET_VOLUMETRIC, TARGET_FLOW, TARGET_RATIO)
# Need a pressure sensor / a pumped-volume measurement: stage 2.
TARGET_TYPES_STAGE2 = ("pressure", "pumped")
OPERATOR_GTE = "gte"
OPERATOR_LTE = "lte"
OPERATORS = (OPERATOR_GTE, OPERATOR_LTE)
PUMP_TARGET_FLOW = "flow"

X_THROTTLE = "x_throttle"
THROTTLE_TARGET_TIME = "target_time"

LABEL_MAX = 60
PHASES_MAX = 20
TARGETS_MAX = 8
DURATION_MAX = 600.0  # s
FLOW_TARGET_MAX = 10.0  # g/s, no espresso pours more
WEIGHT_TARGET_MAX = 500.0  # g
RATIO_TARGET_MAX = 20.0
# Not allowed as a label: states a select or a template yields otherwise.
RESERVED_LABELS = frozenset({"", "none", "unknown", "unavailable"})

# Built-in profile ids (0.7.0b5; the select options, translated through
# the select's state translations; stored and exported as these).
PROFILE_STANDARD = "standard"
PROFILE_SOFT_PI = "soft_preinfusion"
PROFILE_BLOOMING = "blooming"
PROFILE_ALLONGE = "allonge"
PROFILE_LEVER = "lever"
PROFILE_TARGET_TIME = "target_time"

# Their German labels up to 0.7.0b4: still accepted wherever a profile is
# named (services, stored data, bean profiles), reserved for imports.
LEGACY_LABELS: dict[str, str] = {
    "Standard": PROFILE_STANDARD,
    "Sanfte Vorbrühung": PROFILE_SOFT_PI,
    "Blooming": PROFILE_BLOOMING,
    "Allongé": PROFILE_ALLONGE,
    "Hebel-Annäherung": PROFILE_LEVER,
    "Zielzeit": PROFILE_TARGET_TIME,
}

# Display names (en/de) of the built-in profiles; the same as the select's
# state translations in strings.json / translations (a test keeps them
# equal) and the brew card's table.
BUILTIN_NAMES: dict[str, dict[str, str]] = {
    PROFILE_STANDARD: {"en": "Standard", "de": "Standard"},
    PROFILE_SOFT_PI: {"en": "Soft preinfusion", "de": "Sanfte Vorbrühung"},
    PROFILE_BLOOMING: {"en": "Blooming", "de": "Blooming"},
    PROFILE_ALLONGE: {"en": "Allongé", "de": "Allongé"},
    PROFILE_LEVER: {"en": "Lever approximation", "de": "Hebel-Annäherung"},
    PROFILE_TARGET_TIME: {"en": "Target time", "de": "Zielzeit"},
}

# Phase names of the built-in profiles: stable keys in the definition (the
# phase name in records, live data and the ``running_phase`` attribute),
# shown translated.
PHASE_NAMES: dict[str, dict[str, str]] = {
    "brew": {"en": "Brew", "de": "Bezug"},
    "preinfusion": {"en": "Preinfusion", "de": "Vorbrühung"},
    "fill": {"en": "Fill", "de": "Füllen"},
    "bloom": {"en": "Bloom", "de": "Blooming"},
    "flow": {"en": "Flow", "de": "Fluss"},
    "peak": {"en": "Peak", "de": "Spitze"},
    "decline": {"en": "Decline", "de": "Abfall"},
}


def language_of(language: str | None) -> str:
    """"de" for German, else "en" (the languages the names exist in)."""
    return "de" if str(language or "").lower().startswith("de") else "en"


def builtin_id(label: str | None) -> str | None:
    """The built-in profile id for an id or a pre-0.7.0b5 German label
    (case-insensitive), else None."""
    if label is None:
        return None
    key = label_key(label)
    if key in BUILTIN_NAMES:
        return key
    return next((v for k, v in LEGACY_LABELS.items() if label_key(k) == key), None)


def reserved_label(label: str) -> bool:
    """Not allowed for an imported profile (case-insensitive): a built-in's
    id, its pre-0.7.0b5 German label or its name in a shipped language
    (en, de) — an import may not look like a built-in in the select."""
    key = label_key(label)
    if builtin_id(label) is not None:
        return True
    return any(label_key(n) == key for names in BUILTIN_NAMES.values() for n in names.values())


def display_name(label: str, language: str | None) -> str:
    """A profile's name in ``language``: built-ins translated (also by
    their old label), imported ones as they are."""
    pid = builtin_id(label)
    return BUILTIN_NAMES[pid][language_of(language)] if pid else label


def phase_display_name(profile: Profile, name: str, language: str | None) -> str:
    """A phase name in ``language``: built-in phase keys translated,
    imported names as they are."""
    if profile.builtin and name in PHASE_NAMES:
        return PHASE_NAMES[name][language_of(language)]
    return name

# -------------------------------------------------------- flow controller
#
# All UNTUNED (simulation only, see the module docstring).
#
# A power change reaches the cup after the pump's and the puck's lag (~1 to
# 1.5 s at this machine class, not measured on the target machine) and the
# scale's flow is a Theil-Sen slope over FLOW_WINDOW (2 s): only from
# FLOW_WINDOW + FLOW_LAG after a change does the flow window hold nothing
# from before it. A correction earlier would act on a flow that does not
# show the last change yet (the loop would overshoot).
FLOW_LAG = 1.5  # s
FLOW_WAIT = FLOW_WINDOW + FLOW_LAG
# Share of the relative error corrected per step (1 = all at once).
FLOW_GAIN = 0.7
# Largest change per correction (% points).
FLOW_STEP_MAX = 15.0
# The scale flow's noise (2 s Theil-Sen on 0.1 g steps): smaller errors are
# not corrected (a tolerance, not a delay).
FLOW_TOLERANCE = 0.1  # g/s
# Below this flow no relative correction is possible: raise by a step.
FLOW_MIN = 0.2  # g/s
# First drops (the brew metrics' rule): the cup weight at least DROP_GRAMS
# for DROP_HOLD s. Before, the flow controller holds and flow exits wait.
DROP_GRAMS = FIRST_DROP_GRAMS  # 1.5 g
DROP_HOLD = FIRST_DROP_SUSTAIN  # 1 s
# A weight or flow target counts when met on every update for this long:
# pump vibration moves an empty cup by 0.1-1.0 g (a noise filter, the same
# 1 s the first-drop rule holds; not a delay of the profile's own choice).
TARGET_HOLD = 1.0  # s
X_FIRST_DROP_DEADLINE = "x_first_drop_deadline"
# Added to the first drop deadline: scale delay (~0.5 s), the 1 s hold of
# the first-drop rule and BLE frame gaps.
DEADLINE_BUFFER = 3.0  # s

EXIT_DURATION = "duration"
EXIT_END = "end"


class ProfileError(ValueError):
    """An invalid profile; ``str(err)`` is the English reason."""


# ------------------------------------------------------------------ model


@dataclass(frozen=True)
class Target:
    """A phase exit condition."""

    type: str
    operator: str
    value: float

    def met(self, value: float) -> bool:
        """The measured ``value`` meets it."""
        if self.operator == OPERATOR_LTE:
            return value <= self.value
        return value >= self.value


@dataclass(frozen=True)
class Phase:
    """One phase: a power (``power``) or a flow target (``flow``)."""

    name: str
    kind: str
    duration: float
    power: float | None
    flow: float | None
    transition: str
    transition_duration: float
    targets: tuple[Target, ...]


@dataclass(frozen=True)
class Profile:
    """A validated profile; ``raw`` is the JSON it came from (round trip)."""

    label: str
    type: str
    description: str
    phases: tuple[Phase, ...]
    throttle: str | None = None
    builtin: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    deadline_override: float | None = None

    @property
    def has_bloom(self) -> bool:
        """A phase at an explicit 0 % (pump off)."""
        return any(p.power is not None and p.power <= 0 for p in self.phases)

    @property
    def first_drop_deadline(self) -> float | None:
        """Shot time (s) after which "no coffee" is plausible (module doc).

        The first phase with a weight exit (``gte``): its maximum end, plus
        the next phase's transition, plus any 0 % phases right after it
        (drops may come during a pause) and the transition out of them,
        plus DEADLINE_BUFFER (scale lag and the 1 s hold). None if no phase
        has a weight exit, or if up to it everything runs at 100 % and no
        pause follows: then the profile does not delay the first drops
        compared to a shot without profile (consumers keep their default).
        ``x_first_drop_deadline`` overrides it."""
        if self.deadline_override is not None:
            return self.deadline_override
        phases = self.phases
        i = next(
            (
                k
                for k, p in enumerate(phases)
                if any(
                    t.type in (TARGET_VOLUMETRIC, TARGET_RATIO) and t.operator == OPERATOR_GTE
                    for t in p.targets
                )
            ),
            None,
        )
        if i is None:
            return None
        start = sum(p.duration for p in phases[:i])
        end = start + phases[i].duration
        j = i + 1
        paused = False
        while j < len(phases) and phases[j].power is not None and phases[j].power <= 0:
            end += _transition_time(phases[j]) + phases[j].duration
            paused = True
            j += 1
        if j < len(phases):
            end += _transition_time(phases[j])
        full = all(p.power is not None and p.power >= POWER_MAX for p in phases[: i + 1])
        if full and not paused:
            return None
        return round(end + DEADLINE_BUFFER, 1)

    @property
    def key(self) -> str:
        """Case-insensitive identity."""
        return label_key(self.label)

    def to_json(self) -> dict[str, Any]:
        """The profile as GaggiMate-style JSON (unknown fields kept)."""
        return copy.deepcopy(self.raw)

    @property
    def revision(self) -> str:
        """Short hash of the definition: changes when a profile of the same
        label is imported over it (the profile preview refetches on it)."""
        text = json.dumps(self.raw, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _transition_time(phase: Phase) -> float:
    if phase.transition == TRANSITION_INSTANT:
        return 0.0
    return phase.transition_duration


def label_key(label: str) -> str:
    """The case-insensitive identity of a label."""
    return str(label).strip().casefold()


def _num(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{what} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ProfileError(f"{what} must be a finite number")
    return value


def _parse_target(raw: Any, where: str) -> Target:
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: a target must be an object")
    kind = raw.get("type")
    if kind in TARGET_TYPES_STAGE2:
        raise ProfileError(
            f"{where}: target type '{kind}' needs a pressure sensor "
            "(reserved for stage 2)"
        )
    if kind not in TARGET_TYPES:
        raise ProfileError(
            f"{where}: unknown target type {kind!r} "
            f"(supported: {', '.join(TARGET_TYPES)})"
        )
    operator = raw.get("operator", OPERATOR_GTE)
    if operator not in OPERATORS:
        raise ProfileError(f"{where}: operator must be 'gte' or 'lte'")
    value = _num(raw.get("value"), f"{where}: target value")
    high = {
        TARGET_VOLUMETRIC: WEIGHT_TARGET_MAX,
        TARGET_FLOW: FLOW_TARGET_MAX,
        TARGET_RATIO: RATIO_TARGET_MAX,
    }[kind]
    if not 0.0 <= value <= high:
        raise ProfileError(f"{where}: {kind} target must be 0-{high:g}")
    return Target(kind, operator, value)


def _parse_phase(raw: Any, index: int) -> Phase:
    where = f"phase {index + 1}"
    if not isinstance(raw, dict):
        raise ProfileError(f"{where} must be an object")
    name = raw.get("name", f"Phase {index + 1}")
    if not isinstance(name, str):
        raise ProfileError(f"{where}: name must be a string")
    name = name.strip() or f"Phase {index + 1}"
    where = f"phase {index + 1} ({name})"
    kind = raw.get("phase", PHASE_BREW)
    if kind not in PHASE_KINDS:
        raise ProfileError(f"{where}: phase must be 'preinfusion' or 'brew'")
    if "valve" in raw:
        valve = raw["valve"]
        if valve in (0, False):
            raise ProfileError(
                f"{where}: valve 0 is not supported: the machine's valve is "
                "not controllable, only its normal brewing position (1)"
            )
        if valve not in (1, True):
            raise ProfileError(f"{where}: valve must be 1")
    duration = _num(raw.get("duration"), f"{where}: duration")
    if not 0.0 < duration <= DURATION_MAX:
        raise ProfileError(f"{where}: duration must be > 0 and <= {DURATION_MAX:g} s")
    pump = raw.get("pump")
    power: float | None = None
    flow: float | None = None
    if isinstance(pump, dict):
        target = pump.get("target", PUMP_TARGET_FLOW if "flow" in pump else None)
        if target == "pressure":
            raise ProfileError(
                f"{where}: pump target 'pressure' needs a pressure sensor "
                "(reserved for stage 2)"
            )
        if target != PUMP_TARGET_FLOW:
            raise ProfileError(f"{where}: pump target must be 'flow'")
        flow = _num(pump.get("flow"), f"{where}: pump flow")
        if not 0.0 < flow <= FLOW_TARGET_MAX:
            raise ProfileError(f"{where}: pump flow must be > 0 and <= {FLOW_TARGET_MAX:g} g/s")
    elif pump is None:
        raise ProfileError(f"{where}: pump is required (power % or a flow target)")
    else:
        power = _num(pump, f"{where}: pump")
        if not 0.0 <= power <= POWER_MAX:
            raise ProfileError(f"{where}: pump power must be 0-100 %")
    transition = TRANSITION_INSTANT
    transition_duration = 0.0
    trans = raw.get("transition")
    if trans is not None:
        if not isinstance(trans, dict):
            raise ProfileError(f"{where}: transition must be an object")
        transition = trans.get("type", TRANSITION_INSTANT)
        if transition not in TRANSITIONS:
            raise ProfileError(
                f"{where}: transition type must be one of {', '.join(TRANSITIONS)}"
            )
        on = trans.get("target", "time")
        if on in ("volumetric", "pumped"):
            raise ProfileError(
                f"{where}: transitions over '{on}' are not supported "
                "(time only; pumped volume needs stage 2)"
            )
        if on != "time":
            raise ProfileError(f"{where}: transition target must be 'time'")
        transition_duration = _num(
            trans.get("duration", 0.0), f"{where}: transition duration"
        )
        if not 0.0 <= transition_duration <= DURATION_MAX:
            raise ProfileError(f"{where}: transition duration must be 0-{DURATION_MAX:g} s")
    raw_targets = raw.get("targets", [])
    if raw_targets is None:
        raw_targets = []
    if not isinstance(raw_targets, list):
        raise ProfileError(f"{where}: targets must be a list")
    if len(raw_targets) > TARGETS_MAX:
        raise ProfileError(f"{where}: at most {TARGETS_MAX} targets")
    targets = tuple(_parse_target(t, where) for t in raw_targets)
    return Phase(
        name=name,
        kind=kind,
        duration=duration,
        power=power,
        flow=flow,
        transition=transition,
        transition_duration=transition_duration,
        targets=targets,
    )


def parse_profile(data: Any, *, builtin: bool = False) -> Profile:
    """Validate GaggiMate-subset JSON (an object or its JSON text).

    Raises ``ProfileError`` with an English reason. ``x_throttle`` is only
    allowed for built-ins (the Zielzeit profile)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as err:
            raise ProfileError(f"not valid JSON: {err}") from None
    if not isinstance(data, dict):
        raise ProfileError("a profile must be a JSON object")
    label = data.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ProfileError("label is required")
    label = label.strip()
    if len(label) > LABEL_MAX:
        raise ProfileError(f"label must be at most {LABEL_MAX} characters")
    if label_key(label) in RESERVED_LABELS:
        raise ProfileError(f"label '{label}' is reserved")
    kind = data.get("type", TYPE_PRO)
    if kind not in PROFILE_TYPES:
        raise ProfileError("type must be 'standard' or 'pro'")
    description = data.get("description") or ""
    if not isinstance(description, str):
        raise ProfileError("description must be a string")
    throttle = data.get(X_THROTTLE)
    if throttle is not None:
        if not builtin:
            raise ProfileError(f"{X_THROTTLE} is reserved for the built-in profile")
        if throttle != THROTTLE_TARGET_TIME:
            raise ProfileError(f"{X_THROTTLE} must be '{THROTTLE_TARGET_TIME}'")
    deadline = data.get(X_FIRST_DROP_DEADLINE)
    if deadline is not None:
        deadline = _num(deadline, X_FIRST_DROP_DEADLINE)
        if not 0.0 < deadline <= DURATION_MAX:
            raise ProfileError(f"{X_FIRST_DROP_DEADLINE} must be > 0 and <= {DURATION_MAX:g} s")
    phases = data.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ProfileError("phases must be a non-empty list")
    if len(phases) > PHASES_MAX:
        raise ProfileError(f"at most {PHASES_MAX} phases")
    parsed = tuple(_parse_phase(p, i) for i, p in enumerate(phases))
    return Profile(
        label=label,
        type=kind,
        description=description,
        phases=parsed,
        throttle=throttle,
        builtin=builtin,
        raw=copy.deepcopy(data),
        deadline_override=deadline,
    )


# --------------------------------------------------------------- built-ins
#
# UNTUNED starting values. No measurement on the target machine exists yet
# (the pump's stall threshold is unknown): they are derived from published
# Decent/Rao profiles and never go below the floor except an explicit 0 %
# bloom. Last phases run 120 s: longer than any espresso, the auto-stop ends
# the shot.

LAST_PHASE = 120.0

_BUILTIN_JSON: list[dict[str, Any]] = [
    {
        "label": PROFILE_STANDARD,
        "type": TYPE_STANDARD,
        "description": (
            "Constant pump power for the whole shot: the 'Standard pump "
            "power' setting (default 100 %, as without profiles); the "
            "setting is not part of this definition."
        ),
        "phases": [
            {"name": "brew", "phase": PHASE_BREW, "valve": 1,
             "duration": LAST_PHASE, "pump": 100},
        ],
    },
    {
        "label": PROFILE_SOFT_PI,
        "type": TYPE_PRO,
        "description": (
            "Gentle preinfusion at 40 % (at least the floor) until 2 g in the "
            "cup or 15 s, then a 4 s linear ramp to 100 %. Untuned starting "
            "values."
        ),
        "phases": [
            {"name": "preinfusion", "phase": PHASE_PREINFUSION, "valve": 1,
             "duration": 15, "pump": 40,
             "targets": [{"type": TARGET_VOLUMETRIC, "operator": OPERATOR_GTE, "value": 2}]},
            {"name": "brew", "phase": PHASE_BREW, "valve": 1,
             "duration": LAST_PHASE, "pump": 100,
             "transition": {"type": TRANSITION_LINEAR, "duration": 4}},
        ],
    },
    {
        "label": PROFILE_BLOOMING,
        "type": TYPE_PRO,
        "description": (
            "Blooming espresso after Rao/Decent: fill at 100 % until the "
            "first drops (1.5 g) or 12 s, pump off for 30 s, 5 s ramp into a "
            "flow target of 2.2 g/s. Untuned starting values."
        ),
        "phases": [
            {"name": "fill", "phase": PHASE_PREINFUSION, "valve": 1,
             "duration": 12, "pump": 100,
             "targets": [{"type": TARGET_VOLUMETRIC, "operator": OPERATOR_GTE, "value": 1.5}]},
            {"name": "bloom", "phase": PHASE_PREINFUSION, "valve": 1,
             "duration": 30, "pump": 0},
            {"name": "flow", "phase": PHASE_BREW, "valve": 1,
             "duration": LAST_PHASE,
             "pump": {"target": PUMP_TARGET_FLOW, "flow": 2.2},
             "transition": {"type": TRANSITION_LINEAR, "duration": 5}},
        ],
    },
    {
        "label": PROFILE_ALLONGE,
        "type": TYPE_PRO,
        "description": (
            "Allongé after Rao: 100 % until the first drops (1.5 g) or 12 s, "
            "then a flow target of 4.5 g/s; coarse grind, about 1:5. Untuned "
            "starting values."
        ),
        "phases": [
            {"name": "fill", "phase": PHASE_PREINFUSION, "valve": 1,
             "duration": 12, "pump": 100,
             "targets": [{"type": TARGET_VOLUMETRIC, "operator": OPERATOR_GTE, "value": 1.5}]},
            {"name": "flow", "phase": PHASE_BREW, "valve": 1,
             "duration": LAST_PHASE,
             "pump": {"target": PUMP_TARGET_FLOW, "flow": 4.5}},
        ],
    },
    {
        "label": PROFILE_LEVER,
        "type": TYPE_PRO,
        "description": (
            "Lever approximation: 100 % until 4 g in the cup (at most 20 s), "
            "hold 100 % for 8 s, then a linear decline to 60 % (at least the "
            "floor) over 20 s and hold. Untuned starting values."
        ),
        "phases": [
            {"name": "fill", "phase": PHASE_PREINFUSION, "valve": 1,
             "duration": 20, "pump": 100,
             "targets": [{"type": TARGET_VOLUMETRIC, "operator": OPERATOR_GTE, "value": 4}]},
            {"name": "peak", "phase": PHASE_BREW, "valve": 1,
             "duration": 8, "pump": 100},
            {"name": "decline", "phase": PHASE_BREW, "valve": 1,
             "duration": LAST_PHASE, "pump": 60,
             "transition": {"type": TRANSITION_LINEAR, "duration": 20}},
        ],
    },
    {
        "label": PROFILE_TARGET_TIME,
        "type": TYPE_STANDARD,
        "description": (
            "Full power; a shot predicted too fast is slowed down to the "
            "target shot time (the pump throttle of 0.6.0)."
        ),
        X_THROTTLE: THROTTLE_TARGET_TIME,
        "phases": [
            {"name": "brew", "phase": PHASE_BREW, "valve": 1,
             "duration": LAST_PHASE, "pump": 100},
        ],
    },
]

BUILTINS: tuple[Profile, ...] = tuple(
    parse_profile(p, builtin=True) for p in _BUILTIN_JSON
)
BUILTIN_LABELS = tuple(p.label for p in BUILTINS)


def builtin(label: str) -> Profile | None:
    """The built-in profile with this id or pre-0.7.0b5 label
    (case-insensitive)."""
    pid = builtin_id(label)
    return next((p for p in BUILTINS if p.key == pid), None) if pid else None


# ---------------------------------------------------------------- library

ERR_BUILTIN = "profile_builtin"
ERR_EXISTS = "profile_exists"
ERR_UNKNOWN = "profile_unknown"


class LibraryError(ValueError):
    """A library operation failed; ``key`` is the translation key."""

    def __init__(self, key: str, label: str) -> None:
        super().__init__(f"{key}: {label}")
        self.key = key
        self.label = label


class ProfileLibrary:
    """Built-ins plus user profiles and the active one (by label)."""

    def __init__(self, active: str = PROFILE_STANDARD) -> None:
        self.user: list[Profile] = []
        self.active_label = active

    @property
    def profiles(self) -> list[Profile]:
        """All profiles: built-ins first, then user profiles in import order."""
        return [*BUILTINS, *self.user]

    @property
    def labels(self) -> list[str]:
        return [p.label for p in self.profiles]

    def get(self, label: str | None) -> Profile | None:
        """By label, case-insensitive; a built-in also by its pre-0.7.0b5
        German label."""
        if label is None:
            return None
        found = builtin(label)
        if found is not None:
            return found
        key = label_key(label)
        return next((p for p in self.user if p.key == key), None)

    @property
    def active(self) -> Profile:
        """The active profile (Standard if the stored one is gone)."""
        return self.get(self.active_label) or BUILTINS[0]

    def select(self, label: str) -> Profile:
        profile = self.get(label)
        if profile is None:
            raise LibraryError(ERR_UNKNOWN, label)
        self.active_label = profile.label
        return profile

    def add(self, profile: Profile, overwrite: bool = False) -> bool:
        """Add a user profile; True if it replaced one of the same label."""
        if reserved_label(profile.label):
            raise LibraryError(ERR_BUILTIN, profile.label)
        index = next(
            (i for i, p in enumerate(self.user) if p.key == profile.key), None
        )
        if index is not None:
            if not overwrite:
                raise LibraryError(ERR_EXISTS, profile.label)
            was_active = label_key(self.active_label) == profile.key
            self.user[index] = profile
            if was_active:
                self.active_label = profile.label
            return True
        self.user.append(profile)
        return False

    def remove(self, label: str) -> Profile:
        """Delete a user profile; the active one falls back to Standard."""
        if builtin(label) is not None:
            raise LibraryError(ERR_BUILTIN, label)
        key = label_key(label)
        profile = next((p for p in self.user if p.key == key), None)
        if profile is None:
            raise LibraryError(ERR_UNKNOWN, label)
        self.user = [p for p in self.user if p.key != key]
        if label_key(self.active_label) == key:
            self.active_label = PROFILE_STANDARD
        return profile

    def to_store(self) -> dict[str, Any]:
        return {
            "active": self.active.label,
            "user": [p.to_json() for p in self.user],
        }

    def load(self, data: Any, default_active: str = PROFILE_STANDARD) -> int:
        """Restore; returns the number of stored profiles that are invalid
        now (skipped)."""
        self.user = []
        skipped = 0
        if not isinstance(data, dict):
            self.active_label = default_active
            return 0
        for raw in data.get("user") or []:
            try:
                profile = parse_profile(raw)
                self.add(profile)
            except (ProfileError, LibraryError):
                skipped += 1
        active = data.get("active")
        self.active_label = (
            self.get(active).label
            if isinstance(active, str) and self.get(active) is not None
            else default_active
        )
        return skipped


# --------------------------------------------------------------- executor


def ease(kind: str, x: float) -> float:
    """Transition curve 0..1 -> 0..1."""
    x = min(1.0, max(0.0, x))
    if kind == TRANSITION_EASE_IN:
        return x * x
    if kind == TRANSITION_EASE_OUT:
        return 1.0 - (1.0 - x) ** 2
    if kind == TRANSITION_EASE_IN_OUT:
        return 3 * x * x - 2 * x * x * x
    if kind == TRANSITION_LINEAR:
        return x
    return 1.0


class ProfileRun:
    """One shot of a profile (shot time t from the relay start).

    ``update`` with every scale frame and on a clock tick without one;
    returns the new power (% of the pump's maximum, on its grid) when the
    command changes, else None. ``weight``/``flow`` None = no scale data
    (targets on them are skipped, flow phases hold their power).
    ``power``: the power in effect (apply: the last command; observe: the
    pump's real power, 100 %): the flow controller's relative corrections
    start from it."""

    TRACE_MAX = 400

    def __init__(
        self,
        profile: Profile,
        *,
        floor: float = POWER_FLOOR_DEFAULT,
        grid: PowerGrid | None = None,
        model: ThrottleModel | None = None,
        dose: float | None = None,
        start_power: float = POWER_MAX,
    ) -> None:
        self.profile = profile
        self.grid = grid if grid is not None else PowerGrid(0.0, 100.0, 1.0)
        self.floor_setting = floor
        self.floor = self.grid.percent(self.grid.floor_value(floor))
        self.model = model
        self.dose = dose
        self.start_power = start_power
        self.index = -1
        self.phase_start = 0.0
        self.from_power = start_power
        self.flow_start: float | None = None  # flow phase: start power
        self.command: float | None = None
        self.last_change_t: float | None = None
        self.done = False
        # [t, %] on change, [t, index, name, reason] per phase start.
        self.trace: list[list[float]] = []
        self.phases: list[list[Any]] = []
        # Ratio targets ignored (no dose): (phase index, value).
        self.ignored: list[tuple[int, float]] = []
        self.no_scale_skips = 0
        self._flow_reached = False
        # First drops (DROP_GRAMS held DROP_HOLD) and targets met since.
        self.drop_t: float | None = None
        self._drop_since: float | None = None
        self._met_since: dict[int, float] = {}

    @property
    def phase(self) -> Phase | None:
        if self.done or self.index < 0:
            return None
        return self.profile.phases[self.index]

    @staticmethod
    def _target_value(
        target: Target, weight: float | None, flow: float | None
    ) -> float | None:
        if target.type == TARGET_FLOW:
            return flow
        return weight

    def _limit(self, target: Target) -> float | None:
        if target.type != TARGET_RATIO:
            return target.value
        if self.dose is None or self.dose <= 0:
            if (self.index, target.value) not in self.ignored:
                self.ignored.append((self.index, target.value))
            return None
        return target.value * self.dose

    def _exit(self, t: float, weight: float | None, flow: float | None) -> str | None:
        phase = self.phase
        assert phase is not None
        for i, target in enumerate(phase.targets):
            limit = self._limit(target)
            if limit is None:
                self._met_since.pop(i, None)
                continue
            value = self._target_value(target, weight, flow)
            if value is None:
                self.no_scale_skips += 1
                self._met_since.pop(i, None)
                continue
            if target.type == TARGET_FLOW and self.drop_t is None:
                # The scale flow is ~0 until the first drops: a flow exit
                # (above all ``lte``) would fire at once after the tare.
                continue
            if not Target(target.type, target.operator, limit).met(value):
                self._met_since.pop(i, None)
                continue
            since = self._met_since.setdefault(i, t)
            if t - since >= TARGET_HOLD - 1e-9:
                return target.type
        if t - self.phase_start >= phase.duration - 1e-9:
            return EXIT_DURATION
        return None

    def _enter(
        self, index: int, t: float, reason: str | None, flow: float | None, power: float | None
    ) -> None:
        self.from_power = self.command if self.command is not None else self.start_power
        if index >= len(self.profile.phases):
            self.done = True
            self.index = len(self.profile.phases)
            self.phases.append([round(t, 2), self.index, None, reason or EXIT_END])
            return
        self.index = index
        self.phase_start = t
        phase = self.profile.phases[index]
        self.phases.append([round(t, 2), index, phase.name, reason])
        self.flow_start = None
        self._flow_reached = False
        self._met_since = {}
        if phase.flow is not None:
            self.flow_start = self._flow_start_power(phase.flow, flow, power)

    def _flow_start_power(
        self, target: float, flow: float | None, power: float | None
    ) -> float:
        base = power if power is not None else self.from_power
        if (
            self.model is not None
            and self.model.count > 0
            and flow is not None
            and flow > FLOW_MIN
            and base > 0
        ):
            return self._clamp(self.model.power_for(base, target / flow))
        return self.from_power if self.from_power > 0 else POWER_MAX

    def _clamp(self, value: float) -> float:
        return min(POWER_MAX, max(self.floor, value))

    def _transition(self, phase: Phase, t: float, goal: float) -> tuple[float, bool]:
        """Power on the transition to ``goal`` and whether it is complete."""
        if phase.transition == TRANSITION_INSTANT or phase.transition_duration <= 0:
            return goal, True
        x = (t - self.phase_start) / phase.transition_duration
        if x >= 1.0:
            return goal, True
        return self.from_power + (goal - self.from_power) * ease(phase.transition, x), False

    def _flow_control(
        self, phase: Phase, t: float, weight: float | None, flow: float | None, power: float | None
    ) -> float:
        current = self.command if self.command is not None else self.from_power
        if flow is None or weight is None or self.drop_t is None:
            return current  # no scale data or no drops yet: hold
        if self.last_change_t is not None and t - self.last_change_t < FLOW_WAIT:
            return current  # the flow does not show the last change yet
        assert phase.flow is not None
        error = phase.flow - flow
        if abs(error) <= FLOW_TOLERANCE:
            return current
        base = power if power is not None else current
        if flow < FLOW_MIN:
            wanted = base + FLOW_STEP_MAX
        else:
            ratio = 1.0 + FLOW_GAIN * (phase.flow / flow - 1.0)
            model = self.model or ThrottleModel()
            wanted = model.power_for(base, ratio)
        wanted = min(current + FLOW_STEP_MAX, max(current - FLOW_STEP_MAX, wanted))
        return self._clamp(wanted)

    def _want(
        self, t: float, weight: float | None, flow: float | None, power: float | None
    ) -> float:
        phase = self.phase
        if phase is None:
            return POWER_MAX
        if phase.flow is not None:
            assert self.flow_start is not None
            value, complete = self._transition(phase, t, self.flow_start)
            if not complete:
                return value
            if not self._flow_reached:
                # The transition's end: the start power first, then the
                # controller (which waits FLOW_WAIT after this change).
                self._flow_reached = True
                return self.flow_start
            return self._flow_control(phase, t, weight, flow, power)
        assert phase.power is not None
        goal = 0.0 if phase.power <= 0 else self._clamp(phase.power)
        return self._transition(phase, t, goal)[0]

    def quantize(self, percent: float) -> float:
        """% -> the % of the entity value that is set: 0 (or the entity's
        minimum) only for <= 0, else at least the floor."""
        if percent <= 0:
            return self.grid.percent(self.grid.value(0.0))
        return self.grid.quantize(min(POWER_MAX, percent), self.floor_setting)

    def update(
        self,
        t: float,
        weight: float | None = None,
        flow: float | None = None,
        power: float | None = None,
    ) -> float | None:
        """Advance to shot time ``t``; the new power or None (unchanged)."""
        if weight is not None and self.drop_t is None:
            if weight >= DROP_GRAMS:
                self._drop_since = t if self._drop_since is None else self._drop_since
                if t - self._drop_since >= DROP_HOLD - 1e-9:
                    self.drop_t = self._drop_since
            else:
                self._drop_since = None
        if self.index < 0:
            self._enter(0, t, None, flow, power)
        guard = len(self.profile.phases) + 1
        while not self.done and guard > 0:
            guard -= 1
            reason = self._exit(t, weight, flow)
            if reason is None:
                break
            self._enter(self.index + 1, t, reason, flow, power)
        new = self.quantize(self._want(t, weight, flow, power))
        if self.command is not None and abs(new - self.command) < 1e-6:
            return None
        self.command = new
        self.last_change_t = t
        self._trace(t, new)
        return new

    def _trace(self, t: float, value: float) -> None:
        self.trace.append([round(t, 2), round(value, 2)])
        if len(self.trace) > self.TRACE_MAX:
            # Thin: drop every second point of the older half, keep the ends.
            half = len(self.trace) // 2
            self.trace = self.trace[:half:2] + self.trace[half:]

    def finish(self, t: float | None) -> None:
        """The shot ended (relay off, cut, unload)."""
        if not self.done and t is not None:
            self.phases.append([round(t, 2), self.index, None, EXIT_END])
        self.done = True


def migrate_profile_ids(throttle: dict[str, Any]) -> dict[str, Any]:
    """The stop store's throttle part from before 0.7.0b5 with built-in
    profile ids (StopStore 1.3 -> 1.4): the active profile by its id; an
    imported profile whose label is now reserved (a built-in id or English
    name, ``reserved_label``) renamed to "<label> (imported)", with a
    number when that name exists too ("<label> (imported 2)", ...); the
    active reference follows."""
    data = dict(throttle)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        return data
    profiles = dict(profiles)
    active = profiles.get("active")
    raws = list(profiles.get("user") or [])
    taken = {
        label_key(r["label"])
        for r in raws
        if isinstance(r, dict) and isinstance(r.get("label"), str)
    }
    user = []
    active_renamed = False
    for raw in raws:
        if isinstance(raw, dict) and isinstance(raw.get("label"), str):
            label = raw["label"].strip()
            if reserved_label(label):
                n = 1
                new = f"{label} (imported)"
                while label_key(new) in taken or reserved_label(new):
                    n += 1
                    new = f"{label} (imported {n})"
                taken.add(label_key(new))
                if isinstance(active, str) and not active_renamed and label_key(active) == label_key(label):
                    active = new
                    active_renamed = True
                raw = {**raw, "label": new}
        user.append(raw)
    if isinstance(active, str) and not active_renamed:
        pid = builtin_id(active)
        if pid is not None:
            active = pid
    profiles["active"] = active
    profiles["user"] = user
    data["profiles"] = profiles
    return data
