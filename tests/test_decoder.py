"""Independent OEM-style captures exercise validation and burst boundaries."""

import pytest

from custom_components.harf_insoma.protocol import Frame, FrameStream, decode_frame


def remote_frame(word: int) -> list[int]:
    """Build measured OEM pulses independently of the transmit encoder."""
    pulses = []
    for bit in range(15, -1, -1):
        pulses.extend((638, -274) if word & (1 << bit) else (334, -578))
    return pulses + [314, -2116]


@pytest.mark.parametrize("command", range(0, 16, 2))
@pytest.mark.parametrize("address", [0, 0xB750, 0xFFF0])
def test_all_remote_buttons(command, address):
    frame = decode_frame(remote_frame(address | command))
    assert frame == Frame(address | command)
    assert frame.off_code == address
    assert frame.command == command


@pytest.mark.parametrize("scale", [0.85, 1, 1.12])
def test_tolerance_and_split_stream(scale):
    burst = [round(t * scale) for t in remote_frame(0xB75C)] * 6
    for split in range(1, len(burst)):
        decoder = FrameStream()
        frames = decoder.feed(burst[:split], now=0)
        frames += decoder.feed(burst[split:], now=0.01)
        assert frames == [Frame(0xB75C)]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p[:-1],  # Missing sync
        lambda p: p[2:],  # Partial frame
        lambda p: [320, -592] + p,  # Extra bit
        lambda p: p[:-2] + [620, -2116],  # Wrong sync mark
        lambda p: p[:-1] + [-1000],  # Wrong sync gap
        lambda p: [450] + p[1:],  # Ambiguous mark
        lambda p: p[:1] + [-100] + p[2:],  # Invalid space
        lambda p: [True] + p[1:],
        lambda p: [float("nan")] + p[1:],
        lambda p: ["320"] + p[1:],
    ],
)
def test_reject_bad_frames(mutation):
    assert decode_frame(mutation(remote_frame(0xB752))) is None


@pytest.mark.parametrize("command", range(1, 16, 2))
def test_reject_unrecognized_commands(command):
    assert decode_frame(remote_frame(0xB750 | command)) is None


def test_require_consistent_frames_and_deduplicate():
    decoder = FrameStream()
    first, second = remote_frame(0xB752), remote_frame(0x24E2)
    assert decoder.feed(first * 2, now=0) == []
    assert decoder.feed(second * 2, now=0.01) == []
    assert decoder.feed(first * 2, now=0.02) == []
    assert decoder.feed(first * 180, now=0.03) == [Frame(0xB752)]
    assert decoder.feed(first * 180, now=0.1) == []
    assert decoder.feed(first * 3, now=0.5) == [Frame(0xB752)]


def test_noise_disconnect_and_stale_fragments_reset_confirmation():
    decoder = FrameStream()
    frame = remote_frame(0xB752)
    for interruption in ([1, -2116], [], [320, -592] * 10000 + [300, -2116]):
        decoder.reset()
        assert decoder.feed(frame * 2, now=0) == []
        assert decoder.feed(interruption, now=0.01) == []
        assert decoder.feed(frame, now=0.02) == []
        assert decoder.feed(frame * 2, now=0.03) == [Frame(0xB752)]
    decoder.reset()
    assert decoder.feed(frame[:17], now=0) == []
    assert decoder.feed(frame[17:] + frame * 2, now=1) == []
    assert decoder.feed(frame, now=1.01) == [Frame(0xB752)]
