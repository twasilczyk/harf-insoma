"""Local configuration of the duration sent by the next ON command."""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DURATION, DEFAULT_DURATION, DOMAIN, DURATIONS
from .entity import InsomaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the valve's duration selector."""
    async_add_entities([InsomaDurationSelect(entry)])


class InsomaDurationSelect(InsomaEntity, SelectEntity):
    """Persist a supported duration without transmitting or reloading entities."""

    _attr_translation_key = "duration"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the local setting, independent of RF availability."""
        super().__init__(entry)
        self._attr_unique_id = f"{self._off_code:04x}_duration"
        self._attr_options = [str(minutes) for minutes in DURATIONS]

    @property
    def current_option(self) -> str | None:
        """Read the persisted choice, or the initial two-hour limit."""
        duration = self._entry.options.get(CONF_DURATION, DEFAULT_DURATION)
        if type(duration) is not int or duration not in DURATIONS:
            return None
        return str(duration)

    async def async_select_option(self, option: str) -> None:
        """Save the duration for the next ON; never send an RF command here."""
        if option not in self._attr_options:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_duration"
            )
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_DURATION: int(option)},
        )
        self.async_write_ha_state()
