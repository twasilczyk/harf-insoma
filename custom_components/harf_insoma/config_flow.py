"""Select radios and learn an Insoma remote or enter its captured OFF code."""

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.radio_frequency import (
    ModulationType,
    async_get_transmitters,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_DURATION,
    CONF_LEARN,
    CONF_OFF_CODE,
    CONF_RECEIVER,
    CONF_TRANSMITTER,
    DEFAULT_DURATION,
    DOMAIN,
    FREQUENCY,
    LEARN_TIMEOUT,
)
from .protocol import FrameStream, encode_command
from .receiver import FrameSource, async_get_source, async_list_sources

NO_RECEIVER = "none"
_LOGGER = logging.getLogger(__name__)


class InsomaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Learn an address without changing existing entry or entity identities."""

    VERSION = 1

    def __init__(self) -> None:
        self._transmitter = ""
        self._receiver: str | None = None
        self._off_code: int | None = None
        self._learn_task: asyncio.Task[int | None] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        try:
            transmitters = async_get_transmitters(
                self.hass, FREQUENCY, ModulationType.OOK
            )
        except HomeAssistantError:
            return self.async_abort(reason="no_transmitters")
        if not transmitters:
            return self.async_abort(reason="no_compatible_transmitters")
        errors = {}
        if user_input is not None:
            transmitter = er.async_get(self.hass).async_get(
                user_input[CONF_TRANSMITTER]
            )
            if transmitter is None or transmitter.entity_id not in transmitters:
                errors[CONF_TRANSMITTER] = "invalid_transmitter"
            else:
                self._transmitter = transmitter.id
                return await self.async_step_receiver()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRANSMITTER): selector.EntitySelector(
                        selector.EntitySelectorConfig(include_entities=transmitters)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_receiver(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            selected = user_input[CONF_RECEIVER]
            self._receiver = None if selected == NO_RECEIVER else selected
            if self._receiver is None:
                return await self.async_step_manual()
            if async_get_source(self.hass, self._receiver) is None:
                errors[CONF_RECEIVER] = "receiver_unusable"
            else:
                return await self.async_step_learn()
        sources = await async_list_sources(self.hass)
        transmitter = er.async_get(self.hass).async_get(self._transmitter)
        default = next(
            (
                source.id
                for source in sources
                if transmitter and source.entry.entry_id == transmitter.config_entry_id
            ),
            sources[0].id if len(sources) == 1 else NO_RECEIVER,
        )
        return self.async_show_form(
            step_id="receiver",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_RECEIVER, default=default
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=NO_RECEIVER,
                                    label="No receiver (enter OFF code manually)",
                                ),
                                *(
                                    selector.SelectOptionDict(value=s.id, label=s.name)
                                    for s in sources
                                ),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        starting = self._learn_task is None
        if starting:
            source = async_get_source(self.hass, self._receiver or "")
            if source is None:
                return await self.async_step_retry()
            self._learn_task = self.hass.async_create_background_task(
                self._async_learn(source), "Insoma RF learning"
            )
        # Always enter the progress step, even if eager task execution failed
        # immediately. Otherwise HA can pass the receiver form to the retry step.
        if starting or not self._learn_task.done():
            return self.async_show_progress(
                step_id="learn",
                progress_action="listening",
                progress_task=self._learn_task,
            )
        self._off_code = self._learn_task.result()
        self._learn_task = None
        return self.async_show_progress_done(
            next_step_id="retry" if self._off_code is None else "finish"
        )

    async def _async_learn(self, source: FrameSource) -> int | None:
        found: asyncio.Future[int] = self.hass.loop.create_future()
        decoder = FrameStream()

        @callback
        def receive(timings: list[int]) -> None:
            for frame in decoder.feed(timings):
                if not found.done():
                    found.set_result(frame.off_code)

        try:
            unsubscribe = source.async_subscribe(receive)
        except Exception:
            _LOGGER.debug(
                "Could not subscribe to receiver %s", source.id, exc_info=True
            )
            return None
        try:
            async with asyncio.timeout(LEARN_TIMEOUT):
                return await found
        except TimeoutError:
            return None
        finally:
            unsubscribe()

    async def async_step_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if user_input[CONF_LEARN]:
                return await self.async_step_receiver()
            self._receiver = None
            return await self.async_step_manual()
        return self.async_show_form(
            step_id="retry",
            data_schema=vol.Schema(
                {vol.Required(CONF_LEARN, default=True): selector.BooleanSelector()}
            ),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            text = str(user_input[CONF_OFF_CODE]).strip()
            try:
                code = int(text, 16 if text.lower().startswith("0x") else 10)
                encode_command(code, None)
            except ValueError:
                errors[CONF_OFF_CODE] = "invalid_off_code"
            else:
                self._off_code = code
                return await self.async_step_finish()
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {vol.Required(CONF_OFF_CODE): selector.TextSelector()}
            ),
            errors=errors,
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._off_code is not None
        await self.async_set_unique_id(f"{self._off_code:04x}")
        self._abort_if_unique_id_configured()
        data = {CONF_OFF_CODE: self._off_code, CONF_TRANSMITTER: self._transmitter}
        if self._receiver is not None:
            data[CONF_RECEIVER] = self._receiver
        return self.async_create_entry(
            title=f"Insoma RF {self._off_code:04X}",
            data=data,
            options={CONF_DURATION: DEFAULT_DURATION},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace radios without changing the valve's identity."""
        entry = self._get_reconfigure_entry()
        try:
            transmitters = async_get_transmitters(
                self.hass, FREQUENCY, ModulationType.OOK
            )
        except HomeAssistantError:
            return self.async_abort(reason="no_transmitters")
        if not transmitters:
            return self.async_abort(reason="no_compatible_transmitters")
        registry = er.async_get(self.hass)
        configured_transmitter = registry.async_get(entry.data[CONF_TRANSMITTER])
        errors = {}
        if user_input is not None:
            transmitter = registry.async_get(user_input[CONF_TRANSMITTER])
            if transmitter is None or transmitter.entity_id not in transmitters:
                errors[CONF_TRANSMITTER] = "invalid_transmitter"
            selected = user_input[CONF_RECEIVER]
            if (
                selected != NO_RECEIVER
                and async_get_source(self.hass, selected) is None
            ):
                errors[CONF_RECEIVER] = "receiver_unusable"
            if not errors:
                data = dict(entry.data)
                data[CONF_TRANSMITTER] = transmitter.id
                data.pop(CONF_RECEIVER, None)
                if selected != NO_RECEIVER:
                    data[CONF_RECEIVER] = selected
                return self.async_update_reload_and_abort(entry, data=data)
        sources = await async_list_sources(self.hass)
        configured = entry.data.get(CONF_RECEIVER, NO_RECEIVER)
        options = [selector.SelectOptionDict(value=NO_RECEIVER, label="No receiver")]
        options.extend(
            selector.SelectOptionDict(value=s.id, label=s.name) for s in sources
        )
        if configured != NO_RECEIVER and configured not in {s.id for s in sources}:
            options.append(
                selector.SelectOptionDict(
                    value=configured, label="Configured receiver (offline or missing)"
                )
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TRANSMITTER,
                        default=(
                            configured_transmitter.entity_id
                            if configured_transmitter
                            and configured_transmitter.entity_id in transmitters
                            else vol.UNDEFINED
                        ),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(include_entities=transmitters)
                    ),
                    vol.Required(
                        CONF_RECEIVER, default=configured
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            errors=errors,
        )
