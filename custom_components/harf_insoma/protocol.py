"""Encode the Insoma remote's OFF and valve-enforced timed ON commands."""

from typing import Final

from homeassistant.components.radio_frequency import (
    ModulationType,
    RadioFrequencyCommand,
)

# SGW12-RX IQ capture: 433.92622 MHz, adjusted for a measured +230 Hz offset.
FREQUENCY_HZ: Final = 433_926_000
DURATION_MINUTES: Final = (5, 10, 30, 60, 90, 120)

# Captured from two remotes. The low nibble 0xC means permanent ON and is
# deliberately absent: every ON command must select the valve's own timer.
_TIMED_COMMANDS: Final = {5: 0x2, 10: 0x4, 30: 0x6, 60: 0x8, 90: 0xA, 120: 0xE}
_TOTAL_TRANSMISSIONS: Final = 180


class _InsomaCommand(RadioFrequencyCommand):
    """An encoded 16-bit Insoma frame, repeated by the RF transmitter."""

    def __init__(self, code: int) -> None:
        """Build an estimated SGW12-RX waveform for validation with another IQ capture."""
        super().__init__(
            frequency=FREQUENCY_HZ,
            modulation=ModulationType.OOK,
            # rf_protocols counts additional transmissions; ESPHome adds one.
            repeat_count=_TOTAL_TRANSMISSIONS - 1,
        )
        timings: list[int] = []
        # Compensate for measured RF HIGH pulses being about 14–16 µs longer
        # than programmed. These are estimates, not guaranteed RF envelope timings.
        for bit in range(15, -1, -1):
            timings.extend((620, -292) if code & (1 << bit) else (320, -592))
        # This sync space also separates frames. No additional gap is needed.
        timings.extend((300, -2132))
        self._timings = tuple(timings)

    def get_raw_timings(self) -> list[int]:
        """Return signed alternating mark/space durations in microseconds."""
        return list(self._timings)


def encode_command(
    off_code: int, duration_minutes: int | None
) -> RadioFrequencyCommand:
    """Encode OFF (None) or a supported timed ON for the captured remote.

    The complete OFF code must be a 16-bit integer whose last nibble is zero.
    Accepting a timed or permanent ON code as an address would hide a setup
    mistake, so addresses are validated rather than silently masked.
    """
    if (
        not isinstance(off_code, int)
        or isinstance(off_code, bool)
        or not 0 <= off_code <= 0xFFF0
        or off_code & 0xF
    ):
        raise ValueError("OFF code must be a 16-bit integer ending in hexadecimal 0")

    if duration_minutes is None:
        return _InsomaCommand(off_code)

    if (
        not isinstance(duration_minutes, int)
        or isinstance(duration_minutes, bool)
        or duration_minutes not in _TIMED_COMMANDS
    ):
        raise ValueError("Duration must be one of 5, 10, 30, 60, 90 or 120 minutes")

    return _InsomaCommand(off_code | _TIMED_COMMANDS[duration_minutes])
