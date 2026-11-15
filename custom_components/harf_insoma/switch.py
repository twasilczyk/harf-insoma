"""A valve switch whose ON command always includes the hardware time limit."""

import asyncio
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

from homeassistant.components.radio_frequency import (
    RadioFrequencyTransmitterConsumerEntity,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_point_in_utc_time,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_AUTO_OFF_AT,
    CONF_DURATION,
    CONF_RECEIVER,
    CONF_TRANSMITTER,
    DEFAULT_DURATION,
    DOMAIN,
    DURATIONS,
)
from .entity import InsomaEntity
from .protocol import COMMAND_DURATIONS, FrameStream, encode_command
from .receiver import async_get_source


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the valve's ON/OFF control."""
    async_add_entities([InsomaSwitch(entry)])


class InsomaSwitch(
    InsomaEntity, RadioFrequencyTransmitterConsumerEntity, RestoreEntity, SwitchEntity
):
    """Assume valve state from commands; the physical valve enforces the timer."""

    _attr_name = None
    _attr_translation_key = "valve"
    _attr_assumed_state = False
    _attr_is_on = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize state without transmitting anything."""
        super().__init__(entry)
        self._rf_transmitter_entity_id_or_uuid = entry.data[CONF_TRANSMITTER]
        self._deadline: datetime | None = None
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._command_lock = asyncio.Lock()
        self._removed = False
        self._state_source = "default"
        self._restored_once = False
        self._decoder = FrameStream()
        self._echo_until: dict[int, float] = {}
        self._sending: int | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the expected shutoff time, not confirmation from the valve."""
        return {
            ATTR_AUTO_OFF_AT: self._deadline.isoformat() if self._deadline else None,
            "state_source": self._state_source,
        }

    async def async_added_to_hass(self) -> None:
        """Restore only the expected state/deadline, never a radio command."""
        self._removed = False
        # HA 2026.11 requires an entity ID; older releases use the UUID above.
        self._rf_transmitter_entity_id = er.async_validate_entity_id(
            er.async_get(self.hass), self._entry.data[CONF_TRANSMITTER]
        )
        await super().async_added_to_hass()
        self.async_on_remove(self._async_cleanup)
        if source := async_get_source(
            self.hass, self._entry.data.get(CONF_RECEIVER, "")
        ):
            self.async_on_remove(source.async_subscribe(self._async_receive))
        # Entity-ID renames remove and re-add the same object. Its live deadline
        # remains authoritative even when no saved state exists under the new ID.
        if self._restored_once:
            if self._deadline is not None:
                self._async_resume_timer(self._deadline)
            return
        self._restored_once = True
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        if last_state.state == STATE_OFF:
            self._attr_is_on = False
            self._state_source = "restored"
            return
        if last_state.state not in (STATE_ON, STATE_UNAVAILABLE):
            return

        saved_deadline = last_state.attributes.get(ATTR_AUTO_OFF_AT)
        if not isinstance(saved_deadline, str):
            return
        try:
            deadline = dt_util.parse_datetime(saved_deadline)
        except ValueError:
            return
        if deadline is None or deadline.tzinfo is None:
            return
        self._async_resume_timer(deadline)
        self._state_source = "restored"

    @callback
    def _async_track_availability(self, rf_entity_id: str) -> CALLBACK_TYPE:
        """Keep HA 2026.11's availability subscription bound across renames.

        HA 2026.9/2026.10 handle renames themselves and do not call this hook.
        """
        track_availability = super()._async_track_availability
        unsubscribe_availability = track_availability(rf_entity_id)

        @callback
        def registry_updated(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
            nonlocal unsubscribe_availability, unsubscribe_registry
            data = event.data
            if data["action"] != "update" or "entity_id" not in data["changes"]:
                return
            unsubscribe_availability()
            unsubscribe_registry()
            self._rf_transmitter_entity_id = data["entity_id"]
            unsubscribe_availability = track_availability(
                self._rf_transmitter_entity_id
            )
            unsubscribe_registry = async_track_entity_registry_updated_event(
                self.hass, self._rf_transmitter_entity_id, registry_updated
            )

        unsubscribe_registry = async_track_entity_registry_updated_event(
            self.hass, rf_entity_id, registry_updated
        )

        @callback
        def unsubscribe() -> None:
            unsubscribe_availability()
            unsubscribe_registry()

        return unsubscribe

    @callback
    def _async_resume_timer(self, deadline: datetime) -> None:
        """Resume a known deadline without restarting the physical watering run."""
        now = dt_util.utcnow()
        if deadline <= now:
            self._deadline = None
            self._attr_is_on = False
        elif deadline - now <= timedelta(minutes=max(DURATIONS)):
            self._attr_is_on = True
            self._deadline = deadline
            self._async_schedule_timer()
        else:
            self._deadline = None
            self._attr_is_on = False

    @callback
    def _async_receive(self, timings: list[int]) -> None:
        """Observe matching remote presses without transmitting or inferring feedback."""
        if self._removed:
            return
        now = monotonic()
        for frame in self._decoder.feed(timings, now=now):
            if frame.off_code != self._off_code:
                continue
            if frame.command == self._sending or now <= self._echo_until.get(
                frame.command, 0
            ):
                continue
            self._async_cancel_timer()
            self._deadline = None
            self._attr_is_on = frame.command != 0
            if duration := COMMAND_DURATIONS.get(frame.command):
                self._deadline = dt_util.utcnow() + timedelta(minutes=duration)
                self._async_schedule_timer()
            self._state_source = "remote"
            self.async_write_ha_state()

    async def _async_transmit(self, duration: int | None) -> None:
        """Guard echoes during sending and queued airtime, including failed sends."""
        command = encode_command(self._off_code, duration)
        nibble = next(
            (key for key, value in COMMAND_DURATIONS.items() if value == duration), 0
        )
        airtime = (
            sum(abs(t) for t in command.get_raw_timings())
            * (command.repeat_count + 1)
            / 1_000_000
        )
        self._sending = nibble
        try:
            await self._send_command(command)
        except (Exception, asyncio.CancelledError):
            if not self._removed:
                self._state_source = "transmission_error"
                self.async_write_ha_state()
            raise
        finally:
            self._sending = None
            self._echo_until[nibble] = monotonic() + airtime + 0.35

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send a timed command even when ON is requested again."""
        async with self._command_lock:
            if self._removed:
                raise HomeAssistantError("The valve entity has been unloaded")
            duration = self._entry.options.get(CONF_DURATION, DEFAULT_DURATION)
            if type(duration) is not int or duration not in DURATIONS:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="invalid_duration"
                )
            await self._async_transmit(duration)
            if self._removed:
                return
            # Do not replace a running deadline until transmission succeeds.
            self._async_cancel_timer()
            self._attr_is_on = True
            self._deadline = dt_util.utcnow() + timedelta(minutes=duration)
            self._async_schedule_timer()
            self._state_source = "home_assistant"
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send OFF before changing the assumed state or its deadline."""
        async with self._command_lock:
            if self._removed:
                raise HomeAssistantError("The valve entity has been unloaded")
            await self._async_transmit(None)
            if self._removed:
                return
            self._async_cancel_timer()
            self._deadline = None
            self._attr_is_on = False
            self._state_source = "home_assistant"
            self.async_write_ha_state()

    @callback
    def _async_schedule_timer(self) -> None:
        """Schedule a UI update at the expected hardware shutoff time."""
        assert self._deadline is not None
        self._cancel_timer = async_track_point_in_utc_time(
            self.hass, self._async_timer_elapsed, self._deadline
        )

    @callback
    def _async_cancel_timer(self) -> None:
        """Remove a timer on replacement, OFF, or entity unload."""
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    @callback
    def _async_cleanup(self) -> None:
        """Cancel callbacks and suppress updates from pending commands."""
        self._removed = True
        self._async_cancel_timer()
        self._decoder.reset()

    @callback
    def _async_timer_elapsed(self, now: datetime) -> None:
        """Reflect the valve's timed shutoff without another transmission."""
        self._cancel_timer = None
        self._deadline = None
        self._attr_is_on = False
        self._state_source = "timer"
        self.async_write_ha_state()
