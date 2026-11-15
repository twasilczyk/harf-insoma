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


async def manual_flow(hass, transmitter):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TRANSMITTER: transmitter.entity_id}
    )
    assert result["step_id"] == "receiver"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"receiver": "none"}
    )


@pytest.mark.parametrize("off_code", ["0xB750", "46928"])
async def test_create_entry(
    hass: HomeAssistant, transmitter: RecordingTransmitter, off_code
):
    """Hex and decimal OFF codes identify the same valve and store a stable UUID."""
    transmitter.frequency_ranges = [(433_926_000, 433_926_000)]
    result = await manual_flow(hass, transmitter)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"

    with patch(
        "custom_components.harf_insoma.async_setup_entry",
        new=AsyncMock(return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_OFF_CODE: off_code},
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
    result = await manual_flow(hass, transmitter)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_OFF_CODE: off_code},
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
    result = await manual_flow(hass, transmitter)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_OFF_CODE: "46928"},
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
            {CONF_TRANSMITTER: "radio_frequency.missing"},
        )
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert transmitter.commands == []


@pytest.mark.parametrize("missing", [False, True])
async def test_reconfigure_transmitter(hass, transmitter, setup_valve, missing):
    """Replace a renamed or missing transmitter without resetting the valve."""
    from homeassistant.components.radio_frequency import DATA_COMPONENT
    from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE

    from .test_entities import deadline, turn

    valve = await setup_valve(options={"duration": 10})
    if not missing:
        await turn(hass, valve, "turn_on")
    expected_deadline = deadline(hass, valve)
    original_data = dict(valve.entry.data)
    registry = er.async_get(hass)
    registry.async_update_entity(
        transmitter.entity_id, new_entity_id="radio_frequency.renamed"
    )
    await hass.async_block_till_done()
    replacement = RecordingTransmitter()
    replacement._attr_unique_id = "replacement"
    replacement._attr_name = "Replacement"
    await hass.data[DATA_COMPONENT].async_add_entities([replacement])
    if missing:
        registry.async_remove(transmitter.entity_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": valve.entry.entry_id,
        },
    )
    assert result["type"] is FlowResultType.FORM
    if not missing:
        defaults = result["data_schema"]({"receiver": "none"})
        assert defaults[CONF_TRANSMITTER] == "radio_frequency.renamed"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TRANSMITTER: replacement.entity_id, "receiver": "none"},
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert valve.entry.data == {
        **original_data,
        CONF_TRANSMITTER: registry.async_get(replacement.entity_id).id,
    }
    assert valve.entry.unique_id == "b750"
    assert valve.entry.options == {"duration": 10}
    assert registry.async_get_entity_id("switch", DOMAIN, "b750") == valve.switch_id
    assert registry.async_get_entity_id(
        "select", DOMAIN, "b750_duration"
    ) == valve.duration_id
    assert hass.states.get(valve.switch_id).state == (STATE_OFF if missing else STATE_ON)
    assert deadline(hass, valve) == expected_deadline
    assert len(transmitter.commands) == (0 if missing else 1)
    assert replacement.commands == []
    replacement._attr_available = False
    replacement.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(valve.switch_id).state == STATE_UNAVAILABLE
    replacement._attr_available = True
    replacement.async_write_ha_state()
    await hass.async_block_till_done()
    await turn(hass, valve, "turn_off")
    assert len(replacement.commands) == 1
    assert len(transmitter.commands) == (0 if missing else 1)


async def test_reconfigure_rejects_stale_transmitter(hass, transmitter, make_entry):
    """Revalidate compatibility on submission without modifying entry data."""
    from homeassistant.components.radio_frequency import DATA_COMPONENT

    entry = make_entry()
    original_data = dict(entry.data)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    replacement = RecordingTransmitter()
    replacement._attr_unique_id = "replacement"
    await hass.data[DATA_COMPONENT].async_add_entities([replacement])
    transmitter.frequency_ranges = [(433_920_000, 433_920_000)]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TRANSMITTER: transmitter.entity_id, "receiver": "none"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TRANSMITTER: "invalid_transmitter"}
    assert entry.data == original_data
    assert transmitter.commands == []
