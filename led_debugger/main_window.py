from __future__ import annotations

import sys
import ctypes
from dataclasses import replace
from pathlib import Path
from time import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QDoubleValidator,
    QIcon,
    QIntValidator,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from led_debugger.data_store import DataStore
from led_debugger.waveform_panel import WaveformPanel
from led_debugger.waveform_worker import WaveformWorker
from led_debugger.exporter import (
    default_data_dir,
    existing_export_files,
    export_all_histories,
    has_exportable_history,
    required_history_points,
)
from led_debugger.plot_panel import CurvePlotPanel
from led_debugger.raw_serial_panel import RawSerialPanel
from led_debugger.recorder import ContinuousRecorder, prepare_recording_dir
from led_debugger.serial_worker import RawSerialRecord, SerialPoller, list_serial_ports
from led_debugger.settings import (
    CHANNEL_DELAY_RANGE,
    CURVE_VISIBLE_SECONDS_RANGE,
    CURVE_LINE_WIDTH_RANGE,
    DEFAULT_CHANNEL_DELAY_SECONDS,
    DEFAULT_CURVE_VISIBLE_SECONDS,
    DEFAULT_CURVE_LINE_WIDTH,
    DEFAULT_CURVE_SHOW_GRID,
    DEFAULT_DISPLAY_DECIMALS,
    DEFAULT_CONTINUOUS_RECORDING,
    DEFAULT_MAX_POINTS,
    DEFAULT_AUTO_CURVE_VISIBLE_SECONDS,
    DEFAULT_AUTO_SAVE_INTERVAL,
    DEFAULT_AUTO_SAVE_DURATION,
    DEFAULT_READ_TIMEOUT_SECONDS,
    DEFAULT_ROUND_DELAY_SECONDS,
    DEFAULT_SAVE_DURATION_SECONDS,
    DEFAULT_SAVE_INTERVAL_SECONDS,
    DEFAULT_SHOW_SERIAL_ERRORS,
    DEFAULT_SHOW_RAW_SERIAL_VIEW,
    DEFAULT_Y_AXIS_AUTO_SCALE,
    DEFAULT_Y_AXIS_MAX,
    DEFAULT_Y_AXIS_MIN,
    DISPLAY_DECIMALS_RANGE,
    MAX_POINTS_RANGE,
    READ_TIMEOUT_RANGE,
    ROUND_DELAY_RANGE,
    SAVE_DURATION_RANGE,
    SAVE_INTERVAL_RANGE,
    Y_AXIS_RANGE,
    AppSettings,
    calculated_auto_curve_visible_seconds,
    calculated_auto_save_duration_seconds,
    calculated_auto_save_interval_seconds,
    effective_save_duration_seconds,
    effective_save_interval_seconds,
    load_settings,
    save_settings,
)
from led_debugger.widgets import (
    AcrylicButton,
    AcrylicPanel,
    AcrylicSelectButton,
    AcrylicTextButton,
    ElidedLabel,
    LedCard,
    SettingActionRow,
    SettingInputRow,
    SettingSwitchRow,
    apply_shadow,
)


APP_ICON_PATH = Path(__file__).resolve().parent / "assets" / "app_icon.ico"


def _colorref(red: int, green: int, blue: int) -> int:
    """Windows COLORREF 使用 0x00BBGGRR 字节顺序。"""
    return red | (green << 8) | (blue << 16)


def apply_native_title_bar_style(window: QWidget) -> None:
    """在支持的 Windows 版本上把原生标题栏设置为浅色。"""
    if sys.platform != "win32":
        return

    try:
        hwnd = int(window.winId())
        caption_color = ctypes.c_int(_colorref(248, 250, 252))
        text_color = ctypes.c_int(_colorref(15, 23, 42))
        border_color = ctypes.c_int(_colorref(226, 232, 240))
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border_color), ctypes.sizeof(border_color))
    except Exception:
        return


class AcrylicWindowBackground(QWidget):
    """带浅色渐变和雾化光斑的窗口背景。

    Qt 不能直接对桌面背景做系统级 Acrylic Blur，这里通过可透出的底层光斑、
    半透明玻璃层和高光边框来模拟 Windows 11 白色亚克力质感。
    """

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 事件函数沿用 Qt 命名。
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect()
        base = QLinearGradient(rect.topLeft(), rect.bottomRight())
        base.setColorAt(0.0, QColor("#f8fafc"))
        base.setColorAt(1.0, QColor("#e8eef7"))
        painter.fillRect(rect, base)

        # 大面积低透明度光斑提供“玻璃层背后有内容”的视觉基础。
        self._paint_blob(painter, rect.width() * 0.10, rect.height() * 0.12, rect.width() * 0.34, QColor(96, 165, 250, 46))
        self._paint_blob(painter, rect.width() * 0.82, rect.height() * 0.14, rect.width() * 0.30, QColor(168, 85, 247, 26))
        self._paint_blob(painter, rect.width() * 0.18, rect.height() * 0.86, rect.width() * 0.34, QColor(45, 212, 191, 31))
        self._paint_blob(painter, rect.width() * 0.56, rect.height() * 0.52, rect.width() * 0.42, QColor(96, 165, 250, 24))

        # 极轻的噪声点避免背景过于“塑料白”，但透明度很低，不影响阅读。
        painter.setPen(QPen(QColor(255, 255, 255, 22), 1))
        step = 18
        for x in range(0, rect.width(), step):
            offset = (x // step) % 3 * 6
            for y in range(offset, rect.height(), step * 2):
                painter.drawPoint(x, y)

        super().paintEvent(event)

    def _paint_blob(self, painter: QPainter, cx: float, cy: float, radius: float, color: QColor) -> None:
        """绘制一个从中心向外透明的柔和光斑。"""
        gradient = QRadialGradient(cx, cy, radius)
        gradient.setColorAt(0.0, color)
        gradient.setColorAt(0.55, QColor(color.red(), color.green(), color.blue(), int(color.alpha() * 0.42)))
        gradient.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))



