from dataclasses import replace

import pytest
from openpyxl import load_workbook
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from PySide6.QtTest import QTest
import pyqtgraph as pg

from led_debugger.main_window import MainWindow
from led_debugger.settings import AppSettings, load_settings, normalize_settings, save_settings
from led_debugger.waveform_panel import export_waveform
from led_debugger.plot_panel import CurvePlotPanel
from led_debugger.raw_serial_panel import RawSerialPanel, format_hex, format_received_hex
from led_debugger.serial_worker import RawSerialRecord
from led_debugger.waveform_worker import WaveformPoint, WaveformWorker
from test_waveform_worker import FakePort
from test_waveform_protocol import response


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_settings_backwards_compatible_and_waveform_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"continuous_recording":true}', encoding="utf-8")
    assert load_settings(path).operating_mode == "brightness"
    custom = replace(load_settings(path), operating_mode="waveform", waveform_channel=1, waveform_period_ms=25, waveform_point_count=65535)
    save_settings(custom, path)
    assert load_settings(path) == custom
    normalized = normalize_settings(replace(custom, operating_mode="invalid", waveform_channel=9, waveform_period_ms=0, waveform_point_count=999999))
    assert (normalized.operating_mode, normalized.waveform_channel, normalized.waveform_period_ms, normalized.waveform_point_count) == ("brightness", 1, 1, 65535)


def test_export_preserves_raw_values_and_sampling_times_without_lux(tmp_path):
    points = [WaveformPoint(1, 20, 0, 1720000000, 32767), WaveformPoint(1, 21, 0.015, 1720000000, 32770)]
    path = export_waveform(points, 15, 10, tmp_path)
    workbook = load_workbook(path)
    rows = list(workbook["光源波形"].values)
    assert rows[1][0:3] == (2, 20, 0)
    assert rows[1][4:] == (32767, -1)
    assert rows[2][2] == 0.015
    assert "lux" not in str(rows[0])
    assert list(workbook["采集参数"].values)[1] == ("请求点数", 10)
    workbook.close()


def test_mode_switch_settings_and_session_lock(app, monkeypatch):
    saved = []
    monkeypatch.setattr("led_debugger.main_window.load_settings", lambda: AppSettings())
    monkeypatch.setattr("led_debugger.main_window.save_settings", saved.append)
    monkeypatch.setattr("led_debugger.main_window.list_serial_ports", lambda: ["TEST"])
    window = MainWindow()
    window._change_operating_mode("光源波形")
    assert window.page_stack.currentWidget() is window.waveform_view
    assert window.connect_button.text() == "开始采集"
    window._open_settings()
    assert window.settings_page_stack.currentIndex() == 4
    window.waveform_count_row.set_value_text("33")
    window._save_settings_from_page()
    assert window.settings.waveform_point_count == 33
    assert saved[-1].operating_mode == "waveform"
    worker = WaveformWorker("TEST", 0, 15, 33, window)
    window.waveform_worker = worker
    window._update_mode_controls()
    assert not window.mode_combo.isEnabled() and not window.settings_button.isEnabled()
    window._change_operating_mode("亮度检测")
    assert window.settings.operating_mode == "waveform"
    window.waveform_worker = None
    window._update_mode_controls()
    window._change_operating_mode("亮度检测")
    assert window.page_stack.currentWidget() is window.monitor_view
    window.close()


def test_waveform_startup_does_not_automatically_energize(app, monkeypatch):
    monkeypatch.setattr("led_debugger.main_window.load_settings", lambda: AppSettings(operating_mode="waveform", auto_connect_last_port=True, remember_last_port=True, last_port_name="TEST"))
    monkeypatch.setattr("led_debugger.main_window.list_serial_ports", lambda: ["TEST"])
    window = MainWindow()
    window._auto_connect_last_port()
    assert window.waveform_worker is None
    assert window.serial_poller is None
    window.close()


def test_full_window_single_batch_lifecycle(app, monkeypatch):
    settings = AppSettings(operating_mode="waveform", waveform_point_count=3)
    monkeypatch.setattr("led_debugger.main_window.load_settings", lambda: settings)
    monkeypatch.setattr("led_debugger.main_window.list_serial_ports", lambda: ["TEST"])
    port = FakePort()
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", lambda *args, **kwargs: port)
    window = MainWindow()
    window.port_combo.setText("TEST")
    window._toggle_connection()
    assert not window.mode_combo.isEnabled()
    for _ in range(100):
        QTest.qWait(10)
        if window.waveform_worker is None:
            break
    assert window.waveform_worker is None
    assert len(window.waveform_view.points) == 3
    assert "采集完成" in window.status_label.text()
    assert window.connect_button.text() == "开始采集"
    assert window.mode_combo.isEnabled() and window.settings_button.isEnabled()
    QTest.qWait(60)
    assert len(port.writes) == 3  # 不得自动发出第二次采集。
    window.close()


