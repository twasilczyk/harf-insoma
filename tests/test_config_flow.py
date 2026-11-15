"""User setup validation against Home Assistant's RF transmitter registry."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er

from custom_components.harf_insoma.const import (
    CONF_OFF_CODE,
    CONF_TRANSMITTER,
    DOMAIN,
)

from .conftest import RecordingTransmitter

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("off_code", ["0xB750", "46928"])
async def test_create_entry(
    hass: HomeAssistant, transmitter: RecordingTransmitter, off_code
):
    """Hex and decimal OFF codes identify the same valve and store a stable UUID."""
    transmitter.frequency_ranges = [(433_926_000, 433_926_000)]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.harf_insoma.async_setup_entry",
        new=AsyncMock(return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_TRANSMITTER: transmitter.entity_id, CONF_OFF_CODE: off_code},
        )
        await hass.async_block_till_done()

    registered = er.async_get(hass).async_get(transmitter.entity_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Insoma RF B750"
    assert result["data"] == {
        CONF_TRANSMITTER: registered.id,
        CONF_OFF_CODE: 0xB750,
    }
    assert result["result"].unique_id == "b750"
    assert transmitter.commands == []


@pytest.mark.parametrize("off_code", ["-16", "65536", "0xB751", "1.5", "invalid"])
async def test_reject_invalid_off_code(
    hass: HomeAssistant, transmitter: RecordingTransmitter, off_code
):
    """Reject codes that cannot denote a 16-bit OFF command."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TRANSMITTER: transmitter.entity_id, CONF_OFF_CODE: off_code},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_OFF_CODE: "invalid_off_code"}
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert transmitter.commands == []


async def test_duplicate_valve(
    hass: HomeAssistant, transmitter: RecordingTransmitter, make_entry
):
    """Re-entering the valve in a different number format cannot create a duplicate."""
    make_entry()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TRANSMITTER: transmitter.entity_id, CONF_OFF_CODE: "46928"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_no_transmitters(hass: HomeAssistant):
    """Explain the prerequisite when no transmitter has been installed."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_transmitters"


@pytest.mark.parametrize(
    "frequency_range", [(900_000_000, 930_000_000), (433_920_000, 433_920_000)]
)
async def test_no_compatible_transmitters(
    hass: HomeAssistant, transmitter: RecordingTransmitter, frequency_range
):
    """Transmitters must support the adjusted carrier, not just the previous one."""
    transmitter.frequency_ranges = [frequency_range]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_compatible_transmitters"


async def test_reject_unknown_transmitter(
    hass: HomeAssistant, transmitter: RecordingTransmitter
):
    """A stale or forged transmitter selection cannot create an unusable entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_TRANSMITTER: "radio_frequency.missing", CONF_OFF_CODE: "0xB750"},
        )
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert transmitter.commands == []
