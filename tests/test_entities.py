"""Exercise bounded watering, persisted settings, and restart behavior via HA."""

import asyncio
from datetime import timedelta

import pytest
from homeassistant.const import (
    ATTR_ASSUMED_STATE,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)
from rf_protocols import ModulationType, RadioFrequencyCommand

from custom_components.harf_insoma.const import CONF_DURATION, CONF_TRANSMITTER, DOMAIN

from .conftest import RecordingTransmitter, Valve

pytestmark = pytest.mark.asyncio


def restore_cache(hass: HomeAssistant, states: list[State]) -> None:
    """Adapt the test plugin's restore cache to HA 2026.11's registry index."""
    mock_restore_cache(hass, states)
    data = restore_state.async_get(hass)
    if hasattr(data, "last_states_by_entity_registry_id"):
        registry = er.async_get(hass)
        for entity_id, stored in data.last_states.items():
            data.last_states_by_entity_registry_id[registry.async_get(entity_id).id] = (
                stored
            )


async def turn(hass: HomeAssistant, valve: Valve, action: str) -> None:
    """Call the same switch service that the dashboard uses."""
    await hass.services.async_call(
        "switch", action, {"entity_id": valve.switch_id}, blocking=True
    )
    await hass.async_block_till_done()


async def select_duration(hass: HomeAssistant, valve: Valve, minutes: str) -> None:
    """Change the locally stored setting through its public select service."""
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": valve.duration_id, "option": minutes},
        blocking=True,
    )
    await hass.async_block_till_done()


def command_word(command: RadioFrequencyCommand) -> int:
    """Decode the emitted waveform independently of the encoder implementation."""
    assert command.frequency == 433_926_000
    assert command.modulation is ModulationType.OOK
    assert command.repeat_count == 179
    timings = command.get_raw_timings()
    assert len(timings) == 34
    assert timings[-2:] == [300, -2132]
    word = 0
    for offset in range(0, 32, 2):
        pair = timings[offset : offset + 2]
        assert pair in ([320, -592], [620, -292])
        word = (word << 1) | (pair == [620, -292])
    return word


def deadline(hass: HomeAssistant, valve: Valve):
    """Return the estimated hardware shutoff deadline advertised by the switch."""
    state = hass.states.get(valve.switch_id)
    value = state.attributes.get("auto_off_at")
    return dt_util.parse_datetime(value) if value is not None else None


async def advance(hass: HomeAssistant, freezer, when) -> None:
    """Advance wall time and execute due HA timers without waiting in real time."""
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


