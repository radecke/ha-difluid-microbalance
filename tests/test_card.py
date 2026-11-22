"""Runs the brew card's node-based tests (skipped without node)."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
HERE = Path(__file__).resolve().parent
CARD = HERE.parent / "custom_components/difluid_scale/www/difluid-brew-card.js"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_card_syntax() -> None:
    subprocess.run([NODE, "--check", str(CARD)], check=True)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_card_subscribe_retry() -> None:
    """Review 5: a failed subscribe/load is retried with backoff."""
    out = subprocess.run(
        [NODE, str(HERE / "card_retry_test.mjs")],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "4 scenarios OK" in out.stdout


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_card_flow_and_ready() -> None:
    """Flow from weight (monotone weight + trailing slope) and the ready view."""
    out = subprocess.run(
        [NODE, str(HERE / "card_flow_test.mjs")],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "11 scenarios OK" in out.stdout


@pytest.mark.skipif(NODE is None, reason="node not installed")
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
