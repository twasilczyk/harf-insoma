"""Constants for the Insoma RF integration."""

from .protocol import DURATION_MINUTES, FREQUENCY_HZ

DOMAIN = "harf_insoma"
DURATIONS = DURATION_MINUTES
FREQUENCY = FREQUENCY_HZ
CONF_OFF_CODE = "off_code"
CONF_TRANSMITTER = "transmitter"
CONF_DURATION = "duration"
DEFAULT_DURATION = 120
ATTR_AUTO_OFF_AT = "auto_off_at"
