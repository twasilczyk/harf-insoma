"""FrameSource discovery, filtering, disconnect and subscription lifecycle."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioesphomeapi import (
    InfraredInfo,
    InfraredRFReceiveEvent,
    RadioFrequencyCapability,
    RadioFrequencyInfo,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.dispatcher import DATA_DISPATCHER, async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.harf_insoma.receiver import (
    SIGNAL_RX_FRAME,
    EsphomeSource,
    async_get_source,
    async_list_sources,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("frequency_min", "frequency_max", "modulations"),
    [
        (433_000_000, 434_000_000, 1),
        (433_920_000, 433_920_000, 1),
        (0, 0, 0),
        (433_920_000, 433_920_000, 2),
        (300_000_000, 315_000_000, 0),
    ],
)
async def test_esphome_lifecycle(
    hass, caplog, frequency_min, frequency_max, modulations
):
    entry = MockConfigEntry(domain="esphome", title="RF node")
    entry.add_to_hass(hass)
    info = RadioFrequencyInfo(
        key=7,
        capabilities=RadioFrequencyCapability.RECEIVER,
        frequency_min=frequency_min,
        frequency_max=frequency_max,
        supported_modulations=modulations,
    )
    infrared = InfraredInfo(key=1)
    transmitter = RadioFrequencyInfo(
        key=8, capabilities=RadioFrequencyCapability.TRANSMITTER
    )
    no_capabilities = RadioFrequencyInfo(key=9)
    client = MagicMock()
    client.list_entities_services = AsyncMock(
        return_value=([infrared, info, transmitter, no_capabilities], [])
    )
    entry.runtime_data = data = MagicMock(
        available=True,
        client=client,
        info={
            RadioFrequencyInfo: {
                (0, 7): info,
                (0, 8): transmitter,
                (0, 9): no_capabilities,
            },
            InfraredInfo: {(0, 1): infrared},
        },
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    source = EsphomeSource(hass, entry)
    seen = []
    unsubscribe = source.async_subscribe(seen.append)
    try:
        with caplog.at_level(
            logging.DEBUG, logger="custom_components.harf_insoma.receiver"
        ):
            assert [s.id for s in await async_list_sources(hass)] == [source.id]
        assert f"capabilities={info.capabilities}" in caplog.text
        assert f"frequency_min={frequency_min} Hz" in caplog.text
        assert f"frequency_max={frequency_max} Hz" in caplog.text
        assert f"supported_modulations={modulations}" in caplog.text
        assert (
            "key=8" in caplog.text
        )  # Log all RF channels, not just the first receiver.
        await hass.async_block_till_done()
        handler = client.subscribe_infrared_rf_receive.call_args.args[0]
        for key in (1, 8, 9, 99):
            handler(InfraredRFReceiveEvent(key=key, timings=[320, -592]))
        assert seen == []  # IR, transmit-only, non-receiver and unknown channels.
        handler(InfraredRFReceiveEvent(key=7, timings=[320, -592]))
        assert seen == [[320, -592]]
        updated = data.async_subscribe_device_updated.call_args.args[0]
        data.available = False
        updated()
        assert seen[-1] == []
        data.available = True
        client.list_entities_services.return_value = (
            [
                RadioFrequencyInfo(
                    key=17, device_id=2, capabilities=RadioFrequencyCapability.RECEIVER
                )
            ],
            [],
        )
        updated()
        await hass.async_block_till_done()
        handler = client.subscribe_infrared_rf_receive.call_args.args[0]
        handler(InfraredRFReceiveEvent(key=7, timings=[1]))
        handler(InfraredRFReceiveEvent(key=17, device_id=0, timings=[2]))
        assert seen[-1] == []
        handler(InfraredRFReceiveEvent(key=17, device_id=2, timings=[320, -592]))
        assert seen[-1] == [320, -592]
        assert client.subscribe_infrared_rf_receive.call_count == 2
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        await hass.async_block_till_done()
        client.subscribe_infrared_rf_receive.return_value.assert_called_once()
        entry.mock_state(hass, ConfigEntryState.LOADED)
        await hass.async_block_till_done()
        assert client.subscribe_infrared_rf_receive.call_count == 3
    finally:
        unsubscribe()
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    assert client.subscribe_infrared_rf_receive.return_value.call_count == 2


@pytest.mark.parametrize("capabilities", [0, RadioFrequencyCapability.TRANSMITTER])
async def test_discovery_missing_offline_and_failed_receivers(hass, capabilities):
    entry = MockConfigEntry(domain="esphome", title="Offline")
    entry.add_to_hass(hass)
    assert await async_list_sources(hass) == []
    assert async_get_source(hass, f"esphome:{entry.entry_id}") is not None
    assert async_get_source(hass, "esphome:missing") is None
    assert async_get_source(hass, f"hackrf_proxy:{entry.entry_id}") is None
    assert async_get_source(hass, f"unknown:{entry.entry_id}") is None
    entry.runtime_data = MagicMock(available=True)
    entry.runtime_data.client.list_entities_services = AsyncMock(
        side_effect=TimeoutError
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    try:
        assert await async_list_sources(hass) == []
        entry.runtime_data.client.list_entities_services = AsyncMock(
            return_value=(
                [
                    InfraredInfo(key=1),
                    RadioFrequencyInfo(
                        capabilities=capabilities,
                        frequency_min=300_000_000,
                        frequency_max=315_000_000,
                    ),
                ],
                [],
            )
        )
        assert await async_list_sources(hass) == []
    finally:
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_hackrf_disconnect_and_unsubscribe(hass, receiver):
    source = async_get_source(hass, f"hackrf_proxy:{receiver.entry_id}")
    seen = []
    stop = source.async_subscribe(seen.append)
    signal = SIGNAL_RX_FRAME.format(receiver.entry_id)
    async_dispatcher_send(hass, signal, {"timings": (320, -592)})
    await hass.async_block_till_done()
    assert seen == [[320, -592]]
    receiver.mock_state(hass, ConfigEntryState.NOT_LOADED)
    await hass.async_block_till_done()
    assert seen[-1] == []
    async_dispatcher_send(hass, signal, {"timings": [620, -292]})
    await hass.async_block_till_done()
    assert [620, -292] not in seen
    stop()
    assert not hass.data[DATA_DISPATCHER].get(signal)


@pytest.mark.parametrize("disconnect", [False, True])
async def test_cancel_pending_receiver_discovery(hass, disconnect):
    """A pending listing cannot install a subscription after disconnect or cleanup."""
    import asyncio

    entry = MockConfigEntry(domain="esphome", title="RF node")
    entry.add_to_hass(hass)
    pending = hass.loop.create_future()
    client = MagicMock()

    async def listing():
        return await pending

    client.list_entities_services = listing
    entry.runtime_data = data = MagicMock(available=True, client=client)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    stop = EsphomeSource(hass, entry).async_subscribe(lambda timings: None)
    await asyncio.sleep(0)
    if disconnect:
        data.available = False
        data.async_subscribe_device_updated.call_args.args[0]()
    else:
        stop()
    await hass.async_block_till_done()
    assert pending.cancelled()
    client.subscribe_infrared_rf_receive.assert_not_called()
    if disconnect:
        stop()
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_receiver_listing_failure_retries_on_update(hass):
    """Unverified channels stay blocked after API errors until discovery succeeds."""
    entry = MockConfigEntry(domain="esphome", title="RF node")
    entry.add_to_hass(hass)
    client = MagicMock()
    client.list_entities_services = AsyncMock(side_effect=TimeoutError)
    entry.runtime_data = data = MagicMock(available=True, client=client)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    seen = []
    stop = EsphomeSource(hass, entry).async_subscribe(seen.append)
    try:
        await hass.async_block_till_done()
        client.subscribe_infrared_rf_receive.assert_not_called()
        client.list_entities_services.side_effect = None
        client.list_entities_services.return_value = (
            [RadioFrequencyInfo(key=7, capabilities=RadioFrequencyCapability.RECEIVER)],
            [],
        )
        data.async_subscribe_device_updated.call_args.args[0]()
        await hass.async_block_till_done()
        receive = client.subscribe_infrared_rf_receive.call_args.args[0]
        receive(InfraredRFReceiveEvent(key=7, timings=[320, -592]))
        assert seen == [[320, -592]]
    finally:
        stop()
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
