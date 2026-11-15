"""Remote synchronization and HA commands remain local estimates, never feedback."""

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import DATA_DISPATCHER
from homeassistant.util import dt as dt_util

from custom_components.harf_insoma.const import CONF_DURATION, CONF_RECEIVER, DOMAIN
from custom_components.harf_insoma.receiver import SIGNAL_RX_FRAME

from .test_decoder import remote_frame
from .test_entities import advance, deadline, turn
from .test_learning import emit

pytestmark = pytest.mark.asyncio


async def received_valve(hass, receiver, make_entry, setup_valve, off_code=0xB750):
    entry = make_entry(off_code)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_RECEIVER: f"hackrf_proxy:{receiver.entry_id}"}
    )
    return await setup_valve(off_code, entry=entry)


@pytest.mark.parametrize(
    "nibble,minutes", [(2, 5), (4, 10), (6, 30), (8, 60), (10, 90), (14, 120)]
)
async def test_remote_timed_on_expiration_off_and_deduplication(
    hass, transmitter, receiver, make_entry, setup_valve, freezer, nibble, minutes
):
    valve = await received_valve(hass, receiver, make_entry, setup_valve)
    started = dt_util.utcnow()
    frame = remote_frame(0xB750 | nibble)
    await emit(hass, receiver, frame * 180)
    assert hass.states.get(valve.switch_id).state == STATE_ON
    assert hass.states.get(valve.switch_id).attributes["state_source"] == "remote"
    expected = started + timedelta(minutes=minutes)
    assert deadline(hass, valve) == expected
    await emit(hass, receiver, frame * 180)
    assert deadline(hass, valve) == expected
    await advance(hass, freezer, expected + timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert hass.states.get(valve.switch_id).attributes["state_source"] == "timer"
    await emit(hass, receiver, frame * 3)
    assert hass.states.get(valve.switch_id).state == STATE_ON
    await emit(hass, receiver, remote_frame(0xB750) * 3)
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert deadline(hass, valve) is None
    assert transmitter.commands == []


async def test_remote_permanent_on_and_multi_valve_filtering(
    hass, transmitter, receiver, make_entry, setup_valve, freezer
):
    first = await received_valve(hass, receiver, make_entry, setup_valve)
    second = await received_valve(hass, receiver, make_entry, setup_valve, 0x24E0)
    await emit(hass, receiver, remote_frame(0xB752) * 3)
    await emit(hass, receiver, remote_frame(0xB75C) * 3)
    assert hass.states.get(first.switch_id).state == STATE_ON
    assert deadline(hass, first) is None
    assert hass.states.get(second.switch_id).state == STATE_OFF
    await advance(hass, freezer, dt_util.utcnow() + timedelta(hours=3))
    assert hass.states.get(first.switch_id).state == STATE_ON
    await emit(hass, receiver, remote_frame(0x24E4) * 3)
    assert hass.states.get(second.switch_id).state == STATE_ON
    assert deadline(hass, second) is not None
    await emit(hass, receiver, remote_frame(0xB750) * 3)
    assert hass.states.get(first.switch_id).state == STATE_OFF
    assert hass.states.get(second.switch_id).state == STATE_ON
    assert transmitter.commands == []


async def test_ha_echoes_during_send_and_queued_airtime(
    hass, transmitter, receiver, make_entry, setup_valve, monkeypatch
):
    valve = await received_valve(hass, receiver, make_entry, setup_valve)
    original = transmitter.async_send_command

    async def send_with_echo(command):
        await emit(hass, receiver, command.get_raw_timings() * 3)
        assert hass.states.get(valve.switch_id).state == STATE_OFF
        await original(command)

    monkeypatch.setattr(transmitter, "async_send_command", send_with_echo)
    await turn(hass, valve, "turn_on")
    expected = deadline(hass, valve)
    await emit(hass, receiver, transmitter.commands[-1].get_raw_timings() * 180)
    assert (
        hass.states.get(valve.switch_id).attributes["state_source"] == "home_assistant"
    )
    assert deadline(hass, valve) == expected
    # Other remote buttons still work within the echo guard.
    await emit(hass, receiver, remote_frame(0xB750) * 3)
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    with patch("custom_components.harf_insoma.switch.monotonic", return_value=1e12):
        await emit(hass, receiver, remote_frame(0xB75E) * 3)
    assert hass.states.get(valve.switch_id).attributes["state_source"] == "remote"


async def test_every_explicit_request_transmits_and_failure_preserves_state(
    hass, transmitter, setup_valve, freezer
):
    valve = await setup_valve(options={CONF_DURATION: 5})
    await turn(hass, valve, "turn_off")
    await turn(hass, valve, "turn_off")
    assert len(transmitter.commands) == 2
    await asyncio.gather(turn(hass, valve, "turn_on"), turn(hass, valve, "turn_on"))
    assert len(transmitter.commands) == 4
    before = deadline(hass, valve)
    await advance(hass, freezer, dt_util.utcnow() + timedelta(minutes=1))
    await turn(hass, valve, "turn_on")
    assert deadline(hass, valve) == before + timedelta(minutes=1)
    expected = deadline(hass, valve)
    transmitter.failure = HomeAssistantError("Radio failed")
    for action in ("turn_on", "turn_off"):
        with pytest.raises(HomeAssistantError, match="Radio failed"):
            await turn(hass, valve, action)
        state = hass.states.get(valve.switch_id)
        assert state.state == STATE_ON
        assert state.attributes["state_source"] == "transmission_error"
        assert deadline(hass, valve) == expected
    assert len(transmitter.commands) == 5


async def test_reload_preserves_legacy_ids_options_and_deadline(
    hass, transmitter, make_entry, setup_valve, freezer
):
    entry = make_entry(options={CONF_DURATION: 5})
    valve = await setup_valve(entry=entry)
    await turn(hass, valve, "turn_on")
    expected = deadline(hass, valve)
    ids = {
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert {
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    } == ids
    assert entry.unique_id == "b750"
    assert entry.options[CONF_DURATION] == 5
    assert deadline(hass, valve) == expected
    assert hass.states.get(valve.switch_id).attributes["state_source"] == "restored"
    assert len(transmitter.commands) == 1
    await advance(hass, freezer, expected + timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert len(transmitter.commands) == 1


async def test_remote_permanent_on_restores_off_without_deadline(
    hass, transmitter, receiver, make_entry, setup_valve
):
    valve = await received_valve(hass, receiver, make_entry, setup_valve)
    await emit(hass, receiver, remote_frame(0xB75C) * 3)
    assert await hass.config_entries.async_reload(valve.entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert transmitter.commands == []


async def test_missing_receiver_and_cleanup(
    hass, transmitter, receiver, make_entry, setup_valve
):
    valve = await received_valve(hass, receiver, make_entry, setup_valve)
    signal = SIGNAL_RX_FRAME.format(receiver.entry_id)
    assert hass.data[DATA_DISPATCHER][signal]
    await emit(hass, receiver, remote_frame(0xB752) * 3)
    assert await hass.config_entries.async_unload(valve.entry.entry_id)
    assert not hass.data[DATA_DISPATCHER].get(signal)
    await emit(hass, receiver, remote_frame(0xB750) * 3)
    assert hass.states.get(valve.switch_id).state == STATE_UNAVAILABLE
    hass.config_entries.async_update_entry(
        valve.entry, data={**valve.entry.data, CONF_RECEIVER: "hackrf_proxy:missing"}
    )
    assert await hass.config_entries.async_setup(valve.entry.entry_id)
    await hass.async_block_till_done()
    await turn(hass, valve, "turn_off")
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert len(transmitter.commands) == 1


async def test_reconfigure_receiver_preserves_existing_valve(
    hass, transmitter, receiver, make_entry, setup_valve
):
    from homeassistant.config_entries import SOURCE_RECONFIGURE

    valve = await setup_valve(options={CONF_DURATION: 10})
    await turn(hass, valve, "turn_on")
    expected = deadline(hass, valve)
    for selected in (f"hackrf_proxy:{receiver.entry_id}", "none"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": valve.entry.entry_id},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"transmitter": transmitter.entity_id, CONF_RECEIVER: selected},
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert hass.states.get(valve.switch_id).state == STATE_ON
        assert deadline(hass, valve) == expected
        assert valve.entry.unique_id == "b750"
        assert valve.entry.options[CONF_DURATION] == 10
        assert len(transmitter.commands) == 1
    assert not hass.data[DATA_DISPATCHER].get(SIGNAL_RX_FRAME.format(receiver.entry_id))


async def test_receiver_disconnect_does_not_disable_controls_or_timer(
    hass, transmitter, receiver, make_entry, setup_valve, freezer
):
    from homeassistant.config_entries import ConfigEntryState

    valve = await received_valve(hass, receiver, make_entry, setup_valve)
    await emit(hass, receiver, remote_frame(0xB752) * 3)
    expected = deadline(hass, valve)
    receiver.mock_state(hass, ConfigEntryState.NOT_LOADED)
    await hass.async_block_till_done()
    await emit(hass, receiver, remote_frame(0xB750) * 3)
    assert hass.states.get(valve.switch_id).state == STATE_ON
    assert deadline(hass, valve) == expected
    await advance(hass, freezer, expected + timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    await turn(hass, valve, "turn_on")
    receiver.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()
    await emit(hass, receiver, remote_frame(0xB750) * 3)
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert len(transmitter.commands) == 1


async def test_failed_send_echo_cannot_claim_success(
    hass, transmitter, receiver, make_entry, setup_valve, monkeypatch
):
    valve = await received_valve(hass, receiver, make_entry, setup_valve)

    async def failed(command):
        await emit(hass, receiver, command.get_raw_timings() * 3)
        raise HomeAssistantError("Partial transmission")

    monkeypatch.setattr(transmitter, "async_send_command", failed)
    with pytest.raises(HomeAssistantError, match="Partial transmission"):
        await turn(hass, valve, "turn_on")
    await emit(hass, receiver, remote_frame(0xB75E) * 3)
    state = hass.states.get(valve.switch_id)
    assert state.state == STATE_OFF
    assert state.attributes["state_source"] == "transmission_error"
    assert deadline(hass, valve) is None
