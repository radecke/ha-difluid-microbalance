"""Runs the brew card's node-based tests.

Without node they are skipped locally, but fail in CI (env CI set) or with
DIFLUID_REQUIRE_NODE=1: a CI image without node must not pass silently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
HERE = Path(__file__).resolve().parent
COMPONENT = HERE.parent / "custom_components/difluid_scale"
CARD = COMPONENT / "www/difluid-brew-card.js"
NODE_REQUIRED = bool(os.environ.get("CI")) or os.environ.get("DIFLUID_REQUIRE_NODE") == "1"


@pytest.fixture
def _need_node() -> None:
    """Skip (or, where node is required, fail) a node-based test."""
    if NODE is not None:
        return
    if NODE_REQUIRED:
        pytest.fail("node is required (CI or DIFLUID_REQUIRE_NODE=1) but not installed")
    pytest.skip(
        "node not installed: brew card tests not run "
        "(set CI or DIFLUID_REQUIRE_NODE=1 to fail instead)"
    )


def test_card_version_matches_manifest() -> None:
    """The card logs and caches by its version: it is the integration's."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    match = re.search(r'^const CARD_VERSION = "([^"]+)";', CARD.read_text(encoding="utf-8"), re.M)
    assert match, "CARD_VERSION not found in the card"
    assert match.group(1) == manifest["version"]


@pytest.mark.usefixtures("_need_node")
def test_card_syntax() -> None:
    subprocess.run([NODE, "--check", str(CARD)], check=True)


@pytest.mark.usefixtures("_need_node")
def test_card_subscribe_retry() -> None:
    """Review 5: a failed subscribe/load is retried with backoff; a failed
    brew switch recovers (audit C1); a reconnect loads once (audit C5)."""
    out = subprocess.run(
        [NODE, str(HERE / "card_retry_test.mjs")],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "7 scenarios OK" in out.stdout


@pytest.mark.usefixtures("_need_node")
def test_card_flow_and_ready() -> None:
    """Flow from weight (monotone weight + trailing slope) and the ready view."""
    out = subprocess.run(
        [NODE, str(HERE / "card_flow_test.mjs")],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "13 scenarios OK" in out.stdout


@pytest.mark.usefixtures("_need_node")
def test_card_expand_when_active() -> None:
    """expand_when_active: active states, edit mode, mid-animation flips, clean-up."""
    out = subprocess.run(
        [NODE, str(HERE / "card_expand_test.mjs")],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "16 scenarios OK" in out.stdout


@pytest.mark.usefixtures("_need_node")
def test_card_pump_throttle() -> None:
    """0.6.0: pump power band, target time marker, grind finer, shot time;
    0.7.0: pump profile power and name, a profile skipped when not prepared
    (and the old "no_tare" of stored brews); legend fits a narrow card (C4);
    0.7.0b3: legend swatch follows the line style, pump power on hover, no
    band without a pump power entity; 0.7.0b6: the pump profile in the info
    line, live points replaced from replace_from."""
    out = subprocess.run(
        [NODE, str(HERE / "card_throttle_test.mjs")],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "18 scenarios OK" in out.stdout


@pytest.mark.usefixtures("_need_node")
def test_card_profile_preview() -> None:
    """0.7.0b3: profile preview tile feature (graph of the selected profile,
    target shot time buttons for the adaptive throttle profile)."""
    out = subprocess.run(
        [NODE, str(HERE / "card_profile_preview_test.mjs")],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "22 scenarios OK" in out.stdout


@pytest.mark.usefixtures("_need_node")
def test_card_swipe() -> None:
    """0.7.0b7: swipe between brews (direction, thresholds incl. end speed,
    readout, the buttons' locks also as the finger went down, end of the
    list, no click after a swipe, reduced motion, touch/pen only, second
    finger, wiring)."""
    out = subprocess.run(
        [NODE, str(HERE / "card_swipe_test.mjs")],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "10 scenarios OK" in out.stdout