def test_waveform_reuses_brightness_plot_style_and_settings(app, monkeypatch):
    monkeypatch.setattr("led_debugger.main_window.load_settings", lambda: AppSettings(operating_mode="waveform"))
    monkeypatch.setattr("led_debugger.main_window.list_serial_ports", lambda: [])
    window = MainWindow()
    brightness = window.curve_panel
    waveform = window.waveform_view.curve_panel
    assert isinstance(waveform, CurvePlotPanel)
    assert waveform.plot_widget.styleSheet() == brightness.plot_widget.styleSheet()
    assert waveform.plot_widget.viewport().styleSheet() == brightness.plot_widget.viewport().styleSheet()
    assert waveform.plot_widget.getViewBox().state["mouseEnabled"] == brightness.plot_widget.getViewBox().state["mouseEnabled"]
    assert isinstance(brightness.plot_widget.getAxis("bottom"), pg.DateAxisItem)
    assert not isinstance(waveform.plot_widget.getAxis("bottom"), pg.DateAxisItem)
    assert brightness.plot_widget.getAxis("left").labelUnits == "lux"
    assert waveform.plot_widget.getAxis("left").labelUnits == ""
    window.settings = replace(window.settings, curve_line_width=4, curve_show_grid=False,
                              y_axis_auto_scale=False, y_axis_min=-100, y_axis_max=100)
    window._apply_runtime_settings()
    window.waveform_view.add_points([WaveformPoint(0, 0, 0, 1720000000, 32767), WaveformPoint(0, 1, 0.015, 1720000000, 32770)])
    window.waveform_view.refresh_curve()
    assert waveform.plot_curve.opts["pen"].widthF() == brightness.plot_curve.opts["pen"].widthF() == 4
    assert waveform.plot_curve.opts["stepMode"] == brightness.plot_curve.opts["stepMode"] == "left"
    assert waveform.plot_widget.viewRange()[1] == [-100, 100]
    assert waveform.plot_widget.getAxis("left").grid is False
    assert list(waveform.plot_curve.getData()[1]) == [-1, 2]
    window.close()


def test_waveform_raw_return_reuses_full_hex_format_and_same_switch(app, monkeypatch):
    monkeypatch.setattr("led_debugger.main_window.load_settings", lambda: AppSettings(operating_mode="waveform"))
    monkeypatch.setattr("led_debugger.main_window.list_serial_ports", lambda: [])
    window = MainWindow()
    raw = bytes(range(256)) * 2
    record = RawSerialRecord(1, 1720000000, "解析成功", raw)
    window.waveform_view.set_raw_record(1, record)
    window._handle_raw_serial_received(1, record)
    window.settings = replace(window.settings, show_raw_serial_view=True)
    window._apply_runtime_settings()
    panel = window.waveform_view.raw_serial_panel
    assert isinstance(panel, RawSerialPanel)
    assert window.waveform_view.display_stack.currentWidget() is panel
    assert window.right_panel_stack.currentWidget() is window.raw_serial_panel
    assert panel.text_box.toPlainText() == window.raw_serial_panel.text_box.toPlainText()
    assert "长度：512 字节" in panel.text_box.toPlainText()
    assert format_hex(raw, bytes_per_line=None) in panel.text_box.toPlainText()
    assert panel.text_box.lineWrapMode() == window.raw_serial_panel.text_box.lineWrapMode() == QPlainTextEdit.LineWrapMode.WidgetWidth
    assert panel.text_box.toPlainText().endswith("F0 F1 F2 F3 F4 F5 F6 F7 F8 F9 FA FB FC FD FE FF")
    window.settings = replace(window.settings, show_raw_serial_view=False)
    window._apply_runtime_settings()
    assert window.waveform_view.display_stack.currentWidget() is window.waveform_view.curve_panel
    assert window.right_panel_stack.currentWidget() is window.curve_panel
    window.waveform_view.reset_for_session(window.settings)
    assert panel.current_record is None
    window.close()


def test_raw_hex_reflows_with_panel_width_without_changing_bytes(app):
    panel = RawSerialPanel()
    record = RawSerialRecord(1, 1720000000, "解析成功", bytes(range(256)) * 2)
    panel.set_record(record)
    panel.resize(1000, 400)
    panel.show()
    QTest.qWait(30)
    wide_lines = panel.text_box.document().lastBlock().layout().lineCount()
    content = panel.text_box.toPlainText()
    panel.resize(360, 400)
    QTest.qWait(30)
    narrow_lines = panel.text_box.document().lastBlock().layout().lineCount()
    assert narrow_lines > wide_lines > 1
    assert panel.text_box.toPlainText() == content
    assert content.split("RX：\n", 1)[1] == " ".join(f"{byte:02X}" for byte in record.raw_bytes)
    assert "\n" not in content.split("RX：\n", 1)[1]
    panel.close()


