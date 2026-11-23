"""Fixtures for the Home Assistant tests (pytest-homeassistant-custom-component).

``tests/test_brew.py`` needs none of this and also runs with plain python3.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Home Assistant test helpers: ``pytest-homeassistant-custom-component`` up to
# HA 2026.10, ``pytest-homeassistant`` from 2026.11. Install only ONE of them
# per venv (they register overlapping plugins).
try:  # plain ``python3 tests/test_brew.py`` never imports this file
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:  # pragma: no cover
    try:
        # Home Assistant 2026.11+: the helpers ship as ``pytest-homeassistant``
        # (same API); the tests import them under the old name.
        import pytest_homeassistant
        import pytest_homeassistant.common
    except ImportError:
        HAS_HA = False
    else:
        sys.modules["pytest_homeassistant_custom_component"] = pytest_homeassistant
        sys.modules["pytest_homeassistant_custom_component.common"] = (
            pytest_homeassistant.common
        )
        HAS_HA = True
else:
    HAS_HA = True



def pytest_configure(config: pytest.Config) -> None:
    """Fail loudly without a test package (the HA tests would only be skipped).

    ``python3 tests/test_brew.py`` and ``tests/test_stop_model.py`` run
    without Home Assistant and without pytest.
    """
    if not HAS_HA:
        raise pytest.UsageError(
            "No Home Assistant test package installed: install "
            "pytest-homeassistant-custom-component (HA <= 2026.10) or "
            "pytest-homeassistant (HA >= 2026.11), matching the HA version."
        )


ADDR = "AA:BB:CC:DD:EE:FF"


class Clock:
    """Replaces ``time`` in the coordinator."""

    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def time(self) -> float:
        return self.t


class SwitchClock:
    """Replaces ``time`` in the auto-stop (stop switch latency measurement).

    A fake plug advances ``t`` by its latency before it reports ``off``.
    """

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


if HAS_HA:
    from collections.abc import Generator
    from unittest.mock import patch

    from homeassistant.core import HomeAssistant
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.difluid_scale import coordinator as coord_mod
    from custom_components.difluid_scale.const import DOMAIN

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Load custom_components/ in every HA test."""
        yield

    from custom_components.difluid_scale import autostop as autostop_mod

    @pytest.fixture(autouse=True)
    def switch_clock() -> Generator[SwitchClock]:
        """Controlled monotonic clock for the switch latency measurement."""
        c = SwitchClock()
        with patch.object(autostop_mod, "time", c):
            yield c

    @pytest.fixture
    def clock() -> Generator[Clock]:
        """Controlled clock for the coordinator (epoch seconds)."""
        c = Clock()
        with patch.object(coord_mod, "time", c):
            yield c

    @pytest.fixture
    async def lovelace(hass: HomeAssistant) -> None:
        """Lovelace in storage mode (the card resource is registered)."""
        assert await async_setup_component(hass, "lovelace", {})

    @pytest.fixture
    def entry(hass: HomeAssistant) -> MockConfigEntry:
        """A config entry for one scale."""
        hass.config.components.add("bluetooth_adapters")
        hass.config.components.add("bluetooth")
        e = MockConfigEntry(
            domain=DOMAIN, data={"address": ADDR}, title="Microbalance", unique_id=ADDR
        )
        e.add_to_hass(hass)
        return e

    @pytest.fixture
    def no_ble() -> Generator[None]:
        """No Bluetooth: the tests drive the coordinator directly."""
        with (
            patch.object(
                coord_mod.DifluidCoordinator, "async_start", lambda self: (lambda: None)
            ),
            patch.object(
                coord_mod.DifluidCoordinator,
                "_clear_advertisement_history",
                lambda self: None,
            ),
        ):
            yield