async def test_initial_entities_and_persisted_setting(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve
):
    """Setup is silent, state is honestly unknown, and duration is local configuration."""
    valve = await setup_valve()
    state = hass.states.get(valve.switch_id)
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_ASSUMED_STATE] is True
    assert deadline(hass, valve) is None
    duration = hass.states.get(valve.duration_id)
    assert duration.state == "120"
    assert duration.attributes["options"] == ["5", "10", "30", "60", "90", "120"]
    registry = er.async_get(hass)
    switch_entry = registry.async_get(valve.switch_id)
    duration_entry = registry.async_get(valve.duration_id)
    assert duration_entry.entity_category is EntityCategory.CONFIG
    assert duration_entry.device_id == switch_entry.device_id
    assert transmitter.commands == []

    await select_duration(hass, valve, "30")
    assert valve.entry.options[CONF_DURATION] == 30
    assert hass.states.get(valve.duration_id).state == "30"
    assert transmitter.commands == []

    assert await hass.config_entries.async_reload(valve.entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(valve.duration_id).state == "30"
    assert transmitter.commands == []


@pytest.mark.parametrize(
    ("minutes", "word"),
    [
        ("5", 0xB752),
        ("10", 0xB754),
        ("30", 0xB756),
        ("60", 0xB758),
        ("90", 0xB75A),
        ("120", 0xB75E),
    ],
)
async def test_on_always_sends_selected_timed_command(
    hass: HomeAssistant,
    transmitter: RecordingTransmitter,
    setup_valve,
    freezer,
    minutes,
    word,
):
    """Every UI choice emits the matching hardware timer; permanent ON is excluded."""
    valve = await setup_valve()
    await select_duration(hass, valve, minutes)
    started = dt_util.utcnow()
    await turn(hass, valve, "turn_on")
    assert len(transmitter.commands) == 1
    assert command_word(transmitter.commands[0]) == word
    assert word & 0xF != 0xC
    assert hass.states.get(valve.switch_id).state == STATE_ON
    assert deadline(hass, valve) == started + timedelta(minutes=int(minutes))


async def test_default_timer_updates_state_without_sending_off(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve, freezer
):
    """The valve's timer supplies safety; HA only estimates state at the deadline."""
    valve = await setup_valve()
    started = dt_util.utcnow()
    await turn(hass, valve, "turn_on")
    assert command_word(transmitter.commands[0]) == 0xB75E
    expected = started + timedelta(minutes=120)
    assert deadline(hass, valve) == expected
    await advance(hass, freezer, expected - timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_ON
    await advance(hass, freezer, expected + timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert deadline(hass, valve) is None
    assert len(transmitter.commands) == 1


async def test_explicit_off_sends_off_and_cancels_estimate(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve, freezer
):
    """OFF transmits the captured stop command and clears the pending deadline."""
    valve = await setup_valve()
    await turn(hass, valve, "turn_on")
    previous_deadline = deadline(hass, valve)
    await turn(hass, valve, "turn_off")
    assert [command_word(command) for command in transmitter.commands] == [
        0xB75E,
        0xB750,
    ]
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert deadline(hass, valve) is None
    await advance(hass, freezer, previous_deadline + timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert len(transmitter.commands) == 2


async def test_setting_during_watering_only_changes_next_run(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve, freezer
):
    """Changing the setting must neither send RF nor rewrite the active timer."""
    valve = await setup_valve()
    await turn(hass, valve, "turn_on")
    active_deadline = deadline(hass, valve)
    await select_duration(hass, valve, "5")
    assert valve.entry.options[CONF_DURATION] == 5
    assert hass.states.get(valve.switch_id).state == STATE_ON
    assert deadline(hass, valve) == active_deadline
    assert len(transmitter.commands) == 1
    await advance(hass, freezer, dt_util.utcnow() + timedelta(minutes=6))
    assert hass.states.get(valve.switch_id).state == STATE_ON
    await advance(hass, freezer, active_deadline + timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert len(transmitter.commands) == 1


async def test_repeated_on_reschedules_the_timer(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve, freezer
):
    """Sending another timed ON replaces the previous deadline."""
    valve = await setup_valve(options={CONF_DURATION: 5})
    started = dt_util.utcnow()
    await turn(hass, valve, "turn_on")
    await advance(hass, freezer, started + timedelta(minutes=2))
    await turn(hass, valve, "turn_on")
    assert deadline(hass, valve) == started + timedelta(minutes=7)
    await advance(hass, freezer, started + timedelta(minutes=5, seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_ON
    await advance(hass, freezer, started + timedelta(minutes=7, seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert [command_word(command) for command in transmitter.commands] == [
        0xB752,
        0xB752,
    ]


async def test_failed_first_on_preserves_unknown_state(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve
):
    """A transport error is reported without claiming that watering started."""
    valve = await setup_valve()
    transmitter.failure = HomeAssistantError("Radio failed")
    with pytest.raises(HomeAssistantError, match="Radio failed"):
        await turn(hass, valve, "turn_on")
    assert hass.states.get(valve.switch_id).state == STATE_UNKNOWN
    assert deadline(hass, valve) is None
    assert transmitter.commands == []


@pytest.mark.parametrize("action", ["turn_on", "turn_off"])
async def test_failed_command_preserves_running_timer(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve, freezer, action
):
    """Failed ON or OFF cannot erase a previously successful timed command."""
    valve = await setup_valve(options={CONF_DURATION: 5})
    await turn(hass, valve, "turn_on")
    active_deadline = deadline(hass, valve)
    await advance(hass, freezer, dt_util.utcnow() + timedelta(minutes=1))
    transmitter.failure = HomeAssistantError("Radio failed")
    with pytest.raises(HomeAssistantError, match="Radio failed"):
        await turn(hass, valve, action)
    assert hass.states.get(valve.switch_id).state == STATE_ON
    assert deadline(hass, valve) == active_deadline
    await advance(hass, freezer, active_deadline + timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert len(transmitter.commands) == 1


async def test_local_setting_available_while_transmitter_is_unavailable(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve
):
    """Only the radio-dependent switch follows transmitter availability."""
    valve = await setup_valve()
    transmitter._attr_available = False
    transmitter.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(valve.switch_id).state == STATE_UNAVAILABLE
    assert hass.states.get(valve.duration_id).state == "120"
    await select_duration(hass, valve, "10")
    assert valve.entry.options[CONF_DURATION] == 10
    assert transmitter.commands == []
    transmitter._attr_available = True
    transmitter.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(valve.switch_id).state == STATE_UNKNOWN
    await turn(hass, valve, "turn_on")
    assert command_word(transmitter.commands[0]) == 0xB754


async def test_transmitter_uuid_resolves_after_rename_before_setup(
    hass: HomeAssistant, transmitter: RecordingTransmitter, make_entry, setup_valve
):
    """Resolve the saved registry UUID to the current ID when adding the switch."""
    entry = make_entry()
    registry = er.async_get(hass)
    stored_uuid = registry.async_get(transmitter.entity_id).id
    assert entry.data[CONF_TRANSMITTER] == stored_uuid
    registry.async_update_entity(
        transmitter.entity_id, new_entity_id="radio_frequency.before_setup"
    )
    await hass.async_block_till_done()
    transmitter._attr_available = False
    transmitter.async_write_ha_state()
    valve = await setup_valve(entry=entry)
    assert hass.states.get(valve.switch_id).state == STATE_UNAVAILABLE
    transmitter._attr_available = True
    transmitter.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(valve.switch_id).state == STATE_UNKNOWN
    await turn(hass, valve, "turn_on")
    assert command_word(transmitter.commands[0]) == 0xB75E
    assert entry.data[CONF_TRANSMITTER] == stored_uuid


async def test_transmitter_rename_keeps_uuid_binding(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve
):
    """Renaming the radio entity must not sever the valve's transmitter binding."""
    valve = await setup_valve()
    registry = er.async_get(hass)
    stored_uuid = valve.entry.data[CONF_TRANSMITTER]
    for new_id in ("radio_frequency.renamed", "radio_frequency.renamed_again"):
        old_id = transmitter.entity_id
        registry.async_update_entity(old_id, new_entity_id=new_id)
        await hass.async_block_till_done()
        assert hass.states.get(valve.switch_id).state != STATE_UNAVAILABLE
        await turn(hass, valve, "turn_on")
        assert command_word(transmitter.commands[-1]) == 0xB75E
        active_deadline = deadline(hass, valve)
        transmitter._attr_available = False
        transmitter.async_write_ha_state()
        await hass.async_block_till_done()
        assert hass.states.get(valve.switch_id).state == STATE_UNAVAILABLE
        transmitter._attr_available = True
        transmitter.async_write_ha_state()
        await hass.async_block_till_done()
        assert hass.states.get(valve.switch_id).state == STATE_ON
        assert deadline(hass, valve) == active_deadline
        # A state at the old ID must no longer influence the valve.
        hass.states.async_set(old_id, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        assert hass.states.get(valve.switch_id).state == STATE_ON
        assert valve.entry.data[CONF_TRANSMITTER] == stored_uuid
    await turn(hass, valve, "turn_off")
    assert command_word(transmitter.commands[-1]) == 0xB750


async def test_valve_rename_preserves_deadline_and_future_control(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve, freezer
):
    """Renaming the valve itself preserves its timer and keeps its services usable."""
    valve = await setup_valve(options={CONF_DURATION: 5})
    await turn(hass, valve, "turn_on")
    active_deadline = deadline(hass, valve)
    er.async_get(hass).async_update_entity(
        valve.switch_id, new_entity_id="switch.garden_valve"
    )
    valve.switch_id = "switch.garden_valve"
    await hass.async_block_till_done()
    assert hass.states.get(valve.switch_id).state == STATE_ON
    assert deadline(hass, valve) == active_deadline
    assert len(transmitter.commands) == 1
    await advance(hass, freezer, active_deadline + timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert len(transmitter.commands) == 1
    await turn(hass, valve, "turn_on")
    await turn(hass, valve, "turn_off")
    assert [command_word(command) for command in transmitter.commands] == [
        0xB752,
        0xB752,
        0xB750,
    ]


async def test_restore_running_deadline_without_transmission(
    hass: HomeAssistant,
    transmitter: RecordingTransmitter,
    make_entry,
    setup_valve,
    freezer,
):
    """Restart resumes only HA's local estimate and never restarts the hardware timer."""
    entry = make_entry()
    registered = er.async_get(hass).async_get_or_create(
        "switch", DOMAIN, "b750", suggested_object_id="test_valve", config_entry=entry
    )
    restored_deadline = dt_util.utcnow() + timedelta(minutes=17)
    restore_cache(
        hass,
        [
            State(
                registered.entity_id,
                STATE_ON,
                {"auto_off_at": restored_deadline.isoformat()},
            )
        ],
    )
    valve = await setup_valve(entry=entry)
    assert hass.states.get(valve.switch_id).state == STATE_ON
    assert deadline(hass, valve) == restored_deadline
    assert transmitter.commands == []
    await advance(hass, freezer, restored_deadline + timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_OFF
    assert transmitter.commands == []


@pytest.mark.parametrize(
    ("saved_state", "saved_deadline", "expected_state"),
    [
        (STATE_OFF, None, STATE_OFF),
        (STATE_ON, "2000-01-01T00:00:00+00:00", STATE_OFF),
        (STATE_ON, None, STATE_UNKNOWN),
        (STATE_ON, "invalid", STATE_UNKNOWN),
        (STATE_ON, "2026-99-01T00:00:00+00:00", STATE_UNKNOWN),
        (STATE_ON, "2099-01-01T00:00:00", STATE_UNKNOWN),
        (STATE_UNKNOWN, None, STATE_UNKNOWN),
    ],
)
async def test_restore_only_trusts_a_timed_on_state(
    hass: HomeAssistant,
    transmitter: RecordingTransmitter,
    make_entry,
    setup_valve,
    saved_state,
    saved_deadline,
    expected_state,
):
    """Missing or malformed timer data must not turn into an indefinite ON estimate."""
    entry = make_entry()
    registered = er.async_get(hass).async_get_or_create(
        "switch", DOMAIN, "b750", suggested_object_id="test_valve", config_entry=entry
    )
    attrs = {"auto_off_at": saved_deadline} if saved_deadline is not None else {}
    restore_cache(hass, [State(registered.entity_id, saved_state, attrs)])
    valve = await setup_valve(entry=entry)
    assert hass.states.get(valve.switch_id).state == expected_state
    assert deadline(hass, valve) is None
    assert transmitter.commands == []


async def test_unload_cancels_timer_and_does_not_send_rf(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve, freezer
):
    """Removing the integration neither transmits nor lets a stale callback recreate it."""
    valve = await setup_valve(options={CONF_DURATION: 5})
    await turn(hass, valve, "turn_on")
    active_deadline = deadline(hass, valve)
    assert await hass.config_entries.async_unload(valve.entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, active_deadline + timedelta(seconds=1))
    assert hass.states.get(valve.switch_id).state == STATE_UNAVAILABLE
    assert hass.states.get(valve.duration_id).state == STATE_UNAVAILABLE
    assert len(transmitter.commands) == 1


async def test_unload_during_transmission_cannot_recreate_entity_or_timer(
    hass: HomeAssistant,
    transmitter: RecordingTransmitter,
    setup_valve,
    freezer,
    monkeypatch,
):
    """A radio response arriving after removal cannot resurrect the removed valve."""
    valve = await setup_valve(options={CONF_DURATION: 5})
    transmission_started = asyncio.Event()
    finish_transmission = asyncio.Event()
    send_command = transmitter.async_send_command

    async def delayed_send(command):
        transmission_started.set()
        await finish_transmission.wait()
        await send_command(command)

    monkeypatch.setattr(transmitter, "async_send_command", delayed_send)
    request = asyncio.create_task(
        hass.services.async_call(
            "switch", "turn_on", {"entity_id": valve.switch_id}, blocking=True
        )
    )
    try:
        await asyncio.wait_for(transmission_started.wait(), timeout=1)
        assert await hass.config_entries.async_unload(valve.entry.entry_id)
    finally:
        finish_transmission.set()
        await request
    await hass.async_block_till_done()
    assert hass.states.get(valve.switch_id).state == STATE_UNAVAILABLE
    assert deadline(hass, valve) is None
    await advance(hass, freezer, dt_util.utcnow() + timedelta(minutes=6))
    assert hass.states.get(valve.switch_id).state == STATE_UNAVAILABLE
    assert len(transmitter.commands) == 1


async def test_two_valves_keep_independent_duration_and_deadline(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve, freezer
):
    """Shared radio hardware does not couple independent physical valve timers."""
    first = await setup_valve(options={CONF_DURATION: 5})
    second = await setup_valve(0x24E0, options={CONF_DURATION: 10})
    started = dt_util.utcnow()
    await turn(hass, first, "turn_on")
    await turn(hass, second, "turn_on")
    assert [command_word(command) for command in transmitter.commands] == [
        0xB752,
        0x24E4,
    ]
    await select_duration(hass, first, "30")
    assert second.entry.options[CONF_DURATION] == 10
    await advance(hass, freezer, started + timedelta(minutes=5, seconds=1))
    assert hass.states.get(first.switch_id).state == STATE_OFF
    assert hass.states.get(second.switch_id).state == STATE_ON
    await turn(hass, second, "turn_off")
    assert command_word(transmitter.commands[-1]) == 0x24E0


async def test_invalid_duration_cannot_be_selected(
    hass: HomeAssistant, transmitter: RecordingTransmitter, setup_valve
):
    """Arbitrary timer values cannot bypass the finite hardware timer choices."""
    valve = await setup_valve()
    with pytest.raises(HomeAssistantError):
        await select_duration(hass, valve, "0")
    assert hass.states.get(valve.duration_id).state == "120"
    assert transmitter.commands == []


@pytest.mark.parametrize("invalid_duration", [0, 999, True, "120"])
async def test_invalid_saved_duration_cannot_transmit(
    hass: HomeAssistant,
    transmitter: RecordingTransmitter,
    setup_valve,
    invalid_duration,
):
    """Corrupt or manually edited options cannot request indefinite watering."""
    valve = await setup_valve(options={CONF_DURATION: invalid_duration})
    with pytest.raises(HomeAssistantError):
        await turn(hass, valve, "turn_on")
    assert hass.states.get(valve.switch_id).state == STATE_UNKNOWN
    assert deadline(hass, valve) is None
    assert transmitter.commands == []
    await turn(hass, valve, "turn_off")
    assert command_word(transmitter.commands[0]) == 0xB750
    assert hass.states.get(valve.switch_id).state == STATE_OFF
