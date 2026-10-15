from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout

from led_debugger.exporter import application_root
from led_debugger.plot_panel import CurvePlotPanel
from led_debugger.raw_serial_panel import RawSerialPanel
from led_debugger.serial_worker import RawSerialRecord
from led_debugger.settings import AppSettings
from led_debugger.waveform_worker import WaveformPoint
from led_debugger.widgets import AcrylicPanel, apply_shadow


def export_waveform(points: list[WaveformPoint], period_ms: int, point_count: int, directory: Path | None = None) -> Path:
    if not points:
        raise ValueError("没有可保存的采样数据")
    moment = datetime.now()
    directory = directory or application_root() / "data" / "waveforms" / moment.strftime("%Y_%m_%d")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"通道{points[0].channel + 1}_{moment:%H%M%S_%f}.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "光源波形"
    sheet.append(["通道", "序号", "采样相对时间(s)", "接收时间", "原始无符号值", "信号值(raw-32768)"])
    for point in points:
        sheet.append([point.channel + 1, point.sequence, point.elapsed_seconds,
                      datetime.fromtimestamp(point.received_timestamp).strftime("%Y-%m-%d %H:%M:%S.%f"),
                      point.raw_value, point.intensity])
    metadata = workbook.create_sheet("采集参数")
    metadata.append(["采样周期(ms)", period_ms])
    metadata.append(["请求点数", point_count])
    metadata.append(["实际保存点数", len(points)])
    metadata.append(["时间说明", "相对时间按板卡序号和设置周期推算；接收时间不是实际采样时间。"])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in (("A", 10), ("B", 12), ("C", 24), ("D", 30), ("E", 20), ("F", 25)):
        sheet.column_dimensions[column].width = width
    # 独占新文件，绝不覆盖已有记录。
    with path.open("xb") as output:
        workbook.save(output)
    workbook.close()
    return path


class WaveformPanel(AcrylicPanel):
    def __init__(self, settings: AppSettings) -> None:
        super().__init__(radius=26, top_alpha=142, bottom_alpha=104)
        self.points: list[WaveformPoint] = []
        self.period_ms = settings.waveform_period_ms
        self.point_count = settings.waveform_point_count
        self._dirty = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        title = QLabel("光源波形监测")
        title.setStyleSheet("font-size: 23px; font-weight: 600; color: #0f172a;")
        self.parameters_label = QLabel()
        self.parameters_label.setStyleSheet("color: #64748b;")
        layout.addWidget(title)
        layout.addWidget(self.parameters_label)
        metrics = QHBoxLayout()
        self.metric_labels = []
        for name in ("当前信号", "最小值", "最大值", "已接收点数"):
            panel = AcrylicPanel(radius=16, top_alpha=145, bottom_alpha=100)
            box = QVBoxLayout(panel)
            box.setContentsMargins(16, 12, 16, 12)
            caption = QLabel(name)
            caption.setStyleSheet("color: #64748b; font-size: 12px;")
            value = QLabel("—")
            value.setStyleSheet("color: #1d4ed8; font-size: 24px; font-weight: 600;")
            box.addWidget(caption)
            box.addWidget(value)
            self.metric_labels.append(value)
            metrics.addWidget(panel)
        layout.addLayout(metrics)
        self.curve_panel = CurvePlotPanel(
            settings, relative_time=True, title="光源信号实时曲线",
            value_label="信号值（原始值 − 32768）", value_units=None,
        )
        self.plot_widget = self.curve_panel.plot_widget
        self.curve = self.curve_panel.plot_curve
        self.raw_serial_panel = RawSerialPanel()
        self.display_stack = QStackedWidget()
        self.display_stack.setObjectName("rightPanelStack")
        self.display_stack.addWidget(self.curve_panel)
        self.display_stack.addWidget(self.raw_serial_panel)
        apply_shadow(self.curve_panel, blur_radius=30, y_offset=10, alpha=24)
        apply_shadow(self.raw_serial_panel, blur_radius=30, y_offset=10, alpha=24)
        layout.addWidget(self.display_stack, 1)
        hint = QLabel("每次点击开始采集只运行一次，采满设置点数后自动关灯。相对时间按序号和采样周期推算；界面随实际回包更新。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #64748b; font-size: 12px;")
        layout.addWidget(hint)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh_curve)
        self.timer.start()
        self.apply_settings(settings)

    def apply_settings(self, settings: AppSettings) -> None:
        duration = settings.waveform_period_ms * settings.waveform_point_count / 1000
        self.parameters_label.setText(f"通道 {settings.waveform_channel + 1}  ·  {settings.waveform_period_ms} ms / 点  ·  {settings.waveform_point_count} 点  ·  预计 {duration:g} 秒  ·  参数在设置 → 波形设置中修改")
        self.curve_panel.apply_settings(settings)
        self.display_stack.setCurrentWidget(self.raw_serial_panel if settings.show_raw_serial_view else self.curve_panel)
        record = self.raw_serial_panel.current_record
        self.raw_serial_panel.set_channel(record.channel if record is not None else settings.waveform_channel + 1)

    def reset_for_session(self, settings: AppSettings) -> None:
        self.points.clear()
        self.period_ms = settings.waveform_period_ms
        self.point_count = settings.waveform_point_count
        self.raw_serial_panel.set_record(None)
        self.raw_serial_panel.set_channel(settings.waveform_channel + 1)
        self._dirty = False
        for label in self.metric_labels:
            label.setText("—")
        self.curve_panel.set_samples([], [])
        self.apply_settings(settings)

    def add_points(self, points: list[WaveformPoint]) -> None:
        self.points.extend(points)
        self._dirty = True

    def set_raw_record(self, channel: int, record: RawSerialRecord) -> None:
        self.raw_serial_panel.set_channel(channel)
        self.raw_serial_panel.set_record(record)

    def refresh_curve(self) -> None:
        if not self._dirty or not self.points:
            return
        self._dirty = False
        values = [point.intensity for point in self.points]
        self.curve_panel.set_samples([point.elapsed_seconds for point in self.points], values)
        for label, value in zip(self.metric_labels, (values[-1], min(values), max(values), len(values))):
            label.setText(str(value))

    def save(self) -> Path:
        return export_waveform(self.points, self.period_ms, self.point_count)
