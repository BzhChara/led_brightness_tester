import pytest

from led_debugger.waveform_protocol import FRAME_TAIL, RESPONSE_HEADER, ResponseParser, collect_command, light_command


def response(command, channel=0, value=1, sequence=None):
    payload = value.to_bytes(2, "big")
    if sequence is not None:
        payload = sequence.to_bytes(2, "big") + payload
    return RESPONSE_HEADER + bytes((command, channel)) + payload + FRAME_TAIL


def test_commands_match_android_protocol():
    assert light_command(0, True).hex() == "aa000130000001cc33c33c"
    assert light_command(1, False).hex() == "aa000130010000cc33c33c"
    assert collect_command(0, 15, 206).hex() == "aa00015000000f00cecc33c33c"


@pytest.mark.parametrize("period,count", [(0, 1), (1, 0), (65536, 1), (1, 65536)])
def test_invalid_sampling_parameters_rejected(period, count):
    with pytest.raises(ValueError):
        collect_command(0, period, count)


def test_every_possible_stream_split_preserves_values_and_both_end_markers():
    stream = response(0x30) + response(0x40, value=32768, sequence=0) + response(0x40, value=65535, sequence=1) + response(0x50)
    for split in range(len(stream) + 1):
        parser = ResponseParser()
        frames = parser.feed(stream[:split]) + parser.feed(stream[split:])
        assert len(frames) == 4
        assert frames[1].value == 32768
        assert frames[2].sequence == 1
        assert frames[-1].is_end
    parser = ResponseParser()
    assert parser.feed(response(0x40))[0].is_end


def test_byte_by_byte_noise_echo_and_header_prefix():
    parser = ResponseParser()
    frames = []
    stream = b"junk" + light_command(0, True) + response(0x40, value=0xAA01, sequence=0x00AA)
    for byte in stream:
        frames.extend(parser.feed(bytes((byte,))))
    assert len(frames) == 1
    assert frames[0].value == 0xAA01
    assert parser.discarded_bytes > 0


@pytest.mark.parametrize("tail_length", [0, 1, 2, 3])
def test_complete_payload_with_truncated_tail_is_recovered(tail_length):
    parser = ResponseParser()
    raw = response(0x40, value=32770, sequence=8)
    frames = parser.feed(raw[:9 + tail_length] + response(0x50))
    assert len(frames) == 2
    assert frames[0].recovered
    assert frames[0].value == 32770


def test_incomplete_payload_is_never_invented():
    parser = ResponseParser()
    frames = parser.feed(response(0x40, value=32770, sequence=8)[:8] + response(0x50))
    assert len(frames) == 1
    assert frames[0].is_end


def test_invalid_tail_does_not_publish_a_value():
    parser = ResponseParser()
    frames = parser.feed(response(0x40, value=32770, sequence=8)[:-1] + b"\x00" + response(0x50))
    assert len(frames) == 1 and frames[0].is_end


def test_header_inside_payload_survives_every_split_and_truncated_tail():
    sample = response(0x40, sequence=0xAA01, value=0)
    for split in range(len(sample) + 1):
        parser = ResponseParser()
        frames = parser.feed(sample[:split]) + parser.feed(sample[split:])
        assert len(frames) == 1
        assert frames[0].sequence == 0xAA01 and frames[0].value == 0
    parser = ResponseParser()
    frames = parser.feed(sample[:9] + response(0x50))
    assert len(frames) == 2 and frames[0].recovered
    assert frames[0].sequence == 0xAA01
