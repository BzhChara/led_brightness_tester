"""仪器光源协议：与原有 A5 5A / lux 协议独立。"""
from __future__ import annotations

from dataclasses import dataclass

REQUEST_HEADER = b"\xaa\x00\x01"
RESPONSE_HEADER = b"\xaa\x01\x00"
FRAME_TAIL = b"\xcc\x33\xc3\x3c"


def build_command(command: int, channel: int, *values: int) -> bytes:
    if channel not in (0, 1):
        raise ValueError("通道目标必须为 0 或 1")
    if any(not 0 <= value <= 65535 for value in values):
        raise ValueError("参数超出 16 位无符号整数范围")
    return REQUEST_HEADER + bytes((command, channel)) + b"".join(
        value.to_bytes(2, "big") for value in values
    ) + FRAME_TAIL


def light_command(channel: int, enabled: bool) -> bytes:
    return build_command(0x30, channel, int(enabled))


def collect_command(channel: int, period_ms: int, point_count: int) -> bytes:
    if not 1 <= period_ms <= 65535 or not 1 <= point_count <= 65535:
        raise ValueError("采样周期和点数范围均为 1 到 65535")
    return build_command(0x50, channel, period_ms, point_count)


@dataclass(frozen=True)
class ResponseFrame:
    command: int
    channel: int
    value: int
    sequence: int | None = None
    raw_bytes: bytes = b""
    recovered: bool = False

    @property
    def is_sample(self) -> bool:
        return self.command == 0x40 and self.sequence is not None

    @property
    def is_end(self) -> bool:
        return self.command in (0x40, 0x50) and self.sequence is None and self.value == 1


class ResponseParser:
    """处理拆包、粘包；完整帧优先，完整载荷后出现下一帧才恢复截断帧。"""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.discarded_bytes = 0

    def feed(self, data: bytes) -> list[ResponseFrame]:
        self.buffer.extend(data)
        frames: list[ResponseFrame] = []
        while True:
            start = self.buffer.find(RESPONSE_HEADER)
            if start < 0:
                keep = 2 if self.buffer.endswith(RESPONSE_HEADER[:2]) else int(self.buffer.endswith(RESPONSE_HEADER[:1]))
                self.discarded_bytes += len(self.buffer) - keep
                self.buffer[:] = self.buffer[-keep:] if keep else b""
                break
            if start:
                self.discarded_bytes += start
                del self.buffer[:start]
            if len(self.buffer) < 7:
                break
            command = self.buffer[3]
            if command not in (0x30, 0x40, 0x50) or self.buffer[4] not in (0, 1):
                self.discarded_bytes += 1
                del self.buffer[0]
                continue
            short_end = command == 0x40 and self.buffer[5:7] == b"\x00\x01" and self.buffer[7:11] == FRAME_TAIL
            length = 13 if command == 0x40 and not short_end else 11
            payload_length = length - 4
            if len(self.buffer) >= length and self.buffer[payload_length:length] == FRAME_TAIL:
                raw = bytes(self.buffer[:length])
                del self.buffer[:length]
                frames.append(self._decode(raw, length, False))
                continue
            # 载荷本身也可能包含 AA 01 00；收到足够字节前不得提前重同步。
            if len(self.buffer) < length:
                break
            minimum_payload = 7 if command == 0x40 and self.buffer[5:7] == b"\x00\x01" else payload_length
            next_start = self.buffer.find(RESPONSE_HEADER, minimum_payload)
            if next_start < 0:
                next_start = self.buffer.find(RESPONSE_HEADER, 3)
            if next_start >= 0:
                raw = bytes(self.buffer[:next_start])
                # 0x40 短结束帧也允许同样的完整载荷恢复。
                possible_lengths = (11, 13) if command == 0x40 and raw[5:7] == b"\x00\x01" else (length,)
                recovered = False
                for candidate in possible_lengths:
                    payload = candidate - 4
                    if payload <= len(raw) < candidate and raw[payload:] == FRAME_TAIL[:len(raw) - payload]:
                        frames.append(self._decode(raw, candidate, True))
                        recovered = True
                        break
                if not recovered:
                    self.discarded_bytes += next_start
                del self.buffer[:next_start]
                continue
            if len(self.buffer) > length:
                self.discarded_bytes += 1
                del self.buffer[0]
                continue
            break
        return frames

    @staticmethod
    def _decode(raw: bytes, length: int, recovered: bool) -> ResponseFrame:
        sequence = int.from_bytes(raw[5:7], "big") if length == 13 else None
        value = int.from_bytes(raw[7:9] if sequence is not None else raw[5:7], "big")
        return ResponseFrame(raw[3], raw[4], value, sequence, raw, recovered)
