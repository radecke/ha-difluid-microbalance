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

STATUS_POLL_INTERVAL: Final = timedelta(minutes=5)

# Only connect when the scale advertised within this many seconds. HA keeps a
# switched-off scale in its history for minutes; connecting to it then blocks
# for a long time and hides the next real advertisement.
ADVERTISEMENT_MAX_AGE: Final = 10  # seconds

# Extra connect attempts after a failure while the scale is still in range.
CONNECT_RETRIES: Final = 3
CONNECT_RETRY_DELAY: Final = 1  # seconds
