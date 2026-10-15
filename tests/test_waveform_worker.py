import pytest

from led_debugger.waveform_protocol import collect_command, light_command
from led_debugger.waveform_worker import WaveformWorker
from test_waveform_protocol import response


class FakePort:
    def __init__(self, *, sequences=(0, 1, 2), end_command=0x50, on_ack=True, off_ack=True):
        self.incoming = bytearray()
        self.writes = []
        self.sequences = sequences
        self.end_command = end_command
        self.on_ack = on_ack
        self.off_ack = off_ack
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    @property
    def in_waiting(self):
        return len(self.incoming)

    def write(self, frame):
        self.writes.append(frame)
        if frame[3] == 0x30:
            enabled = frame[6] == 1
            if (enabled and self.on_ack) or (not enabled and self.off_ack):
                self.incoming.extend(response(0x30, channel=frame[4]))
        elif frame[3] == 0x50:
            for i, sequence in enumerate(self.sequences):
                self.incoming.extend(response(0x40, channel=frame[4], value=32768 + i, sequence=sequence))
            if self.end_command is not None:
                self.incoming.extend(response(self.end_command, channel=frame[4]))
        return len(frame)

    def read(self, length):
        result = bytes(self.incoming[:length])
        del self.incoming[:length]
        return result

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.incoming.clear()


@pytest.mark.parametrize("end_command", [0x40, 0x50])
def test_one_start_sends_exactly_one_batch_then_turns_light_off(monkeypatch, end_command):
    port = FakePort(end_command=end_command)
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker = WaveformWorker("TEST", 0, 15, 3)
    points = []
    worker.points_received.connect(points.extend)
    worker.run()
    assert worker.completed and worker.light_off_confirmed
    assert port.writes == [light_command(0, True), collect_command(0, 15, 3), light_command(0, False)]
    assert [point.elapsed_seconds for point in points] == [0, 0.015, 0.03]
    assert [point.intensity for point in points] == [0, 1, 2]
    assert port.closed


def test_sequence_wrap_is_preserved(monkeypatch):
    port = FakePort(sequences=(65534, 65535, 0))
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker = WaveformWorker("TEST", 0, 15, 3)
    worker.run()
    assert worker.completed


def test_missing_points_are_reported_and_light_is_still_closed(monkeypatch):
    port = FakePort(sequences=(0, 1))
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker = WaveformWorker("TEST", 0, 15, 3)
    worker.run()
    assert not worker.completed
    assert "2 / 3" in worker.failure_message
    assert worker.light_off_confirmed and port.closed


