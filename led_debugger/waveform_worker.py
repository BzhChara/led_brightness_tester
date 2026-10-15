from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import serial
from PySide6.QtCore import QThread, Signal

from led_debugger.waveform_protocol import ResponseParser, collect_command, light_command
from led_debugger.serial_worker import RawSerialRecord


@dataclass(frozen=True)
class WaveformPoint:
    channel: int
    sequence: int
    elapsed_seconds: float
    received_timestamp: float
    raw_value: int

    @property
    def intensity(self) -> int:
        return self.raw_value - 0x8000


class WaveformWorker(QThread):
    """一次开灯、定长采集、关灯。所有退出路径均尝试关灯并确认 ACK。"""

    points_received = Signal(object)
    log_received = Signal(str)
    raw_response_received = Signal(int, object)
    status_changed = Signal(str)
    error_occurred = Signal(str)
    shutdown_result = Signal(bool)

    def __init__(self, port_name: str, channel: int, period_ms: int, point_count: int, parent=None) -> None:
        super().__init__(parent)
        collect_command(channel, period_ms, point_count)
        self.port_name = port_name
        self.channel = channel
        self.period_ms = period_ms
        self.point_count = point_count
        self._stop_requested = threading.Event()
        self.parser = ResponseParser()
        self._last_send = 0.0
        self.completed = False
        self.light_off_confirmed = False
        self.failure_message = ""
        self._opening_ack_pending = False
        self._last_raw_record: RawSerialRecord | None = None

    def stop(self) -> None:
        self._stop_requested.set()

    def _send(self, port, frame: bytes) -> None:
        self._last_raw_record = None
        delay = 0.003 - (time.monotonic() - self._last_send)
        if delay > 0:
            time.sleep(delay)
        written = port.write(frame)
        if written != len(frame):
            raise IOError("串口指令未完整发送")
        port.flush()
        self._last_send = time.monotonic()
        self.log_received.emit("TX " + frame.hex(" ").upper())

    def _read(self, port):
        pending = bytes(self.parser.buffer)
        data = port.read(max(1, min(getattr(port, "in_waiting", 0), 4096)))
        if data:
            self.log_received.emit("RX " + data[:160].hex(" ").upper() + (f" …共{len(data)}字节" if len(data) > 160 else ""))
        frames = self.parser.feed(data)
        if data:
            status = f"解析成功：{len(frames)} 帧" if frames else "接收中：等待完整帧或有效响应"
            if any(frame.recovered for frame in frames):
                status += "，含截断帧尾恢复"
            channel = self.channel + 1
            self._last_raw_record = RawSerialRecord(
                channel, time.time(), status, pending + bytes(data),
                tuple(frame.raw_bytes for frame in frames),
            )
            self.raw_response_received.emit(channel, self._last_raw_record)
        if any(frame.recovered for frame in frames):
            self.log_received.emit("已按完整载荷和下一帧头恢复截断帧尾")
        return frames

    def _report_failure(self, message: str) -> None:
        """与亮度检测一致，失败时也提供时间、状态、长度和原始RX。"""
        self.failure_message = message
        self.error_occurred.emit(message)
        previous = self._last_raw_record
        channel = self.channel + 1
        self.raw_response_received.emit(channel, RawSerialRecord(
            channel, time.time(), message,
            previous.raw_bytes if previous is not None else b"",
            previous.response_frames if previous is not None else (),
        ))

    def _wait_light_ack(self, port, *, opening: bool) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            for frame in self._read(port):
                if frame.command == 0x30 and frame.channel == self.channel:
                    if opening:
                        self._opening_ack_pending = False
                    elif self._opening_ack_pending:
                        # 协议没有请求ID。开灯确认未被消费时，下一条30响应
                        # 归属仍不确定，不能拿来证明关灯成功。
                        self._opening_ack_pending = False
                        self.log_received.emit("先消费尚未确认的开灯响应，继续等待关灯确认")
                        continue
                    if frame.value == 0xFFFF:
                        raise IOError("设备拒绝灯光指令")
                    return
        raise TimeoutError("灯光指令未收到确认")

    def _collect(self, port) -> None:
        self._send(port, collect_command(self.channel, self.period_ms, self.point_count))
        self.status_changed.emit(f"状态：采集中，0 / {self.point_count} 点")
        deadline = time.monotonic() + self.period_ms * self.point_count / 1000 + 3.0
        first_sequence = None
        last_index = -1
        received_count = 0
        while not self._stop_requested.is_set():
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待采集结束超时，收到 {received_count} / {self.point_count} 点")
            points = []
            ended = False
            failure = ""
            for frame in self._read(port):
                if frame.channel != self.channel:
                    continue
                if frame.is_sample:
                    if first_sequence is None:
                        first_sequence = frame.sequence
                    index = (frame.sequence - first_sequence) & 0xFFFF
                    if index <= last_index:
                        failure = f"采样序号重复或倒退：{frame.sequence}"
                        break
                    if index != last_index + 1:
                        failure = f"采样序号不连续：{frame.sequence}，存在丢点"
                        break
                    last_index = index
                    received_count += 1
                    points.append(WaveformPoint(self.channel, frame.sequence, index * self.period_ms / 1000, time.time(), frame.value))
                elif frame.is_end:
                    ended = True
                elif frame.command == 0x50 and frame.value == 0xFFFF:
                    failure = "设备拒绝采集指令"
                    break
            if points:
                self.points_received.emit(points)
                self.status_changed.emit(f"状态：采集中，{received_count} / {self.point_count} 点")
            if failure:
                raise IOError(failure)
            if received_count > self.point_count:
                raise IOError("收到的点数超过请求点数")
            if ended:
                if received_count != self.point_count:
                    raise IOError(f"采集提前结束，收到 {received_count} / {self.point_count} 点")
                self.completed = True
                return

    def run(self) -> None:
        if self._stop_requested.is_set():
            return
        try:
            with serial.Serial(self.port_name, 115200, timeout=0.05, write_timeout=0.5) as port:
                port.reset_input_buffer()
                try:
                    self.status_changed.emit("状态：正在开灯，等待确认")
                    self._opening_ack_pending = True
                    self._send(port, light_command(self.channel, True))
                    self._wait_light_ack(port, opening=True)
                    if not self._stop_requested.is_set():
                        self._collect(port)
                except Exception as exc:
                    self._report_failure(str(exc))
                finally:
                    self.status_changed.emit("状态：正在关灯，等待确认")
                    try:
                        self._send(port, light_command(self.channel, False))
                        self._wait_light_ack(port, opening=False)
                        self.light_off_confirmed = True
                    except Exception as exc:
                        prefix = self.failure_message + "；" if self.failure_message else ""
                        self._report_failure(prefix + f"关灯未确认：{exc}，请检查硬件光源")
                    self.shutdown_result.emit(self.light_off_confirmed)
        except Exception as exc:
            self._report_failure(f"串口异常：{exc}")
