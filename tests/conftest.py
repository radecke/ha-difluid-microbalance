"""Fixtures for the Home Assistant tests (pytest-homeassistant-custom-component).

``tests/test_brew.py`` needs none of this and also runs with plain python3.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # plain ``python3 tests/test_brew.py`` never imports this file
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:  # pragma: no cover
    HAS_HA = False
else:
    HAS_HA = True

if HAS_HA:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Load custom_components/ in every HA test."""
        yield