def test_distinct_responses_start_new_lines_without_splitting_payload_header():
    first = response(0x40, sequence=0xAA01, value=0)
    second = response(0x40, sequence=22, value=32768)
    record = RawSerialRecord(1, 1720000000, "解析成功", first + second, (first, second))
    lines = format_received_hex(record).splitlines()
    assert lines == [format_hex(first, None), format_hex(second, None)]
    assert len(lines) == 2
    assert all(line.startswith("AA 01 00 40") for line in lines)


def test_response_format_preserves_unparsed_prefix_and_partial_suffix():
    frame = response(0x40, sequence=0, value=32768)
    raw = b"\x99\x88" + frame + b"\xaa\x01"
    record = RawSerialRecord(1, 1720000000, "接收中", raw, (frame,))
    text = format_received_hex(record)
    assert text.splitlines() == ["99 88", format_hex(frame, None), "AA 01"]
    assert bytes.fromhex(text) == raw


@pytest.mark.parametrize("show_errors", [True, False])
def test_modes_share_error_format_count_and_visibility(app, monkeypatch, show_errors):
    settings = AppSettings(operating_mode="waveform", waveform_channel=1, show_serial_errors=show_errors)
    monkeypatch.setattr("led_debugger.main_window.load_settings", lambda: settings)
    monkeypatch.setattr("led_debugger.main_window.list_serial_ports", lambda: [])
    window = MainWindow()
    window.status_label.setText("状态：采集中")
    window._handle_serial_error("通道 2 读取失败：无返回")
    brightness_text = window.status_label.text()
    window._handle_waveform_error("无返回")
    assert window.serial_error_count == 2
    if show_errors:
        assert brightness_text == "状态：通道 2 读取失败：无返回 x 1"
        assert window.status_label.text() == "状态：通道 2 读取失败：无返回 x 2"
    else:
        assert brightness_text == window.status_label.text() == "状态：采集中"
    window.close()


@pytest.mark.parametrize("light_off_confirmed", [True, False])
@pytest.mark.parametrize("show_errors", [True, False])
def test_failure_summary_survives_shutdown_without_counting_twice(app, monkeypatch, show_errors, light_off_confirmed):
    settings = AppSettings(operating_mode="waveform", show_serial_errors=show_errors)
    monkeypatch.setattr("led_debugger.main_window.load_settings", lambda: settings)
    monkeypatch.setattr("led_debugger.main_window.list_serial_ports", lambda: [])
    window = MainWindow()
    worker = WaveformWorker("TEST", 0, 15, 3, window)
    worker.failure_message = "采集提前结束，收到 2 / 3 点"
    worker.light_off_confirmed = light_off_confirmed
    if not light_off_confirmed:
        worker.failure_message += "；关灯未确认，请检查硬件光源"
    window.waveform_worker = worker
    window._handle_waveform_error(worker.failure_message)
    window.status_label.setText("状态：正在关灯，等待确认")
    window._handle_waveform_finished()
    assert window.serial_error_count == 1
    assert window.waveform_worker is None
    if show_errors:
        assert window.status_label.text() == f"状态：通道 1 读取失败：{worker.failure_message} x 1"
    else:
        light = "已确认关灯" if light_off_confirmed else "光源状态未确认"
        assert window.status_label.text() == f"状态：采集未完成，{light}"
    assert window.connect_button.text() == "开始采集"
    assert window.mode_combo.isEnabled()
    window.close()


def test_serial_open_failure_displays_matching_top_and_raw_status(app, monkeypatch):
    settings = AppSettings(operating_mode="waveform", show_raw_serial_view=True)
    monkeypatch.setattr("led_debugger.main_window.load_settings", lambda: settings)
    monkeypatch.setattr("led_debugger.main_window.list_serial_ports", lambda: ["TEST"])
    def fail_to_open(*args, **kwargs):
        raise PermissionError("串口已被占用")
    monkeypatch.setattr("led_debugger.waveform_worker.serial.Serial", fail_to_open)
    window = MainWindow()
    window.port_combo.setText("TEST")
    window._toggle_connection()
    for _ in range(100):
        QTest.qWait(10)
        if window.waveform_worker is None:
            break
    assert window.waveform_worker is None
    assert window.status_label.text() == "状态：通道 1 读取失败：串口异常：串口已被占用 x 1"
    text = window.waveform_view.raw_serial_panel.text_box.toPlainText()
    assert "状态：串口异常：串口已被占用" in text
    assert "长度：0 字节\nRX：\n无返回" in text
    assert window.serial_error_count == 1
    window.close()
