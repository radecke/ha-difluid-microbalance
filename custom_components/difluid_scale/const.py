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

# Brew detection options (keys in config entry options) and defaults.
CONF_TAIL_SECONDS: Final = "tail_seconds"
CONF_MIN_DURATION: Final = "min_duration"
CONF_MIN_YIELD: Final = "min_yield"
CONF_DOSE_MIN: Final = "dose_min"
CONF_DOSE_MAX: Final = "dose_max"
CONF_STABILITY_TOLERANCE: Final = "stability_tolerance"
CONF_STABILITY_SECONDS: Final = "stability_seconds"
CONF_DOSE_LOOKBACK: Final = "dose_lookback_minutes"

DEFAULT_OPTIONS: Final[dict[str, float]] = {
    CONF_TAIL_SECONDS: 3.0,
    CONF_MIN_DURATION: 5.0,
    CONF_MIN_YIELD: 5.0,
    CONF_DOSE_MIN: 12.0,
    CONF_DOSE_MAX: 25.0,
    CONF_STABILITY_TOLERANCE: 0.3,
    CONF_STABILITY_SECONDS: 3.0,
    CONF_DOSE_LOOKBACK: 30.0,
}

EVENT_BREW_COMPLETED: Final = f"{DOMAIN}_brew_completed"
SIGNAL_BREWS_UPDATED: Final = f"{DOMAIN}_brews_updated_{{}}"  # .format(entry_id)
SIGNAL_LIVE: Final = f"{DOMAIN}_live_{{}}"  # .format(entry_id)

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.brews.{{}}"  # .format(entry_id)
MAX_STORED_BREWS: Final = 20

# Brew tracker housekeeping while a brew is active (stall check, tail end,
# parking backstop) and the live-data push cadence for the card.
BREW_TICK_INTERVAL: Final = timedelta(seconds=0.25)

CARD_FILENAME: Final = "difluid-brew-card.js"
CARD_URL: Final = f"/{DOMAIN}/{CARD_FILENAME}"
