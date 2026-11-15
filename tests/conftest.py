"""Home Assistant fixtures with a real RF transmitter entity."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from homeassistant.components.radio_frequency import (
    DATA_COMPONENT,
    RadioFrequencyTransmitterEntity,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from rf_protocols import RadioFrequencyCommand

from custom_components.harf_insoma.const import (
    CONF_OFF_CODE,
    CONF_TRANSMITTER,
    DOMAIN,
)


@pytest.fixture
async def receiver(hass):
    """An independent receiver using the public HackRF dispatcher contract."""
    entry = MockConfigEntry(domain="hackrf_proxy", title="Test receiver")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    yield entry
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading this repository's custom integration."""


class RecordingTransmitter(RadioFrequencyTransmitterEntity):
    """Exercise the RF framework while recording commands at its transport edge."""

    _attr_name = "Test transmitter"
    _attr_unique_id = "test_transmitter"
    _attr_available = True

    def __init__(self) -> None:
        self.commands: list[RadioFrequencyCommand] = []
        self.failure: HomeAssistantError | None = None
        self.frequency_ranges = [(433_000_000, 434_000_000)]

    @property
    def supported_frequency_ranges(self) -> list[tuple[int, int]]:
        return self.frequency_ranges

    async def async_send_command(self, command: RadioFrequencyCommand) -> None:
        if self.failure is not None:
            raise self.failure
        self.commands.append(command)


@pytest.fixture
async def transmitter(hass: HomeAssistant) -> RecordingTransmitter:
    """Add a registered transmitter to HA's actual radio_frequency component."""
    assert await async_setup_component(hass, "radio_frequency", {})
    transmitter = RecordingTransmitter()
    await hass.data[DATA_COMPONENT].async_add_entities([transmitter])
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get(transmitter.entity_id) is not None
    return transmitter


@dataclass
class Valve:
    """An integration entry and its registered entities."""

    entry: MockConfigEntry
    switch_id: str
    duration_id: str


@pytest.fixture
def make_entry(hass: HomeAssistant, transmitter: RecordingTransmitter) -> Callable:
    """Construct independent valve entries referencing the transmitter UUID."""

    def make(
        off_code: int = 0xB750, options: dict[str, Any] | None = None
    ) -> MockConfigEntry:
        registered = er.async_get(hass).async_get(transmitter.entity_id)
        assert registered is not None
        entry = MockConfigEntry(
            domain=DOMAIN,
            title=f"Insoma RF {off_code:04X}",
            unique_id=f"{off_code:04x}",
            data={CONF_TRANSMITTER: registered.id, CONF_OFF_CODE: off_code},
            options=options or {},
        )
        entry.add_to_hass(hass)
        return entry

    return make


@pytest.fixture
def setup_valve(hass: HomeAssistant, make_entry: Callable) -> Callable:
    """Set up an entry and discover entity IDs from their stable registry IDs."""

    async def setup(
        off_code: int = 0xB750,
        options: dict[str, Any] | None = None,
        entry: MockConfigEntry | None = None,
    ) -> Valve:
        entry = entry or make_entry(off_code, options)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        switch_id = registry.async_get_entity_id("switch", DOMAIN, f"{off_code:04x}")
        duration_id = registry.async_get_entity_id(
            "select", DOMAIN, f"{off_code:04x}_duration"
        )
        assert switch_id is not None
        assert duration_id is not None
        return Valve(entry, switch_id, duration_id)

    return setup