class MainWindow(QMainWindow):
    """主窗口。

    当前步骤实现完整静态布局。串口轮询和实时数据更新会在后续步骤接入，
    这样界面结构可以先被单独 review。
    """

    def __init__(self) -> None:
        super().__init__()
        self.current_channel = 1
        self.selected_led = 1
        self.led_cards: list[LedCard] = []
        self.available_ports: list[str] = []
        self.settings = load_settings()
        self.data_store = DataStore(max_points=self.settings.max_points)
        self.raw_serial_records: dict[int, RawSerialRecord] = {}
        self.continuous_recorder: ContinuousRecorder | None = None
        self.serial_poller: SerialPoller | None = None
        self.waveform_worker: WaveformWorker | None = None
        self._close_requested = False
        self.serial_error_count = 0
        self.setWindowTitle("LED亮度检测上位机")
        self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self.resize(1080, 720)
        self._build_ui()
        self._apply_display_decimals()
        apply_native_title_bar_style(self)
        self._select_led(1)
        self._refresh_ports()
        self._apply_operating_mode()
        QTimer.singleShot(0, self._auto_connect_last_port)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 事件函数沿用 Qt 命名。
        super().showEvent(event)
        apply_native_title_bar_style(self)

    def _build_ui(self) -> None:
        # 根容器自绘渐变和光斑，为半透明玻璃面板提供可透出的背景内容。
        root = AcrylicWindowBackground()
        root.setObjectName("root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(24, 20, 24, 20)
        root_layout.setSpacing(18)

        # 顶部工具栏：后续会填充真实 COM 口列表，并接入连接/断开逻辑。
        top_bar = AcrylicPanel(radius=22, top_alpha=118, bottom_alpha=86)
        top_bar.setObjectName("topBar")
        toolbar = QHBoxLayout(top_bar)
        toolbar.setContentsMargins(14, 10, 14, 10)
        toolbar.setSpacing(10)

        self.port_combo = AcrylicSelectButton("选择COM口")
        self.port_combo.setFixedSize(150, 38)
        self.port_combo.set_before_popup_callback(self._refresh_ports)
        self.port_combo.set_selection_changed_callback(self._handle_port_selected)

        self.connect_button = AcrylicTextButton("连接")
        self.connect_button.setObjectName("connectButton")
        self.connect_button.setFixedSize(92, 38)
        self.connect_button.clicked.connect(self._toggle_connection)

        self.mode_combo = AcrylicSelectButton("亮度检测")
        self.mode_combo.setFixedSize(146, 38)
        self.mode_combo.set_items(["亮度检测", "光源波形"])
        self.mode_combo.set_selection_changed_callback(self._change_operating_mode)

        self.status_label = ElidedLabel("状态：未连接")
        self.status_label.setObjectName("statusLabel")

        self.export_button = AcrylicTextButton("保存")
        self.export_button.setObjectName("exportButton")
        self.export_button.setFixedSize(92, 38)
        self.export_button.clicked.connect(self._export_data)

        self.settings_button = AcrylicTextButton("设置")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setFixedSize(92, 38)
        self.settings_button.clicked.connect(self._open_settings)

        toolbar.addWidget(self.port_combo)
        toolbar.addWidget(self.mode_combo)
        toolbar.addWidget(self.connect_button)
        toolbar.addWidget(self.status_label, 1)
        toolbar.addItem(QSpacerItem(20, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        toolbar.addWidget(self.export_button)
        toolbar.addWidget(self.settings_button)

        apply_shadow(top_bar, blur_radius=30, y_offset=10, alpha=24)
        root_layout.addWidget(top_bar)

        # 主内容面板：左侧为 4x4 LED 光照强度卡片，右侧为选中 LED 的曲线区域。
        content = AcrylicPanel(radius=26, top_alpha=142, bottom_alpha=104)
        content.setObjectName("contentPanel")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 20, 20, 20)
        content_layout.setSpacing(18)

        work_area = QHBoxLayout()
        work_area.setSpacing(18)

        cards_panel = QFrame()
        cards_panel.setObjectName("cardsPanel")
        cards_panel_layout = QVBoxLayout(cards_panel)
        cards_panel_layout.setContentsMargins(0, 0, 0, 0)
        cards_panel_layout.setSpacing(14)

        self.channel_label = QLabel("通道 1")
        self.channel_label.setObjectName("channelTitle")
        self.channel_label.setAlignment(Qt.AlignCenter)

        cards_grid = QWidget()
        cards_grid.setObjectName("cardsGrid")
        cards_layout = QGridLayout(cards_grid)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(12)

        for index in range(1, 17):
            card = LedCard(index)
            card.clicked.connect(lambda _checked=False, led=index: self._select_led(led))
            self.led_cards.append(card)
            row = (index - 1) // 4
            column = (index - 1) % 4
            cards_layout.addWidget(card, row, column)

        cards_panel_layout.addWidget(self.channel_label)
        cards_panel_layout.addWidget(cards_grid, 1)

        self.curve_panel = CurvePlotPanel(self.settings)
        self.raw_serial_panel = RawSerialPanel()
        self.right_panel_stack = QStackedWidget()
        self.right_panel_stack.setObjectName("rightPanelStack")
        self.right_panel_stack.addWidget(self.curve_panel)
        self.right_panel_stack.addWidget(self.raw_serial_panel)
        self._apply_right_panel_mode()
        apply_shadow(self.curve_panel, blur_radius=30, y_offset=10, alpha=24)
        apply_shadow(self.raw_serial_panel, blur_radius=30, y_offset=10, alpha=24)
        work_area.addWidget(cards_panel, 3)
        work_area.addWidget(self.right_panel_stack, 2)
        content_layout.addLayout(work_area, 1)

        # 通道切换按钮固定在面板底部左右两侧，符合用户提出的切换方式。
        nav_layout = QHBoxLayout()
        self.prev_button = AcrylicButton("‹")
        self.prev_button.setObjectName("navButton")
        self.prev_button.setFixedSize(56, 44)
        self.next_button = AcrylicButton("›")
        self.next_button.setObjectName("navButton")
        self.next_button.setFixedSize(56, 44)
        self.prev_button.clicked.connect(lambda: self._change_channel(-1))
        self.next_button.clicked.connect(lambda: self._change_channel(1))
        apply_shadow(self.prev_button, blur_radius=22, y_offset=7, alpha=28)
        apply_shadow(self.next_button, blur_radius=22, y_offset=7, alpha=28)
        nav_layout.addWidget(self.prev_button, 0, Qt.AlignLeft)
        nav_layout.addItem(QSpacerItem(20, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        nav_layout.addWidget(self.next_button, 0, Qt.AlignRight)
        content_layout.addLayout(nav_layout)

        self.monitor_view = content
        self.settings_view = self._build_settings_view()
        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("pageStack")
        self.page_stack.addWidget(self.monitor_view)
        self.page_stack.addWidget(self.settings_view)
        self.waveform_view = WaveformPanel(self.settings)
        self.page_stack.addWidget(self.waveform_view)
        root_layout.addWidget(self.page_stack, 1)
        self.setCentralWidget(root)
        # 样式集中在当前窗口内，后续如果界面复杂再拆分为独立 qss 文件。
        self.setStyleSheet(
            """
            #root {
                background: transparent;
                color: #0f172a;
                font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
                font-size: 14px;
            }
            #connectButton {
                background: transparent;
                border: none;
                color: #0f172a;
                padding: 0;
            }
            #selectOption {
                min-height: 30px;
                border: none;
                border-radius: 8px;
                background: transparent;
                color: #0f172a;
                text-align: center;
            }
            #selectOption:hover {
                background: rgba(219, 234, 254, 0.86);
            }
            #selectEmpty {
                min-height: 30px;
                color: #475569;
                background: transparent;
            }
            #statusLabel {
                color: #64748b;
            }
            #pageStack {
                background: transparent;
            }
            #settingsPageStack {
                background: transparent;
            }
            #rightPanelStack {
                background: transparent;
            }
            #settingsScroll {
                background: transparent;
                border: none;
            }
            #settingsScroll > QWidget > QWidget {
                background: transparent;
            }
            #settingsScroll QScrollBar:vertical {
                width: 8px;
                margin: 4px 0 4px 0;
                border: none;
                border-radius: 4px;
                background: rgba(226, 232, 240, 0.36);
            }
            #settingsScroll QScrollBar::handle:vertical {
                min-height: 44px;
                border-radius: 4px;
                background: rgba(100, 116, 139, 0.42);
            }
            #settingsScroll QScrollBar::handle:vertical:hover {
                background: rgba(71, 85, 105, 0.58);
            }
            #settingsScroll QScrollBar::handle:vertical:pressed {
                background: rgba(51, 65, 85, 0.65);
            }
            #settingsScroll QScrollBar::add-line:vertical,
            #settingsScroll QScrollBar::sub-line:vertical {
                height: 0;
                border: none;
                background: transparent;
            }
            #settingsScroll QScrollBar::add-page:vertical,
            #settingsScroll QScrollBar::sub-page:vertical {
                background: transparent;
            }
            #channelTitle {
                color: #0f172a;
                font-size: 24px;
                font-weight: 600;
            }
            #cardsPanel {
                background: transparent;
            }
            #cardsGrid {
                background: transparent;
            }
            #ledCard {
                color: #0f172a;
                font-size: 15px;
                font-weight: 400;
                background: transparent;
                border: none;
            }
            #curveTitle {
                color: #0f172a;
                font-size: 17px;
                font-weight: 600;
            }
            #curvePlaceholder {
                color: #64748b;
                background: rgba(255, 255, 255, 0.22);
                border: 1px dashed rgba(148, 163, 184, 0.35);
                border-radius: 14px;
                font-size: 14px;
            }
            #navButton {
                background: transparent;
                border: none;
                padding: 0;
            }
            #settingsSideTitle {
                color: #0f172a;
                font-size: 26px;
                font-weight: 600;
            }
            #settingsSideDescription {
                color: #64748b;
                font-size: 13px;
                line-height: 1.5;
            }
            #settingsSectionTitle {
                color: #0f172a;
                font-size: 18px;
                font-weight: 600;
            }
            #settingsName {
                color: #0f172a;
                font-size: 14px;
                font-weight: 600;
            }
            #settingsDescription {
                color: #64748b;
                font-size: 12px;
            }
            #settingsInput {
                min-height: 32px;
                border: 1px solid rgba(148, 163, 184, 0.62);
                border-radius: 8px;
                padding: 0 10px;
                background: rgba(255, 255, 255, 0.78);
                color: #0f172a;
                selection-background-color: #bfdbfe;
            }
            #settingsInput:focus {
                border-color: #60a5fa;
                background: rgba(255, 255, 255, 0.94);
            }
            #settingsInput[error="true"] {
                border-color: #ef4444;
                background: rgba(254, 242, 242, 0.88);
            }
            #settingsInput:disabled {
                border-color: rgba(203, 213, 225, 0.56);
                background: rgba(226, 232, 240, 0.52);
                color: #94a3b8;
                selection-background-color: rgba(203, 213, 225, 0.72);
            }
            #settingsHint {
                color: #64748b;
                font-size: 12px;
            }
            #memoryEstimateLabel {
                color: #64748b;
                font-size: 12px;
            }
            #saveIntervalEstimateLabel {
                color: #64748b;
                font-size: 12px;
            }
            #curveDurationEstimateLabel {
                color: #64748b;
                font-size: 12px;
            }
            #rawSerialText {
                border: 1px solid rgba(148, 163, 184, 0.34);
                border-radius: 12px;
                padding: 10px;
                background: rgba(255, 255, 255, 0.44);
                color: #0f172a;
                font-family: "Consolas", "Microsoft YaHei UI", monospace;
                font-size: 12px;
            }
            """
        )

    def _build_settings_view(self) -> QWidget:
        """构建嵌入主窗口的设置页面。"""
        settings_panel = AcrylicPanel(radius=26, top_alpha=142, bottom_alpha=104)
        settings_panel.setObjectName("settingsPanel")

        layout = QHBoxLayout(settings_panel)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(28)

        side = QWidget()
        side.setObjectName("settingsSide")
        side.setFixedWidth(250)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(12)

        title = QLabel("设置")
        title.setObjectName("settingsSideTitle")
        self.common_settings_button = AcrylicTextButton("常用设置")
        self.common_settings_button.setFixedSize(178, 38)
        self.common_settings_button.setCheckable(True)
        self.common_settings_button.clicked.connect(lambda: self._set_settings_section(0))
        self.curve_settings_button = AcrylicTextButton("曲线设置")
        self.curve_settings_button.setFixedSize(178, 38)
        self.curve_settings_button.setCheckable(True)
        self.curve_settings_button.clicked.connect(lambda: self._set_settings_section(1))
        self.serial_settings_button = AcrylicTextButton("串口设置")
        self.serial_settings_button.setFixedSize(178, 38)
        self.serial_settings_button.setCheckable(True)
        self.serial_settings_button.clicked.connect(lambda: self._set_settings_section(2))
        self.advanced_settings_button = AcrylicTextButton("高级设置")
        self.advanced_settings_button.setFixedSize(178, 38)
        self.advanced_settings_button.setCheckable(True)
        self.advanced_settings_button.clicked.connect(lambda: self._set_settings_section(3))
        self.waveform_settings_button = AcrylicTextButton("波形设置")
        self.waveform_settings_button.setFixedSize(178, 38)
        self.waveform_settings_button.setCheckable(True)
        self.waveform_settings_button.clicked.connect(lambda: self._set_settings_section(4))

        side_layout.addWidget(title)
        side_layout.addSpacing(12)
        side_layout.addWidget(self.common_settings_button)
        side_layout.addWidget(self.curve_settings_button)
        side_layout.addWidget(self.serial_settings_button)
        side_layout.addWidget(self.advanced_settings_button)
        side_layout.addWidget(self.waveform_settings_button)
        side_layout.addStretch(1)

        settings_content = QWidget()
        settings_content.setObjectName("settingsContent")
        content_layout = QVBoxLayout(settings_content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)

        self.settings_page_stack = QStackedWidget()
        self.settings_page_stack.setObjectName("settingsPageStack")
        self.settings_page_stack.addWidget(self._wrap_settings_scroll(self._build_common_settings_page()))
        self.settings_page_stack.addWidget(self._wrap_settings_scroll(self._build_curve_settings_page()))
        self.settings_page_stack.addWidget(self._wrap_settings_scroll(self._build_serial_settings_page()))
        self.settings_page_stack.addWidget(self._wrap_settings_scroll(self._build_advanced_settings_page()))
        self.settings_page_stack.addWidget(self._wrap_settings_scroll(self._build_waveform_settings_page()))
        content_layout.addWidget(self.settings_page_stack, 1)

        self.settings_hint = QLabel("")
        self.settings_hint.setObjectName("settingsHint")
        self.settings_hint.setWordWrap(True)
        content_layout.addWidget(self.settings_hint)

        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 8, 0, 0)
        button_layout.addStretch(1)

        self.restore_defaults_button = AcrylicTextButton("恢复默认")
        self.restore_defaults_button.setFixedSize(104, 38)
        self.restore_defaults_button.clicked.connect(self._restore_default_settings_inputs)

        self.discard_settings_button = AcrylicTextButton("放弃更改")
        self.discard_settings_button.setFixedSize(104, 38)
        self.discard_settings_button.clicked.connect(self._discard_settings_changes)

        self.save_settings_button = AcrylicTextButton("保存")
        self.save_settings_button.setFixedSize(92, 38)
        self.save_settings_button.clicked.connect(self._save_settings_from_page)

        button_layout.addWidget(self.restore_defaults_button)
        button_layout.addWidget(self.discard_settings_button)
        button_layout.addWidget(self.save_settings_button)
        content_layout.addLayout(button_layout)

        layout.addWidget(side)
        layout.addWidget(settings_content, 1)
        self._set_settings_section(0)
        return settings_panel

    def _wrap_settings_scroll(self, page: QWidget) -> QScrollArea:
        """为设置页添加透明滚动容器。"""
        scroll_area = QScrollArea()
        scroll_area.setObjectName("settingsScroll")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.viewport().setStyleSheet("background: transparent;")
        scroll_area.setWidget(page)
        return scroll_area

    def _build_waveform_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 14, 0)
        layout.setSpacing(12)
        title = QLabel("光源波形 · 单次采集设置")
        title.setObjectName("settingsSectionTitle")
        layout.addWidget(title)
        self.waveform_channel_row = SettingInputRow(
            "CHANNEL", "采集通道", "通道1对应设备目标0，通道2对应目标1。",
            str(self.settings.waveform_channel + 1), QIntValidator(1, 2, self),
        )
        self.waveform_period_row = SettingInputRow(
            "PERIOD_MS", "采样周期（毫秒）", "沿用仪器 0x50 协议，范围1到65535，默认15ms。",
            str(self.settings.waveform_period_ms), QIntValidator(1, 65535, self),
        )
        self.waveform_count_row = SettingInputRow(
            "POINT_COUNT", "单次采集点数", "范围1到65535。采满一次后关灯结束，不自动重复。默认206点。",
            str(self.settings.waveform_point_count), QIntValidator(1, 65535, self),
        )
        for row in (self.waveform_channel_row, self.waveform_period_row, self.waveform_count_row):
            self._add_settings_row(layout, row)
        hint = QLabel("仅作用于光源波形模式。光源波形不会使用亮度检测的轮询间隔、lux单位或连续记录设置。数据保存包含本次全部已收到的有效点。")
        hint.setWordWrap(True)
        hint.setObjectName("settingsHint")
        layout.addWidget(hint)
        layout.addStretch(1)
        return page

    def _build_curve_settings_page(self) -> QWidget:
        """构建曲线设置页。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 14, 0)
        layout.setSpacing(12)

        section_title = QLabel("曲线设置")
        section_title.setObjectName("settingsSectionTitle")
        layout.addWidget(section_title)

        curve_seconds_validator = QIntValidator(
            int(CURVE_VISIBLE_SECONDS_RANGE[0]),
            int(CURVE_VISIBLE_SECONDS_RANGE[1]),
            self,
        )
        line_width_validator = QDoubleValidator(CURVE_LINE_WIDTH_RANGE[0], CURVE_LINE_WIDTH_RANGE[1], 1, self)
        line_width_validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        y_axis_validator = QDoubleValidator(Y_AXIS_RANGE[0], Y_AXIS_RANGE[1], 3, self)
        y_axis_validator.setNotation(QDoubleValidator.Notation.StandardNotation)

        self.curve_seconds_row = SettingInputRow(
            "CURVE_VISIBLE_SECONDS",
            "曲线显示时长",
            "关闭自动曲线显示时长后生效，只影响显示窗口，不改变采集。",
            self._format_int(self.settings.curve_visible_seconds),
            curve_seconds_validator,
        )
        self.auto_curve_visible_row = SettingSwitchRow(
            "AUTO_CURVE_VISIBLE_SECONDS",
            "自动曲线显示时长",
            "开启后按当前保存间隔估算约 100 个点的显示窗口。",
            self.settings.auto_curve_visible_seconds,
        )
        self.curve_grid_row = SettingSwitchRow(
            "CURVE_SHOW_GRID",
            "显示曲线网格",
            "控制右侧曲线区域是否显示浅色网格线。",
            self.settings.curve_show_grid,
        )
        self.curve_line_width_row = SettingInputRow(
            "CURVE_LINE_WIDTH",
            "曲线线宽",
            "实时曲线线条宽度。范围 1.0 到 6.0，实际绘制时保留整数线宽。",
            self._format_float(self.settings.curve_line_width),
            line_width_validator,
        )
        self.y_axis_auto_row = SettingSwitchRow(
            "Y_AXIS_AUTO_SCALE",
            "Y 轴自动缩放",
            "开启时曲线纵轴随数据自动调整；关闭时使用下面的固定范围。",
            self.settings.y_axis_auto_scale,
        )
        self.y_axis_min_row = SettingInputRow(
            "Y_AXIS_MIN",
            "Y 轴固定下限",
            "关闭自动缩放后使用的纵轴最小值。",
            self._format_float(self.settings.y_axis_min),
            y_axis_validator,
        )
        self.y_axis_max_row = SettingInputRow(
            "Y_AXIS_MAX",
            "Y 轴固定上限",
            "关闭自动缩放后使用的纵轴最大值。",
            self._format_float(self.settings.y_axis_max),
            y_axis_validator,
        )
        clear_history_row = SettingActionRow(
            "清空当前曲线历史",
            "只清空当前通道、当前 LED 的内存曲线数据，不影响最新读数和其他通道。",
            "清空当前曲线",
            self._clear_current_curve_history,
        )

        for row in (
            self.auto_curve_visible_row,
            self.curve_seconds_row,
            self.curve_grid_row,
            self.curve_line_width_row,
            self.y_axis_auto_row,
            self.y_axis_min_row,
            self.y_axis_max_row,
            clear_history_row,
        ):
            self._add_settings_row(layout, row)
        self.curve_duration_estimate_label = QLabel("")
        self.curve_duration_estimate_label.setObjectName("curveDurationEstimateLabel")
        self.curve_duration_estimate_label.setWordWrap(True)
        layout.addWidget(self.curve_duration_estimate_label)

        layout.addStretch(1)
        return page

    def _build_common_settings_page(self) -> QWidget:
        """构建常用设置页。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 14, 0)
        layout.setSpacing(12)

        section_title = QLabel("常用设置")
        section_title.setObjectName("settingsSectionTitle")
        layout.addWidget(section_title)

        decimals_validator = QIntValidator(DISPLAY_DECIMALS_RANGE[0], DISPLAY_DECIMALS_RANGE[1], self)

        self.remember_port_row = SettingSwitchRow(
            "REMEMBER_LAST_PORT",
            "记住上次选择的 COM 口",
            "开启后会保存最近选择或连接的串口，下次启动时自动选中。",
            self.settings.remember_last_port,
        )
        self.auto_connect_row = SettingSwitchRow(
            "AUTO_CONNECT_LAST_PORT",
            "启动后自动连接上次串口",
            "开启后程序启动时会尝试连接已记住的串口。默认关闭，避免误连接。",
            self.settings.auto_connect_last_port,
        )
        self.display_decimals_row = SettingInputRow(
            "DISPLAY_DECIMALS",
            "显示小数位数",
            "LED 光照强度卡片显示的小数位数量。范围 0 到 6。",
            self._format_int(self.settings.display_decimals),
            decimals_validator,
        )

        for row in (
            self.remember_port_row,
            self.auto_connect_row,
            self.display_decimals_row,
        ):
            self._add_settings_row(layout, row)

        layout.addStretch(1)
        return page

    def _build_serial_settings_page(self) -> QWidget:
        """构建串口设置页。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 14, 0)
        layout.setSpacing(12)

        section_title = QLabel("串口设置")
        section_title.setObjectName("settingsSectionTitle")
        layout.addWidget(section_title)

        read_timeout_validator = QDoubleValidator(READ_TIMEOUT_RANGE[0], READ_TIMEOUT_RANGE[1], 2, self)
        read_timeout_validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        round_delay_validator = QDoubleValidator(ROUND_DELAY_RANGE[0], ROUND_DELAY_RANGE[1], 2, self)
        round_delay_validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        channel_delay_validator = QDoubleValidator(CHANNEL_DELAY_RANGE[0], CHANNEL_DELAY_RANGE[1], 2, self)
        channel_delay_validator.setNotation(QDoubleValidator.Notation.StandardNotation)

        self.read_timeout_row = SettingInputRow(
            "READ_TIMEOUT_SECONDS",
            "读取超时",
            "串口读写返回帧的最大等待时间，单位秒。过小可能误报超时，过大异常响应会变慢。",
            self._format_float(self.settings.read_timeout_seconds),
            read_timeout_validator,
        )
        self.round_delay_row = SettingInputRow(
            "ROUND_DELAY_SECONDS",
            "轮询间隔",
            "完成 1-16 通道一轮读取后的暂停时间，单位秒。数值越小刷新越快。",
            self._format_float(self.settings.round_delay_seconds),
            round_delay_validator,
        )
        self.channel_delay_row = SettingInputRow(
            "CHANNEL_DELAY_SECONDS",
            "通道间隔",
            "每读取一个通道后的等待时间，单位秒。下位机响应不稳定时可设置为 0.5 或 1。",
            self._format_float(self.settings.channel_delay_seconds),
            channel_delay_validator,
        )
        self.show_serial_errors_row = SettingSwitchRow(
            "SHOW_SERIAL_ERRORS",
            "串口读取失败提示到状态栏",
            "开启后读取失败会刷新顶部状态栏；关闭后减少频繁错误提示干扰。",
            self.settings.show_serial_errors,
        )
        self.show_raw_serial_view_row = SettingSwitchRow(
            "SHOW_RAW_SERIAL_VIEW",
            "显示串口原始返回",
            "开启后右侧区域显示当前通道最近一次 RX 的 HEX 原始字节，用于诊断下位机返回。",
            self.settings.show_raw_serial_view,
        )

        for row in (
            self.read_timeout_row,
            self.round_delay_row,
            self.channel_delay_row,
            self.show_serial_errors_row,
            self.show_raw_serial_view_row,
        ):
            self._add_settings_row(layout, row)

        layout.addStretch(1)
        return page

    def _build_advanced_settings_page(self) -> QWidget:
        """构建高级设置页。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 14, 0)
        layout.setSpacing(12)

        section_title = QLabel("高级设置")
        section_title.setObjectName("settingsSectionTitle")
        layout.addWidget(section_title)

        max_points_validator = QIntValidator(MAX_POINTS_RANGE[0], MAX_POINTS_RANGE[1], self)
        save_duration_validator = QDoubleValidator(SAVE_DURATION_RANGE[0], SAVE_DURATION_RANGE[1], 1, self)
        save_duration_validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        save_interval_validator = QDoubleValidator(SAVE_INTERVAL_RANGE[0], SAVE_INTERVAL_RANGE[1], 1, self)
        save_interval_validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self.max_points_row = SettingInputRow(
            "DEFAULT_MAX_POINTS",
            "历史最大点数",
            "每个通道/LED 在内存中保留的历史点数量，用于控制长时间运行的内存占用。",
            self._format_int(self.settings.max_points),
            max_points_validator,
        )
        self._add_settings_row(layout, self.max_points_row)
        self.memory_estimate_label = QLabel("")
        self.memory_estimate_label.setObjectName("memoryEstimateLabel")
        self.memory_estimate_label.setWordWrap(True)
        layout.addWidget(self.memory_estimate_label)
        self.max_points_row.input.textChanged.connect(self._update_memory_estimate_label)
        self._update_memory_estimate_label()

        self.continuous_recording_row = SettingSwitchRow(
            "CONTINUOUS_RECORDING",
            "连续记录模式",
            "开启后保存按钮用于开始/停止连续记录；保存时长设置不生效。",
            self.settings.continuous_recording,
        )
        self.save_duration_row = SettingInputRow(
            "SAVE_DURATION_SECONDS",
            "保存时长",
            "关闭自动保存时长后生效，点击保存时导出最近多少秒的历史数据。",
            self._format_float(self.settings.save_duration_seconds),
            save_duration_validator,
        )
        self.auto_save_duration_row = SettingSwitchRow(
            "AUTO_SAVE_DURATION",
            "自动保存时长",
            "开启后按当前保存间隔估算约 100 个点的保存窗口。",
            self.settings.auto_save_duration,
        )
        self.save_interval_row = SettingInputRow(
            "SAVE_INTERVAL_SECONDS",
            "保存间隔",
            "关闭自动保存间隔后生效，按真实采样时间向后查找间隔后的第一个数据点，单位秒。",
            self._format_float(self.settings.save_interval_seconds),
            save_interval_validator,
        )
        self.auto_save_interval_row = SettingSwitchRow(
            "AUTO_SAVE_INTERVAL",
            "自动保存间隔",
            "开启后按 16 通道轮询节奏估算保存间隔；关闭后使用下方保存间隔。",
            self.settings.auto_save_interval,
        )
        for row in (
            self.continuous_recording_row,
            self.auto_save_duration_row,
            self.save_duration_row,
            self.auto_save_interval_row,
            self.save_interval_row,
        ):
            self._add_settings_row(layout, row)
        self.save_interval_estimate_label = QLabel("")
        self.save_interval_estimate_label.setObjectName("saveIntervalEstimateLabel")
        self.save_interval_estimate_label.setWordWrap(True)
        layout.addWidget(self.save_interval_estimate_label)
        self.round_delay_row.input.textChanged.connect(self._update_save_interval_estimate_label)
        self.channel_delay_row.input.textChanged.connect(self._update_save_interval_estimate_label)
        self.curve_seconds_row.input.textChanged.connect(self._update_save_interval_estimate_label)
        self.save_duration_row.input.textChanged.connect(self._update_save_interval_estimate_label)
        self.save_interval_row.input.textChanged.connect(self._update_save_interval_estimate_label)
        self.continuous_recording_row.checkbox.toggled.connect(self._update_auto_setting_input_states)
        self.auto_curve_visible_row.checkbox.toggled.connect(self._update_auto_setting_input_states)
        self.auto_save_duration_row.checkbox.toggled.connect(self._update_auto_setting_input_states)
        self.auto_save_interval_row.checkbox.toggled.connect(self._update_auto_setting_input_states)
        self._update_auto_setting_input_states()

        layout.addStretch(1)
        return page

    def _add_settings_row(self, layout: QVBoxLayout, row: QWidget) -> None:
        """把设置行添加到布局并套用轻量阴影。"""
        apply_shadow(row, blur_radius=18, y_offset=5, alpha=18)
        layout.addWidget(row)

    def _set_settings_section(self, index: int) -> None:
        """切换设置页右侧具体分类。"""
        self.settings_page_stack.setCurrentIndex(index)
        self.common_settings_button.setChecked(index == 0)
        self.curve_settings_button.setChecked(index == 1)
        self.serial_settings_button.setChecked(index == 2)
        self.advanced_settings_button.setChecked(index == 3)
        self.waveform_settings_button.setChecked(index == 4)
        hints = {
            0: "常用设置保存后立即应用；自动连接会在下一次启动时生效。",
            1: "曲线显示设置保存后立即应用；清空曲线历史只影响当前通道和当前 LED。",
            2: "串口超时和轮询间隔会在下一次连接串口时生效。",
            3: "高级设置会影响历史缓存、保存数据和长时间运行的资源占用。",
            4: "保存后在下次单次采集时生效。新组件允许的实际周期和点数以实机返回为准。",
        }
        self.settings_hint.setText(hints.get(index, ""))

    def _format_float(self, value: float) -> str:
        """格式化设置页面中的浮点数，避免不必要的尾随零。"""
        return f"{value:.2f}".rstrip("0").rstrip(".")

    def _format_int(self, value: float | int) -> str:
        """格式化设置页面中的整数。"""
        return str(int(value))

    def _update_memory_estimate_label(self, _text: str = "") -> None:
        """按历史点数估算原始历史缓存数据量。"""
        try:
            max_points = int(self.max_points_row.value_text())
        except ValueError:
            self.memory_estimate_label.setText("预计历史缓存：请输入历史最大点数后估算")
            return

        point_count = 16 * 16 * max_points
        raw_bytes = point_count * 16
        raw_mb = raw_bytes / 1024 / 1024
        self.memory_estimate_label.setText(
            f"预计历史缓存：每个 LED 最多保留 {max_points} 次历史采样，"
            f"合计 16 通道 x 16 LED，原始数据约 {raw_mb:.2f} MB"
        )

    def _update_save_interval_estimate_label(self, _value=None) -> None:
        """显示保存/曲线当前将使用的自动估算值。"""
        try:
            channel_delay = float(self.channel_delay_row.value_text())
            round_delay = float(self.round_delay_row.value_text())
        except ValueError:
            self.save_interval_estimate_label.setText("当前保存间隔：请输入通道间隔和整轮间隔后估算")
            self.curve_duration_estimate_label.setText("当前曲线显示时长：请输入通道间隔和整轮间隔后估算")
            return

        estimated_settings = AppSettings(
            channel_delay_seconds=channel_delay,
            round_delay_seconds=round_delay,
            curve_visible_seconds=self._float_from_row(self.curve_seconds_row, DEFAULT_CURVE_VISIBLE_SECONDS),
            save_duration_seconds=self._float_from_row(self.save_duration_row, DEFAULT_SAVE_DURATION_SECONDS),
            save_interval_seconds=self._float_from_row(self.save_interval_row, DEFAULT_SAVE_INTERVAL_SECONDS),
            auto_save_interval=self.auto_save_interval_row.is_checked(),
            auto_save_duration=self.auto_save_duration_row.is_checked(),
            auto_curve_visible_seconds=self.auto_curve_visible_row.is_checked(),
        )
        auto_interval = calculated_auto_save_interval_seconds(estimated_settings)
        save_interval = effective_save_interval_seconds(estimated_settings)
        save_duration = effective_save_duration_seconds(estimated_settings)
        curve_duration = calculated_auto_curve_visible_seconds(estimated_settings)
        if self.continuous_recording_row.is_checked():
            duration_text = "当前保存时长：连续记录模式，不限制时长"
        elif self.auto_save_duration_row.is_checked():
            duration_text = f"当前保存时长：自动约 {self._format_float(save_duration)} 秒（约 100 个点）"
        else:
            duration_text = (
                f"当前保存时长：手动 {self._format_float(estimated_settings.save_duration_seconds)} 秒；"
                f"自动估算为 {self._format_float(calculated_auto_save_duration_seconds(estimated_settings))} 秒"
            )
        if self.auto_save_interval_row.is_checked():
            interval_text = (
                f"当前保存间隔：自动约 {self._format_float(auto_interval)} 秒"
                f"（16 通道 x 通道间隔 {self._format_float(channel_delay)} 秒 + 整轮间隔 {self._format_float(round_delay)} 秒）"
            )
        else:
            interval_text = (
                f"当前保存间隔：手动 {self._format_float(save_interval)} 秒；自动估算为 {self._format_float(auto_interval)} 秒"
            )
        self.save_interval_estimate_label.setText(f"{duration_text}\n{interval_text}")

        if self.auto_curve_visible_row.is_checked():
            self.curve_duration_estimate_label.setText(
                f"当前曲线显示时长：自动约 {self._format_float(curve_duration)} 秒（约 100 个点）"
            )
        else:
            self.curve_duration_estimate_label.setText(
                f"当前曲线显示时长：手动 {self._format_float(estimated_settings.curve_visible_seconds)} 秒；"
                f"自动估算为 {self._format_float(curve_duration)} 秒"
            )

    def _update_auto_setting_input_states(self, _value=None) -> None:
        """自动选项开启时禁用对应的手动输入行。"""
        continuous_recording = self.continuous_recording_row.is_checked()
        self.curve_seconds_row.set_row_enabled(not self.auto_curve_visible_row.is_checked())
        self.auto_save_duration_row.set_row_enabled(not continuous_recording)
        self.save_duration_row.set_row_enabled(
            not continuous_recording and not self.auto_save_duration_row.is_checked()
        )
        self.save_interval_row.set_row_enabled(not self.auto_save_interval_row.is_checked())
        self._update_save_interval_estimate_label()

    def _float_from_row(self, row: SettingInputRow, fallback: float) -> float:
        """估算提示使用，输入未完成时临时使用默认值避免提示闪烁报错。"""
        try:
            return float(row.value_text())
        except ValueError:
            return fallback

    def _populate_settings_inputs(self, settings: AppSettings) -> None:
        """用当前设置刷新设置页输入框。"""
        self.read_timeout_row.set_value_text(self._format_float(settings.read_timeout_seconds))
        self.round_delay_row.set_value_text(self._format_float(settings.round_delay_seconds))
        self.channel_delay_row.set_value_text(self._format_float(settings.channel_delay_seconds))
        self.curve_seconds_row.set_value_text(self._format_int(settings.curve_visible_seconds))
        self.auto_curve_visible_row.set_checked(settings.auto_curve_visible_seconds)
        self.max_points_row.set_value_text(self._format_int(settings.max_points))
        self.remember_port_row.set_checked(settings.remember_last_port)
        self.auto_connect_row.set_checked(settings.auto_connect_last_port)
        self.display_decimals_row.set_value_text(self._format_int(settings.display_decimals))
        self.curve_grid_row.set_checked(settings.curve_show_grid)
        self.curve_line_width_row.set_value_text(self._format_float(settings.curve_line_width))
        self.y_axis_auto_row.set_checked(settings.y_axis_auto_scale)
        self.y_axis_min_row.set_value_text(self._format_float(settings.y_axis_min))
        self.y_axis_max_row.set_value_text(self._format_float(settings.y_axis_max))
        self.show_serial_errors_row.set_checked(settings.show_serial_errors)
        self.show_raw_serial_view_row.set_checked(settings.show_raw_serial_view)
        self.save_duration_row.set_value_text(self._format_float(settings.save_duration_seconds))
        self.save_interval_row.set_value_text(self._format_float(settings.save_interval_seconds))
        self.continuous_recording_row.set_checked(settings.continuous_recording)
        self.auto_save_interval_row.set_checked(settings.auto_save_interval)
        self.auto_save_duration_row.set_checked(settings.auto_save_duration)
        self.waveform_channel_row.set_value_text(str(settings.waveform_channel + 1))
        self.waveform_period_row.set_value_text(str(settings.waveform_period_ms))
        self.waveform_count_row.set_value_text(str(settings.waveform_point_count))
        self._update_auto_setting_input_states()

    def _show_monitor_page(self, status_text: str | None = None) -> None:
        """切回监测页面。"""
        self.page_stack.setCurrentWidget(self.waveform_view if self.settings.operating_mode == "waveform" else self.monitor_view)
        self.settings_button.setText("设置")
        self._update_mode_controls()
        if status_text is not None:
            self.status_label.setText(status_text)

    def _change_channel(self, offset: int) -> None:
        """循环切换 1-16 通道，并刷新界面标题。"""
        self.current_channel = ((self.current_channel - 1 + offset) % 16) + 1
        self.channel_label.setText(f"通道 {self.current_channel}")
        self._update_cards_for_current_channel()
        self._update_curve()
        if self.settings.show_raw_serial_view:
            self._refresh_raw_serial_panel()

    def _select_led(self, led_index: int) -> None:
        """选中一个 LED 卡片，并更新曲线区域标题。"""
        self.selected_led = led_index
        for card in self.led_cards:
            card.setChecked(card.led_index == led_index)
        self.curve_panel.set_selected_led(led_index)
        self._update_curve()

    def _refresh_ports(self) -> None:
        """刷新可用 COM 口列表。"""
        self.available_ports = list_serial_ports()
        self.port_combo.set_items(self.available_ports)
        if (
            self.settings.remember_last_port
            and self.port_combo.currentText() not in self.available_ports
            and self.settings.last_port_name in self.available_ports
        ):
            self.port_combo.setText(self.settings.last_port_name)
        if self.serial_poller is None:
            if self.available_ports:
                self.status_label.setText(f"状态：未连接，发现 {len(self.available_ports)} 个COM口")
            else:
                self.status_label.setText("状态：未连接，未发现COM口")

    def _handle_port_selected(self, port_name: str) -> None:
        """记住用户手动选择的 COM 口。"""
        if not self.settings.remember_last_port:
            return
        self.settings = replace(self.settings, last_port_name=port_name)
        save_settings(self.settings)

    def _auto_connect_last_port(self) -> None:
        """启动后按设置自动连接上次串口。"""
        if self.settings.operating_mode == "waveform":
            return  # 波形模式需要用户明确开始，启动程序不自动点亮光源。
        if not self.settings.auto_connect_last_port or self.serial_poller is not None:
            return

        last_port_name = self.settings.last_port_name
        if not self.settings.remember_last_port:
            self.status_label.setText("状态：自动连接失败，未开启记住上次串口")
            return
        if not last_port_name:
            self.status_label.setText("状态：自动连接失败，未保存上次串口")
            return
        if not self.available_ports:
            self.status_label.setText("状态：自动连接失败，未发现COM口")
            return
        if last_port_name not in self.available_ports:
            self.status_label.setText(f"状态：自动连接失败，上次串口 {last_port_name} 不存在")
            return

        self.port_combo.setText(last_port_name)
        self._toggle_connection()
        if self.serial_poller is not None:
            self.status_label.setText(f"状态：正在自动连接 {last_port_name}")
        elif last_port_name not in self.available_ports:
            self.status_label.setText(f"状态：自动连接失败，上次串口 {last_port_name} 不存在")

    def _open_settings(self) -> None:
        """在主窗口内切换到设置页面。"""
        if self.page_stack.currentWidget() == self.settings_view:
            self._discard_settings_changes()
            return
        self._populate_settings_inputs(self.settings)
        self.page_stack.setCurrentWidget(self.settings_view)
        self.settings_button.setText("返回")
        self._update_mode_controls()
        if self.settings.operating_mode == "waveform":
            self._set_settings_section(4)
        self.status_label.setText("状态：正在修改设置")

    def _restore_default_settings_inputs(self) -> None:
        """把设置页输入框恢复为默认值，点击保存后才会生效。"""
        self._populate_settings_inputs(
            AppSettings(
                read_timeout_seconds=DEFAULT_READ_TIMEOUT_SECONDS,
                round_delay_seconds=DEFAULT_ROUND_DELAY_SECONDS,
                channel_delay_seconds=DEFAULT_CHANNEL_DELAY_SECONDS,
                curve_visible_seconds=DEFAULT_CURVE_VISIBLE_SECONDS,
                max_points=DEFAULT_MAX_POINTS,
                display_decimals=DEFAULT_DISPLAY_DECIMALS,
                curve_show_grid=DEFAULT_CURVE_SHOW_GRID,
                curve_line_width=DEFAULT_CURVE_LINE_WIDTH,
                y_axis_auto_scale=DEFAULT_Y_AXIS_AUTO_SCALE,
                y_axis_min=DEFAULT_Y_AXIS_MIN,
                y_axis_max=DEFAULT_Y_AXIS_MAX,
                show_serial_errors=DEFAULT_SHOW_SERIAL_ERRORS,
                show_raw_serial_view=DEFAULT_SHOW_RAW_SERIAL_VIEW,
                save_duration_seconds=DEFAULT_SAVE_DURATION_SECONDS,
                save_interval_seconds=DEFAULT_SAVE_INTERVAL_SECONDS,
                auto_save_interval=DEFAULT_AUTO_SAVE_INTERVAL,
                auto_save_duration=DEFAULT_AUTO_SAVE_DURATION,
                auto_curve_visible_seconds=DEFAULT_AUTO_CURVE_VISIBLE_SECONDS,
                continuous_recording=DEFAULT_CONTINUOUS_RECORDING,
            )
        )
        self.status_label.setText("状态：已填入默认设置，点击保存后生效")

    def _discard_settings_changes(self) -> None:
        """放弃未保存的输入并回到监测页面。"""
        self._populate_settings_inputs(self.settings)
        self._show_monitor_page("状态：已放弃设置更改")

    def _export_data(self) -> None:
        """导出所有通道/LED 的历史数据到 Excel。"""
        if self.settings.operating_mode == "waveform":
            try:
                path = self.waveform_view.save()
                self.status_label.setText(f"状态：已保存 {len(self.waveform_view.points)} 点，到 {path}")
            except (OSError, ValueError) as exc:
                self.status_label.setText(f"状态：保存失败：{exc}")
            return
        if self.settings.continuous_recording:
            if self.continuous_recorder is None:
                self._start_continuous_recording()
            else:
                self._stop_continuous_recording()
            return

        if not has_exportable_history(self.data_store):
            self.status_label.setText("状态：没有可保存的历史数据")
            return

        save_interval_seconds = effective_save_interval_seconds(self.settings)
        save_duration_seconds = effective_save_duration_seconds(self.settings)
        required_points = required_history_points(
            save_duration_seconds,
            save_interval_seconds,
        )
        if self.settings.max_points < required_points:
            self.status_label.setText(
                f"状态：历史最大点数不足，当前 {self.settings.max_points} 点，建议至少 {required_points} 点"
            )
            return

        output_dir = default_data_dir()
        if existing_export_files(output_dir) and not self._confirm_overwrite_export_data():
            self.status_label.setText("状态：已取消保存")
            return

        try:
            summary = export_all_histories(
                self.data_store,
                output_dir,
                save_duration_seconds,
                save_interval_seconds,
            )
        except OSError as exc:
            self.status_label.setText(f"状态：保存失败：{exc}")
            return

        self.status_label.setText(
            f"状态：已保存 {summary.files_written} 个Excel，共 {summary.rows_written} 行，到 {summary.output_dir}"
        )

    def _start_continuous_recording(self) -> None:
        """连续记录模式下开始记录。"""
        if self.serial_poller is None:
            self.status_label.setText("状态：请先连接串口再开始连续记录")
            return

        output_dir = prepare_recording_dir()
        save_interval_seconds = effective_save_interval_seconds(self.settings)
        try:
            self.continuous_recorder = ContinuousRecorder(output_dir, save_interval_seconds)
        except OSError as exc:
            self.status_label.setText(f"状态：开始记录失败：{exc}")
            return

        self.export_button.setText("停止")
        self.settings_button.setEnabled(False)
        self._update_mode_controls()
        self.status_label.setText(f"状态：连续记录中，共 0 行，到 {output_dir}")

    def _stop_continuous_recording(self) -> None:
        """连续记录模式下停止记录并生成 Excel。"""
        recorder = self.continuous_recorder
        if recorder is None:
            return

        self.export_button.setEnabled(False)
        self.status_label.setText("状态：正在生成连续记录 Excel...")
        QApplication.processEvents()
        try:
            summary = recorder.finish()
        except OSError as exc:
            self.status_label.setText(f"状态：生成连续记录 Excel 失败：{exc}")
            return
        finally:
            self.continuous_recorder = None
            self.export_button.setText("保存")
            self.export_button.setEnabled(True)
            self.settings_button.setEnabled(True)
            self._update_mode_controls()

        self.status_label.setText(
            f"状态：连续记录完成 {summary.files_written} 个Excel，共 {summary.rows_written} 行，到 {summary.output_dir}"
        )

    def _confirm_overwrite_export_data(self) -> bool:
        """显示和主界面风格一致的保存覆盖确认框。"""
        message_box = QMessageBox(self)
        message_box.setWindowTitle("覆盖旧数据")
        message_box.setText("检测到已有保存数据，是否覆盖？")
        message_box.setIcon(QMessageBox.Icon.Question)
        message_box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

        yes_button = message_box.button(QMessageBox.StandardButton.Yes)
        no_button = message_box.button(QMessageBox.StandardButton.No)
        if yes_button is not None:
            yes_button.setText("覆盖")
            yes_button.setAutoDefault(False)
            yes_button.setDefault(False)
        if no_button is not None:
            no_button.setText("取消")
            no_button.setAutoDefault(False)
            no_button.setDefault(False)
            message_box.setEscapeButton(no_button)

        message_box.setStyleSheet(
            """
            QMessageBox {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #f8fafc,
                    stop: 1 #e8eef7
                );
                color: #0f172a;
                font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
                font-size: 14px;
            }
            QMessageBox QLabel {
                color: #0f172a;
            }
            QMessageBox QPushButton {
                min-width: 62px;
                min-height: 24px;
                border: 1px solid rgba(203, 213, 225, 0.86);
                border-radius: 7px;
                padding: 2px 10px;
                background: rgba(255, 255, 255, 0.72);
                color: #0f172a;
            }
            QMessageBox QPushButton:hover {
                border-color: rgba(96, 165, 250, 0.86);
                background: rgba(239, 246, 255, 0.92);
            }
            QMessageBox QPushButton:pressed {
                background: rgba(219, 234, 254, 0.96);
            }
            """
        )
        apply_native_title_bar_style(message_box)
        return message_box.exec() == QMessageBox.StandardButton.Yes

    def _save_settings_from_page(self) -> None:
        """校验设置页输入并保存。"""
        new_settings = self._settings_from_inputs()
        if new_settings is None:
            return

        port_name = self.port_combo.currentText()
        if new_settings.remember_last_port and port_name in self.available_ports:
            new_settings = replace(new_settings, last_port_name=port_name)
        self.settings = new_settings
        save_settings(self.settings)
        self._apply_runtime_settings()
        self._update_curve()
        if self.serial_poller is None:
            self._show_monitor_page("状态：设置已保存")
        else:
            self._show_monitor_page("状态：设置已保存，串口参数下次连接生效")

    def _settings_from_inputs(self) -> AppSettings | None:
        """从设置页输入框读取并校验参数。"""
        for row in (
            self.waveform_channel_row,
            self.waveform_period_row,
            self.waveform_count_row,
            self.read_timeout_row,
            self.round_delay_row,
            self.channel_delay_row,
            self.curve_seconds_row,
            self.max_points_row,
            self.display_decimals_row,
            self.curve_line_width_row,
            self.y_axis_min_row,
            self.y_axis_max_row,
            self.save_duration_row,
            self.save_interval_row,
        ):
            row.set_error(False)

        auto_save_interval = self.auto_save_interval_row.is_checked()
        auto_save_duration = self.auto_save_duration_row.is_checked()
        auto_curve_visible_seconds = self.auto_curve_visible_row.is_checked()
        continuous_recording = self.continuous_recording_row.is_checked()

        read_timeout = self._parse_float_setting(
            self.read_timeout_row,
            READ_TIMEOUT_RANGE,
            "READ_TIMEOUT_SECONDS",
        )
        round_delay = self._parse_float_setting(
            self.round_delay_row,
            ROUND_DELAY_RANGE,
            "ROUND_DELAY_SECONDS",
        )
        channel_delay = self._parse_float_setting(
            self.channel_delay_row,
            CHANNEL_DELAY_RANGE,
            "CHANNEL_DELAY_SECONDS",
        )
        curve_seconds = self._parse_int_setting(
            self.curve_seconds_row,
            (int(CURVE_VISIBLE_SECONDS_RANGE[0]), int(CURVE_VISIBLE_SECONDS_RANGE[1])),
            "CURVE_VISIBLE_SECONDS",
            int(self.settings.curve_visible_seconds) if auto_curve_visible_seconds else None,
        )
        max_points = self._parse_int_setting(
            self.max_points_row,
            MAX_POINTS_RANGE,
            "DEFAULT_MAX_POINTS",
        )
        display_decimals = self._parse_int_setting(
            self.display_decimals_row,
            DISPLAY_DECIMALS_RANGE,
            "DISPLAY_DECIMALS",
        )
        curve_line_width = self._parse_float_setting(
            self.curve_line_width_row,
            CURVE_LINE_WIDTH_RANGE,
            "CURVE_LINE_WIDTH",
        )
        y_axis_min = self._parse_float_setting(
            self.y_axis_min_row,
            Y_AXIS_RANGE,
            "Y_AXIS_MIN",
        )
        y_axis_max = self._parse_float_setting(
            self.y_axis_max_row,
            Y_AXIS_RANGE,
            "Y_AXIS_MAX",
        )
        save_duration = self._parse_float_setting(
            self.save_duration_row,
            SAVE_DURATION_RANGE,
            "SAVE_DURATION_SECONDS",
            self.settings.save_duration_seconds if auto_save_duration or continuous_recording else None,
        )
        save_interval = self._parse_float_setting(
            self.save_interval_row,
            SAVE_INTERVAL_RANGE,
            "SAVE_INTERVAL_SECONDS",
            self.settings.save_interval_seconds if auto_save_interval else None,
        )

        if None in (
            read_timeout,
            round_delay,
            channel_delay,
            curve_seconds,
            max_points,
            display_decimals,
            curve_line_width,
            y_axis_min,
            y_axis_max,
            save_duration,
            save_interval,
        ):
            return None
        if y_axis_min >= y_axis_max:
            self.y_axis_min_row.set_error(True)
            self.y_axis_max_row.set_error(True)
            self.status_label.setText("状态：Y_AXIS_MIN 必须小于 Y_AXIS_MAX")
            self.y_axis_min_row.input.setFocus()
            return None
        waveform_channel = self._parse_int_setting(self.waveform_channel_row, (1, 2), "CHANNEL")
        waveform_period = self._parse_int_setting(self.waveform_period_row, (1, 65535), "PERIOD_MS")
        waveform_count = self._parse_int_setting(self.waveform_count_row, (1, 65535), "POINT_COUNT")
        if None in (waveform_channel, waveform_period, waveform_count):
            return None
        timing_settings = AppSettings(
            round_delay_seconds=round_delay,
            channel_delay_seconds=channel_delay,
            curve_visible_seconds=float(curve_seconds),
            save_duration_seconds=save_duration,
            save_interval_seconds=save_interval,
            auto_save_interval=auto_save_interval,
            auto_save_duration=auto_save_duration,
            auto_curve_visible_seconds=auto_curve_visible_seconds,
        )
        if (
            not continuous_recording
            and not auto_save_duration
            and effective_save_interval_seconds(timing_settings) > save_duration
        ):
            self.save_interval_row.set_error(True)
            self.status_label.setText("状态：实际保存间隔不能大于 SAVE_DURATION_SECONDS")
            self.save_interval_row.input.setFocus()
            return None
        return AppSettings(
            read_timeout_seconds=read_timeout,
            round_delay_seconds=round_delay,
            channel_delay_seconds=channel_delay,
            curve_visible_seconds=float(curve_seconds),
            max_points=max_points,
            remember_last_port=self.remember_port_row.is_checked(),
            last_port_name=self.settings.last_port_name,
            auto_connect_last_port=self.auto_connect_row.is_checked(),
            display_decimals=display_decimals,
            curve_show_grid=self.curve_grid_row.is_checked(),
            curve_line_width=curve_line_width,
            y_axis_auto_scale=self.y_axis_auto_row.is_checked(),
            y_axis_min=y_axis_min,
            y_axis_max=y_axis_max,
            show_serial_errors=self.show_serial_errors_row.is_checked(),
            show_raw_serial_view=self.show_raw_serial_view_row.is_checked(),
            save_duration_seconds=save_duration,
            save_interval_seconds=save_interval,
            auto_save_interval=auto_save_interval,
            auto_save_duration=auto_save_duration,
            auto_curve_visible_seconds=auto_curve_visible_seconds,
            continuous_recording=continuous_recording,
            operating_mode=self.settings.operating_mode,
            waveform_channel=waveform_channel - 1,
            waveform_period_ms=waveform_period,
            waveform_point_count=waveform_count,
        )

    def _parse_float_setting(
        self,
        row: SettingInputRow,
        value_range: tuple[float, float],
        variable_name: str,
        fallback: float | None = None,
    ) -> float | None:
        """校验浮点参数输入。"""
        if not row.input.isEnabled() and fallback is not None:
            row.set_error(False)
            return fallback
        try:
            value = float(row.value_text())
        except ValueError:
            row.set_error(True)
            self.status_label.setText(f"状态：{variable_name} 请输入数字")
            row.input.setFocus()
            return None
        if not value_range[0] <= value <= value_range[1]:
            row.set_error(True)
            self.status_label.setText(f"状态：{variable_name} 范围为 {value_range[0]} 到 {value_range[1]}")
            row.input.setFocus()
            return None
        return value

    def _parse_int_setting(
        self,
        row: SettingInputRow,
        value_range: tuple[int, int],
        variable_name: str,
        fallback: int | None = None,
    ) -> int | None:
        """校验整数参数输入。"""
        if not row.input.isEnabled() and fallback is not None:
            row.set_error(False)
            return fallback
        try:
            value = int(row.value_text())
        except ValueError:
            row.set_error(True)
            self.status_label.setText(f"状态：{variable_name} 请输入整数")
            row.input.setFocus()
            return None
        if not value_range[0] <= value <= value_range[1]:
            row.set_error(True)
            self.status_label.setText(f"状态：{variable_name} 范围为 {value_range[0]} 到 {value_range[1]}")
            row.input.setFocus()
            return None
        return value

    def _apply_runtime_settings(self) -> None:
        """把已保存设置应用到当前运行中的界面状态。"""
        self.data_store.set_max_points(self.settings.max_points)
        self._apply_display_decimals()
        self._apply_curve_settings()
        self._apply_right_panel_mode()
        self.waveform_view.apply_settings(self.settings)

    def _apply_display_decimals(self) -> None:
        """应用 LED 卡片显示小数位设置。"""
        for card in self.led_cards:
            card.set_decimal_places(self.settings.display_decimals)

    def _apply_curve_settings(self) -> None:
        """应用曲线显示设置。"""
        self.curve_panel.apply_settings(self.settings)

    def _apply_right_panel_mode(self) -> None:
        """根据设置切换右侧曲线/串口原始返回视图。"""
        if self.settings.show_raw_serial_view:
            self.right_panel_stack.setCurrentWidget(self.raw_serial_panel)
            self._refresh_raw_serial_panel()
        else:
            self.right_panel_stack.setCurrentWidget(self.curve_panel)

    def _refresh_raw_serial_panel(self) -> None:
        """刷新当前通道的串口原始返回诊断内容。"""
        self.raw_serial_panel.set_channel(self.current_channel)
        self.raw_serial_panel.set_record(self.raw_serial_records.get(self.current_channel))

    def _clear_current_curve_history(self) -> None:
        """清空当前通道、当前 LED 的内存曲线历史。"""
        self.data_store.clear_history(self.current_channel, self.selected_led)
        self._update_curve()
        self.status_label.setText(f"状态：已清空通道 {self.current_channel} / LED {self.selected_led} 曲线历史")

    def _toggle_connection(self) -> None:
        """连接或断开串口轮询线程。"""
        if self.settings.operating_mode == "waveform":
            self._toggle_waveform()
            return
        if self.serial_poller is not None:
            self._disconnect_serial()
            return

        self._refresh_ports()
        port_name = self.port_combo.currentText()
        if port_name not in self.available_ports:
            self.status_label.setText("状态：请选择COM口")
            return

        if self.settings.remember_last_port:
            self.settings = replace(self.settings, last_port_name=port_name)
            save_settings(self.settings)

        self.serial_poller = SerialPoller(
            port_name,
            self,
            read_timeout_seconds=self.settings.read_timeout_seconds,
            round_delay_seconds=self.settings.round_delay_seconds,
            channel_delay_seconds=self.settings.channel_delay_seconds,
        )
        self.serial_poller.reading_received.connect(self._handle_reading_received)
        self.serial_poller.raw_response_received.connect(self._handle_raw_serial_received)
        self.serial_poller.status_changed.connect(self.status_label.setText)
        self.serial_poller.error_occurred.connect(self._handle_serial_error)
        self.serial_poller.finished.connect(self._handle_serial_finished)
        self.connect_button.setText("断开")
        self.status_label.setText(f"状态：连接中 {port_name}")
        self.serial_poller.start()
        self._update_mode_controls()

    def _disconnect_serial(self) -> None:
        """停止串口线程，按钮状态在 finished 回调中恢复。"""
        if self.serial_poller is None:
            return
        self.status_label.setText("状态：正在断开...")
        self.connect_button.setEnabled(False)
        self.serial_poller.stop()

    def _handle_reading_received(self, channel: int, illuminance_lux: tuple[float, ...]) -> None:
        """接收后台线程推送的最新通道数据，并刷新当前页面。"""
        sample_time = time()
        self.data_store.add_channel_reading(channel, illuminance_lux, timestamp=sample_time)
        if self.continuous_recorder is not None:
            rows_added = self.continuous_recorder.record_channel_reading(channel, illuminance_lux, sample_time)
            if rows_added:
                self.status_label.setText(
                    f"状态：连续记录中，共 {self.continuous_recorder.rows_written} 行，到 "
                    f"{self.continuous_recorder.output_dir}"
                )
        if channel == self.current_channel:
            self._update_cards_for_current_channel()
            self._update_curve()

    def _handle_raw_serial_received(self, channel: int, record: RawSerialRecord) -> None:
        """记录并显示每个通道最近一次串口原始返回。"""
        self.raw_serial_records[channel] = record
        if self.settings.show_raw_serial_view and channel == self.current_channel:
            self.raw_serial_panel.set_record(record)

    def _handle_serial_error(self, message: str) -> None:
        """记录串口错误次数，按两个模式共用的格式显示。"""
        self.serial_error_count += 1
        self._display_serial_error(message)

    def _display_serial_error(self, message: str) -> None:
        """两个模式共用错误前缀、次数和提示开关；重显不重复计数。"""
        if not self.settings.show_serial_errors:
            return
        self.status_label.setText(f"状态：{message} x {self.serial_error_count}")

    def _handle_serial_finished(self) -> None:
        """串口线程退出后恢复连接按钮状态。"""
        self.serial_poller = None
        self.connect_button.setText("连接")
        self.connect_button.setEnabled(True)
        self._update_mode_controls()
        if self._close_requested:
            QTimer.singleShot(0, self.close)
        if self.continuous_recorder is not None:
            self.status_label.setText("状态：串口已断开，连续记录暂停，点击停止生成Excel")

    def _update_cards_for_current_channel(self) -> None:
        """用当前通道最新值刷新 16 个 LED 卡片。"""
        values = self.data_store.get_latest(self.current_channel)
        for index, card in enumerate(self.led_cards):
            card.set_illuminance_value(values[index] if values is not None else None)

    def _update_curve(self) -> None:
        """刷新当前通道、当前 LED 的实时历史曲线。"""
        points = self.data_store.get_history(self.current_channel, self.selected_led)
        self.curve_panel.set_history(points)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 事件函数沿用 Qt 命名。
        """关闭窗口前停止后台串口线程，避免进程残留。"""
        if self.waveform_worker is not None or self.serial_poller is not None:
            self._close_requested = True
            if self.waveform_worker is not None:
                self.waveform_worker.stop()
            if self.serial_poller is not None:
                self.serial_poller.stop()
            self.connect_button.setEnabled(False)
            self.status_label.setText("状态：正在停止并关闭，请稍候...")
            event.ignore()
            return
        if self.continuous_recorder is not None:
            self.continuous_recorder.close()
        super().closeEvent(event)

    def _change_operating_mode(self, label: str) -> None:
        if self.serial_poller is not None or self.waveform_worker is not None or self.continuous_recorder is not None:
            return
        mode = "waveform" if label == "光源波形" else "brightness"
        updated = replace(self.settings, operating_mode=mode)
        try:
            save_settings(updated)
        except OSError as exc:
            self.mode_combo.setText("光源波形" if self.settings.operating_mode == "waveform" else "亮度检测")
            self.status_label.setText(f"状态：模式保存失败：{exc}")
            return
        self.settings = updated
        self._apply_operating_mode()
        self.status_label.setText(f"状态：已切换到{label}，请选择串口")

    def _apply_operating_mode(self) -> None:
        self.mode_combo.setText("光源波形" if self.settings.operating_mode == "waveform" else "亮度检测")
        self._show_monitor_page()

    def _update_mode_controls(self) -> None:
        busy = self.serial_poller is not None or self.waveform_worker is not None
        self.mode_combo.setEnabled(not busy and self.continuous_recorder is None and self.page_stack.currentWidget() != self.settings_view)
        self.port_combo.setEnabled(not busy)
        waveform = self.settings.operating_mode == "waveform"
        if waveform:
            self.connect_button.setText("停止采集" if self.waveform_worker is not None else "开始采集")
            self.export_button.setText("保存本次")
            self.export_button.setEnabled(self.waveform_worker is None)
            self.settings_button.setEnabled(self.waveform_worker is None)
        else:
            self.connect_button.setText("断开" if self.serial_poller is not None else "连接")
            self.export_button.setText("停止" if self.continuous_recorder is not None else "保存")

    def _toggle_waveform(self) -> None:
        if self.waveform_worker is not None:
            self.waveform_worker.stop()
            self.connect_button.setEnabled(False)
            self.status_label.setText("状态：正在停止采集并关灯...")
            return
        self._refresh_ports()
        port_name = self.port_combo.currentText()
        if port_name not in self.available_ports:
            self.status_label.setText("状态：请选择COM口")
            return
        if self.settings.remember_last_port:
            self.settings = replace(self.settings, last_port_name=port_name)
            save_settings(self.settings)
        self._show_monitor_page()
        self.waveform_view.reset_for_session(self.settings)
        worker = WaveformWorker(port_name, self.settings.waveform_channel, self.settings.waveform_period_ms,
                                self.settings.waveform_point_count, self)
        self.waveform_worker = worker
        worker.points_received.connect(self.waveform_view.add_points)
        worker.raw_response_received.connect(self.waveform_view.set_raw_record)
        worker.status_changed.connect(self.status_label.setText)
        worker.error_occurred.connect(self._handle_waveform_error)
        worker.finished.connect(self._handle_waveform_finished)
        self._update_mode_controls()
        self.status_label.setText(f"状态：正在连接 {port_name}")
        worker.start()

    def _handle_waveform_finished(self) -> None:
        worker = self.waveform_worker
        self.waveform_worker = None
        self.connect_button.setEnabled(True)
        self._update_mode_controls()
        self.waveform_view.refresh_curve()
        if worker.failure_message:
            if self.settings.show_serial_errors:
                self._display_serial_error(f"通道 {worker.channel + 1} 读取失败：{worker.failure_message}")
            else:
                summary = "采集已结束" if worker.completed else "采集未完成"
                light = "已确认关灯" if worker.light_off_confirmed else "光源状态未确认"
                self.status_label.setText(f"状态：{summary}，{light}")
        elif worker.completed:
            message = f"采集完成，共 {len(self.waveform_view.points)} 点，已确认关灯"
            self.status_label.setText("状态：" + message)
        else:
            message = f"已手动停止，保留 {len(self.waveform_view.points)} 点" + ("，已确认关灯" if worker.light_off_confirmed else "")
            self.status_label.setText("状态：" + message)
        worker.deleteLater()
        if self._close_requested:
            if not worker.light_off_confirmed and worker.failure_message:
                QMessageBox.warning(self, "光源状态未确认", worker.failure_message)
            QTimer.singleShot(0, self.close)

    def _handle_waveform_error(self, message: str) -> None:
        self._handle_serial_error(f"通道 {self.settings.waveform_channel + 1} 读取失败：{message}")


def run() -> int:
    """创建 Qt 应用并显示主窗口。"""
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    window = MainWindow()
    window.show()
    return app.exec()
