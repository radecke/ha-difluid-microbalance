"""Constants for the DiFluid Microbalance integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "difluid_scale"

MANUFACTURER: Final = "DiFluid"
MODEL: Final = "Microbalance"

SERVICE_UUID: Final = "000000ee-0000-1000-8000-00805f9b34fb"
# Cleartext channel (notify/write/read). 0000ff01 is the encrypted channel and
# does not answer cleartext commands, so it is deliberately not used.
CHAR_UUID: Final = "0000aa01-0000-1000-8000-00805f9b34fb"

LOCAL_NAME_PREFIX: Final = "Microbalance"

STATUS_POLL_INTERVAL: Final = timedelta(seconds=60)

# The scale does not power itself off while a BLE connection is held. After
# the idle timeout without weight change (timer not running) the connection is
# dropped and reconnects are held back for the cooldown, so the scale's own
# auto-off can take effect. Both are options of the config entry.
CONF_IDLE_TIMEOUT: Final = "idle_timeout"
CONF_RECONNECT_COOLDOWN: Final = "reconnect_cooldown"
DEFAULT_IDLE_TIMEOUT: Final = 120  # seconds, 0 = never disconnect
DEFAULT_RECONNECT_COOLDOWN: Final = 60  # seconds
IDLE_CHECK_INTERVAL: Final = timedelta(seconds=5)
# Weight changes below this (grams) are measurement noise, not activity.
ACTIVITY_THRESHOLD_G: Final = 0.5

# Extra connect attempts after a failure while the scale is still in range.
CONNECT_RETRIES: Final = 3
CONNECT_RETRY_DELAY: Final = 3  # seconds
