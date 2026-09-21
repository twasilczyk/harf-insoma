"""Shared identity for a valve and its local duration setting."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import CONF_OFF_CODE, DOMAIN


class InsomaEntity(Entity):
    """An entity belonging to a single paired valve remote."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Keep the config entry so duration changes apply to the next ON."""
        self._entry = entry
        self._off_code: int = entry.data[CONF_OFF_CODE]
        self._attr_unique_id = f"{self._off_code:04x}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=entry.title,
            manufacturer="Insoma",
            model="RF garden hose valve",
        )
