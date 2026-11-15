"""Real HA progress flows learn consistent bursts and clean up subscriptions."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.dispatcher import DATA_DISPATCHER, async_dispatcher_send

from custom_components.harf_insoma.const import (
    CONF_OFF_CODE,
    CONF_RECEIVER,
    CONF_TRANSMITTER,
    DOMAIN,
)
from custom_components.harf_insoma.receiver import SIGNAL_RX_FRAME

from .test_decoder import remote_frame

pytestmark = pytest.mark.asyncio


async def start_learning(hass, transmitter, receiver):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TRANSMITTER: transmitter.entity_id}
    )
    assert result["step_id"] == "receiver"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_RECEIVER: f"hackrf_proxy:{receiver.entry_id}"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await asyncio.sleep(0)
    return result


async def emit(hass, receiver, timings):
    async_dispatcher_send(
        hass, SIGNAL_RX_FRAME.format(receiver.entry_id), {"timings": timings}
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("command", range(0, 16, 2))
async def test_learn_any_button_split_stream_finishes_and_cleans_up(
    hass, transmitter, receiver, command
):
    result = await start_learning(hass, transmitter, receiver)
    signal = SIGNAL_RX_FRAME.format(receiver.entry_id)
    assert hass.data[DATA_DISPATCHER][signal]
    burst = remote_frame(0xB750 | command) * 3
    await emit(hass, receiver, [120, -90, 500, -2116])
    await emit(hass, receiver, remote_frame(0x24E1) * 10)
    await emit(hass, receiver, burst[:49])
    assert hass.config_entries.flow.async_get(result["flow_id"])["step_id"] == "learn"
    await emit(hass, receiver, burst[49:])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not hass.data[DATA_DISPATCHER].get(signal)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OFF_CODE] == 0xB750
    assert result["data"][CONF_RECEIVER] == f"hackrf_proxy:{receiver.entry_id}"
    assert result["result"].unique_id == "b750"
    await hass.async_block_till_done()
    assert transmitter.commands == []
    assert await hass.config_entries.async_unload(result["result"].entry_id)
    assert not hass.data[DATA_DISPATCHER].get(signal)


async def test_cancel_learning_unsubscribes(hass, transmitter, receiver):
    result = await start_learning(hass, transmitter, receiver)
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not hass.data[DATA_DISPATCHER].get(SIGNAL_RX_FRAME.format(receiver.entry_id))
    await emit(hass, receiver, remote_frame(0xB752) * 3)
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_timeout_retry_and_manual_fallback(hass, transmitter, receiver):
    with patch("custom_components.harf_insoma.config_flow.LEARN_TIMEOUT", 0.01):
        result = await start_learning(hass, transmitter, receiver)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert not hass.data[DATA_DISPATCHER].get(SIGNAL_RX_FRAME.format(receiver.entry_id))
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "retry"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"learn": True}
    )
    assert result["step_id"] == "receiver"
    receiver.mock_state(hass, ConfigEntryState.NOT_LOADED)
    # No usable receiver still permits manual setup.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"receiver": "none"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"off_code": "0x24E0"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_RECEIVER not in result["data"]
    await hass.async_block_till_done()
    assert transmitter.commands == []


async def test_disconnect_discards_partial_confirmation(hass, transmitter, receiver):
    result = await start_learning(hass, transmitter, receiver)
    frame = remote_frame(0xB752)
    await emit(hass, receiver, frame * 2)
    receiver.mock_state(hass, ConfigEntryState.NOT_LOADED)
    await hass.async_block_till_done()
    await emit(hass, receiver, frame * 3)
    receiver.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()
    await emit(hass, receiver, frame)
    assert hass.config_entries.flow.async_get(result["flow_id"])["step_id"] == "learn"
    await emit(hass, receiver, frame * 2)
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OFF_CODE] == 0xB750
    await hass.async_block_till_done()
    assert transmitter.commands == []


async def test_duplicate_learned_address(hass, transmitter, receiver, make_entry):
    make_entry()
    result = await start_learning(hass, transmitter, receiver)
    await emit(hass, receiver, remote_frame(0xB75E) * 3)
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["reason"] == "already_configured"
    assert not hass.data[DATA_DISPATCHER].get(SIGNAL_RX_FRAME.format(receiver.entry_id))


async def test_retry_can_skip_learning(hass, transmitter, receiver):
    with patch("custom_components.harf_insoma.config_flow.LEARN_TIMEOUT", 0):
        result = await start_learning(hass, transmitter, receiver)
        await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"learn": False}
    )
    assert result["step_id"] == "manual"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OFF_CODE: "0xB750"}
    )
    assert CONF_RECEIVER not in result["data"]
    await hass.async_block_till_done()


async def test_subscription_failure_offers_retry(hass, transmitter, receiver):
    with patch(
        "custom_components.harf_insoma.receiver.HackrfProxySource.async_subscribe",
        side_effect=RuntimeError,
    ):
        result = await start_learning(hass, transmitter, receiver)
        await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "retry"
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize("cached_receiver", [False, True])
async def test_esphome_learning_ignores_filtered_ha_cache(
    hass, transmitter, cached_receiver
):
    """HA 2026.11.1 caches only transmitters; newer HA also caches receivers."""
    from unittest.mock import AsyncMock, MagicMock

    from aioesphomeapi import (
        InfraredInfo,
        InfraredRFReceiveEvent,
        RadioFrequencyCapability,
        RadioFrequencyInfo,
    )
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="esphome", title="RF receiver")
    entry.add_to_hass(hass)
    infrared = InfraredInfo(key=1)
    rx = RadioFrequencyInfo(
        key=7,
        capabilities=RadioFrequencyCapability.RECEIVER,
        frequency_min=433_920_000,
        frequency_max=433_920_000,
    )
    tx = RadioFrequencyInfo(key=8, capabilities=RadioFrequencyCapability.TRANSMITTER)
    client = MagicMock()
    client.list_entities_services = AsyncMock(return_value=([infrared, rx, tx], []))
    cache = {(0, 8): tx}
    if cached_receiver:
        cache[(0, 7)] = rx
    entry.runtime_data = MagicMock(
        available=True,
        client=client,
        info={RadioFrequencyInfo: cache, InfraredInfo: {(0, 1): infrared}},
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    try:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TRANSMITTER: transmitter.entity_id}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_RECEIVER: f"esphome:{entry.entry_id}"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        receive = client.subscribe_infrared_rf_receive.call_args.args[0]
        for key in (1, 8, 99):
            receive(InfraredRFReceiveEvent(key=key, timings=remote_frame(0x24E2) * 3))
        await hass.async_block_till_done()
        assert (
            hass.config_entries.flow.async_get(result["flow_id"])["step_id"] == "learn"
        )
        receive(InfraredRFReceiveEvent(key=7, timings=remote_frame(0xB752) * 3))
        await hass.async_block_till_done(wait_background_tasks=True)
        client.subscribe_infrared_rf_receive.return_value.assert_called_once()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_OFF_CODE] == 0xB750
        await hass.async_block_till_done()
        assert transmitter.commands == []
        assert await hass.config_entries.async_unload(result["result"].entry_id)
    finally:
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
