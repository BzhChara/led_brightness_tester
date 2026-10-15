"""显式指定串口才进行一次光源实机测试；不发送任何电机指令。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication
from led_debugger.waveform_worker import WaveformWorker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--channel", type=int, default=0, choices=(0, 1))
    parser.add_argument("--period", type=int, default=15)
    parser.add_argument("--points", type=int, default=206)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    app = QCoreApplication.instance() or QCoreApplication([])
    worker = WaveformWorker(arguments.port, arguments.channel, arguments.period, arguments.points)
    started = time.monotonic()
    points = []
    events = []
    arrivals = []
    def receive(batch):
        points.extend(batch)
        arrivals.append({"elapsed": round(time.monotonic() - started, 4), "count": len(batch)})
    worker.points_received.connect(receive)
    worker.log_received.connect(lambda message: events.append({"elapsed": round(time.monotonic() - started, 4), "message": message}))
    worker.run()
    result = {"port": arguments.port, "channel": arguments.channel, "period_ms": arguments.period,
              "requested_points": arguments.points, "received_points": len(points), "completed": worker.completed,
              "light_off_confirmed": worker.light_off_confirmed, "failure": worker.failure_message,
              "arrivals": arrivals, "events": events,
              "samples": [point.__dict__ for point in points]}
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in ("events", "samples")}, ensure_ascii=False))
    return 0 if worker.completed and worker.light_off_confirmed else 1


if __name__ == "__main__":
    raise SystemExit(main())