def test_manual_stop_before_thread_starts_does_not_open_port(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("不应打开串口")
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", forbidden)
    worker = WaveformWorker("TEST", 0, 15, 3)
    worker.stop()
    worker.run()
    assert not worker.completed


def test_manual_stop_during_samples_turns_light_off(monkeypatch):
    port = FakePort(end_command=None)
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker = WaveformWorker("TEST", 0, 15, 3)
    worker.points_received.connect(lambda points: worker.stop())
    worker.run()
    assert not worker.completed and worker.light_off_confirmed
    assert port.writes[-1] == light_command(0, False)


def test_open_light_timeout_still_attempts_off(monkeypatch):
    port = FakePort(on_ack=False)
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker = WaveformWorker("TEST", 0, 15, 3)
    original_read = worker._read
    # 将启动 ACK 超时推进，不改变关灯 ACK 路径。
    clock_value = [0.0]
    def read_and_advance(current_port):
        clock_value[0] += 1.1
        return original_read(current_port)
    monkeypatch.setattr("led_debugger.waveform_worker.time.monotonic", lambda: clock_value[0])
    worker._read = read_and_advance
    worker.run()
    assert "未收到确认" in worker.failure_message
    # 唯一收到的30响应可能是迟到的开灯ACK，不能认定关灯已确认。
    assert not worker.light_off_confirmed
    assert port.writes == [light_command(0, True), light_command(0, False)]


def test_off_timeout_is_not_reported_as_success(monkeypatch):
    port = FakePort(off_ack=False)
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker = WaveformWorker("TEST", 0, 15, 3)
    original_read = worker._read
    clock_value = [0.0]
    def read_and_advance(current_port):
        clock_value[0] += 0.7
        return original_read(current_port)
    monkeypatch.setattr("led_debugger.waveform_worker.time.monotonic", lambda: clock_value[0])
    worker._read = read_and_advance
    worker.run()
    assert worker.completed and not worker.light_off_confirmed
    assert "关灯未确认" in worker.failure_message


def test_gap_preserves_valid_points_before_failure(monkeypatch):
    port = FakePort(sequences=(0, 2, 3))
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker = WaveformWorker("TEST", 0, 15, 3)
    points = []
    worker.points_received.connect(points.extend)
    worker.run()
    assert "丢点" in worker.failure_message
    assert [point.sequence for point in points] == [0]
    assert worker.light_off_confirmed


def test_late_open_ack_cannot_hide_off_rejection(monkeypatch):
    worker = WaveformWorker("TEST", 0, 15, 3)
    class LateAckPort(FakePort):
        def write(self, frame):
            self.writes.append(frame)
            if frame[3] == 0x30 and frame[6] == 1:
                worker.stop()
            elif frame[3] == 0x30:
                self.incoming.extend(response(0x30) + response(0x30, value=0xFFFF))
            return len(frame)
    port = LateAckPort()
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    original_read = worker._read
    clock_value = [0.0]
    def read_and_advance(current_port):
        clock_value[0] += 0.7
        return original_read(current_port)
    monkeypatch.setattr("led_debugger.waveform_worker.time.monotonic", lambda: clock_value[0])
    worker._read = read_and_advance
    worker.run()
    assert not worker.light_off_confirmed
    assert "设备拒绝灯光指令" in worker.failure_message
    assert port.writes == [light_command(0, True), light_command(0, False)]


def test_stop_waits_for_inflight_open_ack_then_closes(monkeypatch):
    worker = WaveformWorker("TEST", 0, 15, 3)
    class StopPort(FakePort):
        def write(self, frame):
            result = super().write(frame)
            if frame[3] == 0x30 and frame[6] == 1:
                worker.stop()
            return result
    port = StopPort()
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker.run()
    assert not worker.completed and worker.light_off_confirmed
    assert port.writes == [light_command(0, True), light_command(0, False)]


def test_raw_response_signal_preserves_every_received_byte(monkeypatch):
    port = FakePort(sequences=range(30))
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker = WaveformWorker("TEST", 1, 15, 30)
    records = []
    worker.raw_response_received.connect(lambda channel, record: records.append((channel, record)))
    worker.run()
    expected = b"".join(response(0x40, channel=1, value=32768 + i, sequence=i) for i in range(30)) + response(0x50, channel=1)
    assert worker.completed and worker.light_off_confirmed
    assert any(record.raw_bytes == expected and channel == record.channel == 2 for channel, record in records)
    assert len(expected) > 160


def test_raw_response_reassembles_split_frame_for_separate_display_rows():
    worker = WaveformWorker("TEST", 0, 15, 2)
    first = response(0x40, sequence=0xAA01, value=0)
    second = response(0x40, sequence=22, value=32768)
    class ChunkPort:
        def __init__(self):
            self.chunks = [first[:8], first[8:] + second]
        @property
        def in_waiting(self):
            return len(self.chunks[0])
        def read(self, length):
            return self.chunks.pop(0)
    port = ChunkPort()
    records = []
    worker.raw_response_received.connect(lambda channel, record: records.append(record))
    worker._read(port)
    worker._read(port)
    assert records[0].raw_bytes == first[:8]
    assert records[1].raw_bytes == first + second
    assert records[1].response_frames == (first, second)


def test_serial_open_failure_emits_empty_raw_error_record(monkeypatch):
    def fail_to_open(*args, **kwargs):
        raise PermissionError("串口已被占用")
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", fail_to_open)
    worker = WaveformWorker("TEST", 1, 15, 3)
    records = []
    errors = []
    worker.raw_response_received.connect(lambda channel, record: records.append((channel, record)))
    worker.error_occurred.connect(errors.append)
    worker.run()
    assert errors == ["串口异常：串口已被占用"]
    channel, record = records[-1]
    assert channel == record.channel == 2
    assert record.timestamp > 0
    assert record.status == worker.failure_message == errors[0]
    assert record.raw_bytes == b"" and record.response_frames == ()


def test_failure_preserves_received_frame_boundaries_before_shutdown(monkeypatch):
    port = FakePort(sequences=(0, 2))
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker = WaveformWorker("TEST", 0, 15, 3)
    records = []
    worker.raw_response_received.connect(lambda channel, record: records.append(record))
    worker.run()
    error_record = next(record for record in records if "丢点" in record.status)
    expected_frames = (response(0x40, value=32768, sequence=0), response(0x40, value=32769, sequence=2), response(0x50))
    assert error_record.raw_bytes == b"".join(expected_frames)
    assert error_record.response_frames == expected_frames
    assert worker.light_off_confirmed


def test_missing_shutdown_response_does_not_reuse_collection_bytes(monkeypatch):
    port = FakePort(off_ack=False)
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    worker = WaveformWorker("TEST", 0, 15, 3)
    records = []
    worker.raw_response_received.connect(lambda channel, record: records.append(record))
    original_read = worker._read
    current_time = [0.0]
    def advance_time(current_port):
        current_time[0] += 0.7
        return original_read(current_port)
    monkeypatch.setattr("led_debugger.waveform_worker.time.monotonic", lambda: current_time[0])
    worker._read = advance_time
    worker.run()
    assert worker.completed and not worker.light_off_confirmed
    assert "关灯未确认" in records[-1].status
    assert any(record.raw_bytes for record in records[:-1])
    assert records[-1].raw_bytes == b"" and records[-1].response_frames == ()
