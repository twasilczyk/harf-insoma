"""Bridge the published test plugin to HA 2026.11 until it catches up."""

# Load HA before plugin autoload so its httpx alias is installed first.
import homeassistant  # noqa: F401
from homeassistant.components import logger

if not hasattr(logger, "_clear_logger_overwrites"):
    from homeassistant.components.logger.helpers import _clear_logger_overwrites

    logger._clear_logger_overwrites = _clear_logger_overwrites
