"""Verify RF frames against both captured Insoma remotes."""

import pytest
from homeassistant.components.radio_frequency import (
    ModulationType,
    RadioFrequencyCommand,
)

from custom_components.harf_insoma.protocol import encode_command


def decode_frame(command: RadioFrequencyCommand) -> int:
    """Decode the independent pulse-width protocol from one complete frame."""
    timings = command.get_raw_timings()
    assert len(timings) == 34
    assert timings[-2:] == [300, -2132]
    code = 0
    for mark, space in zip(timings[:32:2], timings[1:32:2], strict=True):
        assert mark > 0 and space < 0
        assert mark - space == 912
        assert (mark, space) in ((320, -592), (620, -292))
        code = (code << 1) | (mark > -space)
    return code


@pytest.mark.parametrize(
    ("off_code", "duration", "captured_code"),
    [
        (0xB750, None, 0xB750),
        (0xB750, 5, 0xB752),
        (0xB750, 10, 0xB754),
        (0xB750, 30, 0xB756),
        (0xB750, 60, 0xB758),
        (0xB750, 90, 0xB75A),
        (0xB750, 120, 0xB75E),
        (0x24E0, None, 0x24E0),
        (0x24E0, 5, 0x24E2),
        (0x24E0, 10, 0x24E4),
        (0x24E0, 30, 0x24E6),
        (0x24E0, 60, 0x24E8),
        (0x24E0, 90, 0x24EA),
        (0x24E0, 120, 0x24EE),
    ],
)
def test_captured_remote_commands(
    off_code: int, duration: int | None, captured_code: int
) -> None:
    """Every supported action matches the exact captured 16-bit word."""
    command = encode_command(off_code, duration)
    assert isinstance(command, RadioFrequencyCommand)
    assert command.frequency == 433_926_000
    assert command.modulation == ModulationType.OOK
    assert decode_frame(command) == captured_code
    assert decode_frame(command) & 0xF != 0xC


@pytest.mark.parametrize("off_code", [0x0000, 0xB750, 0xFFF0])
@pytest.mark.parametrize("duration", [None, 5, 10, 30, 60, 90, 120])
def test_transmission_timing(off_code: int, duration: int | None) -> None:
    """Send 180 complete frames with only their built-in sync gaps."""
    command = encode_command(off_code, duration)
    timings = command.get_raw_timings()
    assert command.repeat_count + 1 == 180
    assert len(timings) == 34
    assert timings[-2:] == [300, -2132]
    assert sum(abs(timing) for timing in timings) == 17_024
    assert (
        sum(abs(timing) for timing in timings) * (command.repeat_count + 1) == 3_064_320
    )
    # No extra inter-frame gap is inserted into the RF command.
    assert timings[0] == (620 if off_code & 0x8000 else 320)
    assert command.symbol_rate is None
    assert command.output_power is None


@pytest.mark.parametrize("off_code", [0, 0x0010, 0xFFF0])
def test_complete_off_address_range(off_code: int) -> None:
    """Preserve the entire 12-bit address, including zero and maximum."""
    assert decode_frame(encode_command(off_code, None)) == off_code
    assert decode_frame(encode_command(off_code, 120)) == off_code | 0xE


@pytest.mark.parametrize(
    "off_code",
    [
        None,
        True,
        False,
        -16,
        0x10000,
        0xFFFF,
        0xB752,
        0xB75C,
        0x24EC,
        0xB750 + 0.0,
        "0xB750",
    ],
)
def test_invalid_off_code(off_code: object) -> None:
    """Reject wrong types, out-of-range words and non-OFF button codes."""
    with pytest.raises(ValueError, match="OFF code"):
        encode_command(off_code, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "duration",
    [
        True,
        False,
        -1,
        0,
        1,
        6,
        12,
        15,
        45,
        121,
        180,
        5.0,
        120.0,
        "120",
        "on",
        float("inf"),
        [],
        {},
    ],
)
def test_unsupported_duration(duration: object) -> None:
    """No coercion, arbitrary timer, or permanent-ON request is accepted."""
    with pytest.raises(ValueError, match="Duration"):
        encode_command(0xB750, duration)  # type: ignore[arg-type]


def test_returned_timings_do_not_mutate_command() -> None:
    """A transmitter cannot accidentally alter subsequent frame reads."""
    command = encode_command(0x24E0, 30)
    timings = command.get_raw_timings()
    timings.clear()
    assert decode_frame(command) == 0x24E6
