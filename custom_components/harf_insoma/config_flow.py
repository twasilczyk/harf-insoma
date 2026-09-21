"""Configure a paired Insoma remote and its RF transmitter."""

from typing import Any

import voluptuous as vol
from homeassistant.components.radio_frequency import (
    ModulationType,
    async_get_transmitters,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_DURATION,
    CONF_OFF_CODE,
    CONF_TRANSMITTER,
    DEFAULT_DURATION,
    DOMAIN,
    FREQUENCY,
)
from .protocol import encode_command


class InsomaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a valve using the OFF code of an already paired remote."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a compatible transmitter and the remote's OFF code."""
        try:
            transmitters = async_get_transmitters(
                self.hass, FREQUENCY, ModulationType.OOK
            )
        except HomeAssistantError:
            return self.async_abort(reason="no_transmitters")
        if not transmitters:
            return self.async_abort(reason="no_compatible_transmitters")

        errors: dict[str, str] = {}
        if user_input is not None:
            code_text = str(user_input[CONF_OFF_CODE]).strip()
            try:
                off_code = int(
                    code_text, 16 if code_text.lower().startswith("0x") else 10
                )
                # Validate the complete OFF word; never mask a different command.
                encode_command(off_code, None)
            except ValueError:
                errors[CONF_OFF_CODE] = "invalid_off_code"

            transmitter = er.async_get(self.hass).async_get(
                user_input[CONF_TRANSMITTER]
            )
            if transmitter is None or transmitter.entity_id not in transmitters:
                errors[CONF_TRANSMITTER] = "invalid_transmitter"

            if not errors:
                await self.async_set_unique_id(f"{off_code:04x}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Insoma RF {off_code:04X}",
                    data={
                        CONF_OFF_CODE: off_code,
                        CONF_TRANSMITTER: transmitter.id,
                    },
                    options={CONF_DURATION: DEFAULT_DURATION},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_TRANSMITTER): selector.EntitySelector(
                    selector.EntitySelectorConfig(include_entities=transmitters)
                ),
                vol.Required(CONF_OFF_CODE): selector.TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )
