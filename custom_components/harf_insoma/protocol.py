"""Encode the Insoma remote's OFF and valve-enforced timed ON commands."""

from dataclasses import dataclass
from time import monotonic
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
COMMAND_DURATIONS: Final = {
    command: minutes for minutes, command in _TIMED_COMMANDS.items()
}
PRESS_TIMEOUT: Final = 0.3
CONFIRM_FRAMES: Final = 3


@dataclass(frozen=True)
class Frame:
    """A recognized remote command, not an acknowledgment from the valve."""

    word: int

    @property
    def off_code(self) -> int:
        return self.word & 0xFFF0

    @property
    def command(self) -> int:
        return self.word & 0xF


def decode_frame(timings: list[int]) -> Frame | None:
    """Validate a complete MSB-first PWM frame including its final sync pair."""
    if len(timings) != 34 or any(type(t) is not int for t in timings):
        return None
    if not 225 <= timings[-2] <= 375 or not -2600 <= timings[-1] <= -1700:
        return None
    word = 0
    for high, low in zip(timings[:32:2], timings[1:32:2], strict=True):
        # Nonoverlapping +/-25% symbol windows; +/-20% on total bit duration.
        if not 730 <= high - low <= 1094:
            return None
        if 240 <= high <= 400 and -740 <= low <= -444:
            bit = 0
        elif 468 <= high <= 780 and -365 <= low <= -219:
            bit = 1
        else:
            return None
        word = (word << 1) | bit
    if word & 0xF not in (0, 2, 4, 6, 8, 0xA, 0xC, 0xE):
        return None
    return Frame(word)


class FrameStream:
    """Bounded split-stream decoder confirming and deduplicating button bursts.

    Three consecutive identical valid frames are required. A malformed frame or
    another word breaks confirmation. A silence of 300 ms starts a new press.
    Empty input signals a receiver disconnect and discards all partial state.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._timings: list[int] = []
        self._overflow = False
        self._candidate: Frame | None = None
        self._count = 0
        self._last_rx: float | None = None

    def feed(self, timings: list[int], *, now: float | None = None) -> list[Frame]:
        now = monotonic() if now is None else now
        if not timings or (
            self._last_rx is not None and now - self._last_rx >= PRESS_TIMEOUT
        ):
            self.reset()
        self._last_rx = now
        frames = []
        for timing in timings:
            if type(timing) is not int:
                self._overflow = True
                self._candidate = None
                self._count = 0
                continue
            if len(self._timings) < 34:
                self._timings.append(timing)
            else:
                self._overflow = True
            if timing > -1500:
                continue
            frame = None if self._overflow else decode_frame(self._timings)
            self._timings = []
            self._overflow = False
            if frame is None or frame != self._candidate:
                self._candidate = frame
                self._count = 0
            if frame is not None:
                self._count = min(self._count + 1, CONFIRM_FRAMES + 1)
                if self._count == CONFIRM_FRAMES:
                    frames.append(frame)
        return frames


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
