"""
Main application window for ONVIF PTZ Controller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from functools import partial

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit, QSpinBox, QGroupBox,
    QListWidget, QListWidgetItem, QStatusBar, QMessageBox,
    QSlider, QSplitter, QFrame, QInputDialog, QApplication,
    QSizePolicy, QToolBar, QComboBox, QAbstractSpinBox, QDoubleSpinBox,
    QScrollArea, QTabWidget, QStackedWidget, QRadioButton, QButtonGroup,
    QDialog, QDialogButtonBox, QFormLayout, QMenu, QCheckBox, QToolButton,
    QStyle, QFileDialog,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSlot, pyqtSignal, QSize, QStandardPaths, QEvent, QPoint, QMimeData, QIODevice
from PyQt6.QtGui import QImage, QPixmap, QIcon, QKeySequence, QShortcut, QAction, QColor, QDrag
from PyQt6.QtMultimedia import QAudioSink, QAudioFormat, QAudio

from camera_discovery import (
    DiscoveredCameraInfo,
    default_discovery_targets,
    discover_onvif_cameras,
    parse_discovery_targets,
)
from onvif_client import (
    ONVIFPTZClient,
    ImagingSettingsPayload,
    MediaProfileInfo,
    NetworkSettingsPayload,
    Preset,
    PTZStatus,
    UserAccountInfo,
    VideoEncoderSettings,
    VideoResolutionOption,
)
from video_player import VideoStreamThread, AUDIO_OUTPUT_SAMPLE_RATE, AUDIO_OUTPUT_CHANNELS

logger = logging.getLogger(__name__)

# PTZ speed
DEFAULT_PTZ_SPEED = 0.5
WORKSPACE_MODE_SINGLE = "single"
WORKSPACE_MODE_MATRIX = "matrix"


@dataclass
class CameraSession:
    """Per-camera state stored behind a browser-like tab."""
    camera_id: str
    page: QWidget
    video_widget: VideoWidget
    matrix_tile: CameraMatrixTile
    matrix_video_widget: VideoWidget
    matrix_title_label: QLabel
    client: ONVIFPTZClient
    loop: asyncio.AbstractEventLoop
    host: str
    port: int
    username: str
    password: str
    stream_profiles: list[MediaProfileInfo] = field(default_factory=list)
    active_stream_token: Optional[str] = None
    current_stream_uri: Optional[str] = None
    video_thread: Optional[VideoStreamThread] = None
    last_status: PTZStatus = field(default_factory=PTZStatus)
    audio_volume: float = 0.0


@dataclass
class SavedCameraConfig:
    """Persisted camera configuration used for auto-reconnect."""

    host: str
    port: int
    username: str
    password: str
    active_stream_token: Optional[str] = None

    @property
    def camera_id(self) -> str:
        return f"{self.host}:{self.port}:{self.username}"


class VolumeButtonWidget(QWidget):
    """Compact speaker button that shows a vertical slider popup on click."""

    volume_changed = pyqtSignal(float)
    _BTN = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self._BTN, self._BTN)
        self._volume = 0.0

        self._btn = QToolButton(self)
        self._btn.setFixedSize(self._BTN, self._BTN)
        self._btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn.setAutoRaise(True)
        self._btn.setStyleSheet("""
            QToolButton {
                background-color: rgba(20,20,20,0.82);
                border: 1px solid #444;
                border-radius: 4px;
                padding: 2px;
            }
            QToolButton:hover { background-color: rgba(55,55,55,0.95); border-color: #3794ff; }
        """)
        self._btn.clicked.connect(self._toggle_popup)

        self._popup = QFrame(None,
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._popup.setFixedSize(32, 110)
        self._popup.setStyleSheet("""
            QFrame { background:#1e1e1e; border:1px solid #3c3c3c; border-radius:6px; }
            QSlider::groove:vertical { background:#3c3c3c; width:4px; border-radius:2px; }
            QSlider::handle:vertical { background:#3794ff; width:12px; height:12px;
                                       margin:-4px; border-radius:6px; }
            QSlider::sub-page:vertical { background:#3794ff; border-radius:2px; }
        """)
        pl = QVBoxLayout(self._popup)
        pl.setContentsMargins(4, 6, 4, 6)
        self._slider = QSlider(Qt.Orientation.Vertical)
        self._slider.setRange(0, 100)
        self._slider.setValue(0)
        self._slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slider.valueChanged.connect(self._on_slider)
        pl.addWidget(self._slider)

        self._update_icon()

    def set_volume(self, v: float):
        self._volume = max(0.0, min(1.0, v))
        self._slider.blockSignals(True)
        self._slider.setValue(round(self._volume * 100))
        self._slider.blockSignals(False)
        self._update_icon()

    def get_volume(self) -> float:
        return self._volume

    def _toggle_popup(self):
        if self._popup.isVisible():
            self._popup.hide()
        else:
            pos = self._btn.mapToGlobal(QPoint(
                (self._BTN - self._popup.width()) // 2, -self._popup.height() - 4
            ))
            self._popup.move(pos)
            self._popup.show()

    def _on_slider(self, value: int):
        self._volume = value / 100.0
        self._update_icon()
        self.volume_changed.emit(self._volume)

    def _update_icon(self):
        sp = (QStyle.StandardPixmap.SP_MediaVolumeMuted
              if self._volume == 0.0
              else QStyle.StandardPixmap.SP_MediaVolume)
        self._btn.setIcon(self.style().standardIcon(sp))

    def hideEvent(self, event):
        self._popup.hide()
        super().hideEvent(event)


class VideoWidget(QLabel):
    """Widget for displaying the video stream."""

    def __init__(self, parent=None, minimum_size: Optional[QSize] = None):
        super().__init__(parent)
        self.setMinimumSize(minimum_size or QSize(640, 360))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setText("No video stream")
        self.setStyleSheet("""
            QLabel {
                background-color: #111111;
                color: #808080;
                font-size: 16px;
                border: 1px solid #2d2d30;
                border-radius: 6px;
            }
        """)
        self.volume_btn = VolumeButtonWidget(self)
        self.volume_btn.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        bw = self.volume_btn.width()
        bh = self.volume_btn.height()
        self.volume_btn.move(self.width() - bw - 8, self.height() - bh - 8)
        self.volume_btn.raise_()

    @pyqtSlot(QImage)
    def update_frame(self, image: QImage):
        """Update displayed frame, scaling to widget size."""
        pixmap = QPixmap.fromImage(image)
        scaled = pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self.setPixmap(scaled)


class CameraMatrixTile(QFrame):
    """Clickable tile used in matrix mode."""

    clicked = pyqtSignal(str)
    double_clicked = pyqtSignal(str)
    swap_requested = pyqtSignal(str, str)
    remove_requested = pyqtSignal(str)

    def __init__(self, camera_id: str, title: str, parent=None):
        super().__init__(parent)
        self.camera_id = camera_id
        self._drag_start_position = QPoint()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptDrops(True)
        self.setProperty("selected", False)
        self.setStyleSheet("""
            QFrame {
                background-color: #181818;
                border: 1px solid #2d2d30;
                border-radius: 10px;
            }
            QFrame[selected="true"] {
                background-color: #1f1f1f;
                border: 1px solid #3794ff;
            }
            QLabel#matrixCameraTitle {
                color: #c5c5c5;
                font-size: 12px;
                font-weight: 600;
                padding: 2px 4px 0 4px;
            }
            QToolButton#matrixCloseButton {
                background-color: rgba(30, 30, 30, 0.95);
                border: 1px solid #3c3c3c;
                border-radius: 10px;
                padding: 2px;
            }
            QToolButton#matrixCloseButton:hover {
                background-color: #2d2d30;
                border-color: #3794ff;
            }
            QFrame[selected="true"] QLabel#matrixCameraTitle {
                color: #ffffff;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("matrixCameraTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setFixedHeight(34)
        header_layout.addWidget(self.title_label, 1)

        self.close_button = QToolButton()
        self.close_button.setObjectName("matrixCloseButton")
        self.close_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton))
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setAutoRaise(True)
        self.close_button.setFixedSize(20, 20)
        self.close_button.setIconSize(QSize(12, 12))
        self.close_button.setVisible(False)
        self.close_button.clicked.connect(lambda: self.remove_requested.emit(self.camera_id))
        header_layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)

        layout.addLayout(header_layout)

        self.video_widget = VideoWidget(minimum_size=QSize(160, 90))
        self.video_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.video_widget, 1)

        # Volume button as direct child of tile (outside WA_TransparentForMouseEvents area)
        self.volume_btn = VolumeButtonWidget(self)
        self.volume_btn.hide()

        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.video_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.set_preview_size(320, 180)

    def set_title(self, title: str):
        self.title_label.setText(title)

    def set_selected(self, selected: bool):
        self.setProperty("selected", selected)
        self.close_button.setVisible(selected)
        self.volume_btn.setVisible(selected)
        if selected:
            self.volume_btn.raise_()
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_preview_size(self, width: int, height: int):
        width = max(160, width)
        height = max(90, height)
        self.video_widget.setFixedSize(width, height)
        self.setFixedSize(width + 20, height + 62)
        # Position volume_btn at bottom-right of the video area
        # video_widget starts at x=10, y=52 (10 margin + 34 header + 8 spacing)
        bsize = VolumeButtonWidget._BTN
        self.volume_btn.move(10 + width - bsize - 6, 52 + height - bsize - 6)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_position = event.position().toPoint()
            self.clicked.emit(self.camera_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self.camera_id)
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return

        if (event.position().toPoint() - self._drag_start_position).manhattanLength() < QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return

        mime_data = QMimeData()
        mime_data.setText(self.camera_id)
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.setPixmap(self.grab())
        drag.exec(Qt.DropAction.MoveAction)
        super().mouseMoveEvent(event)

    def dragEnterEvent(self, event):
        source_id = event.mimeData().text().strip()
        if source_id and source_id != self.camera_id:
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        source_id = event.mimeData().text().strip()
        if source_id and source_id != self.camera_id:
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        source_id = event.mimeData().text().strip()
        if source_id and source_id != self.camera_id:
            self.swap_requested.emit(source_id, self.camera_id)
            event.acceptProposedAction()
            return
        event.ignore()


class PTZControlWidget(QGroupBox):
    """Widget with PTZ directional controls and zoom."""

    # Movement modes
    MODE_CONTINUOUS = "Continuous"
    MODE_RELATIVE = "Relative"
    MODE_ABSOLUTE = "Absolute"

    def __init__(self, parent=None):
        super().__init__("PTZ Control", parent)
        self.ptz_callback = None  # callback(mode, pan, tilt, zoom)
        self.stop_callback = None  # async callback for stop

        self._speed = DEFAULT_PTZ_SPEED
        self._init_ui()
        self._setup_shortcuts()

    def _init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(10)

        # Mode selector
        mode_layout = QVBoxLayout()
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(6)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)

        mode_buttons_layout = QHBoxLayout()
        mode_buttons_layout.setContentsMargins(0, 0, 0, 0)
        mode_buttons_layout.setSpacing(14)
        self.mode_buttons: dict[str, QRadioButton] = {}
        for index, mode in enumerate(
            [self.MODE_CONTINUOUS, self.MODE_RELATIVE, self.MODE_ABSOLUTE]
        ):
            button = QRadioButton(mode)
            button.setObjectName("modeToggle")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            button.toggled.connect(
                lambda checked, selected_mode=mode: checked and self._on_mode_changed(selected_mode)
            )
            self.mode_group.addButton(button, index)
            self.mode_buttons[mode] = button
            mode_buttons_layout.addWidget(button)

        mode_layout.addLayout(mode_buttons_layout)
        main_layout.addLayout(mode_layout)

        # Direction controls
        dir_layout = QGridLayout()
        dir_layout.setContentsMargins(0, 0, 0, 0)
        dir_layout.setSpacing(8)

        # Arrow buttons
        self.btn_up = self._make_btn("▲", "Up (W)")
        self.btn_down = self._make_btn("▼", "Down (S)")
        self.btn_left = self._make_btn("◀", "Left (A)")
        self.btn_right = self._make_btn("▶", "Right (D)")
        self.btn_up_left = self._make_btn("◤", "Up-Left (Q)")
        self.btn_up_right = self._make_btn("◥", "Up-Right (E)")
        self.btn_down_left = self._make_btn("◣", "Down-Left (Z)")
        self.btn_down_right = self._make_btn("◢", "Down-Right (C)")
        self.btn_stop = self._make_btn("⏹", "Stop (Space)")
        self.btn_stop.setObjectName("ptzStopButton")

        dir_layout.addWidget(self.btn_up_left, 0, 0)
        dir_layout.addWidget(self.btn_up, 0, 1)
        dir_layout.addWidget(self.btn_up_right, 0, 2)
        dir_layout.addWidget(self.btn_left, 1, 0)
        dir_layout.addWidget(self.btn_stop, 1, 1)
        dir_layout.addWidget(self.btn_right, 1, 2)
        dir_layout.addWidget(self.btn_down_left, 2, 0)
        dir_layout.addWidget(self.btn_down, 2, 1)
        dir_layout.addWidget(self.btn_down_right, 2, 2)

        main_layout.addLayout(dir_layout)

        # Zoom controls
        zoom_layout = QHBoxLayout()
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.setSpacing(8)
        self.btn_zoom_in = QPushButton("Zoom +")
        self.btn_zoom_out = QPushButton("Zoom -")
        for btn in (self.btn_zoom_in, self.btn_zoom_out):
            btn.setObjectName("ptzZoomButton")
            btn.setMinimumHeight(34)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        zoom_layout.addWidget(self.btn_zoom_out)
        zoom_layout.addWidget(self.btn_zoom_in)
        main_layout.addLayout(zoom_layout)

        # Speed slider
        speed_layout = QHBoxLayout()
        speed_layout.setContentsMargins(0, 0, 0, 0)
        speed_layout.setSpacing(8)
        speed_layout.addWidget(QLabel("Speed:"))
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setMinimum(1)
        self.speed_slider.setMaximum(100)
        self.speed_slider.setValue(int(DEFAULT_PTZ_SPEED * 100))
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        speed_layout.addWidget(self.speed_slider)
        self.speed_label = QLabel(f"{DEFAULT_PTZ_SPEED:.2f}")
        speed_layout.addWidget(self.speed_label)
        main_layout.addLayout(speed_layout)

        # --- Absolute mode settings ---
        self.absolute_group = QGroupBox("Absolute Position")
        abs_layout = QGridLayout()
        abs_layout.setContentsMargins(0, 0, 0, 0)
        abs_layout.setHorizontalSpacing(8)
        abs_layout.setVerticalSpacing(8)
        abs_layout.addWidget(QLabel("Pan:"), 0, 0)
        self.abs_pan = QDoubleSpinBox()
        self.abs_pan.setRange(-1.0, 1.0)
        self.abs_pan.setSingleStep(0.05)
        self.abs_pan.setDecimals(3)
        self.abs_pan.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        abs_layout.addWidget(self.abs_pan, 0, 1)
        abs_layout.addWidget(QLabel("Tilt:"), 0, 2)
        self.abs_tilt = QDoubleSpinBox()
        self.abs_tilt.setRange(-1.0, 1.0)
        self.abs_tilt.setSingleStep(0.05)
        self.abs_tilt.setDecimals(3)
        self.abs_tilt.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        abs_layout.addWidget(self.abs_tilt, 0, 3)
        abs_layout.addWidget(QLabel("Zoom:"), 1, 0)
        self.abs_zoom = QDoubleSpinBox()
        self.abs_zoom.setRange(0.0, 1.0)
        self.abs_zoom.setSingleStep(0.05)
        self.abs_zoom.setDecimals(3)
        self.abs_zoom.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        abs_layout.addWidget(self.abs_zoom, 1, 1)
        self.btn_abs_go = QPushButton("Go To Position")
        self.btn_abs_go.setMinimumHeight(32)
        self.btn_abs_go.setObjectName("primaryButton")
        self.btn_abs_go.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_abs_go.clicked.connect(self._on_absolute_go)
        abs_layout.addWidget(self.btn_abs_go, 1, 2, 1, 2)
        self.absolute_group.setLayout(abs_layout)
        self.absolute_group.setVisible(False)
        main_layout.addWidget(self.absolute_group)

        # --- Relative mode settings ---
        self.relative_group = QGroupBox("Relative Step Size")
        rel_layout = QHBoxLayout()
        rel_layout.setContentsMargins(0, 0, 0, 0)
        rel_layout.setSpacing(8)
        rel_layout.addWidget(QLabel("Step:"))
        self.rel_step = QDoubleSpinBox()
        self.rel_step.setRange(0.01, 1.0)
        self.rel_step.setSingleStep(0.01)
        self.rel_step.setValue(0.1)
        self.rel_step.setDecimals(3)
        self.rel_step.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        rel_layout.addWidget(self.rel_step)
        self.relative_group.setLayout(rel_layout)
        self.relative_group.setVisible(False)
        main_layout.addWidget(self.relative_group)

        self.setLayout(main_layout)
        self.mode_buttons[self.MODE_CONTINUOUS].setChecked(True)
        self._on_mode_changed(self.MODE_CONTINUOUS)

        # Connect button signals
        self.btn_up.pressed.connect(lambda: self._move(0, 1, 0))
        self.btn_down.pressed.connect(lambda: self._move(0, -1, 0))
        self.btn_left.pressed.connect(lambda: self._move(-1, 0, 0))
        self.btn_right.pressed.connect(lambda: self._move(1, 0, 0))
        self.btn_up_left.pressed.connect(lambda: self._move(-1, 1, 0))
        self.btn_up_right.pressed.connect(lambda: self._move(1, 1, 0))
        self.btn_down_left.pressed.connect(lambda: self._move(-1, -1, 0))
        self.btn_down_right.pressed.connect(lambda: self._move(1, -1, 0))
        self.btn_zoom_in.pressed.connect(lambda: self._move(0, 0, 1))
        self.btn_zoom_out.pressed.connect(lambda: self._move(0, 0, -1))

        self.btn_stop.clicked.connect(self._stop)

        # Release = stop for continuous mode
        for btn in (self.btn_up, self.btn_down, self.btn_left, self.btn_right,
                    self.btn_up_left, self.btn_up_right, self.btn_down_left,
                    self.btn_down_right, self.btn_zoom_in, self.btn_zoom_out):
            btn.released.connect(self._on_release)

    @property
    def current_mode(self) -> str:
        for mode, button in self.mode_buttons.items():
            if button.isChecked():
                return mode
        return self.MODE_CONTINUOUS

    def _on_mode_changed(self, mode: str):
        self.absolute_group.setVisible(mode == self.MODE_ABSOLUTE)
        self.relative_group.setVisible(mode == self.MODE_RELATIVE)

    def _make_btn(self, text: str, tooltip: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.setObjectName("ptzDirectionButton")
        btn.setMinimumSize(50, 50)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return btn

    def _setup_shortcuts(self):
        """Configure keyboard shortcuts for PTZ."""
        pass  # Handled via keyPressEvent / keyReleaseEvent

    def _on_speed_changed(self, value: int):
        self._speed = value / 100.0
        self.speed_label.setText(f"{self._speed:.2f}")

    def _move(self, pan_dir: int, tilt_dir: int, zoom_dir: int):
        if not self.ptz_callback:
            return
        mode = self.current_mode
        if mode == self.MODE_CONTINUOUS:
            pan = pan_dir * self._speed
            tilt = tilt_dir * self._speed
            zoom = zoom_dir * self._speed
            self.ptz_callback(mode, pan, tilt, zoom)
        elif mode == self.MODE_RELATIVE:
            step = self.rel_step.value()
            pan = pan_dir * step
            tilt = tilt_dir * step
            zoom = zoom_dir * step
            self.ptz_callback(mode, pan, tilt, zoom)
        elif mode == self.MODE_ABSOLUTE:
            # Direction buttons nudge absolute fields
            step = 0.05
            self.abs_pan.setValue(self.abs_pan.value() + pan_dir * step)
            self.abs_tilt.setValue(self.abs_tilt.value() + tilt_dir * step)
            self.abs_zoom.setValue(max(0, self.abs_zoom.value() + zoom_dir * step))

    def _on_release(self):
        """On button release — stop only in continuous mode."""
        if self.current_mode == self.MODE_CONTINUOUS:
            self._stop()

    def _on_absolute_go(self):
        """Send absolute move command from the spin boxes."""
        if self.ptz_callback:
            self.ptz_callback(
                self.MODE_ABSOLUTE,
                self.abs_pan.value(),
                self.abs_tilt.value(),
                self.abs_zoom.value(),
            )

    def _stop(self):
        if self.stop_callback:
            self.stop_callback()


class PresetsWidget(QGroupBox):
    """Widget for managing camera presets."""

    def __init__(self, parent=None):
        super().__init__("Presets", parent)
        self.goto_callback = None
        self.refresh_callback = None
        self.save_callback = None
        self.delete_callback = None
        self._all_presets: list[Preset] = []

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()

        # Search field
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search presets...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: #1a1a2e; color: #eee;
                border: 1px solid #444; border-radius: 4px;
                padding: 4px 8px; font-size: 13px;
            }
            QLineEdit:focus { border-color: #3498db; }
        """)
        self.search_input.textChanged.connect(self._filter_presets)
        layout.addWidget(self.search_input)

        self.presets_list = QListWidget()
        self.presets_list.setMinimumHeight(120)
        self.presets_list.setStyleSheet("""
            QListWidget {
                background-color: #1a1a2e; color: #eee;
                border: 1px solid #444; border-radius: 4px;
                font-size: 13px;
            }
            QListWidget::item:selected { 
                background-color: #2980b9; 
            }
            QListWidget::item:hover {
                background-color: #34495e;
            }
        """)
        layout.addWidget(self.presets_list)

        btn_layout = QGridLayout()

        self.btn_goto = QPushButton("▶ Go To")
        self.btn_refresh = QPushButton("🔄 Refresh")
        self.btn_save = QPushButton("💾 Save New")
        self.btn_delete = QPushButton("🗑 Delete")

        for btn in (self.btn_goto, self.btn_refresh, self.btn_save, self.btn_delete):
            btn.setMinimumHeight(32)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #2c3e50; color: white;
                    font-size: 12px; border-radius: 4px; padding: 4px 8px;
                }
                QPushButton:hover { background-color: #34495e; }
                QPushButton:pressed { background-color: #1a252f; }
            """)

        btn_layout.addWidget(self.btn_goto, 0, 0)
        btn_layout.addWidget(self.btn_refresh, 0, 1)
        btn_layout.addWidget(self.btn_save, 1, 0)
        btn_layout.addWidget(self.btn_delete, 1, 1)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

        # Connect signals
        self.btn_goto.clicked.connect(self._on_goto)
        self.btn_refresh.clicked.connect(self._on_refresh)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_delete.clicked.connect(self._on_delete)
        self.presets_list.itemDoubleClicked.connect(self._on_item_double_clicked)

    def update_presets(self, presets: list[Preset]):
        """Update the presets list widget."""
        self._all_presets = presets
        self._filter_presets(self.search_input.text())

    def _filter_presets(self, text: str):
        """Filter displayed presets by search text."""
        query = text.strip().lower()
        self.presets_list.clear()
        for preset in self._all_presets:
            if query and query not in preset.name.lower():
                continue
            item = QListWidgetItem(f"{preset.name}")
            item.setData(Qt.ItemDataRole.UserRole, preset.token)
            self.presets_list.addItem(item)

    def _get_selected_token(self) -> Optional[str]:
        item = self.presets_list.currentItem()
        if item:
            return item.data(Qt.ItemDataRole.UserRole)
        return None

    def _on_goto(self):
        token = self._get_selected_token()
        if token and self.goto_callback:
            self.goto_callback(token)

    def _on_refresh(self):
        if self.refresh_callback:
            self.refresh_callback()

    def _on_save(self):
        if self.save_callback:
            self.save_callback()

    def _on_delete(self):
        token = self._get_selected_token()
        if token and self.delete_callback:
            self.delete_callback(token)

    def _on_item_double_clicked(self, item: QListWidgetItem):
        token = item.data(Qt.ItemDataRole.UserRole)
        if token and self.goto_callback:
            self.goto_callback(token)


class ConnectionWidget(QGroupBox):
    """Widget for camera connection settings."""

    def __init__(self, parent=None):
        super().__init__("Camera Connection", parent)
        self.connect_callback = None
        self.disconnect_callback = None

        self._init_ui()

    def _init_ui(self):
        layout = QGridLayout()

        layout.addWidget(QLabel("Host:"), 0, 0)
        self.host_input = QLineEdit("172.18.212.18")
        layout.addWidget(self.host_input, 0, 1)

        layout.addWidget(QLabel("Port:"), 0, 2)
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(80)
        self.port_input.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        layout.addWidget(self.port_input, 0, 3)

        layout.addWidget(QLabel("User:"), 1, 0)
        self.user_input = QLineEdit("admin")
        layout.addWidget(self.user_input, 1, 1)

        layout.addWidget(QLabel("Password:"), 1, 2)
        self.pass_input = QLineEdit("Supervisor")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.pass_input, 1, 3)

        btn_layout = QHBoxLayout()
        self.btn_connect = QPushButton("🔗 Add Camera")
        self.btn_connect.setMinimumHeight(36)
        self.btn_connect.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white;
                font-size: 14px; font-weight: bold;
                border-radius: 6px; padding: 6px 20px;
            }
            QPushButton:hover { background-color: #2ecc71; }
            QPushButton:pressed { background-color: #1e8449; }
        """)

        self.btn_disconnect = QPushButton("✖ Remove Camera")
        self.btn_disconnect.setMinimumHeight(36)
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.setStyleSheet("""
            QPushButton {
                background-color: #c0392b; color: white;
                font-size: 14px; font-weight: bold;
                border-radius: 6px; padding: 6px 20px;
            }
            QPushButton:hover { background-color: #e74c3c; }
            QPushButton:pressed { background-color: #96281b; }
            QPushButton:disabled { background-color: #555; color: #999; }
        """)

        btn_layout.addWidget(self.btn_connect)
        btn_layout.addWidget(self.btn_disconnect)
        layout.addLayout(btn_layout, 2, 0, 1, 4)

        self.setLayout(layout)

        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect.clicked.connect(self._on_disconnect)

    def _on_connect(self):
        if self.connect_callback:
            self.connect_callback(
                self.host_input.text().strip(),
                self.port_input.value(),
                self.user_input.text().strip(),
                self.pass_input.text(),
            )

    def _on_disconnect(self):
        if self.disconnect_callback:
            self.disconnect_callback()

    def set_connected(self, connected: bool):
        self.btn_disconnect.setEnabled(connected)


class AddCameraDialog(QDialog):
    """Compact modal dialog for adding a camera."""

    def __init__(
        self,
        initial: Optional[SavedCameraConfig] = None,
        parent=None,
        *,
        dialog_title: str = "Add Camera",
        confirm_text: str = "Add Camera",
        allow_local_search: bool = True,
    ):
        super().__init__(parent)
        self.setWindowTitle(dialog_title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self._confirm_text = confirm_text
        self._allow_local_search = allow_local_search
        self._discovered_configs: list[SavedCameraConfig] = []
        self._initial = initial or SavedCameraConfig(
            host="172.18.212.18",
            port=80,
            username="admin",
            password="Supervisor",
        )
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        intro = QLabel(
            "The camera will be saved locally and will open in a browser tab when you click it in Saved Cameras."
        )
        intro.setWordWrap(True)
        intro.setObjectName("dialogHint")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.host_input = QLineEdit(self._initial.host)
        self.host_input.setPlaceholderText("192.168.1.100")
        form.addRow("Host", self.host_input)

        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(self._initial.port)
        self.port_input.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        form.addRow("Port", self.port_input)

        self.user_input = QLineEdit(self._initial.username)
        form.addRow("User", self.user_input)

        self.pass_input = QLineEdit(self._initial.password)
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password", self.pass_input)

        layout.addLayout(form)

        if self._allow_local_search:
            actions_row = QHBoxLayout()
            actions_row.addStretch(1)
            self.local_search_button = QPushButton("Local Search")
            self.local_search_button.clicked.connect(self._open_local_search)
            actions_row.addWidget(self.local_search_button)
            layout.addLayout(actions_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self._confirm_text)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self):
        self._discovered_configs = []
        if not self.host_input.text().strip():
            QMessageBox.warning(self, "Validation Error", "Host is required.")
            return
        self.accept()

    def _open_local_search(self):
        dialog = LocalSearchDialog(self.camera_config(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        configs = dialog.camera_configs()
        if not configs:
            return

        self._discovered_configs = configs
        first_config = configs[0]
        self.host_input.setText(first_config.host)
        self.port_input.setValue(first_config.port)
        self.user_input.setText(first_config.username)
        self.pass_input.setText(first_config.password)
        self.accept()

    def camera_config(self) -> SavedCameraConfig:
        return SavedCameraConfig(
            host=self.host_input.text().strip(),
            port=self.port_input.value(),
            username=self.user_input.text().strip(),
            password=self.pass_input.text(),
        )

    def camera_configs(self) -> list[SavedCameraConfig]:
        if self._discovered_configs:
            return list(self._discovered_configs)
        return [self.camera_config()]


class CameraCredentialsDialog(QDialog):
    """Prompts for credentials when a discovered camera requires authentication."""

    def __init__(
        self,
        initial: Optional[SavedCameraConfig] = None,
        parent=None,
        *,
        dialog_title: str = "Camera Credentials",
        intro_text: str = "Enter camera credentials.",
        confirm_text: str = "Save Camera",
    ):
        super().__init__(parent)
        self.setWindowTitle(dialog_title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self._initial = initial or SavedCameraConfig(host="", port=80, username="", password="")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        intro = QLabel(intro_text)
        intro.setWordWrap(True)
        intro.setObjectName("dialogHint")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.user_input = QLineEdit(self._initial.username)
        form.addRow("User", self.user_input)

        self.pass_input = QLineEdit(self._initial.password)
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password", self.pass_input)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(confirm_text)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def credentials(self) -> tuple[str, str]:
        return self.user_input.text().strip(), self.pass_input.text()


class LocalSearchDialog(QDialog):
    """Searches the local network for ONVIF cameras."""

    def __init__(
        self,
        initial: Optional[SavedCameraConfig] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Local Camera Search")
        self.setModal(True)
        self.setMinimumSize(640, 420)
        self._initial = initial or SavedCameraConfig(host="", port=80, username="", password="")
        self._camera_map: dict[str, DiscoveredCameraInfo] = {}
        self._selected_configs: list[SavedCameraConfig] = []
        self._init_ui()
        QTimer.singleShot(0, self._refresh_search)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        intro = QLabel(
            "Searches the current local subnet for ONVIF cameras. Double-click a result to save it. If you enter a filter, the search switches to the specified IPs or subnet."
        )
        intro.setWordWrap(True)
        intro.setObjectName("dialogHint")
        layout.addWidget(intro)

        targets_form = QFormLayout()
        targets_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        targets_form.setHorizontalSpacing(12)
        targets_form.setVerticalSpacing(8)

        self.targets_input = QLineEdit()
        self.targets_input.setPlaceholderText("172.18, 172.18.212.18 or 192.168.1.0/24")
        self.targets_input.returnPressed.connect(self._refresh_search)
        targets_form.addRow("Targets", self.targets_input)
        layout.addLayout(targets_form)

        self.status_label = QLabel("Preparing local search...")
        self.status_label.setObjectName("sectionHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.cameras_list = QListWidget()
        self.cameras_list.itemChanged.connect(self._on_camera_item_changed)
        self.cameras_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.cameras_list)

        hint = QLabel(
            "Check the cameras you want to save, then click Add Selected to apply one login and password to all of them."
        )
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        actions_row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh Search")
        self.refresh_button.clicked.connect(self._refresh_search)
        actions_row.addWidget(self.refresh_button)

        self.select_all_button = QPushButton("Select All")
        self.select_all_button.setEnabled(False)
        self.select_all_button.clicked.connect(self._select_all_cameras)
        actions_row.addWidget(self.select_all_button)

        actions_row.addStretch(1)

        self.add_selected_button = QPushButton("Add Selected")
        self.add_selected_button.setEnabled(False)
        self.add_selected_button.clicked.connect(self._add_selected_cameras)
        actions_row.addWidget(self.add_selected_button)
        layout.addLayout(actions_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_search(self):
        self.cameras_list.clear()
        self.cameras_list.setEnabled(False)
        self._camera_map = {}
        self._selected_configs = []
        self.add_selected_button.setEnabled(False)
        self.select_all_button.setEnabled(False)
        self.refresh_button.setEnabled(False)

        raw_targets = self.targets_input.text().strip()
        target_networks: list[str] = []

        try:
            if raw_targets:
                targets = parse_discovery_targets(raw_targets)
            else:
                targets, target_networks = default_discovery_targets(self._initial.host)
        except ValueError as e:
            self.refresh_button.setEnabled(True)
            QMessageBox.warning(self, "Local Search", str(e))
            self.status_label.setText(
                "Enter a valid target IP, dotted prefix, hostname, or CIDR range."
            )
            return

        if raw_targets and targets:
            self.status_label.setText(
                f"Searching {len(targets)} target(s) for ONVIF cameras..."
            )
        elif target_networks:
            networks_text = ", ".join(target_networks[:2])
            if len(target_networks) > 2:
                networks_text = f"{networks_text} ..."
            self.status_label.setText(
                f"Searching local subnet(s): {networks_text}"
            )
        else:
            self.status_label.setText("Searching local network for ONVIF cameras...")

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            discovered_cameras = discover_onvif_cameras(targets=targets)
        except ValueError as e:
            QMessageBox.warning(self, "Local Search", str(e))
            self.status_label.setText(
                "Enter a valid target IP, dotted prefix, hostname, or CIDR range."
            )
            discovered_cameras = []
        except Exception as e:
            logger.error(f"Local camera discovery failed: {e}")
            QMessageBox.warning(self, "Local Search", f"Camera search failed: {e}")
            discovered_cameras = []
        finally:
            QApplication.restoreOverrideCursor()

        self.refresh_button.setEnabled(True)
        self._populate_results(discovered_cameras)

    def _populate_results(self, cameras: list[DiscoveredCameraInfo]):
        self.cameras_list.clear()
        self._camera_map = {camera.camera_id: camera for camera in cameras}
        self._selected_configs = []

        if not cameras:
            self.status_label.setText("No ONVIF cameras were found on the local network.")
            return

        for camera in cameras:
            title = camera.name or camera.hardware or camera.host
            details = f"{camera.host}:{camera.port}"
            if camera.hardware and camera.hardware != title:
                details = f"{details}   {camera.hardware}"
            if camera.location:
                details = f"{details}   {camera.location}"

            item = QListWidgetItem(f"{title}\n{details}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, camera.camera_id)
            item.setToolTip(self._tooltip_for_camera(camera))
            self.cameras_list.addItem(item)

        self.cameras_list.setEnabled(True)
        self.select_all_button.setEnabled(True)
        self.status_label.setText(f"Found {len(cameras)} ONVIF camera(s).")
        self._update_add_button()

    def _tooltip_for_camera(self, camera: DiscoveredCameraInfo) -> str:
        lines = [f"Address: {camera.host}:{camera.port}", f"Service: {camera.xaddr}"]
        if camera.name:
            lines.append(f"Name: {camera.name}")
        if camera.hardware:
            lines.append(f"Hardware: {camera.hardware}")
        if camera.location:
            lines.append(f"Location: {camera.location}")
        if camera.types:
            lines.append(f"Types: {' '.join(camera.types)}")
        return "\n".join(lines)

    def _checked_cameras(self) -> list[DiscoveredCameraInfo]:
        checked_cameras: list[DiscoveredCameraInfo] = []
        for index in range(self.cameras_list.count()):
            item = self.cameras_list.item(index)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            camera_id = item.data(Qt.ItemDataRole.UserRole)
            camera = self._camera_map.get(str(camera_id)) if camera_id else None
            if camera is not None:
                checked_cameras.append(camera)
        return checked_cameras

    def _on_camera_item_changed(self, _item: QListWidgetItem):
        self._update_add_button()

    def _update_add_button(self):
        self.add_selected_button.setEnabled(bool(self._checked_cameras()))

    def _on_item_double_clicked(self, item: QListWidgetItem):
        next_state = (
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        item.setCheckState(next_state)

    def _select_all_cameras(self):
        for index in range(self.cameras_list.count()):
            item = self.cameras_list.item(index)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked)

    def _add_selected_cameras(self):
        cameras = self._checked_cameras()
        if not cameras:
            return

        camera_count = len(cameras)
        credentials_dialog = CameraCredentialsDialog(
            self._initial,
            self,
            dialog_title="Camera Credentials",
            intro_text=(
                f"Enter credentials to apply to {camera_count} selected camera(s). "
                "Leave the fields empty if the cameras allow anonymous access."
            ),
            confirm_text="Add Cameras",
        )
        if credentials_dialog.exec() != QDialog.DialogCode.Accepted:
            self.status_label.setText("Camera search is ready. Select cameras to add them.")
            return

        username, password = credentials_dialog.credentials()
        self._selected_configs = [
            SavedCameraConfig(
                host=camera.host,
                port=camera.port,
                username=username,
                password=password,
            )
            for camera in cameras
        ]
        self.accept()

    def camera_configs(self) -> list[SavedCameraConfig]:
        return list(self._selected_configs)

    def camera_config(self) -> SavedCameraConfig:
        if not self._selected_configs:
            raise RuntimeError("No discovered camera has been selected.")
        return self._selected_configs[0]


class SavedCamerasWidget(QGroupBox):
    """Persistent saved camera library used to open camera tabs on demand."""

    def __init__(self, parent=None):
        super().__init__("Saved Cameras", parent)
        self.open_camera_callback = None
        self.forget_camera_callback = None
        self.edit_camera_callback = None
        self._init_ui()
        self.set_cameras({}, set(), None)

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)
        self._form_enabled = False

        self.status_label = QLabel()
        self.status_label.setObjectName("sectionHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.cameras_list = QListWidget()
        self.cameras_list.itemClicked.connect(self._on_item_clicked)
        self.cameras_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.cameras_list.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.cameras_list)

        hint = QLabel("Click a saved camera to open it. Right click to edit or delete the saved entry.")
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.setLayout(layout)

    def set_cameras(
        self,
        cameras: dict[str, SavedCameraConfig],
        open_camera_ids: set[str],
        active_camera_id: Optional[str],
    ):
        self.cameras_list.clear()

        if not cameras:
            self.status_label.setText("No saved cameras yet")
            self.setEnabled(True)
            return

        sorted_cameras = sorted(
            cameras.values(),
            key=lambda config: (config.host, config.port, config.username),
        )
        for config in sorted_cameras:
            if config.camera_id == active_camera_id:
                prefix = "ACTIVE"
            elif config.camera_id in open_camera_ids:
                prefix = "OPEN"
            else:
                prefix = "SAVED"

            item = QListWidgetItem(f"{prefix}  {config.host}:{config.port}   {config.username}")
            item.setData(Qt.ItemDataRole.UserRole, config.camera_id)
            item.setToolTip(
                f"Host: {config.host}\nPort: {config.port}\nUser: {config.username}"
            )
            self.cameras_list.addItem(item)

        self.status_label.setText(f"{len(sorted_cameras)} saved camera(s)")
        self.setEnabled(True)

    def _selected_camera_id(self, item: Optional[QListWidgetItem] = None) -> Optional[str]:
        if item is None:
            item = self.cameras_list.currentItem()
        if item is None:
            return None
        camera_id = item.data(Qt.ItemDataRole.UserRole)
        return str(camera_id) if camera_id else None

    def _on_item_clicked(self, item: QListWidgetItem):
        camera_id = self._selected_camera_id(item)
        if camera_id and self.open_camera_callback:
            self.open_camera_callback(camera_id)

    def _on_context_menu(self, position):
        item = self.cameras_list.itemAt(position)
        camera_id = self._selected_camera_id(item)
        if camera_id is None:
            return

        menu = QMenu(self)
        edit_action = menu.addAction("Edit")
        delete_action = menu.addAction("Delete")
        chosen_action = menu.exec(self.cameras_list.mapToGlobal(position))
        if chosen_action == edit_action and self.edit_camera_callback:
            self.edit_camera_callback(camera_id)
        elif chosen_action == delete_action and self.forget_camera_callback:
            self.forget_camera_callback(camera_id)


class CameraDetailsWidget(QGroupBox):
    """Shows read-only details for the currently selected camera."""

    def __init__(self, parent=None):
        super().__init__("Info", parent)
        self._init_ui()
        self.set_session(None)

    def _init_ui(self):
        layout = QGridLayout()

        layout.addWidget(QLabel("Address:"), 0, 0)
        self.address_value = QLabel("-")
        layout.addWidget(self.address_value, 0, 1)

        layout.addWidget(QLabel("Vendor:"), 1, 0)
        self.vendor_value = QLabel("-")
        layout.addWidget(self.vendor_value, 1, 1)

        layout.addWidget(QLabel("Model:"), 2, 0)
        self.model_value = QLabel("-")
        layout.addWidget(self.model_value, 2, 1)

        layout.addWidget(QLabel("Firmware:"), 3, 0)
        self.firmware_value = QLabel("-")
        layout.addWidget(self.firmware_value, 3, 1)

        layout.addWidget(QLabel("Connection:"), 4, 0)
        self.connection_value = QLabel("Disconnected")
        layout.addWidget(self.connection_value, 4, 1)

        layout.addWidget(QLabel("Active Profile:"), 5, 0)
        self.profile_value = QLabel("-")
        self.profile_value.setWordWrap(True)
        layout.addWidget(self.profile_value, 5, 1)

        self.setLayout(layout)

    def set_session(self, session: Optional["CameraSession"]):
        if session is None:
            self.address_value.setText("-")
            self.vendor_value.setText("-")
            self.model_value.setText("-")
            self.firmware_value.setText("-")
            self.connection_value.setText("Disconnected")
            self.profile_value.setText("-")
            self.setEnabled(False)
            return

        info = session.client.camera_info
        active_profile = next(
            (profile.display_name for profile in session.stream_profiles if profile.token == session.active_stream_token),
            session.active_stream_token or "-",
        )

        self.address_value.setText(f"{session.host}:{session.port}")
        self.vendor_value.setText(info.manufacturer or "-")
        self.model_value.setText(info.model or "-")
        self.firmware_value.setText(info.firmware or "-")
        self.connection_value.setText("Connected" if info.connected else "Disconnected")
        self.profile_value.setText(active_profile)
        self.setEnabled(True)


class UserAccountDialog(QDialog):
    """Dialog used for creating and editing ONVIF users."""

    ROLE_OPTIONS = ["Administrator", "Operator", "User", "Anonymous"]

    def __init__(
        self,
        parent=None,
        *,
        dialog_title: str,
        confirm_text: str,
        username: str = "",
        role: str = "User",
        username_editable: bool = True,
        allow_empty_password: bool = False,
    ):
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle(dialog_title)
        self.setMinimumWidth(380)
        self._confirm_text = confirm_text
        self._allow_empty_password = allow_empty_password
        self._initial_username = username
        self._initial_role = role or "User"
        self._username_editable = username_editable
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.username_input = QLineEdit(self._initial_username)
        self.username_input.setReadOnly(not self._username_editable)
        form.addRow("User", self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        if self._allow_empty_password:
            self.password_input.setPlaceholderText("Leave blank to keep the current password")
        form.addRow("Password", self.password_input)

        self.password_repeat_input = QLineEdit()
        self.password_repeat_input.setEchoMode(QLineEdit.EchoMode.Password)
        if self._allow_empty_password:
            self.password_repeat_input.setPlaceholderText("Leave blank to keep the current password")
        form.addRow("Repeat password", self.password_repeat_input)

        self.role_combo = QComboBox()
        for role in self.ROLE_OPTIONS:
            self.role_combo.addItem(role, role)
        role_index = self.role_combo.findData(self._initial_role)
        if role_index >= 0:
            self.role_combo.setCurrentIndex(role_index)
        form.addRow("Role", self.role_combo)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self._confirm_text)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        password_repeat = self.password_repeat_input.text()

        if not username:
            QMessageBox.warning(self, "User Management", "User name is required.")
            return

        if password != password_repeat:
            QMessageBox.warning(self, "User Management", "Passwords do not match.")
            return

        if not self._allow_empty_password and not password:
            QMessageBox.warning(self, "User Management", "Password is required.")
            return

        self.accept()

    def values(self) -> tuple[str, Optional[str], str]:
        password = self.password_input.text()
        return (
            self.username_input.text().strip(),
            password if password else None,
            str(self.role_combo.currentData() or self.role_combo.currentText()),
        )


class NetworkSettingsWidget(QGroupBox):
    """Editable ONVIF device-management network settings."""

    def __init__(self, parent=None):
        super().__init__("Network Settings", parent)
        self.refresh_callback = None
        self.apply_callback = None
        self._interface_token: Optional[str] = None
        self._init_ui()
        self.clear_state("No active camera")

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)

        self.status_label = QLabel()
        self.status_label.setObjectName("sectionHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        ipv4_group = QGroupBox("IPv4")
        ipv4_layout = QFormLayout(ipv4_group)
        ipv4_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        ipv4_layout.setHorizontalSpacing(12)
        ipv4_layout.setVerticalSpacing(10)
        self.dhcp_checkbox = QCheckBox("Enable DHCP")
        self.dhcp_checkbox.toggled.connect(self._sync_ipv4_controls)
        ipv4_layout.addRow("DHCP", self.dhcp_checkbox)
        self.ip_address_input = QLineEdit()
        ipv4_layout.addRow("IP Address", self.ip_address_input)
        self.subnet_mask_input = QLineEdit()
        ipv4_layout.addRow("Subnet mask", self.subnet_mask_input)
        self.default_gateway_input = QLineEdit()
        ipv4_layout.addRow("Default gateway", self.default_gateway_input)
        layout.addWidget(ipv4_group)

        identity_group = QGroupBox("Identity")
        identity_layout = QFormLayout(identity_group)
        identity_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        identity_layout.setHorizontalSpacing(12)
        identity_layout.setVerticalSpacing(10)
        host_name_row = QWidget()
        host_name_row_layout = QHBoxLayout(host_name_row)
        host_name_row_layout.setContentsMargins(0, 0, 0, 0)
        host_name_row_layout.setSpacing(8)
        self.host_name_mode_combo = QComboBox()
        self.host_name_mode_combo.addItem("Manual", False)
        self.host_name_mode_combo.addItem("DHCP", True)
        self.host_name_mode_combo.currentIndexChanged.connect(self._sync_host_name_controls)
        host_name_row_layout.addWidget(self.host_name_mode_combo, 0)
        self.host_name_input = QLineEdit()
        host_name_row_layout.addWidget(self.host_name_input, 1)
        identity_layout.addRow("Host name", host_name_row)
        self.discovery_mode_combo = QComboBox()
        self.discovery_mode_combo.addItem("Discoverable", "Discoverable")
        self.discovery_mode_combo.addItem("NonDiscoverable", "NonDiscoverable")
        identity_layout.addRow("ONVIF discovery mode", self.discovery_mode_combo)
        self.zero_config_checkbox = QCheckBox("Enable zero config")
        identity_layout.addRow("Zero config", self.zero_config_checkbox)
        self.zero_config_addresses_value = QLineEdit()
        self.zero_config_addresses_value.setReadOnly(True)
        identity_layout.addRow("Zero config address", self.zero_config_addresses_value)
        layout.addWidget(identity_group)

        dns_ntp_group = QGroupBox("DNS / NTP")
        dns_ntp_layout = QFormLayout(dns_ntp_group)
        dns_ntp_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        dns_ntp_layout.setHorizontalSpacing(12)
        dns_ntp_layout.setVerticalSpacing(10)
        self.dns_from_dhcp_checkbox = QCheckBox("Use DHCP")
        self.dns_from_dhcp_checkbox.toggled.connect(self._sync_dns_controls)
        dns_ntp_layout.addRow("DNS mode", self.dns_from_dhcp_checkbox)
        self.dns_manual_input = QLineEdit()
        self.dns_manual_input.setPlaceholderText("8.8.8.8;1.1.1.1")
        dns_ntp_layout.addRow("DNS", self.dns_manual_input)
        self.ntp_from_dhcp_checkbox = QCheckBox("Use DHCP")
        self.ntp_from_dhcp_checkbox.toggled.connect(self._sync_ntp_controls)
        dns_ntp_layout.addRow("NTP mode", self.ntp_from_dhcp_checkbox)
        self.ntp_manual_input = QLineEdit()
        self.ntp_manual_input.setPlaceholderText("time.windows.com;pool.ntp.org")
        dns_ntp_layout.addRow("NTP servers", self.ntp_manual_input)
        layout.addWidget(dns_ntp_group)

        ports_group = QGroupBox("Protocols")
        ports_layout = QGridLayout(ports_group)
        ports_layout.setHorizontalSpacing(12)
        ports_layout.setVerticalSpacing(10)
        ports_layout.addWidget(QLabel("Protocol"), 0, 0)
        ports_layout.addWidget(QLabel("Enabled"), 0, 1)
        ports_layout.addWidget(QLabel("Port"), 0, 2)

        self.http_enabled_checkbox, self.http_port_input = self._build_protocol_controls()
        ports_layout.addWidget(QLabel("HTTP"), 1, 0)
        ports_layout.addWidget(self.http_enabled_checkbox, 1, 1)
        ports_layout.addWidget(self.http_port_input, 1, 2)

        self.https_enabled_checkbox, self.https_port_input = self._build_protocol_controls()
        ports_layout.addWidget(QLabel("HTTPS"), 2, 0)
        ports_layout.addWidget(self.https_enabled_checkbox, 2, 1)
        ports_layout.addWidget(self.https_port_input, 2, 2)

        self.rtsp_enabled_checkbox, self.rtsp_port_input = self._build_protocol_controls()
        ports_layout.addWidget(QLabel("RTSP"), 3, 0)
        ports_layout.addWidget(self.rtsp_enabled_checkbox, 3, 1)
        ports_layout.addWidget(self.rtsp_port_input, 3, 2)
        layout.addWidget(ports_group)

        button_layout = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_apply = QPushButton("Apply")
        self.btn_refresh.clicked.connect(self._on_refresh)
        self.btn_apply.clicked.connect(self._on_apply)
        button_layout.addWidget(self.btn_refresh)
        button_layout.addWidget(self.btn_apply)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def _build_protocol_controls(self) -> tuple[QCheckBox, QSpinBox]:
        enabled_checkbox = QCheckBox()
        port_input = QSpinBox()
        port_input.setRange(1, 65535)
        port_input.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        return enabled_checkbox, port_input

    def _set_form_enabled(self, enabled: bool):
        self._form_enabled = enabled
        for widget in (
            self.dhcp_checkbox,
            self.ip_address_input,
            self.subnet_mask_input,
            self.default_gateway_input,
            self.host_name_mode_combo,
            self.host_name_input,
            self.discovery_mode_combo,
            self.zero_config_checkbox,
            self.dns_from_dhcp_checkbox,
            self.dns_manual_input,
            self.ntp_from_dhcp_checkbox,
            self.ntp_manual_input,
            self.http_enabled_checkbox,
            self.http_port_input,
            self.https_enabled_checkbox,
            self.https_port_input,
            self.rtsp_enabled_checkbox,
            self.rtsp_port_input,
            self.btn_apply,
        ):
            widget.setEnabled(enabled)

    def _sync_ipv4_controls(self):
        use_dhcp = self.dhcp_checkbox.isChecked() and self.dhcp_checkbox.isEnabled()
        self.ip_address_input.setEnabled(not use_dhcp and self.dhcp_checkbox.isEnabled())
        self.subnet_mask_input.setEnabled(not use_dhcp and self.dhcp_checkbox.isEnabled())

    def _sync_host_name_controls(self):
        if not self._form_enabled:
            self.host_name_input.setEnabled(False)
            return

        if not self.host_name_mode_combo.isEnabled():
            self.host_name_input.setEnabled(True)
            return

        self.host_name_input.setEnabled(not bool(self.host_name_mode_combo.currentData()))

    def _sync_dns_controls(self):
        self.dns_manual_input.setEnabled(not self.dns_from_dhcp_checkbox.isChecked() and self.dns_from_dhcp_checkbox.isEnabled())

    def _sync_ntp_controls(self):
        self.ntp_manual_input.setEnabled(not self.ntp_from_dhcp_checkbox.isChecked() and self.ntp_from_dhcp_checkbox.isEnabled())

    def clear_state(self, message: str, allow_refresh: bool = False):
        self._interface_token = None
        self.status_label.setText(message)
        self.dhcp_checkbox.setChecked(False)
        self.ip_address_input.clear()
        self.subnet_mask_input.clear()
        self.default_gateway_input.clear()
        self.host_name_mode_combo.setCurrentIndex(0)
        self.host_name_input.clear()
        self.discovery_mode_combo.setCurrentIndex(0)
        self.zero_config_checkbox.setChecked(False)
        self.zero_config_addresses_value.clear()
        self.dns_from_dhcp_checkbox.setChecked(False)
        self.dns_manual_input.clear()
        self.ntp_from_dhcp_checkbox.setChecked(False)
        self.ntp_manual_input.clear()
        self.http_enabled_checkbox.setChecked(False)
        self.http_port_input.setValue(80)
        self.https_enabled_checkbox.setChecked(False)
        self.https_port_input.setValue(443)
        self.rtsp_enabled_checkbox.setChecked(False)
        self.rtsp_port_input.setValue(554)
        self._set_form_enabled(allow_refresh)
        self.btn_refresh.setEnabled(allow_refresh)
        self._sync_ipv4_controls()
        self._sync_host_name_controls()
        self._sync_dns_controls()
        self._sync_ntp_controls()

    def set_payload(self, payload: NetworkSettingsPayload):
        self._interface_token = payload.interface_token
        self.status_label.setText("Network settings loaded")
        self._set_form_enabled(True)
        self.btn_refresh.setEnabled(True)

        self.dhcp_checkbox.setEnabled(payload.dhcp is not None)
        self.dhcp_checkbox.setChecked(bool(payload.dhcp))
        self.ip_address_input.setText(payload.ip_address)
        self.subnet_mask_input.setText(payload.subnet_mask)
        self.default_gateway_input.setText(payload.default_gateway)
        self.host_name_mode_combo.setEnabled(payload.host_name_from_dhcp is not None)
        host_name_mode_index = self.host_name_mode_combo.findData(bool(payload.host_name_from_dhcp))
        if host_name_mode_index >= 0:
            self.host_name_mode_combo.setCurrentIndex(host_name_mode_index)
        else:
            self.host_name_mode_combo.setCurrentIndex(0)
        self.host_name_input.setText(payload.host_name)

        self.discovery_mode_combo.setEnabled(bool(payload.discovery_mode))
        discovery_index = self.discovery_mode_combo.findData(payload.discovery_mode)
        if discovery_index >= 0:
            self.discovery_mode_combo.setCurrentIndex(discovery_index)
        else:
            self.discovery_mode_combo.setCurrentIndex(0)

        self.zero_config_checkbox.setEnabled(payload.zero_config_enabled is not None)
        self.zero_config_checkbox.setChecked(bool(payload.zero_config_enabled))
        self.zero_config_addresses_value.setText(';'.join(payload.zero_config_addresses))

        self.dns_from_dhcp_checkbox.setEnabled(payload.dns_from_dhcp is not None)
        self.dns_from_dhcp_checkbox.setChecked(bool(payload.dns_from_dhcp))
        self.dns_manual_input.setText(';'.join(payload.dns_manual))

        self.ntp_from_dhcp_checkbox.setEnabled(payload.ntp_from_dhcp is not None)
        self.ntp_from_dhcp_checkbox.setChecked(bool(payload.ntp_from_dhcp))
        self.ntp_manual_input.setText(';'.join(payload.ntp_manual))

        self.http_enabled_checkbox.setEnabled(payload.http_enabled is not None)
        self.http_enabled_checkbox.setChecked(bool(payload.http_enabled))
        self.http_port_input.setEnabled(payload.http_port is not None)
        if payload.http_port is not None:
            self.http_port_input.setValue(payload.http_port)

        self.https_enabled_checkbox.setEnabled(payload.https_enabled is not None)
        self.https_enabled_checkbox.setChecked(bool(payload.https_enabled))
        self.https_port_input.setEnabled(payload.https_port is not None)
        if payload.https_port is not None:
            self.https_port_input.setValue(payload.https_port)

        self.rtsp_enabled_checkbox.setEnabled(payload.rtsp_enabled is not None)
        self.rtsp_enabled_checkbox.setChecked(bool(payload.rtsp_enabled))
        self.rtsp_port_input.setEnabled(payload.rtsp_port is not None)
        if payload.rtsp_port is not None:
            self.rtsp_port_input.setValue(payload.rtsp_port)

        self._sync_ipv4_controls()
        self._sync_host_name_controls()
        self._sync_dns_controls()
        self._sync_ntp_controls()

    def values(self) -> dict[str, Any]:
        return {
            'interface_token': self._interface_token,
            'dhcp': self.dhcp_checkbox.isChecked() if self.dhcp_checkbox.isEnabled() else None,
            'ip_address': self.ip_address_input.text().strip(),
            'subnet_mask': self.subnet_mask_input.text().strip(),
            'default_gateway': self.default_gateway_input.text().strip(),
            'host_name_from_dhcp': self.host_name_mode_combo.currentData() if self.host_name_mode_combo.isEnabled() else None,
            'host_name': self.host_name_input.text().strip(),
            'dns_from_dhcp': self.dns_from_dhcp_checkbox.isChecked() if self.dns_from_dhcp_checkbox.isEnabled() else None,
            'dns_manual': self.dns_manual_input.text().strip(),
            'ntp_from_dhcp': self.ntp_from_dhcp_checkbox.isChecked() if self.ntp_from_dhcp_checkbox.isEnabled() else None,
            'ntp_manual': self.ntp_manual_input.text().strip(),
            'http_enabled': self.http_enabled_checkbox.isChecked() if self.http_enabled_checkbox.isEnabled() else None,
            'http_port': self.http_port_input.value() if self.http_port_input.isEnabled() else None,
            'https_enabled': self.https_enabled_checkbox.isChecked() if self.https_enabled_checkbox.isEnabled() else None,
            'https_port': self.https_port_input.value() if self.https_port_input.isEnabled() else None,
            'rtsp_enabled': self.rtsp_enabled_checkbox.isChecked() if self.rtsp_enabled_checkbox.isEnabled() else None,
            'rtsp_port': self.rtsp_port_input.value() if self.rtsp_port_input.isEnabled() else None,
            'zero_config_enabled': self.zero_config_checkbox.isChecked() if self.zero_config_checkbox.isEnabled() else None,
            'discovery_mode': str(self.discovery_mode_combo.currentData() or self.discovery_mode_combo.currentText()),
        }

    def _on_refresh(self):
        if self.refresh_callback:
            self.refresh_callback()

    def _on_apply(self):
        if self.apply_callback:
            self.apply_callback(self.values())


class MaintenanceWidget(QGroupBox):
    """Simple device maintenance panel."""

    def __init__(self, parent=None):
        super().__init__("Maintenance", parent)
        self.soft_reset_callback = None
        self.hard_reset_callback = None
        self.reboot_callback = None
        self.upgrade_callback = None
        self._init_ui()
        self.clear_state("No active camera")

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)

        self.status_label = QLabel()
        self.status_label.setObjectName("sectionHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        firmware_group = QGroupBox("Firmware")
        firmware_layout = QFormLayout(firmware_group)
        firmware_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        firmware_layout.setHorizontalSpacing(12)
        firmware_layout.setVerticalSpacing(10)
        self.firmware_value = QLabel("-")
        self.firmware_value.setWordWrap(True)
        firmware_layout.addRow("Version", self.firmware_value)
        self.btn_upgrade_firmware = QPushButton("Upgrade")
        self.btn_upgrade_firmware.clicked.connect(self._on_upgrade)
        firmware_layout.addRow("Firmware", self.btn_upgrade_firmware)
        layout.addWidget(firmware_group)

        actions_group = QGroupBox("Maintenance")
        actions_layout = QGridLayout(actions_group)
        actions_layout.setHorizontalSpacing(10)
        actions_layout.setVerticalSpacing(10)
        self.btn_soft_reset = QPushButton("Soft reset")
        self.btn_hard_reset = QPushButton("Hard reset")
        self.btn_reboot = QPushButton("Reboot")
        self.btn_soft_reset.clicked.connect(self._on_soft_reset)
        self.btn_hard_reset.clicked.connect(self._on_hard_reset)
        self.btn_reboot.clicked.connect(self._on_reboot)
        actions_layout.addWidget(self.btn_soft_reset, 0, 0)
        actions_layout.addWidget(self.btn_hard_reset, 0, 1)
        actions_layout.addWidget(self.btn_reboot, 1, 0, 1, 2)
        layout.addWidget(actions_group)

        self.setLayout(layout)

    def clear_state(self, message: str):
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message))
        self.firmware_value.setText("-")
        for button in (
            self.btn_upgrade_firmware,
            self.btn_soft_reset,
            self.btn_hard_reset,
            self.btn_reboot,
        ):
            button.setEnabled(False)

    def set_firmware_version(self, firmware_version: str):
        self.status_label.clear()
        self.status_label.hide()
        self.firmware_value.setText(firmware_version or "Unknown")
        for button in (
            self.btn_upgrade_firmware,
            self.btn_soft_reset,
            self.btn_hard_reset,
            self.btn_reboot,
        ):
            button.setEnabled(True)

    def _on_soft_reset(self):
        if self.soft_reset_callback:
            self.soft_reset_callback()

    def _on_hard_reset(self):
        if self.hard_reset_callback:
            self.hard_reset_callback()

    def _on_reboot(self):
        if self.reboot_callback:
            self.reboot_callback()

    def _on_upgrade(self):
        if self.upgrade_callback:
            self.upgrade_callback()


class UserManagementWidget(QGroupBox):
    """User list with add/edit/delete actions."""

    def __init__(self, parent=None):
        super().__init__("User Management", parent)
        self.refresh_callback = None
        self.create_callback = None
        self.edit_callback = None
        self.delete_callback = None
        self._init_ui()
        self.clear_state("No active camera")

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)

        self.status_label = QLabel()
        self.status_label.setObjectName("sectionHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.user_list = QListWidget()
        self.user_list.currentItemChanged.connect(self._update_actions)
        layout.addWidget(self.user_list)

        button_layout = QGridLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_add = QPushButton("Add")
        self.btn_edit = QPushButton("Edit")
        self.btn_delete = QPushButton("Delete")
        self.btn_refresh.clicked.connect(self._on_refresh)
        self.btn_add.clicked.connect(self._on_create)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_delete.clicked.connect(self._on_delete)
        button_layout.addWidget(self.btn_refresh, 0, 0, 1, 2)
        button_layout.addWidget(self.btn_add, 1, 0)
        button_layout.addWidget(self.btn_edit, 1, 1)
        button_layout.addWidget(self.btn_delete, 2, 0, 1, 2)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def clear_state(self, message: str, allow_refresh: bool = False):
        self.user_list.clear()
        self.status_label.setText(message)
        self.btn_refresh.setEnabled(allow_refresh)
        self.btn_add.setEnabled(False)
        self.btn_edit.setEnabled(False)
        self.btn_delete.setEnabled(False)

    def set_users(self, users: list[UserAccountInfo]):
        self.user_list.clear()
        for user in users:
            item = QListWidgetItem(f"{user.username}    {user.role}")
            item.setData(Qt.ItemDataRole.UserRole, user.username)
            item.setData(Qt.ItemDataRole.UserRole + 1, user.role)
            self.user_list.addItem(item)

        self.status_label.setText(f"{len(users)} user(s) found")
        self.btn_refresh.setEnabled(True)
        self.btn_add.setEnabled(True)
        self._update_actions()

    def selected_user(self) -> Optional[UserAccountInfo]:
        item = self.user_list.currentItem()
        if item is None:
            return None
        username = item.data(Qt.ItemDataRole.UserRole)
        role = item.data(Qt.ItemDataRole.UserRole + 1)
        if not username:
            return None
        return UserAccountInfo(username=str(username), role=str(role or 'User'))

    def _update_actions(self, *_args):
        selected = self.selected_user() is not None
        self.btn_edit.setEnabled(self.btn_add.isEnabled() and selected)
        self.btn_delete.setEnabled(self.btn_add.isEnabled() and selected)

    def _on_refresh(self):
        if self.refresh_callback:
            self.refresh_callback()

    def _on_create(self):
        if self.create_callback:
            self.create_callback()

    def _on_edit(self):
        selected = self.selected_user()
        if selected is not None and self.edit_callback:
            self.edit_callback(selected)

    def _on_delete(self):
        selected = self.selected_user()
        if selected is not None and self.delete_callback:
            self.delete_callback(selected)


class LiveVideoWidget(QGroupBox):
    """ODM-like live video menu reduced to RTSP URI tools."""

    def __init__(self, parent=None):
        super().__init__("Live Video", parent)
        self.refresh_uri_callback = None
        self._init_ui()
        self.set_session(None)

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)

        self.status_label = QLabel("No active camera")
        self.status_label.setObjectName("sectionHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        form_layout = QGridLayout()
        form_layout.addWidget(QLabel("RTSP URI:"), 0, 0)
        self.uri_value = QLineEdit()
        self.uri_value.setReadOnly(True)
        self.uri_value.setPlaceholderText("URI will appear after the stream is resolved")
        form_layout.addWidget(self.uri_value, 0, 1)
        layout.addLayout(form_layout)

        button_layout = QHBoxLayout()
        self.btn_refresh_uri = QPushButton("Refresh URI")
        self.btn_copy_uri = QPushButton("Copy URI")
        self.btn_refresh_uri.clicked.connect(self._on_refresh_uri)
        self.btn_copy_uri.clicked.connect(self._on_copy_uri)
        button_layout.addWidget(self.btn_refresh_uri)
        button_layout.addWidget(self.btn_copy_uri)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def set_session(self, session: Optional["CameraSession"]):
        if session is None:
            self.uri_value.clear()
            self.status_label.setText("No active camera")
            self.btn_refresh_uri.setEnabled(False)
            self.btn_copy_uri.setEnabled(False)
            self.setEnabled(False)
            return

        self.uri_value.setText(session.current_stream_uri or "")
        if session.current_stream_uri:
            self.status_label.setText("RTSP URI ready")
        else:
            self.status_label.setText("Resolve the RTSP URI or start live video")
        self.btn_refresh_uri.setEnabled(True)
        self.btn_copy_uri.setEnabled(bool(session.current_stream_uri))
        self.setEnabled(True)

    def set_stream_uri(self, uri: Optional[str]):
        self.uri_value.setText(uri or "")
        self.btn_copy_uri.setEnabled(bool(uri))

    def set_stream_status(self, status_text: str):
        self.status_label.setText(status_text)

    def _on_refresh_uri(self):
        if self.refresh_uri_callback:
            self.refresh_uri_callback()

    def _on_copy_uri(self):
        if self.uri_value.text():
            QApplication.clipboard().setText(self.uri_value.text())


class VideoStreamingWidget(QGroupBox):
    """ODM-like video streaming editor for encoder settings."""

    def __init__(self, parent=None):
        super().__init__("Video Streaming", parent)
        self.refresh_callback = None
        self.apply_callback = None
        self._current_settings: Optional[VideoEncoderSettings] = None
        self._init_ui()
        self.clear_state("No active camera")

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)

        self.status_label = QLabel()
        self.status_label.setObjectName("sectionHint")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        layout.addWidget(self.status_label)

        form_layout = QGridLayout()

        form_layout.addWidget(QLabel("Profile:"), 0, 0)
        self.profile_value = QLabel("-")
        self.profile_value.setWordWrap(True)
        form_layout.addWidget(self.profile_value, 0, 1)

        form_layout.addWidget(QLabel("Encoder + Resolution:"), 1, 0)
        self.encoder_resolution_combo = QComboBox()
        self.encoder_resolution_combo.currentIndexChanged.connect(self._sync_codec_specific_controls)
        form_layout.addWidget(self.encoder_resolution_combo, 1, 1)

        form_layout.addWidget(QLabel("Encoding Interval:"), 2, 0)
        self.encoding_interval_input = QSpinBox()
        self.encoding_interval_input.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        form_layout.addWidget(self.encoding_interval_input, 2, 1)

        form_layout.addWidget(QLabel("Quality:"), 3, 0)
        self.quality_input = QDoubleSpinBox()
        self.quality_input.setDecimals(2)
        self.quality_input.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        form_layout.addWidget(self.quality_input, 3, 1)

        form_layout.addWidget(QLabel("Frame Rate, fps:"), 4, 0)
        self.frame_rate_input = QSpinBox()
        self.frame_rate_input.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        form_layout.addWidget(self.frame_rate_input, 4, 1)

        form_layout.addWidget(QLabel("Bitrate Limit, kbps:"), 5, 0)
        self.bitrate_limit_input = QSpinBox()
        self.bitrate_limit_input.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        form_layout.addWidget(self.bitrate_limit_input, 5, 1)

        form_layout.addWidget(QLabel("GOV Length:"), 6, 0)
        self.gov_length_input = QSpinBox()
        self.gov_length_input.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        form_layout.addWidget(self.gov_length_input, 6, 1)

        layout.addLayout(form_layout)

        button_layout = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_apply = QPushButton("Apply")
        self.btn_refresh.clicked.connect(self._on_refresh)
        self.btn_apply.clicked.connect(self._on_apply)
        button_layout.addWidget(self.btn_refresh)
        button_layout.addWidget(self.btn_apply)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def _configure_numeric_input(
        self,
        widget: QAbstractSpinBox,
        value: Optional[float],
        minimum: float,
        maximum: float,
        enabled: bool,
    ):
        widget.blockSignals(True)
        if isinstance(widget, QDoubleSpinBox):
            widget.setRange(float(minimum), float(maximum))
            widget.setValue(float(value if value is not None else minimum))
        else:
            widget.setRange(int(minimum), int(maximum))
            widget.setValue(int(value if value is not None else minimum))
        widget.setEnabled(enabled)
        widget.blockSignals(False)

    def clear_state(self, message: str, allow_refresh: bool = False):
        self._current_settings = None
        self.status_label.clear()
        self.profile_value.setText("-")

        self.encoder_resolution_combo.blockSignals(True)
        self.encoder_resolution_combo.clear()
        self.encoder_resolution_combo.addItem("Unavailable", None)
        self.encoder_resolution_combo.setEnabled(False)
        self.encoder_resolution_combo.blockSignals(False)

        self._configure_numeric_input(self.encoding_interval_input, 1, 1, 1, False)
        self._configure_numeric_input(self.quality_input, 0.0, 0.0, 100.0, False)
        self._configure_numeric_input(self.frame_rate_input, 1, 1, 1, False)
        self._configure_numeric_input(self.bitrate_limit_input, 1, 1, 1, False)
        self._configure_numeric_input(self.gov_length_input, 1, 1, 1, False)

        self.btn_refresh.setEnabled(allow_refresh)
        self.btn_apply.setEnabled(False)
        self.setEnabled(allow_refresh)

    def _encoder_resolution_label(self, encoding: str, width: Optional[int], height: Optional[int]) -> str:
        if width and height:
            return f"{encoding}  {width}x{height}"
        return encoding or "Unknown"

    def set_encoder_settings(self, settings: Optional[VideoEncoderSettings]):
        if settings is None:
            self.clear_state(
                "Video encoder settings are unavailable for the active profile.",
                allow_refresh=True,
            )
            return

        self._current_settings = settings
        self.status_label.clear()
        self.profile_value.setText(settings.profile_name)

        current_choices: list[tuple[str, Optional[int], Optional[int]]] = []
        encodings = settings.available_encodings or ([settings.encoding] if settings.encoding else [])
        resolutions = list(settings.available_resolutions) or [VideoResolutionOption(width=settings.width or 0, height=settings.height or 0)]
        for encoding in encodings or [settings.encoding or "Unknown"]:
            for resolution in resolutions:
                width = resolution.width if resolution.width else settings.width
                height = resolution.height if resolution.height else settings.height
                current_choices.append((str(encoding), width, height))

        self.encoder_resolution_combo.blockSignals(True)
        self.encoder_resolution_combo.clear()
        for encoding, width, height in current_choices:
            self.encoder_resolution_combo.addItem(
                self._encoder_resolution_label(encoding, width, height),
                {"encoding": encoding, "width": width, "height": height},
            )

        if settings.encoding:
            preferred_index = next(
                (
                    index
                    for index in range(self.encoder_resolution_combo.count())
                    if isinstance(self.encoder_resolution_combo.itemData(index), dict)
                    and self.encoder_resolution_combo.itemData(index).get("encoding") == settings.encoding
                    and self.encoder_resolution_combo.itemData(index).get("width") == settings.width
                    and self.encoder_resolution_combo.itemData(index).get("height") == settings.height
                ),
                -1,
            )
            if preferred_index < 0 and self.encoder_resolution_combo.count() > 0:
                preferred_index = 0
            if preferred_index >= 0:
                self.encoder_resolution_combo.setCurrentIndex(preferred_index)
        self.encoder_resolution_combo.setEnabled(self.encoder_resolution_combo.count() > 0)
        self.encoder_resolution_combo.blockSignals(False)

        quality_min = settings.quality_range.minimum if settings.quality_range else 0.0
        quality_max = settings.quality_range.maximum if settings.quality_range else 100.0
        self._configure_numeric_input(
            self.quality_input,
            settings.quality,
            quality_min,
            quality_max,
            settings.quality is not None or settings.quality_range is not None,
        )

        frame_min = settings.frame_rate_range.minimum if settings.frame_rate_range else 1
        frame_max = settings.frame_rate_range.maximum if settings.frame_rate_range else 120
        self._configure_numeric_input(
            self.frame_rate_input,
            settings.frame_rate,
            frame_min,
            frame_max,
            settings.frame_rate is not None or settings.frame_rate_range is not None,
        )

        interval_min = settings.encoding_interval_range.minimum if settings.encoding_interval_range else 1
        interval_max = settings.encoding_interval_range.maximum if settings.encoding_interval_range else 120
        self._configure_numeric_input(
            self.encoding_interval_input,
            settings.encoding_interval,
            interval_min,
            interval_max,
            settings.encoding_interval is not None or settings.encoding_interval_range is not None,
        )

        bitrate_min = settings.bitrate_range.minimum if settings.bitrate_range else 1
        bitrate_max = settings.bitrate_range.maximum if settings.bitrate_range else 100000
        self._configure_numeric_input(
            self.bitrate_limit_input,
            settings.bitrate_limit,
            bitrate_min,
            bitrate_max,
            settings.bitrate_limit is not None or settings.bitrate_range is not None,
        )

        gov_min = settings.gov_length_range.minimum if settings.gov_length_range else 1
        gov_max = settings.gov_length_range.maximum if settings.gov_length_range else 1000
        self._configure_numeric_input(
            self.gov_length_input,
            settings.gov_length,
            gov_min,
            gov_max,
            settings.gov_length is not None or settings.gov_length_range is not None,
        )

        self.btn_refresh.setEnabled(True)
        self.btn_apply.setEnabled(True)
        self.setEnabled(True)
        self._sync_codec_specific_controls()

    def values(self) -> dict[str, Any]:
        if self._current_settings is None:
            return {}

        values: dict[str, Any] = {}
        selected_choice = self.encoder_resolution_combo.currentData()
        if isinstance(selected_choice, dict):
            if selected_choice.get("encoding"):
                values["encoding"] = str(selected_choice["encoding"])
            if selected_choice.get("width") is not None and selected_choice.get("height") is not None:
                values["width"] = int(selected_choice["width"])
                values["height"] = int(selected_choice["height"])

        if self.quality_input.isEnabled():
            values["quality"] = float(self.quality_input.value())
        if self.frame_rate_input.isEnabled():
            values["frame_rate"] = int(self.frame_rate_input.value())
        if self.encoding_interval_input.isEnabled():
            values["encoding_interval"] = int(self.encoding_interval_input.value())
        if self.bitrate_limit_input.isEnabled():
            values["bitrate_limit"] = int(self.bitrate_limit_input.value())
        if self.gov_length_input.isEnabled() and isinstance(selected_choice, dict) and selected_choice.get("encoding") in {"H264", "MPEG4"}:
            values["gov_length"] = int(self.gov_length_input.value())

        return values

    def _sync_codec_specific_controls(self):
        is_inter_frame_codec = False
        current_choice = self.encoder_resolution_combo.currentData()
        if isinstance(current_choice, dict):
            is_inter_frame_codec = current_choice.get("encoding") in {"H264", "MPEG4"}
        self.gov_length_input.setEnabled(self._current_settings is not None and is_inter_frame_codec)

    def _on_refresh(self):
        if self.refresh_callback:
            self.refresh_callback()

    def _on_apply(self):
        if self.apply_callback:
            self.apply_callback(self.values())


class MediaProfilesWidget(QGroupBox):
    """ODM-like profiles browser with activation and CRUD actions."""

    def __init__(self, parent=None):
        super().__init__("Profiles", parent)
        self.activate_callback = None
        self.refresh_callback = None
        self.create_callback = None
        self.edit_callback = None
        self.delete_callback = None
        self._has_active_camera = False
        self._init_ui()
        self.set_session(None)

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)

        self.status_label = QLabel("No active camera")
        self.status_label.setObjectName("sectionHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.profile_list = QListWidget()
        self.profile_list.itemDoubleClicked.connect(self._on_activate)
        self.profile_list.currentItemChanged.connect(self._update_actions)
        layout.addWidget(self.profile_list)

        button_layout = QGridLayout()
        self.btn_create = QPushButton("Create")
        self.btn_edit = QPushButton("Edit")
        self.btn_delete = QPushButton("Delete")
        self.btn_refresh = QPushButton("Refresh")

        self.btn_create.clicked.connect(self._on_create)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_refresh.clicked.connect(self._on_refresh)

        button_layout.addWidget(self.btn_refresh, 0, 0, 1, 2)
        button_layout.addWidget(self.btn_create, 1, 0)
        button_layout.addWidget(self.btn_edit, 1, 1)
        button_layout.addWidget(self.btn_delete, 2, 0, 1, 2)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def set_session(self, session: Optional["CameraSession"]):
        self.profile_list.clear()

        if session is None:
            self._has_active_camera = False
            self.status_label.setText("No active camera")
            self._update_actions()
            self.setEnabled(False)
            return

        self._has_active_camera = True
        active_count = 0
        for profile in session.stream_profiles:
            item_text = profile.display_name

            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, profile.token)
            item.setToolTip(f"Token: {profile.token}")
            if profile.token == session.active_stream_token:
                active_count += 1
                item.setBackground(QColor("#1f3553"))
                item.setForeground(QColor("#ffffff"))
            self.profile_list.addItem(item)

        if self.profile_list.count() == 0:
            self.status_label.setText("No media profiles available for this camera")
        else:
            self.status_label.setText(
                f"{self.profile_list.count()} profile(s) available, {active_count} active"
            )

        self.setEnabled(True)
        self._update_actions()

    def _selected_token(self) -> Optional[str]:
        item = self.profile_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _update_actions(self, *_args):
        selected = self._selected_token() is not None
        self.btn_create.setEnabled(self._has_active_camera)
        self.btn_edit.setEnabled(self._has_active_camera and selected)
        self.btn_delete.setEnabled(self._has_active_camera and selected)
        self.btn_refresh.setEnabled(self._has_active_camera)

    def _on_activate(self, *_args):
        token = self._selected_token()
        if token and self.activate_callback:
            self.activate_callback(token)

    def _on_create(self):
        if self.create_callback:
            self.create_callback()

    def _on_edit(self):
        token = self._selected_token()
        if token and self.edit_callback:
            self.edit_callback(token)

    def _on_delete(self):
        token = self._selected_token()
        if token and self.delete_callback:
            self.delete_callback(token)

    def _on_refresh(self):
        if self.refresh_callback:
            self.refresh_callback()


class ImagingSettingsWidget(QGroupBox):
    """ODM-like imaging settings editor generated from the camera response."""

    def __init__(self, parent=None):
        super().__init__("Imaging Settings", parent)
        self.refresh_callback = None
        self.apply_callback = None
        self._editor_meta: dict[str, tuple[QWidget, str]] = {}
        self._displayed_row_count = 0
        self._init_ui()
        self.clear_state("No active camera")

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)

        self.status_label = QLabel()
        self.status_label.setObjectName("sectionHint")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        layout.addWidget(self.status_label)

        self.fields_container = QWidget()
        self.fields_layout = QVBoxLayout(self.fields_container)
        self.fields_layout.setContentsMargins(0, 0, 0, 0)
        self.fields_layout.setSpacing(8)
        layout.addWidget(self.fields_container)

        button_layout = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_apply = QPushButton("Apply")
        self.btn_refresh.clicked.connect(self._on_refresh)
        self.btn_apply.clicked.connect(self._on_apply)
        button_layout.addWidget(self.btn_refresh)
        button_layout.addWidget(self.btn_apply)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def _clear_layout(self, layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            child_widget = item.widget()
            child_layout = item.layout()
            if child_widget is not None:
                child_widget.deleteLater()
            elif child_layout is not None:
                self._clear_layout(child_layout)

    def _format_key(self, key: str) -> str:
        characters: list[str] = []
        previous = ""
        for character in key.replace("_", " "):
            if previous and character.isupper() and previous not in {" ", "/"} and not previous.isupper():
                characters.append(" ")
            characters.append(character)
            previous = character
        return "".join(characters).strip() or key

    def _canonical_option_key(self, key: str) -> str:
        if key.endswith("Modes"):
            mode_candidate = key[:-1]
            if mode_candidate.endswith("Mode"):
                return mode_candidate
            return key[:-5]
        return key

    def _resolve_option_value(self, key: str, options: Any) -> Any:
        if not isinstance(options, dict):
            return None

        if key in options:
            return options.get(key)

        for option_key, option_value in options.items():
            if self._canonical_option_key(str(option_key)) == key:
                return option_value

        return None

    def _split_group_entries(self, value: Any, matcher) -> tuple[Any, Any]:
        if not isinstance(value, dict):
            return value, None

        remaining: dict[str, Any] = {}
        extracted: dict[str, Any] = {}

        for key, item in value.items():
            key_text = str(key)
            if matcher(key_text):
                extracted[key] = item
                continue

            child_remaining, child_extracted = self._split_group_entries(item, matcher)
            if isinstance(item, dict):
                if isinstance(child_remaining, dict) and child_remaining:
                    remaining[key] = child_remaining
                elif not isinstance(child_remaining, dict) and child_remaining is not None:
                    remaining[key] = child_remaining

                if isinstance(child_extracted, dict) and child_extracted:
                    extracted[key] = child_extracted
                elif not isinstance(child_extracted, dict) and child_extracted is not None:
                    extracted[key] = child_extracted
                continue

            remaining[key] = item

        return remaining, extracted

    def _combined_keys(self, settings: Any, options: Any) -> list[str]:
        keys: list[str] = []
        for candidate, use_option_aliases in ((settings, False), (options, True)):
            if not isinstance(candidate, dict):
                continue
            for key in candidate:
                key_text = str(key)
                if use_option_aliases:
                    key_text = self._canonical_option_key(key_text)
                if key_text not in keys:
                    keys.append(key_text)
        return keys

    def _is_range_dict(self, value: Any) -> bool:
        return isinstance(value, dict) and "Min" in value and "Max" in value

    def _is_choice_list(self, value: Any) -> bool:
        return isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value)

    def _set_nested_value(self, target: dict[str, Any], path: list[str], value: Any):
        current = target
        for segment in path[:-1]:
            current = current.setdefault(segment, {})
        current[path[-1]] = value

    def _create_readonly_row(self, label_text: str, value_text: str) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(10)

        caption = QLabel(f"{label_text}:")
        caption.setMinimumWidth(170)
        caption.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        row_layout.addWidget(caption)

        value_label = QLabel(value_text)
        value_label.setWordWrap(True)
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row_layout.addWidget(value_label, 1)
        return row

    def _create_editor_row(self, label_text: str, editor: QWidget) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(10)

        caption = QLabel(f"{label_text}:")
        caption.setMinimumWidth(170)
        row_layout.addWidget(caption)
        row_layout.addWidget(editor, 1)
        return row

    def _make_editor(self, current_value: Any, option_value: Any) -> tuple[Optional[QWidget], Optional[str]]:
        if isinstance(current_value, bool):
            checkbox = QCheckBox()
            checkbox.setChecked(current_value)
            return checkbox, "bool"

        if self._is_choice_list(option_value):
            combo = QComboBox()
            choices = list(option_value)
            if current_value is not None and current_value not in choices:
                choices.insert(0, current_value)
            for choice in choices:
                combo.addItem(str(choice), choice)
            if current_value is not None:
                current_index = combo.findData(current_value)
                if current_index >= 0:
                    combo.setCurrentIndex(current_index)
            return combo, "enum"

        if self._is_range_dict(option_value) or isinstance(current_value, (int, float)):
            minimum = option_value.get("Min", 0) if self._is_range_dict(option_value) else current_value
            maximum = option_value.get("Max", minimum if minimum is not None else 0) if self._is_range_dict(option_value) else current_value
            is_float = any(isinstance(item, float) for item in (current_value, minimum, maximum) if item is not None)

            if is_float:
                spin_box = QDoubleSpinBox()
                spin_box.setDecimals(3)
                spin_box.setRange(float(minimum or 0), float(maximum or 0))
                spin_box.setValue(float(current_value if current_value is not None else minimum or 0))
                spin_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
                return spin_box, "float"

            spin_box = QSpinBox()
            spin_box.setRange(int(minimum or 0), int(maximum or 0))
            spin_box.setValue(int(current_value if current_value is not None else minimum or 0))
            spin_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            return spin_box, "int"

        if isinstance(current_value, str) or isinstance(option_value, str):
            line_edit = QLineEdit(str(current_value or ""))
            return line_edit, "str"

        if current_value is None and option_value is not None and not isinstance(option_value, (dict, list)):
            line_edit = QLineEdit(str(option_value))
            return line_edit, "str"

        return None, None

    def _build_fields(
        self,
        layout: QVBoxLayout,
        settings: dict[str, Any],
        options: dict[str, Any],
        path: tuple[str, ...],
    ):
        for key in self._combined_keys(settings, options):
            current_value = settings.get(key) if isinstance(settings, dict) else None
            option_value = self._resolve_option_value(key, options)
            label_text = self._format_key(key)
            full_path = path + (key,)
            path_text = ".".join(full_path)

            current_is_group = isinstance(current_value, dict) and not self._is_range_dict(current_value)
            option_is_group = isinstance(option_value, dict) and not self._is_range_dict(option_value)
            if current_is_group or option_is_group:
                group = QGroupBox(label_text)
                group_layout = QVBoxLayout(group)
                group_layout.setContentsMargins(12, 10, 12, 10)
                group_layout.setSpacing(8)
                self._build_fields(
                    group_layout,
                    current_value if isinstance(current_value, dict) else {},
                    option_value if isinstance(option_value, dict) else {},
                    full_path,
                )
                if group_layout.count() > 0:
                    layout.addWidget(group)
                else:
                    group.deleteLater()
                continue

            if isinstance(current_value, list) and not self._is_choice_list(current_value):
                layout.addWidget(
                    self._create_readonly_row(
                        label_text,
                        json.dumps(current_value, ensure_ascii=False, indent=2),
                    )
                )
                self._displayed_row_count += 1
                continue

            if isinstance(option_value, list) and not self._is_choice_list(option_value):
                layout.addWidget(
                    self._create_readonly_row(
                        label_text,
                        json.dumps(option_value, ensure_ascii=False, indent=2),
                    )
                )
                self._displayed_row_count += 1
                continue

            editor, editor_kind = self._make_editor(current_value, option_value)
            if editor is not None and editor_kind is not None:
                self._editor_meta[path_text] = (editor, editor_kind)
                layout.addWidget(self._create_editor_row(label_text, editor))
                self._displayed_row_count += 1
                continue

            if current_value is not None:
                layout.addWidget(
                    self._create_readonly_row(
                        label_text,
                        str(current_value),
                    )
                )
                self._displayed_row_count += 1

    def clear_state(self, message: str, allow_refresh: bool = False):
        self._editor_meta.clear()
        self._displayed_row_count = 0
        self._clear_layout(self.fields_layout)
        self.status_label.clear()
        self.btn_refresh.setEnabled(allow_refresh)
        self.btn_apply.setEnabled(False)
        self.setEnabled(allow_refresh)

    def set_configuration(self, payload: ImagingSettingsPayload):
        settings = payload.settings if isinstance(payload.settings, dict) else {}
        options = payload.options if isinstance(payload.options, dict) else {}

        self._editor_meta.clear()
        self._displayed_row_count = 0
        self._clear_layout(self.fields_layout)

        adjustment_keys = {"Brightness", "ColorSaturation", "Contrast", "Sharpness"}

        def _matches_group(key: str, expected: set[str]) -> bool:
            canonical = self._canonical_option_key(key).casefold()
            expected_values = {item.casefold() for item in expected}
            return canonical in expected_values or any(marker in canonical for marker in expected_values)

        remaining_settings, adjustment_settings = self._split_group_entries(
            settings,
            lambda key: _matches_group(key, adjustment_keys),
        )
        remaining_options, adjustment_options = self._split_group_entries(
            options,
            lambda key: _matches_group(key, adjustment_keys),
        )

        if adjustment_settings or adjustment_options:
            adjustments_group = QGroupBox("Image Adjustments")
            adjustments_layout = QVBoxLayout(adjustments_group)
            adjustments_layout.setContentsMargins(12, 10, 12, 10)
            adjustments_layout.setSpacing(8)
            self._build_fields(adjustments_layout, adjustment_settings, adjustment_options, ())
            if adjustments_layout.count() > 0:
                self.fields_layout.addWidget(adjustments_group)

        infrared_keys = {"IrCutFilterMode"}
        remaining_settings, infrared_settings = self._split_group_entries(
            remaining_settings,
            lambda key: _matches_group(key, infrared_keys),
        )
        remaining_options, infrared_options = self._split_group_entries(
            remaining_options,
            lambda key: _matches_group(key, infrared_keys),
        )

        if infrared_settings or infrared_options:
            infrared_group = QGroupBox("Infrared")
            infrared_layout = QVBoxLayout(infrared_group)
            infrared_layout.setContentsMargins(12, 10, 12, 10)
            infrared_layout.setSpacing(8)
            self._build_fields(infrared_layout, infrared_settings, infrared_options, ())
            if infrared_layout.count() > 0:
                self.fields_layout.addWidget(infrared_group)

        self._build_fields(self.fields_layout, remaining_settings, remaining_options, ())

        if self._displayed_row_count == 0:
            self.status_label.clear()
            self.btn_refresh.setEnabled(True)
            self.btn_apply.setEnabled(False)
            self.setEnabled(True)
            return

        self.status_label.clear()
        self.btn_refresh.setEnabled(True)
        self.btn_apply.setEnabled(bool(self._editor_meta))
        self.setEnabled(True)

    def values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for path_text, (editor, editor_kind) in self._editor_meta.items():
            if editor_kind == "bool":
                value = bool(editor.isChecked())
            elif editor_kind == "float":
                value = float(editor.value())
            elif editor_kind == "int":
                value = int(editor.value())
            elif editor_kind == "enum":
                value = editor.currentData()
            else:
                value = editor.text()
            self._set_nested_value(values, path_text.split("."), value)
        return values

    def _on_refresh(self):
        if self.refresh_callback:
            self.refresh_callback()

    def _on_apply(self):
        if self.apply_callback:
            self.apply_callback(self.values())


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ONVIF PTZ Controller — PoC")
        self.setMinimumSize(1024, 700)
        self.resize(1360, 860)

        self._camera_sessions: dict[str, CameraSession] = {}
        self._current_camera_id: Optional[str] = None
        self._matrix_camera_order: list[str] = []
        self._workspace_mode = WORKSPACE_MODE_SINGLE
        self._saved_cameras: dict[str, SavedCameraConfig] = self._load_saved_camera_configs()
        self._add_camera_defaults = next(
            iter(self._saved_cameras.values()),
            SavedCameraConfig(
                host="172.18.212.18",
                port=80,
                username="admin",
                password="Supervisor",
            ),
        )

        # Audio playback via QAudioSink (PCM from VideoStreamThread)
        self._audio_sink: Optional[QAudioSink] = None
        self._audio_io: Optional[QIODevice] = None
        # Continuous PCM byte buffer; _flush_audio drains it into the sink.
        # Using a bytearray avoids truncating chunks at chunk boundaries.
        self._audio_buf: bytearray = bytearray()
        self._audio_timer = QTimer()
        self._audio_timer.setInterval(10)
        self._audio_timer.timeout.connect(self._flush_audio)

        # Status polling timer
        self._status_timer = QTimer()
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._poll_status)

        self._init_ui()
        self._apply_dark_theme()
        self._setup_keyboard_shortcuts()
        self._refresh_saved_cameras_widget()

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.saved_cameras_widget = SavedCamerasWidget()
        self.saved_cameras_widget.open_camera_callback = self._open_saved_camera
        self.saved_cameras_widget.forget_camera_callback = self._remove_saved_camera_entry
        self.saved_cameras_widget.edit_camera_callback = self._edit_saved_camera_entry

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 10, 10)
        body_layout.setSpacing(0)

        self.left_activity_bar = QFrame()
        self.left_activity_bar.setObjectName("leftActivityRail")
        left_activity_layout = QVBoxLayout(self.left_activity_bar)
        left_activity_layout.setContentsMargins(6, 10, 6, 10)
        left_activity_layout.setSpacing(8)
        self.btn_add_camera_sidebar = self._build_activity_button(
            "+",
            "Add Camera",
            self._show_add_camera_dialog,
            checkable=False,
        )
        left_activity_layout.addWidget(self.btn_add_camera_sidebar, 0, Qt.AlignmentFlag.AlignTop)
        self.btn_saved_cameras_sidebar = self._build_activity_button(
            "CAM",
            "Saved Cameras",
            self._toggle_left_sidebar,
        )
        left_activity_layout.addWidget(self.btn_saved_cameras_sidebar, 0, Qt.AlignmentFlag.AlignTop)
        left_activity_layout.addStretch()
        body_layout.addWidget(self.left_activity_bar)

        self.left_sidebar_frame = QFrame()
        self.left_sidebar_frame.setObjectName("sidebarPanel")
        self.left_sidebar_frame.setMinimumWidth(300)
        self.left_sidebar_frame.setMaximumWidth(360)
        left_sidebar_layout = QVBoxLayout(self.left_sidebar_frame)
        left_sidebar_layout.setContentsMargins(12, 12, 12, 12)
        left_sidebar_layout.setSpacing(12)
        left_sidebar_header = QHBoxLayout()
        left_sidebar_header.setContentsMargins(0, 0, 0, 0)
        self.left_sidebar_title = QLabel("Saved Cameras")
        self.left_sidebar_title.setObjectName("sidebarTitle")
        left_sidebar_header.addWidget(self.left_sidebar_title)
        left_sidebar_header.addStretch()
        left_sidebar_header.addWidget(self._build_sidebar_close_button(self._close_left_sidebar))
        left_sidebar_layout.addLayout(left_sidebar_header)
        left_sidebar_layout.addWidget(self.saved_cameras_widget, 1)
        self.left_sidebar_frame.hide()
        body_layout.addWidget(self.left_sidebar_frame)

        center_panel = QFrame()
        center_panel.setObjectName("surfacePanel")
        center_layout = QVBoxLayout(center_panel)
        center_layout.setContentsMargins(18, 18, 18, 18)
        center_layout.setSpacing(0)
        self.workspace_hint_label = QLabel()
        self.workspace_hint_label.hide()

        self.camera_stack = QStackedWidget()
        self.empty_camera_widget = self._build_empty_camera_widget()

        self.camera_tabs = QTabWidget()
        self.camera_tabs.setDocumentMode(True)
        self.camera_tabs.setMovable(True)
        self.camera_tabs.setTabsClosable(True)
        self.camera_tabs.setUsesScrollButtons(True)
        self.camera_tabs.currentChanged.connect(self._on_camera_tab_changed)
        self.camera_tabs.tabCloseRequested.connect(self._on_camera_tab_close_requested)
        self.camera_tabs.tabBar().tabMoved.connect(self._on_camera_tab_moved)
        self.camera_tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.camera_tabs.tabBar().customContextMenuRequested.connect(self._on_camera_tab_context_menu)

        self.matrix_scroll = QScrollArea()
        self.matrix_scroll.setWidgetResizable(True)
        self.matrix_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.matrix_container = QWidget()
        self.matrix_layout = QGridLayout(self.matrix_container)
        self.matrix_layout.setContentsMargins(0, 0, 0, 0)
        self.matrix_layout.setSpacing(12)
        self.matrix_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.matrix_scroll.setWidget(self.matrix_container)
        self.matrix_scroll.viewport().installEventFilter(self)
        self.matrix_scroll.setProperty("workspace_tab", WORKSPACE_MODE_MATRIX)
        self._matrix_tab_index = -1

        self.camera_stack.addWidget(self.empty_camera_widget)
        self.camera_stack.addWidget(self.camera_tabs)
        self.camera_stack.setCurrentWidget(self.empty_camera_widget)
        center_layout.addWidget(self.camera_stack)
        body_layout.addWidget(center_panel, 1)

        self.details_widget = CameraDetailsWidget()

        self.ptz_widget = PTZControlWidget()
        self.ptz_widget.ptz_callback = self._on_ptz_move
        self.ptz_widget.stop_callback = self._on_ptz_stop

        self.presets_widget = PresetsWidget()
        self.presets_widget.goto_callback = self._on_goto_preset
        self.presets_widget.refresh_callback = self._on_refresh_presets
        self.presets_widget.save_callback = self._on_save_preset
        self.presets_widget.delete_callback = self._on_delete_preset

        self.live_video_widget = LiveVideoWidget()
        self.live_video_widget.refresh_uri_callback = self._on_refresh_stream_uri

        self.video_streaming_widget = VideoStreamingWidget()
        self.video_streaming_widget.refresh_callback = self._on_refresh_video_streaming
        self.video_streaming_widget.apply_callback = self._on_apply_video_streaming

        self.profiles_widget = MediaProfilesWidget()
        self.profiles_widget.activate_callback = self._on_stream_changed
        self.profiles_widget.refresh_callback = self._on_refresh_profiles
        self.profiles_widget.create_callback = self._on_create_profile
        self.profiles_widget.edit_callback = self._on_edit_profile
        self.profiles_widget.delete_callback = self._on_delete_profile

        self.imaging_settings_widget = ImagingSettingsWidget()
        self.imaging_settings_widget.refresh_callback = self._on_refresh_imaging_settings
        self.imaging_settings_widget.apply_callback = self._on_apply_imaging_settings

        self.network_settings_widget = NetworkSettingsWidget()
        self.network_settings_widget.refresh_callback = self._on_refresh_network_settings
        self.network_settings_widget.apply_callback = self._on_apply_network_settings

        self.maintenance_widget = MaintenanceWidget()
        self.maintenance_widget.soft_reset_callback = self._on_soft_factory_reset
        self.maintenance_widget.hard_reset_callback = self._on_hard_factory_reset
        self.maintenance_widget.reboot_callback = self._on_reboot_device
        self.maintenance_widget.upgrade_callback = self._on_upgrade_firmware

        self.user_management_widget = UserManagementWidget()
        self.user_management_widget.refresh_callback = self._on_refresh_user_accounts
        self.user_management_widget.create_callback = self._on_create_user_account
        self.user_management_widget.edit_callback = self._on_edit_user_account
        self.user_management_widget.delete_callback = self._on_delete_user_account

        self.ptz_widget.setEnabled(False)
        self.presets_widget.setEnabled(False)

        self.tool_tabs = QTabWidget()
        self.tool_tabs.setObjectName("toolTabs")
        self.tool_tabs.setMinimumWidth(360)
        self.tool_tabs.currentChanged.connect(self._on_tool_panel_index_changed)
        self._info_tab_index = self.tool_tabs.addTab(
            self._build_tool_page(self.details_widget),
            "Info",
        )
        self._ptz_tab_index = self.tool_tabs.addTab(
            self._build_tool_page(self._build_ptz_control_panel()),
            "PTZ Control",
        )
        self._video_streaming_tab_index = self.tool_tabs.addTab(
            self._build_tool_page(self.video_streaming_widget),
            "Video Streaming",
        )
        self._imaging_tab_index = self.tool_tabs.addTab(
            self._build_tool_page(self.imaging_settings_widget),
            "Imaging Settings",
        )
        self._profiles_tab_index = self.tool_tabs.addTab(
            self._build_tool_page(self.profiles_widget),
            "Profiles",
        )
        self._network_settings_tab_index = self.tool_tabs.addTab(
            self._build_tool_page(self.network_settings_widget),
            "Network Settings",
        )
        self._maintenance_tab_index = self.tool_tabs.addTab(
            self._build_tool_page(self.maintenance_widget),
            "Maintenance",
        )
        self._user_management_tab_index = self.tool_tabs.addTab(
            self._build_tool_page(self.user_management_widget),
            "User Management",
        )
        self.tool_tabs.tabBar().hide()
        self._tool_panel_titles = {
            self._info_tab_index: "Info",
            self._ptz_tab_index: "PTZ Control",
            self._video_streaming_tab_index: "Video Streaming",
            self._imaging_tab_index: "Imaging Settings",
            self._profiles_tab_index: "Profiles",
            self._network_settings_tab_index: "Network Settings",
            self._maintenance_tab_index: "Maintenance",
            self._user_management_tab_index: "User Management",
        }

        self.right_sidebar_frame = QFrame()
        self.right_sidebar_frame.setObjectName("assistantPanel")
        self.right_sidebar_frame.setMinimumWidth(360)
        self.right_sidebar_frame.setMaximumWidth(420)
        right_sidebar_layout = QVBoxLayout(self.right_sidebar_frame)
        right_sidebar_layout.setContentsMargins(12, 12, 12, 12)
        right_sidebar_layout.setSpacing(12)
        right_sidebar_header = QHBoxLayout()
        right_sidebar_header.setContentsMargins(0, 0, 0, 0)
        self.right_sidebar_title_label = QLabel("Info")
        self.right_sidebar_title_label.setObjectName("sidebarTitle")
        right_sidebar_header.addWidget(self.right_sidebar_title_label)
        right_sidebar_header.addStretch()
        right_sidebar_header.addWidget(self._build_sidebar_close_button(self._close_tool_sidebar))
        right_sidebar_layout.addLayout(right_sidebar_header)
        right_sidebar_layout.addWidget(self.tool_tabs, 1)
        self.right_sidebar_frame.hide()
        body_layout.addWidget(self.right_sidebar_frame)

        self._tool_button_by_index: dict[int, QToolButton] = {}
        self.right_activity_bar = QFrame()
        self.right_activity_bar.setObjectName("rightActivityRail")
        right_activity_layout = QVBoxLayout(self.right_activity_bar)
        right_activity_layout.setContentsMargins(6, 10, 6, 10)
        right_activity_layout.setSpacing(8)
        for button_text, tab_index, tooltip in [
            ("INFO", self._info_tab_index, "Info"),
            ("PTZ", self._ptz_tab_index, "PTZ Control"),
            ("STR", self._video_streaming_tab_index, "Video Streaming"),
            ("IMG", self._imaging_tab_index, "Imaging Settings"),
            ("PROF", self._profiles_tab_index, "Profiles"),
            ("NET", self._network_settings_tab_index, "Network Settings"),
            ("MNT", self._maintenance_tab_index, "Maintenance"),
            ("USR", self._user_management_tab_index, "User Management"),
        ]:
            button = self._build_activity_button(
                button_text,
                tooltip,
                partial(self._toggle_tool_sidebar, tab_index),
            )
            self._tool_button_by_index[tab_index] = button
            right_activity_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        right_activity_layout.addStretch()
        body_layout.addWidget(self.right_activity_bar)
        main_layout.addWidget(body, 1)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.lbl_rtsp_uri = QLabel("RTSP URL: -")
        self.lbl_rtsp_uri.setObjectName("statusRtspLabel")
        self.lbl_rtsp_uri.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_bar.addWidget(self.lbl_rtsp_uri, 1)
        self._sync_sidebar_state()

    def _build_activity_button(self, text: str, tooltip: str, callback, *, checkable: bool = True) -> QToolButton:
        button = QToolButton()
        button.setObjectName("activityButton")
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCheckable(checkable)
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(52, 52)
        button.clicked.connect(callback)
        return button

    def _build_sidebar_close_button(self, callback) -> QToolButton:
        button = QToolButton()
        button.setObjectName("sidebarCloseButton")
        button.setText("x")
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(28, 28)
        button.clicked.connect(callback)
        return button

    def _build_tool_page(self, widget: QWidget) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(widget)
        layout.addStretch()
        scroll.setWidget(page)
        return scroll

    def _build_ptz_control_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(self.ptz_widget)
        layout.addWidget(self.presets_widget)
        layout.addStretch()
        return panel

    def _toggle_left_sidebar(self, checked: bool = False):
        if not hasattr(self, "left_sidebar_frame"):
            return
        self.left_sidebar_frame.setVisible(not self.left_sidebar_frame.isVisible())
        self._sync_sidebar_state()

    def _close_left_sidebar(self, checked: bool = False):
        if not hasattr(self, "left_sidebar_frame"):
            return
        self.left_sidebar_frame.hide()
        self._sync_sidebar_state()

    def _toggle_tool_sidebar(self, tab_index: int, checked: bool = False):
        if not hasattr(self, "tool_tabs"):
            return

        if not self.right_sidebar_frame.isVisible():
            self.right_sidebar_frame.show()
            self.tool_tabs.setCurrentIndex(tab_index)
        elif self.tool_tabs.currentIndex() == tab_index:
            self.right_sidebar_frame.hide()
        else:
            self.tool_tabs.setCurrentIndex(tab_index)
            self.right_sidebar_frame.show()

        self._sync_sidebar_state()

    def _close_tool_sidebar(self, checked: bool = False):
        if not hasattr(self, "right_sidebar_frame"):
            return
        self.right_sidebar_frame.hide()
        self._sync_sidebar_state()

    def _on_tool_panel_index_changed(self, index: int):
        if hasattr(self, "right_sidebar_title_label"):
            self.right_sidebar_title_label.setText(getattr(self, "_tool_panel_titles", {}).get(index, "Inspector"))
        self._sync_sidebar_state()

    def _sync_sidebar_state(self):
        if hasattr(self, "btn_saved_cameras_sidebar"):
            self.btn_saved_cameras_sidebar.blockSignals(True)
            self.btn_saved_cameras_sidebar.setChecked(
                hasattr(self, "left_sidebar_frame") and self.left_sidebar_frame.isVisible()
            )
            self.btn_saved_cameras_sidebar.blockSignals(False)

        right_sidebar_visible = hasattr(self, "right_sidebar_frame") and self.right_sidebar_frame.isVisible()
        current_index = self.tool_tabs.currentIndex() if hasattr(self, "tool_tabs") else -1

        if hasattr(self, "right_sidebar_title_label"):
            self.right_sidebar_title_label.setText(getattr(self, "_tool_panel_titles", {}).get(current_index, "Inspector"))

        for index, button in getattr(self, "_tool_button_by_index", {}).items():
            button.blockSignals(True)
            button.setChecked(right_sidebar_visible and index == current_index and button.isVisible())
            button.blockSignals(False)

    def _set_rtsp_status(self, rtsp_url: Optional[str]):
        if not hasattr(self, "lbl_rtsp_uri"):
            return

        text = f"RTSP URL: {rtsp_url}" if rtsp_url else "RTSP URL: -"
        self.lbl_rtsp_uri.setText(text)
        self.lbl_rtsp_uri.setToolTip(rtsp_url or "")

    def _separator(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        return sep

    def _set_ptz_tab_visible(self, visible: bool):
        if not hasattr(self, "tool_tabs"):
            return

        if not visible and self.tool_tabs.currentIndex() == self._ptz_tab_index:
            self.tool_tabs.setCurrentIndex(self._info_tab_index)

        self.tool_tabs.tabBar().setTabVisible(self._ptz_tab_index, visible)
        ptz_button = getattr(self, "_tool_button_by_index", {}).get(self._ptz_tab_index)
        if ptz_button is not None:
            ptz_button.setVisible(visible)
        self._sync_sidebar_state()

    def _update_header_state(self, active_session: Optional[CameraSession] = None):
        if not hasattr(self, "lbl_camera_count_chip") or not hasattr(self, "lbl_active_camera_chip"):
            return

        camera_count = len(self._camera_sessions)
        label = "camera" if camera_count == 1 else "cameras"
        self.lbl_camera_count_chip.setText(f"{camera_count} {label}")

        if active_session is None:
            self.lbl_active_camera_chip.setText("No active camera")
            return

        info = active_session.client.camera_info
        camera_name = " ".join(
            part for part in [info.manufacturer, info.model] if part
        ).strip() or active_session.host
        self.lbl_active_camera_chip.setText(f"Active: {camera_name}")

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e1e;
                color: #cccccc;
            }
            QGroupBox {
                border: 1px solid #2d2d30;
                border-radius: 8px;
                margin-top: 14px;
                padding: 16px 12px 12px 12px;
                font-weight: bold;
                font-size: 13px;
                color: #4fc1ff;
                background-color: #252526;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
            }
            QLineEdit, QSpinBox, QComboBox, QDoubleSpinBox {
                background-color: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 6px;
                padding: 7px 9px;
                color: #f3f3f3;
                font-size: 13px;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QDoubleSpinBox:focus {
                border-color: #3794ff;
            }
            QLabel {
                font-size: 13px;
            }
            QStatusBar {
                background-color: #007acc;
                color: #ffffff;
                font-size: 12px;
                border-top: none;
            }
            QLabel#statusRtspLabel {
                font-family: 'Consolas', 'Cascadia Code', monospace;
                font-size: 12px;
                color: #ffffff;
                padding: 0 8px;
            }
            QSlider::groove:horizontal {
                background: rgba(255, 255, 255, 0.24);
                height: 6px;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                width: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
            QSplitter::handle {
                background-color: #2d2d30;
                width: 4px;
            }
            QTabWidget::pane {
                border: 1px solid #2d2d30;
                border-radius: 8px;
                top: -1px;
                background-color: #1e1e1e;
            }
            QTabBar::tab {
                background-color: #2d2d30;
                border: 1px solid #3c3c3c;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                color: #9d9d9d;
                min-width: 120px;
                padding: 8px 14px;
                margin-right: 4px;
            }
            QTabBar::tab:selected {
                background-color: #1e1e1e;
                color: #ffffff;
            }
            QTabBar::tab:hover:!selected {
                background-color: #37373d;
                color: #f3f3f3;
            }
            QTabWidget#toolTabs::pane {
                border: none;
                background: transparent;
            }
            QFrame#topHeader {
                background-color: #252526;
                border-bottom: 1px solid #2d2d30;
            }
            QLabel#headerCaption {
                color: #9da1a6;
                font-size: 13px;
                font-weight: 600;
            }
            QFrame#surfacePanel {
                background-color: #1e1e1e;
                border: 1px solid #2d2d30;
                border-radius: 8px;
            }
            QFrame#leftActivityRail, QFrame#rightActivityRail {
                background-color: #181818;
            }
            QFrame#leftActivityRail {
                border-right: 1px solid #2d2d30;
            }
            QFrame#rightActivityRail {
                border-left: 1px solid #2d2d30;
            }
            QFrame#sidebarPanel, QFrame#assistantPanel {
                background-color: #252526;
                border: 1px solid #2d2d30;
            }
            QLabel#sidebarTitle {
                color: #ffffff;
                font-size: 12px;
                font-weight: 600;
            }
            QLabel#headerChip {
                background-color: #1f1f1f;
                border: 1px solid #313135;
                border-radius: 11px;
                padding: 6px 10px;
                color: #d6d6d6;
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#sectionTitle {
                font-size: 16px;
                font-weight: 700;
                color: #ffffff;
            }
            QLabel#sectionHint, QLabel#dialogHint {
                color: #8f8f8f;
            }
            QPushButton {
                background-color: #2d2d30;
                color: #f3f3f3;
                border: 1px solid #3c3c3c;
                border-radius: 6px;
                padding: 7px 12px;
            }
            QPushButton:hover {
                background-color: #37373d;
            }
            QPushButton:pressed {
                background-color: #1f1f1f;
            }
            QPushButton:disabled {
                background-color: #252526;
                color: #6c6c6c;
                border-color: #2d2d30;
            }
            QPushButton#primaryButton {
                background-color: #0e639c;
                border-color: #1177bb;
                color: #ffffff;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#primaryButton:hover {
                background-color: #1177bb;
            }
            QPushButton#primaryButton:pressed {
                background-color: #094771;
            }
            QPushButton#secondaryButton {
                background-color: #252526;
                border-color: #3c3c3c;
            }
            QPushButton#secondaryButton:hover {
                background-color: #333337;
            }
            QToolButton#activityButton {
                background-color: transparent;
                color: #9d9d9d;
                border: none;
                border-left: 2px solid transparent;
                border-radius: 0;
                font-size: 11px;
                font-weight: 700;
                padding: 6px 4px;
            }
            QToolButton#activityButton:hover {
                background-color: #2a2d2e;
                color: #ffffff;
            }
            QToolButton#activityButton:checked {
                background-color: #2a2d2e;
                color: #ffffff;
                border-left-color: #3794ff;
            }
            QPushButton#ptzDirectionButton,
            QPushButton#ptzStopButton {
                background-color: #2d2d30;
                color: #f3f3f3;
                border: 1px solid #3c3c3c;
                border-radius: 10px;
                font-size: 18px;
                font-weight: 700;
                min-width: 50px;
                min-height: 50px;
                padding: 0;
            }
            QPushButton#ptzDirectionButton:hover,
            QPushButton#ptzStopButton:hover {
                background-color: #37373d;
            }
            QPushButton#ptzDirectionButton:pressed,
            QPushButton#ptzStopButton:pressed {
                background-color: #1f1f1f;
            }
            QPushButton#ptzStopButton {
                background-color: #5a1d1d;
                border-color: #7a2d2d;
            }
            QPushButton#ptzStopButton:hover {
                background-color: #6c2323;
            }
            QPushButton#ptzZoomButton {
                background-color: #2d2d30;
                color: #f3f3f3;
                border: 1px solid #3c3c3c;
                border-radius: 8px;
                padding: 7px 10px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton#ptzZoomButton:hover {
                background-color: #37373d;
            }
            QRadioButton#modeToggle {
                color: #cfcfcf;
                background: transparent;
                border: none;
                padding: 2px 0;
                spacing: 8px;
            }
            QRadioButton#modeToggle:hover {
                color: #ffffff;
            }
            QRadioButton#modeToggle::indicator {
                width: 14px;
                height: 14px;
                border-radius: 7px;
                border: 1px solid #6c6c6c;
                background-color: #1e1e1e;
            }
            QRadioButton#modeToggle::indicator:hover {
                border-color: #3794ff;
            }
            QRadioButton#modeToggle::indicator:checked {
                background-color: #3794ff;
                border-color: #3794ff;
            }
            QRadioButton#modeToggle:checked {
                color: #ffffff;
            }
            QToolButton#sidebarCloseButton {
                background-color: transparent;
                color: #9d9d9d;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: 700;
            }
            QToolButton#sidebarCloseButton:hover {
                background-color: #333337;
                color: #ffffff;
            }
            QListWidget {
                background-color: #1f1f1f;
                border: 1px solid #2d2d30;
                border-radius: 6px;
                padding: 4px;
            }
            QListWidget::item {
                padding: 6px 8px;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #094771;
                color: #ffffff;
            }
            QScrollArea {
                background: transparent;
                border: none;
            }
            QRadioButton {
                color: #d0d0d0;
                background: transparent;
                border: none;
                padding: 2px 0;
                spacing: 8px;
            }
            QRadioButton:hover {
                color: #ffffff;
            }
            QRadioButton::indicator {
                width: 14px;
                height: 14px;
                border-radius: 7px;
                border: 1px solid #6c6c6c;
                background-color: #1e1e1e;
            }
            QRadioButton::indicator:checked {
                background-color: #3794ff;
                border-color: #3794ff;
            }
            QRadioButton:checked {
                color: #ffffff;
            }
        """)

    def _setup_keyboard_shortcuts(self):
        """Install a global event filter so tab shortcuts and PTZ keys work across child widgets."""
        app = QApplication.instance()
        if app is None:
            return

        if getattr(self, "_global_shortcuts_installed", False):
            return

        app.installEventFilter(self)
        self._global_shortcuts_installed = True

    def _focus_blocks_ptz_shortcuts(self) -> bool:
        focus_widget = QApplication.focusWidget()
        if focus_widget is None:
            return False

        return isinstance(focus_widget, (QLineEdit, QAbstractSpinBox, QComboBox, QListWidget))

    def _camera_tab_indices_without_matrix(self) -> list[int]:
        if not hasattr(self, "camera_tabs"):
            return []

        return [
            index
            for index in range(self.camera_tabs.count())
            if not self._is_matrix_tab(self.camera_tabs.widget(index))
        ]

    def _activate_single_tab_by_shortcut(self, position: int):
        if position <= 0:
            return

        single_tab_indices = self._camera_tab_indices_without_matrix()
        if position > len(single_tab_indices):
            return

        self.camera_tabs.setCurrentIndex(single_tab_indices[position - 1])

    def _activate_matrix_tab_from_shortcut(self):
        matrix_index = self._matrix_tab_current_index()
        if matrix_index >= 0:
            self.camera_tabs.setCurrentIndex(matrix_index)

    def _cycle_tabs_by_shortcut(self, step: int):
        if not hasattr(self, "camera_tabs") or self.camera_tabs.count() < 2:
            return

        current_index = self.camera_tabs.currentIndex()
        next_index = (current_index + step) % self.camera_tabs.count()
        self.camera_tabs.setCurrentIndex(next_index)

    def _close_current_tab_from_shortcut(self):
        if not hasattr(self, "camera_tabs"):
            return

        current_index = self.camera_tabs.currentIndex()
        if current_index < 0:
            return

        page = self.camera_tabs.widget(current_index)
        if self._is_matrix_tab(page):
            return

        self._on_camera_tab_close_requested(current_index)

    def _perform_ptz_action(self, action: str) -> bool:
        session = self._active_session()
        if session is None:
            return False

        mode = self.ptz_widget.current_mode
        speed = self.ptz_widget._speed
        step = self.ptz_widget.rel_step.value() if mode == PTZControlWidget.MODE_RELATIVE else speed

        if action == "up":
            self._on_ptz_move(mode, 0, step, 0)
        elif action == "down":
            self._on_ptz_move(mode, 0, -step, 0)
        elif action == "left":
            self._on_ptz_move(mode, -step, 0, 0)
        elif action == "right":
            self._on_ptz_move(mode, step, 0, 0)
        elif action == "up_left":
            self._on_ptz_move(mode, -step, step, 0)
        elif action == "up_right":
            self._on_ptz_move(mode, step, step, 0)
        elif action == "down_left":
            self._on_ptz_move(mode, -step, -step, 0)
        elif action == "down_right":
            self._on_ptz_move(mode, step, -step, 0)
        elif action == "zoom_in":
            self._on_ptz_move(mode, 0, 0, step)
        elif action == "zoom_out":
            self._on_ptz_move(mode, 0, 0, -step)
        elif action == "stop":
            self._on_ptz_stop()
        else:
            return False

        return True

    def _handle_global_key_event(self, event) -> bool:
        if not self.isVisible() or not self.isActiveWindow():
            return False

        active_modal = QApplication.activeModalWidget()
        if active_modal is not None and active_modal is not self:
            return False

        event_type = event.type()
        if event_type not in {QEvent.Type.KeyPress, QEvent.Type.KeyRelease}:
            return False

        modifiers = event.modifiers()
        ctrl_pressed = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        alt_or_meta_pressed = bool(
            modifiers & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
        )

        if event_type == QEvent.Type.KeyPress and ctrl_pressed and not alt_or_meta_pressed:
            key = event.key()
            text = event.text().lower()
            quote_left_key = getattr(Qt.Key, "Key_QuoteLeft", None)
            if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
                self._activate_single_tab_by_shortcut(int(key) - int(Qt.Key.Key_0))
                event.accept()
                return True

            if key in {Qt.Key.Key_Tab, Qt.Key.Key_Backtab}:
                step = -1 if (modifiers & Qt.KeyboardModifier.ShiftModifier or key == Qt.Key.Key_Backtab) else 1
                self._cycle_tabs_by_shortcut(step)
                event.accept()
                return True

            if key == Qt.Key.Key_W:
                self._close_current_tab_from_shortcut()
                event.accept()
                return True

            if (quote_left_key is not None and key == quote_left_key) or text in {"`", "~", "ё"}:
                self._activate_matrix_tab_from_shortcut()
                event.accept()
                return True

        if ctrl_pressed or alt_or_meta_pressed or self._focus_blocks_ptz_shortcuts():
            return False

        action = self._resolve_ptz_action(event)
        if action is None:
            return False

        if event_type == QEvent.Type.KeyPress:
            if event.isAutoRepeat():
                return True
            handled = self._perform_ptz_action(action)
            if handled:
                event.accept()
            return handled

        if event.isAutoRepeat():
            return True

        if action in {
            "up",
            "down",
            "left",
            "right",
            "up_left",
            "up_right",
            "down_left",
            "down_right",
            "zoom_in",
            "zoom_out",
        } and self.ptz_widget.current_mode == PTZControlWidget.MODE_CONTINUOUS:
            self._on_ptz_stop()
            event.accept()
            return True

        return action == "stop"

    def _build_empty_camera_widget(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addStretch()

        placeholder = QLabel(
            "Save a camera in the library above, then click it to open a workspace tab.\n"
            "Saved cameras stay available even when they are offline."
        )
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setWordWrap(True)
        placeholder.setStyleSheet("""
            QLabel {
                background-color: #0f1726;
                color: #8fa3bf;
                font-size: 18px;
                border: 2px dashed #2a3d57;
                border-radius: 18px;
                padding: 40px;
            }
        """)
        layout.addWidget(placeholder)
        layout.addStretch()
        return page

    def _build_camera_session(
        self,
        camera_id: str,
        client: ONVIFPTZClient,
        loop: asyncio.AbstractEventLoop,
        host: str,
        port: int,
        username: str,
        password: str,
        stream_profiles: list[MediaProfileInfo],
        preferred_stream_token: Optional[str] = None,
    ) -> CameraSession:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)

        video_widget = VideoWidget()
        video_widget.volume_btn.setVisible(True)
        video_widget.volume_btn.volume_changed.connect(
            lambda v, cid=camera_id: self._on_camera_volume_changed(cid, v)
        )
        page_layout.addWidget(video_widget)
        page.setProperty("camera_id", camera_id)

        matrix_tile = CameraMatrixTile(camera_id, f"{host}:{port}")
        matrix_tile.clicked.connect(self._on_matrix_tile_clicked)
        matrix_tile.double_clicked.connect(self._on_matrix_tile_double_clicked)
        matrix_tile.swap_requested.connect(self._on_matrix_tile_swap_requested)
        matrix_tile.remove_requested.connect(self._on_matrix_tile_remove_requested)
        matrix_tile.volume_btn.volume_changed.connect(
            lambda v, cid=camera_id: self._on_camera_volume_changed(cid, v)
        )

        active_stream_token = None
        if preferred_stream_token and client.set_active_profile(preferred_stream_token):
            active_stream_token = preferred_stream_token
        else:
            active_stream_token = client.profile_token
            if active_stream_token is None and stream_profiles:
                active_stream_token = stream_profiles[0].token
                client.set_active_profile(active_stream_token)

        return CameraSession(
            camera_id=camera_id,
            page=page,
            video_widget=video_widget,
            matrix_tile=matrix_tile,
            matrix_video_widget=matrix_tile.video_widget,
            matrix_title_label=matrix_tile.title_label,
            client=client,
            loop=loop,
            host=host,
            port=port,
            username=username,
            password=password,
            stream_profiles=stream_profiles,
            active_stream_token=active_stream_token,
        )

    def _set_workspace_mode(self, mode: str):
        if mode not in {WORKSPACE_MODE_SINGLE, WORKSPACE_MODE_MATRIX}:
            return

        if self._workspace_mode == mode:
            return

        self._workspace_mode = mode
        if self._workspace_mode == WORKSPACE_MODE_MATRIX:
            self.workspace_hint_label.setText(
                "Matrix tab keeps all connected cameras visible. Click to select, drag to reorder, double-click to open a single tab."
            )
        else:
            self.workspace_hint_label.setText(
                "Single cameras and Matrix live in the same browser-style tab bar."
            )
        self._apply_workspace_mode()

    def _set_workspace_surface(self):
        if not self._camera_sessions:
            self.camera_stack.setCurrentWidget(self.empty_camera_widget)
        else:
            self.camera_stack.setCurrentWidget(self.camera_tabs)

    def _is_matrix_tab(self, page: Optional[QWidget]) -> bool:
        return page is self.matrix_scroll

    def _is_matrix_tab_active(self) -> bool:
        return self._is_matrix_tab(self.camera_tabs.currentWidget())

    def _matrix_tab_current_index(self) -> int:
        return self.camera_tabs.indexOf(self.matrix_scroll)

    def _has_matrix_tab(self) -> bool:
        return self._matrix_tab_current_index() >= 0

    def _ensure_matrix_tab(self, insert_index: Optional[int] = None) -> int:
        existing_index = self._matrix_tab_current_index()
        if existing_index < 0:
            self._matrix_tab_index = self.camera_tabs.insertTab(0, self.matrix_scroll, "Matrix")
            return self._matrix_tab_index

        if existing_index != 0:
            was_active = self.camera_tabs.currentIndex() == existing_index
            self.camera_tabs.blockSignals(True)
            self.camera_tabs.removeTab(existing_index)
            self._matrix_tab_index = self.camera_tabs.insertTab(0, self.matrix_scroll, "Matrix")
            self.camera_tabs.blockSignals(False)
            if was_active:
                self.camera_tabs.setCurrentIndex(0)
        else:
            self._matrix_tab_index = 0

        return self._matrix_tab_index

    def _remove_matrix_tab_if_empty(self):
        if self._matrix_camera_order:
            return

        matrix_index = self._matrix_tab_current_index()
        if matrix_index < 0:
            return

        was_active = self.camera_tabs.currentIndex() == matrix_index
        self.camera_tabs.blockSignals(True)
        self.camera_tabs.removeTab(matrix_index)
        self.camera_tabs.blockSignals(False)
        self._matrix_tab_index = -1
        self._workspace_mode = WORKSPACE_MODE_SINGLE

        if was_active:
            if self.camera_tabs.count() > 0:
                self.camera_tabs.setCurrentIndex(0)
            elif self._camera_sessions:
                self._show_empty_state()

    def _is_session_tab_visible(self, session: CameraSession) -> bool:
        return self.camera_tabs.indexOf(session.page) >= 0

    def _ensure_session_tab(self, session: CameraSession, activate: bool = True) -> int:
        tab_index = self.camera_tabs.indexOf(session.page)
        if tab_index < 0:
            matrix_index = self._matrix_tab_current_index()
            insert_index = matrix_index + 1 if matrix_index >= 0 else self.camera_tabs.count()
            tab_index = self.camera_tabs.insertTab(insert_index, session.page, session.host)
            self._update_camera_tab_caption(session)

        if activate:
            self.camera_tabs.setCurrentIndex(tab_index)
        return tab_index

    def _detach_session_tab(self, session: CameraSession):
        tab_index = self.camera_tabs.indexOf(session.page)
        if tab_index < 0:
            return

        self.camera_tabs.blockSignals(True)
        self.camera_tabs.removeTab(tab_index)
        self.camera_tabs.blockSignals(False)

    def _move_session_to_matrix(
        self,
        camera_id: str,
        activate_matrix: bool = True,
        detach_tab: bool = True,
    ):
        session = self._camera_sessions.get(camera_id)
        if session is None:
            return

        source_index = self.camera_tabs.indexOf(session.page)
        matrix_index = self._ensure_matrix_tab(insert_index=source_index if source_index >= 0 else None)

        if camera_id not in self._matrix_camera_order:
            self._matrix_camera_order.append(camera_id)

        if detach_tab:
            self._detach_session_tab(session)
        self._workspace_mode = WORKSPACE_MODE_MATRIX
        self._current_camera_id = camera_id
        self._rebuild_matrix_grid()

        matrix_index = self._matrix_tab_current_index()
        if activate_matrix and matrix_index >= 0:
            self.camera_tabs.setCurrentIndex(matrix_index)

    def _on_camera_tab_moved(self, from_index: int, to_index: int):
        if self._has_matrix_tab() and self._matrix_tab_current_index() != 0:
            self._ensure_matrix_tab()

    def _on_camera_tab_context_menu(self, position):
        tab_bar = self.camera_tabs.tabBar()
        index = tab_bar.tabAt(position)
        if index < 0:
            return

        page = self.camera_tabs.widget(index)
        if self._is_matrix_tab(page):
            return

        camera_id = page.property("camera_id") if page else None
        if not camera_id or camera_id not in self._camera_sessions:
            return

        menu = QMenu(self)
        to_matrix_action = menu.addAction("To Matrix Mode")
        chosen_action = menu.exec(tab_bar.mapToGlobal(position))
        if chosen_action == to_matrix_action:
            self._move_session_to_matrix(str(camera_id), activate_matrix=True, detach_tab=True)

    def _clear_matrix_layout(self):
        while self.matrix_layout.count():
            item = self.matrix_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

    def _matrix_column_count(self, camera_count: int) -> int:
        if camera_count <= 1:
            return 1
        if camera_count <= 4:
            return 2
        if camera_count <= 9:
            return 3
        return 4

    def _refresh_matrix_selection(self):
        for session in self._camera_sessions.values():
            session.matrix_tile.set_selected(session.camera_id == self._current_camera_id)

    def _ordered_matrix_sessions(self) -> list[CameraSession]:
        ordered: list[CameraSession] = []
        for camera_id in self._matrix_camera_order:
            session = self._camera_sessions.get(camera_id)
            if session is None:
                continue
            ordered.append(session)

        return ordered

    def _update_matrix_tile_sizes(self):
        sessions = self._ordered_matrix_sessions()
        if not sessions or not hasattr(self, "matrix_scroll"):
            return

        columns = self._matrix_column_count(len(sessions))
        available_width = max(320, self.matrix_scroll.viewport().width())
        spacing = self.matrix_layout.horizontalSpacing()
        spacing = 12 if spacing < 0 else spacing
        preview_width = max(220, min(360, (available_width - (columns - 1) * spacing) // columns - 20))
        preview_height = int(preview_width * 9 / 16)

        for session in sessions:
            session.matrix_tile.set_preview_size(preview_width, preview_height)

    def _rebuild_matrix_grid(self):
        self._clear_matrix_layout()
        sessions = self._ordered_matrix_sessions()
        if not sessions:
            return

        columns = self._matrix_column_count(len(sessions))
        for index, session in enumerate(sessions):
            self._update_camera_tab_caption(session)
            row = index // columns
            column = index % columns
            self.matrix_layout.addWidget(session.matrix_tile, row, column)

        for column in range(columns):
            self.matrix_layout.setColumnStretch(column, 1)

        self._update_matrix_tile_sizes()
        self._refresh_matrix_selection()

    def _on_matrix_tile_clicked(self, camera_id: str):
        if camera_id not in self._camera_sessions:
            return

        self._current_camera_id = camera_id
        session = self._active_session()
        if session is None:
            return

        self._refresh_active_camera_ui(session)
        self._sync_video_streams_for_mode()

    def _on_matrix_tile_double_clicked(self, camera_id: str):
        session = self._camera_sessions.get(camera_id)
        if session is None:
            return

        self._ensure_session_tab(session, activate=True)

    def _on_matrix_tile_remove_requested(self, camera_id: str):
        session = self._camera_sessions.get(camera_id)
        if session is None or camera_id not in self._matrix_camera_order:
            return

        if not self._is_session_tab_visible(session):
            self._remove_camera_session(camera_id, forget_saved=False)
            return

        self._matrix_camera_order = [item for item in self._matrix_camera_order if item != camera_id]
        self._rebuild_matrix_grid()

        if self._matrix_camera_order:
            self._on_matrix_tile_clicked(self._matrix_camera_order[0])
            return

        self._current_camera_id = camera_id
        self._remove_matrix_tab_if_empty()
        tab_index = self.camera_tabs.indexOf(session.page)
        if tab_index >= 0:
            self.camera_tabs.setCurrentIndex(tab_index)
        else:
            self._show_empty_state()

    def _on_matrix_tile_swap_requested(self, source_camera_id: str, target_camera_id: str):
        if source_camera_id == target_camera_id:
            return

        if source_camera_id not in self._matrix_camera_order or target_camera_id not in self._matrix_camera_order:
            return

        source_index = self._matrix_camera_order.index(source_camera_id)
        target_index = self._matrix_camera_order.index(target_camera_id)
        self._matrix_camera_order[source_index], self._matrix_camera_order[target_index] = (
            self._matrix_camera_order[target_index],
            self._matrix_camera_order[source_index],
        )
        self._rebuild_matrix_grid()

    def _sync_video_streams_for_mode(self):
        active_session = self._active_session()
        if active_session is None:
            return

        if self._workspace_mode == WORKSPACE_MODE_MATRIX:
            self._stop_audio()
            matrix_camera_ids = set(self._matrix_camera_order)
            for session in self._camera_sessions.values():
                if session.camera_id not in matrix_camera_ids:
                    self._stop_video_for_session(session, clear_widget=False)
                    continue
                self._start_video_for_session(
                    session,
                    background=session.camera_id != active_session.camera_id,
                    start_audio=False,
                    force_restart=False,
                )
            return

        for session in self._camera_sessions.values():
            if session.camera_id != active_session.camera_id:
                self._stop_video_for_session(session, clear_widget=False)

        self._start_video_for_session(
            active_session,
            background=False,
            start_audio=True,
            force_restart=False,
        )

    def _apply_workspace_mode(self):
        self._set_workspace_surface()
        if not self._camera_sessions:
            return

        active_session = self._active_session()
        if active_session is None:
            if self._workspace_mode == WORKSPACE_MODE_MATRIX and self._matrix_camera_order:
                self._current_camera_id = self._matrix_camera_order[0]
                active_session = self._camera_sessions.get(self._current_camera_id)
            else:
                current_page = self.camera_tabs.currentWidget()
                camera_id = current_page.property("camera_id") if current_page else None
                if camera_id and str(camera_id) in self._camera_sessions:
                    self._current_camera_id = str(camera_id)
                    active_session = self._camera_sessions[self._current_camera_id]
                else:
                    active_session = next(iter(self._camera_sessions.values()))
                    self._current_camera_id = active_session.camera_id

        if active_session is None:
            return

        self._rebuild_matrix_grid()

        if self._workspace_mode == WORKSPACE_MODE_SINGLE and not self._is_matrix_tab_active():
            active_index = self.camera_tabs.indexOf(active_session.page)
            if active_index >= 0 and self.camera_tabs.currentIndex() != active_index:
                self.camera_tabs.setCurrentIndex(active_index)
                return

        self._refresh_active_camera_ui(active_session)
        self._sync_video_streams_for_mode()

    def eventFilter(self, watched, event):
        if self._handle_global_key_event(event):
            return True

        if watched is getattr(self, "matrix_scroll", None).viewport() and event.type() == QEvent.Type.Resize:
            self._update_matrix_tile_sizes()
        return super().eventFilter(watched, event)

    def _active_session(self) -> Optional[CameraSession]:
        if self._current_camera_id is None:
            return None
        return self._camera_sessions.get(self._current_camera_id)

    def _format_position_label(self, status: PTZStatus) -> str:
        suffix = "  [Moving]" if status.moving else ""
        return f"P: {status.pan:.2f}  T: {status.tilt:.2f}  Z: {status.zoom:.2f}{suffix}"

    def _update_camera_tab_caption(self, session: CameraSession):
        info = session.client.camera_info
        tooltip_title = " ".join(
            part for part in [info.manufacturer, info.model] if part
        ).strip() or session.host

        index = self.camera_tabs.indexOf(session.page)
        if index >= 0:
            self.camera_tabs.setTabText(index, session.host)
            self.camera_tabs.setTabToolTip(index, f"{tooltip_title}\n{session.host}:{session.port}")

        if tooltip_title != session.host:
            matrix_title = f"{tooltip_title}\n{session.host}:{session.port}"
        else:
            matrix_title = f"{session.host}:{session.port}"
        session.matrix_title_label.setText(matrix_title)
        session.matrix_tile.setToolTip(f"{tooltip_title}\n{session.host}:{session.port}")

    def _show_empty_state(self):
        self._set_workspace_surface()
        self.details_widget.set_session(None)
        self.live_video_widget.set_session(None)
        self.video_streaming_widget.clear_state("")
        self.profiles_widget.set_session(None)
        self.imaging_settings_widget.clear_state("")
        self.network_settings_widget.clear_state("No active camera")
        self.maintenance_widget.clear_state("No active camera")
        self.user_management_widget.clear_state("No active camera")
        self._set_ptz_tab_visible(False)
        self.ptz_widget.setEnabled(False)
        self.presets_widget.setEnabled(False)
        self.presets_widget.update_presets([])
        self._set_rtsp_status(None)
        self._update_header_state(None)
        self._refresh_saved_cameras_widget()

    def _refresh_active_camera_ui(self, session: CameraSession):
        info = session.client.camera_info
        camera_name = " ".join(
            part for part in [info.manufacturer, info.model] if part
        ).strip() or session.host
        ptz_available = session.client.has_ptz

        self._set_workspace_surface()
        self.details_widget.set_session(session)
        self.live_video_widget.set_session(session)
        self.profiles_widget.set_session(session)
        self._set_ptz_tab_visible(ptz_available)
        self.ptz_widget.setEnabled(ptz_available)
        self.presets_widget.setEnabled(ptz_available)
        self._set_rtsp_status(session.current_stream_uri)
        self._update_header_state(session)
        self._update_camera_tab_caption(session)
        self._refresh_matrix_selection()
        self._refresh_saved_cameras_widget()

        self.live_video_widget.set_stream_uri(session.current_stream_uri)
        self.maintenance_widget.set_firmware_version(info.firmware)
        self._on_refresh_presets()
        self._on_refresh_video_streaming()
        self._on_refresh_imaging_settings()
        self._on_refresh_network_settings()
        self._on_refresh_user_accounts()

    def _inject_rtsp_credentials(self, rtsp_url: str, username: str, password: str) -> str:
        if username and password and "rtsp://" in rtsp_url and "@" not in rtsp_url:
            return rtsp_url.replace("rtsp://", f"rtsp://{username}:{password}@", 1)
        return rtsp_url

    def _on_stream_started(self, camera_id: str):
        if self._current_camera_id == camera_id:
            session = self._camera_sessions.get(camera_id)
            self._set_rtsp_status(session.current_stream_uri if session else None)

    def _on_stream_stopped(self, camera_id: str):
        if self._current_camera_id == camera_id:
            session = self._camera_sessions.get(camera_id)
            self._set_rtsp_status(session.current_stream_uri if session else None)

    def _on_stream_error(self, camera_id: str, message: str):
        if self._current_camera_id == camera_id:
            session = self._camera_sessions.get(camera_id)
            self._set_rtsp_status(session.current_stream_uri if session else None)

    def _stop_video_for_session(self, session: CameraSession, clear_widget: bool = False):
        if session.video_thread:
            if session.video_thread.is_running:
                session.video_thread.stop_stream()
            session.video_thread = None

        if clear_widget:
            session.current_stream_uri = None
            for widget in (session.video_widget, session.matrix_video_widget):
                widget.setPixmap(QPixmap())
                widget.setText("No video stream")

    def _start_video_for_session(
        self,
        session: CameraSession,
        *,
        background: bool = False,
        start_audio: Optional[bool] = None,
        force_restart: bool = True,
    ):
        if start_audio is None:
            start_audio = self._workspace_mode == WORKSPACE_MODE_SINGLE and not background

        if (
            not background
            and self._workspace_mode == WORKSPACE_MODE_SINGLE
            and self._current_camera_id != session.camera_id
        ):
            return

        if session.video_thread and session.video_thread.is_running and not force_restart:
            if not background:
                if session.current_stream_uri:
                    self._set_rtsp_status(session.current_stream_uri)
                    self.live_video_widget.set_stream_uri(session.current_stream_uri)
                if start_audio and self._audio_sink is None and session.video_thread:
                    session.video_thread.audio_chunk_ready.connect(self._on_audio_chunk_ready)
                    self._on_audio_format_ready(AUDIO_OUTPUT_SAMPLE_RATE, AUDIO_OUTPUT_CHANNELS)
            return

        self._stop_video_for_session(session, clear_widget=False)
        for widget in (session.video_widget, session.matrix_video_widget):
            widget.setPixmap(QPixmap())
            widget.setText("Connecting to video stream...")

        try:
            rtsp_url = session.loop.run_until_complete(
                session.client.get_stream_uri(session.active_stream_token)
            )
        except Exception as e:
            logger.error(f"Failed to get stream URI for {session.host}: {e}")
            rtsp_url = None

        if not rtsp_url:
            session.current_stream_uri = None
            for widget in (session.video_widget, session.matrix_video_widget):
                widget.setText("No video stream")
            if not background:
                self._set_rtsp_status(None)
                self.live_video_widget.set_stream_uri(None)
            return

        session.current_stream_uri = rtsp_url
        if not background:
            self._set_rtsp_status(rtsp_url)
            self.live_video_widget.set_stream_uri(rtsp_url)
        auth_url = self._inject_rtsp_credentials(rtsp_url, session.username, session.password)

        session.video_thread = VideoStreamThread()
        session.video_thread.set_url(auth_url)
        session.video_thread.frame_ready.connect(session.video_widget.update_frame)
        session.video_thread.frame_ready.connect(session.matrix_video_widget.update_frame)
        session.video_thread.stream_started.connect(
            partial(self._on_stream_started, session.camera_id)
        )
        session.video_thread.stream_stopped.connect(
            partial(self._on_stream_stopped, session.camera_id)
        )
        session.video_thread.error_occurred.connect(
            partial(self._on_stream_error, session.camera_id)
        )
        if not background and start_audio:
            session.video_thread.audio_format_ready.connect(self._on_audio_format_ready)
            session.video_thread.audio_chunk_ready.connect(self._on_audio_chunk_ready)
        session.video_thread.start()

    def _on_refresh_stream_uri(self):
        session = self._active_session()
        if session is None:
            self.live_video_widget.set_session(None)
            return

        try:
            rtsp_url = session.loop.run_until_complete(
                session.client.get_stream_uri(session.active_stream_token)
            )
        except Exception as e:
            logger.error(f"Refresh stream URI error: {e}")
            rtsp_url = None

        session.current_stream_uri = rtsp_url
        self.live_video_widget.set_stream_uri(rtsp_url)
        self._set_rtsp_status(rtsp_url)

    def _refresh_session_profiles(self, session: CameraSession):
        try:
            session.stream_profiles = session.loop.run_until_complete(
                session.client.refresh_media_profiles()
            )
        except Exception as e:
            logger.error(f"Refresh profiles error: {e}")

        session.active_stream_token = session.client.profile_token

    def _on_refresh_video_streaming(self):
        session = self._active_session()
        if session is None:
            self.video_streaming_widget.clear_state("No active camera")
            return

        try:
            encoder_settings = session.loop.run_until_complete(
                session.client.get_video_encoder_settings()
            )
            self.video_streaming_widget.set_encoder_settings(encoder_settings)
        except Exception as e:
            logger.error(f"Refresh video streaming settings error: {e}")
            self.video_streaming_widget.clear_state(
                "Failed to load video streaming settings",
                allow_refresh=True,
            )

    def _on_apply_video_streaming(self, values: dict[str, Any]):
        session = self._active_session()
        if session is None or not values:
            return

        try:
            success = session.loop.run_until_complete(
                session.client.set_video_encoder_settings(values)
            )
        except Exception as e:
            logger.error(f"Apply video streaming settings error: {e}")
            success = False

        if not success:
            QMessageBox.warning(
                self,
                "Video Streaming",
                "Could not apply the video encoder settings to the active profile.",
            )
            return

        self._refresh_session_profiles(session)
        self._remember_camera(self._camera_config_from_session(session))
        self.status_bar.showMessage("Video streaming settings applied", 3000)
        self._refresh_active_camera_ui(session)
        self._start_video_for_session(session)

    def _on_refresh_imaging_settings(self):
        session = self._active_session()
        if session is None:
            self.imaging_settings_widget.clear_state("No active camera")
            return

        try:
            payload = session.loop.run_until_complete(session.client.get_imaging_settings())
            self.imaging_settings_widget.set_configuration(payload)
        except Exception as e:
            logger.error(f"Refresh imaging settings error: {e}")
            self.imaging_settings_widget.clear_state(
                "Failed to load imaging settings",
                allow_refresh=True,
            )

    def _on_apply_imaging_settings(self, values: dict[str, Any]):
        session = self._active_session()
        if session is None or not values:
            return

        try:
            success = session.loop.run_until_complete(
                session.client.set_imaging_settings(values)
            )
        except Exception as e:
            logger.error(f"Apply imaging settings error: {e}")
            success = False

        if not success:
            self.status_bar.showMessage("Imaging settings could not be applied", 3000)
            return

        self.status_bar.showMessage("Imaging settings applied", 3000)
        self._on_refresh_imaging_settings()

    def _on_refresh_network_settings(self):
        session = self._active_session()
        if session is None:
            self.network_settings_widget.clear_state("No active camera")
            return

        try:
            payload = session.loop.run_until_complete(
                session.client.get_network_settings()
            )
            self.network_settings_widget.set_payload(payload)
        except Exception as e:
            logger.error(f"Refresh network settings error: {e}")
            self.network_settings_widget.clear_state(
                "Failed to load network settings",
                allow_refresh=True,
            )

    def _on_apply_network_settings(self, values: dict[str, Any]):
        session = self._active_session()
        if session is None:
            return

        try:
            success, messages = session.loop.run_until_complete(
                session.client.set_network_settings(values)
            )
        except Exception as e:
            logger.error(f"Apply network settings error: {e}")
            success = False
            messages = [str(e)]

        if not success:
            QMessageBox.warning(
                self,
                "Network Settings",
                "\n".join(messages) if messages else "Could not apply network settings.",
            )
            return

        if messages:
            QMessageBox.information(self, "Network Settings", "\n".join(messages))
        self.status_bar.showMessage("Network settings applied", 3000)
        self._on_refresh_network_settings()

    def _confirm_device_action(self, title: str, text: str) -> bool:
        return (
            QMessageBox.question(
                self,
                title,
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _on_soft_factory_reset(self):
        session = self._active_session()
        if session is None:
            return
        if not self._confirm_device_action(
            "Maintenance",
            "Run a soft factory reset on the active camera?",
        ):
            return

        try:
            success, message = session.loop.run_until_complete(
                session.client.system_factory_reset("Soft")
            )
        except Exception as e:
            logger.error(f"Soft factory reset error: {e}")
            success, message = False, str(e)

        if success:
            self.status_bar.showMessage(message or "Soft reset requested", 5000)
        else:
            QMessageBox.warning(self, "Maintenance", message or "Soft reset failed.")

    def _on_hard_factory_reset(self):
        session = self._active_session()
        if session is None:
            return
        if not self._confirm_device_action(
            "Maintenance",
            "Run a hard factory reset on the active camera?",
        ):
            return

        try:
            success, message = session.loop.run_until_complete(
                session.client.system_factory_reset("Hard")
            )
        except Exception as e:
            logger.error(f"Hard factory reset error: {e}")
            success, message = False, str(e)

        if success:
            self.status_bar.showMessage(message or "Hard reset requested", 5000)
        else:
            QMessageBox.warning(self, "Maintenance", message or "Hard reset failed.")

    def _on_reboot_device(self):
        session = self._active_session()
        if session is None:
            return
        if not self._confirm_device_action(
            "Maintenance",
            "Reboot the active camera now?",
        ):
            return

        try:
            success, message = session.loop.run_until_complete(
                session.client.system_reboot()
            )
        except Exception as e:
            logger.error(f"Device reboot error: {e}")
            success, message = False, str(e)

        if success:
            self.status_bar.showMessage(message or "Reboot requested", 5000)
        else:
            QMessageBox.warning(self, "Maintenance", message or "Reboot failed.")

    def _on_upgrade_firmware(self):
        session = self._active_session()
        if session is None:
            return

        firmware_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Firmware File",
            "",
            "All files (*.*)",
        )
        if not firmware_path:
            return

        if not self._confirm_device_action(
            "Maintenance",
            f"Send firmware upgrade request using\n{firmware_path}\n?",
        ):
            return

        try:
            success, message = session.loop.run_until_complete(
                session.client.upgrade_firmware(firmware_path)
            )
        except Exception as e:
            logger.error(f"Firmware upgrade error: {e}")
            success, message = False, str(e)

        if success:
            QMessageBox.information(self, "Maintenance", message or "Firmware upgrade request sent.")
            self.status_bar.showMessage("Firmware upgrade request sent", 5000)
        else:
            QMessageBox.warning(self, "Maintenance", message or "Firmware upgrade failed.")

    def _on_refresh_user_accounts(self):
        session = self._active_session()
        if session is None:
            self.user_management_widget.clear_state("No active camera")
            return

        try:
            users = session.loop.run_until_complete(session.client.get_user_accounts())
            self.user_management_widget.set_users(users)
        except Exception as e:
            logger.error(f"Refresh user accounts error: {e}")
            self.user_management_widget.clear_state(
                "Failed to load user accounts",
                allow_refresh=True,
            )

    def _on_create_user_account(self):
        session = self._active_session()
        if session is None:
            return

        dialog = UserAccountDialog(
            self,
            dialog_title="Create User",
            confirm_text="Create",
            role="User",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        username, password, role = dialog.values()
        if password is None:
            QMessageBox.warning(self, "User Management", "Password is required.")
            return

        try:
            success = session.loop.run_until_complete(
                session.client.create_user_account(username, password, role)
            )
        except Exception as e:
            logger.error(f"Create user account error: {e}")
            success = False

        if not success:
            QMessageBox.warning(self, "User Management", "Could not create the user account.")
            return

        self.status_bar.showMessage("User account created", 3000)
        self._on_refresh_user_accounts()

    def _on_edit_user_account(self, user: UserAccountInfo):
        session = self._active_session()
        if session is None:
            return

        dialog = UserAccountDialog(
            self,
            dialog_title="Edit User",
            confirm_text="Save",
            username=user.username,
            role=user.role,
            username_editable=False,
            allow_empty_password=True,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        username, password, role = dialog.values()
        try:
            success = session.loop.run_until_complete(
                session.client.update_user_account(username, password, role)
            )
        except Exception as e:
            logger.error(f"Update user account error: {e}")
            success = False

        if not success:
            QMessageBox.warning(self, "User Management", "Could not update the user account.")
            return

        self.status_bar.showMessage("User account updated", 3000)
        self._on_refresh_user_accounts()

    def _on_delete_user_account(self, user: UserAccountInfo):
        session = self._active_session()
        if session is None:
            return

        if not self._confirm_device_action(
            "User Management",
            f"Delete user '{user.username}'?",
        ):
            return

        try:
            success = session.loop.run_until_complete(
                session.client.delete_user_account(user.username)
            )
        except Exception as e:
            logger.error(f"Delete user account error: {e}")
            success = False

        if not success:
            QMessageBox.warning(self, "User Management", "Could not delete the user account.")
            return

        self.status_bar.showMessage("User account deleted", 3000)
        self._on_refresh_user_accounts()

    def _on_refresh_profiles(self):
        session = self._active_session()
        if session is None:
            self.profiles_widget.set_session(None)
            return

        self._refresh_session_profiles(session)
        self._remember_camera(self._camera_config_from_session(session))
        self._refresh_active_camera_ui(session)

    def _on_create_profile(self):
        session = self._active_session()
        if session is None:
            return

        name, ok = QInputDialog.getText(self, "Create Profile", "Profile name:")
        if not ok or not name.strip():
            return

        try:
            created_profile = session.loop.run_until_complete(
                session.client.create_profile(
                    name=name.strip(),
                    copy_from_token=session.active_stream_token,
                )
            )
        except Exception as e:
            logger.error(f"Create profile error: {e}")
            created_profile = None

        if created_profile is None:
            QMessageBox.warning(self, "Profiles", "Could not create a new media profile.")
            return

        session.client.set_active_profile(created_profile.token)
        self._refresh_session_profiles(session)
        self._remember_camera(self._camera_config_from_session(session))
        self.status_bar.showMessage("Profile created", 3000)
        self._refresh_active_camera_ui(session)
        self._start_video_for_session(session)

    def _on_edit_profile(self, profile_token: str):
        session = self._active_session()
        if session is None:
            return

        current_profile = next(
            (profile for profile in session.stream_profiles if profile.token == profile_token),
            None,
        )
        default_name = current_profile.name if current_profile is not None else profile_token
        new_name, ok = QInputDialog.getText(
            self,
            "Edit Profile",
            "New profile name:",
            text=default_name,
        )
        if not ok or not new_name.strip():
            return

        was_active = session.active_stream_token == profile_token
        try:
            replacement = session.loop.run_until_complete(
                session.client.edit_profile(profile_token, new_name.strip())
            )
        except Exception as e:
            logger.error(f"Edit profile error: {e}")
            replacement = None

        if replacement is None:
            QMessageBox.warning(self, "Profiles", "Could not edit the selected media profile.")
            return

        self._refresh_session_profiles(session)
        self._remember_camera(self._camera_config_from_session(session))
        self.status_bar.showMessage("Profile updated", 3000)
        self._refresh_active_camera_ui(session)
        if was_active and session.active_stream_token:
            self._start_video_for_session(session)

    def _on_delete_profile(self, profile_token: str):
        session = self._active_session()
        if session is None:
            return

        current_profile = next(
            (profile for profile in session.stream_profiles if profile.token == profile_token),
            None,
        )
        profile_name = current_profile.name if current_profile is not None else profile_token
        reply = QMessageBox.question(
            self,
            "Delete Profile",
            f"Delete media profile '{profile_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        was_active = session.active_stream_token == profile_token
        try:
            success = session.loop.run_until_complete(
                session.client.delete_profile(profile_token)
            )
        except Exception as e:
            logger.error(f"Delete profile error: {e}")
            success = False

        if not success:
            QMessageBox.warning(self, "Profiles", "Could not delete the selected media profile.")
            return

        self._refresh_session_profiles(session)
        self._remember_camera(self._camera_config_from_session(session))
        self.status_bar.showMessage("Profile deleted", 3000)
        self._refresh_active_camera_ui(session)
        if was_active and session.active_stream_token:
            self._start_video_for_session(session)

    def _remove_camera_session(self, camera_id: str, forget_saved: bool = False):
        session = self._camera_sessions.get(camera_id)
        if session is None:
            return

        was_active = camera_id == self._current_camera_id
        index = self.camera_tabs.indexOf(session.page)
        fallback_page = None

        if was_active and index >= 0 and self.camera_tabs.count() > 1:
            fallback_index = index - 1 if index > 0 else 1
            fallback_page = self.camera_tabs.widget(fallback_index)

        if was_active:
            self._stop_audio()
            self._current_camera_id = None

        self._stop_video_for_session(session, clear_widget=True)

        try:
            session.loop.run_until_complete(session.client.disconnect())
        except Exception:
            pass

        try:
            session.loop.close()
        except Exception:
            pass

        if index >= 0:
            self.camera_tabs.blockSignals(True)
            self.camera_tabs.removeTab(index)
            self.camera_tabs.blockSignals(False)

        session.page.deleteLater()
        session.matrix_tile.deleteLater()
        del self._camera_sessions[camera_id]
        self._matrix_camera_order = [item for item in self._matrix_camera_order if item != camera_id]
        self._remove_matrix_tab_if_empty()

        if forget_saved:
            self._forget_saved_camera(camera_id)
        else:
            self._refresh_saved_cameras_widget()

        if not self._camera_sessions:
            self._status_timer.stop()
            self._show_empty_state()
            return

        if fallback_page is not None:
            new_index = self.camera_tabs.indexOf(fallback_page)
            if new_index >= 0:
                self.camera_tabs.setCurrentIndex(new_index)
                return

        active_session = self._active_session()
        if active_session is not None:
            self._refresh_active_camera_ui(active_session)
            self._apply_workspace_mode()
        else:
            self._update_header_state(None)
            self._on_camera_tab_changed(self.camera_tabs.currentIndex())

    def _on_camera_tab_changed(self, index: int):
        if index < 0:
            if not self._camera_sessions:
                self._show_empty_state()
            return

        page = self.camera_tabs.widget(index)
        if self._is_matrix_tab(page):
            if not self._matrix_camera_order:
                self._remove_matrix_tab_if_empty()
                return
            self._workspace_mode = WORKSPACE_MODE_MATRIX
            self.workspace_hint_label.setText(
                "Matrix tab keeps all connected cameras visible. Click to select, drag to reorder, double-click to open a single tab."
            )
            if self._active_session() is None and self._matrix_camera_order:
                self._current_camera_id = self._matrix_camera_order[0]
            elif self._active_session() is None and self._camera_sessions:
                self._current_camera_id = next(iter(self._camera_sessions))

            session = self._active_session()
            if session is not None:
                self._refresh_active_camera_ui(session)
                self._sync_video_streams_for_mode()
            return

        camera_id = page.property("camera_id") if page else None
        if not camera_id:
            return

        self._workspace_mode = WORKSPACE_MODE_SINGLE
        self.workspace_hint_label.setText(
            "Single cameras and Matrix live in the same browser-style tab bar."
        )

        if self._current_camera_id == camera_id:
            session = self._active_session()
            if session is not None:
                self._refresh_active_camera_ui(session)
                self._sync_video_streams_for_mode()
            return

        previous_session = self._active_session()
        if previous_session is not None:
            self._stop_audio()
            if self._workspace_mode == WORKSPACE_MODE_SINGLE:
                self._stop_video_for_session(previous_session, clear_widget=False)

        self._current_camera_id = str(camera_id)
        session = self._active_session()
        if session is None:
            self._show_empty_state()
            return

        self._refresh_active_camera_ui(session)
        self._update_camera_tab_caption(session)
        self._on_refresh_presets()
        self._poll_status()
        self._sync_video_streams_for_mode()

    def _on_camera_tab_close_requested(self, index: int):
        page = self.camera_tabs.widget(index)
        if self._is_matrix_tab(page):
            return
        camera_id = page.property("camera_id") if page else None
        if camera_id:
            self._remove_camera_session(str(camera_id), forget_saved=False)

    def _on_stream_changed(self, stream_token: str):
        session = self._active_session()
        if session is None or session.active_stream_token == stream_token:
            return

        if not session.client.set_active_profile(stream_token):
            QMessageBox.warning(self, "Stream Error", "Could not switch to the selected stream.")
            self.details_widget.set_session(session)
            return

        session.active_stream_token = stream_token
        self._remember_camera(self._camera_config_from_session(session))
        self._refresh_active_camera_ui(session)
        self._start_video_for_session(session)
        self._on_refresh_presets()
        self._poll_status()

    def _camera_storage_path(self) -> Path:
        config_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppConfigLocation
        )
        if not config_dir:
            config_dir = str(Path.cwd() / ".onvif-ptz-controller")

        path = Path(config_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path / "saved_cameras.json"

    def _load_saved_camera_configs(self) -> dict[str, SavedCameraConfig]:
        path = self._camera_storage_path()
        if not path.exists():
            return {}

        try:
            raw_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not read saved camera list: {e}")
            return {}

        if not isinstance(raw_data, list):
            return {}

        saved_configs: dict[str, SavedCameraConfig] = {}
        for item in raw_data:
            if not isinstance(item, dict):
                continue

            host = str(item.get("host", "")).strip()
            if not host:
                continue

            try:
                port = int(item.get("port", 80))
            except (TypeError, ValueError):
                port = 80

            config = SavedCameraConfig(
                host=host,
                port=port,
                username=str(item.get("username", "")).strip(),
                password=str(item.get("password", "")),
                active_stream_token=(
                    str(item["active_stream_token"])
                    if item.get("active_stream_token")
                    else None
                ),
            )
            saved_configs[config.camera_id] = config

        return saved_configs

    def _write_saved_camera_configs(self):
        path = self._camera_storage_path()
        payload = [
            {
                "host": config.host,
                "port": config.port,
                "username": config.username,
                "password": config.password,
                "active_stream_token": config.active_stream_token,
            }
            for config in sorted(
                self._saved_cameras.values(),
                key=lambda item: (item.host, item.port, item.username),
            )
        ]

        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Could not save camera list: {e}")

    def _remember_camera(self, config: SavedCameraConfig):
        self._saved_cameras[config.camera_id] = config
        self._write_saved_camera_configs()
        self._refresh_saved_cameras_widget()

    def _forget_saved_camera(self, camera_id: str):
        if camera_id in self._saved_cameras:
            del self._saved_cameras[camera_id]
            self._write_saved_camera_configs()
            self._refresh_saved_cameras_widget()

    def _camera_config_from_session(self, session: CameraSession) -> SavedCameraConfig:
        return SavedCameraConfig(
            host=session.host,
            port=session.port,
            username=session.username,
            password=session.password,
            active_stream_token=session.active_stream_token,
        )

    def _show_add_camera_dialog(self):
        dialog = AddCameraDialog(self._add_camera_defaults, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        configs = dialog.camera_configs()
        if not configs:
            return

        self._add_camera_defaults = configs[0]
        for config in configs:
            self._remember_camera(config)

        if len(configs) == 1:
            self.status_bar.showMessage(
                "Camera saved. Click it in Saved Cameras to open a tab.",
                4000,
            )
        else:
            self.status_bar.showMessage(
                f"{len(configs)} cameras saved. Click any of them in Saved Cameras to open a tab.",
                5000,
            )

    def _edit_saved_camera_entry(self, camera_id: str):
        config = self._saved_cameras.get(camera_id)
        if config is None:
            return

        dialog = AddCameraDialog(
            config,
            self,
            dialog_title="Edit Camera",
            confirm_text="Save",
            allow_local_search=False,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        new_config = dialog.camera_config()
        new_camera_id = new_config.camera_id
        if new_camera_id != camera_id and new_camera_id in self._saved_cameras:
            QMessageBox.warning(
                self,
                "Edit Camera",
                "A saved camera with the same host, port, and user already exists.",
            )
            return

        if camera_id in self._saved_cameras:
            del self._saved_cameras[camera_id]

        if new_camera_id == camera_id:
            new_config = SavedCameraConfig(
                host=new_config.host,
                port=new_config.port,
                username=new_config.username,
                password=new_config.password,
                active_stream_token=config.active_stream_token,
            )

        self._saved_cameras[new_config.camera_id] = new_config
        self._add_camera_defaults = new_config
        self._write_saved_camera_configs()
        self._refresh_saved_cameras_widget()
        self.status_bar.showMessage("Camera updated.", 3000)

    def _refresh_saved_cameras_widget(self):
        if not hasattr(self, "saved_cameras_widget"):
            return

        self.saved_cameras_widget.set_cameras(
            self._saved_cameras,
            set(self._camera_sessions),
            self._current_camera_id,
        )

    def _open_saved_camera(self, camera_id: str):
        config = self._saved_cameras.get(camera_id)
        if config is None:
            return
        self._connect_camera_config(
            config,
            persist=True,
            activate_tab=not self._is_matrix_tab_active(),
            open_in_matrix_only=self._is_matrix_tab_active(),
        )

    def _remove_saved_camera_entry(self, camera_id: str):
        config = self._saved_cameras.get(camera_id)
        if config is None:
            return

        reply = QMessageBox.question(
            self,
            "Delete Camera",
            f"Delete saved camera {config.host}:{config.port} from the library?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if camera_id in self._camera_sessions:
            self._remove_camera_session(camera_id, forget_saved=False)
        self._forget_saved_camera(camera_id)

    def _connect_camera_config(
        self,
        config: SavedCameraConfig,
        *,
        persist: bool = True,
        activate_tab: bool = True,
        open_in_matrix_only: bool = False,
        show_error_dialogs: bool = True,
    ) -> Optional[CameraSession]:
        host = config.host.strip()
        user = config.username.strip()
        normalized_config = SavedCameraConfig(
            host=host,
            port=config.port,
            username=user,
            password=config.password,
            active_stream_token=config.active_stream_token,
        )
        camera_id = normalized_config.camera_id

        if not host:
            if show_error_dialogs:
                QMessageBox.warning(self, "Connection Error", "Host is required.")
            return None

        existing_session = self._camera_sessions.get(camera_id)
        if existing_session is not None:
            if persist:
                self._remember_camera(self._camera_config_from_session(existing_session))
            if open_in_matrix_only:
                self._move_session_to_matrix(
                    existing_session.camera_id,
                    activate_matrix=True,
                    detach_tab=False,
                )
            else:
                self._ensure_session_tab(existing_session, activate=activate_tab)
            if self._is_matrix_tab_active():
                self._on_matrix_tile_clicked(existing_session.camera_id)
            self._update_header_state(self._active_session())
            return existing_session

        self.status_bar.showMessage(f"Connecting to {host}:{config.port}...", 2000)
        QApplication.processEvents()

        client = ONVIFPTZClient(host, config.port, user, config.password)
        loop = asyncio.new_event_loop()

        try:
            success = loop.run_until_complete(client.connect())
        except Exception as e:
            logger.error(f"Connection error: {e}")
            if show_error_dialogs:
                QMessageBox.critical(self, "Connection Error", str(e))
            loop.close()
            active_session = self._active_session()
            if active_session is not None:
                self._refresh_active_camera_ui(active_session)
            else:
                self._show_empty_state()
            return None

        if not success:
            loop.close()
            if show_error_dialogs:
                QMessageBox.warning(
                    self,
                    "Connection Failed",
                    "Could not connect to camera. Check settings and try again.",
                )
            active_session = self._active_session()
            if active_session is not None:
                self._refresh_active_camera_ui(active_session)
            else:
                self._show_empty_state()
            return None

        try:
            stream_profiles = loop.run_until_complete(client.get_media_profiles())
        except Exception as e:
            logger.error(f"Failed to load media profiles: {e}")
            stream_profiles = []

        session = self._build_camera_session(
            camera_id=camera_id,
            client=client,
            loop=loop,
            host=host,
            port=config.port,
            username=user,
            password=config.password,
            stream_profiles=stream_profiles,
            preferred_stream_token=normalized_config.active_stream_token,
        )
        self._camera_sessions[camera_id] = session

        self._set_workspace_surface()
        if open_in_matrix_only:
            self._move_session_to_matrix(camera_id, activate_matrix=True, detach_tab=False)
            tab_index = -1
        else:
            tab_index = self._ensure_session_tab(session, activate=False)
        self._status_timer.start()

        if persist:
            self._remember_camera(self._camera_config_from_session(session))

        visible_session_tabs = sum(
            1 for item in self._camera_sessions.values() if self._is_session_tab_visible(item)
        )
        if not open_in_matrix_only and (activate_tab or visible_session_tabs == 1):
            self.camera_tabs.setCurrentIndex(tab_index)
        else:
            if self._current_camera_id is None or open_in_matrix_only:
                self._current_camera_id = camera_id
            self._update_header_state(self._active_session())
            self._refresh_saved_cameras_widget()

        self._apply_workspace_mode()

        return session

    # ---- Keyboard PTZ ----

    def _resolve_ptz_action(self, event) -> Optional[str]:
        key = event.key()
        text = event.text().lower()

        text_actions = {
            "w": "up",
            "ц": "up",
            "s": "down",
            "ы": "down",
            "a": "left",
            "ф": "left",
            "d": "right",
            "в": "right",
            "q": "up_left",
            "й": "up_left",
            "e": "up_right",
            "у": "up_right",
            "z": "down_left",
            "я": "down_left",
            "c": "down_right",
            "с": "down_right",
        }
        if text in text_actions:
            return text_actions[text]

        key_actions = {
            Qt.Key.Key_Up: "up",
            Qt.Key.Key_Down: "down",
            Qt.Key.Key_Left: "left",
            Qt.Key.Key_Right: "right",
            Qt.Key.Key_Plus: "zoom_in",
            Qt.Key.Key_Equal: "zoom_in",
            Qt.Key.Key_Minus: "zoom_out",
            Qt.Key.Key_Space: "stop",
        }
        return key_actions.get(key)

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return

        action = self._resolve_ptz_action(event)
        if action is None:
            super().keyPressEvent(event)
            return

        mode = self.ptz_widget.current_mode
        speed = self.ptz_widget._speed
        step = self.ptz_widget.rel_step.value() if mode == PTZControlWidget.MODE_RELATIVE else speed

        if action == "up":
            self._on_ptz_move(mode, 0, step, 0)
        elif action == "down":
            self._on_ptz_move(mode, 0, -step, 0)
        elif action == "left":
            self._on_ptz_move(mode, -step, 0, 0)
        elif action == "right":
            self._on_ptz_move(mode, step, 0, 0)
        elif action == "up_left":
            self._on_ptz_move(mode, -step, step, 0)
        elif action == "up_right":
            self._on_ptz_move(mode, step, step, 0)
        elif action == "down_left":
            self._on_ptz_move(mode, -step, -step, 0)
        elif action == "down_right":
            self._on_ptz_move(mode, step, -step, 0)
        elif action == "zoom_in":
            self._on_ptz_move(mode, 0, 0, step)
        elif action == "zoom_out":
            self._on_ptz_move(mode, 0, 0, -step)
        elif action == "stop":
            self._on_ptz_stop()

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return

        action = self._resolve_ptz_action(event)
        if action in {
            "up",
            "down",
            "left",
            "right",
            "up_left",
            "up_right",
            "down_left",
            "down_right",
            "zoom_in",
            "zoom_out",
        }:
            # Only stop for continuous mode
            if self.ptz_widget.current_mode == PTZControlWidget.MODE_CONTINUOUS:
                self._on_ptz_stop()
        else:
            super().keyReleaseEvent(event)

    # ---- Connection ----

    def _on_connect(self, host: str, port: int, user: str, password: str):
        self._connect_camera_config(
            SavedCameraConfig(
                host=host,
                port=port,
                username=user,
                password=password,
            )
        )

    def _on_disconnect(self):
        """Remove the currently selected camera tab."""
        active_session = self._active_session()
        if active_session is not None:
            self._remove_camera_session(active_session.camera_id, forget_saved=False)

    def _on_audio_format_ready(self, sample_rate: int, channels: int):
        """Create QAudioSink once VideoStreamThread reports the audio format."""
        self._stop_audio()
        try:
            fmt = QAudioFormat()
            fmt.setSampleRate(sample_rate)
            fmt.setChannelCount(channels)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            self._audio_sink = QAudioSink(fmt)
            # ~500 ms hardware buffer gives plenty of headroom against RTSP jitter.
            # The timer does NOT start here — playback begins only after the
            # pre-buffer fills (_on_audio_chunk_ready starts the timer).
            self._audio_sink.setBufferSize(96000)
            session = self._active_session()
            initial_volume = session.audio_volume if session else 0.0
            self._audio_sink.setVolume(initial_volume)
            self._audio_io = self._audio_sink.start()
            self._audio_buf = bytearray()
            logger.info("Audio sink created, pre-buffering…")
        except Exception as e:
            logger.error(f"Audio playback error: {e}")

    # ~170 ms at 48 kHz stereo s16 — wait for this much data before starting
    _AUDIO_PREBUF = 32768
    # Trim to this many bytes (~250 ms) when the in-memory buffer exceeds 1 s
    _AUDIO_BUF_TRIM = 48000
    _AUDIO_BUF_MAX = 192000  # ~1 s max before trimming
    # Silence written per tick when _audio_buf is empty (~10 ms) to keep the
    # sink in ActiveState and avoid the click that occurs on IdleState resume
    _AUDIO_SILENCE = 1920

    def _on_audio_chunk_ready(self, pcm_bytes: bytes):
        """Append incoming PCM to the continuous byte buffer."""
        self._audio_buf.extend(pcm_bytes)
        if len(self._audio_buf) > self._AUDIO_BUF_MAX:
            # Align discard position to an 8-byte stereo float32 frame boundary
            trim = len(self._audio_buf) - self._AUDIO_BUF_TRIM
            trim -= trim % 4  # align to stereo int16 frame boundary (4 bytes)
            del self._audio_buf[:trim]
        # Start the flush timer only after the pre-buffer has filled,
        # so the sink's hardware buffer begins nearly full and never starves.
        if not self._audio_timer.isActive() and self._audio_sink is not None:
            if len(self._audio_buf) >= self._AUDIO_PREBUF:
                self._audio_timer.start()
                logger.info("Audio pre-buffer ready, playback started")

    def _flush_audio(self):
        """QTimer slot: write buffered PCM into the sink; pad with silence on underrun."""
        if self._audio_io is None or self._audio_sink is None:
            return
        if self._audio_sink.state() == QAudio.State.StoppedState:
            return
        free = self._audio_sink.bytesFree()
        if free <= 0:
            return
        if self._audio_buf:
            n = min(free, len(self._audio_buf))
            written = self._audio_io.write(bytes(self._audio_buf[:n]))
            if written > 0:
                del self._audio_buf[:written]
        else:
            # Keep sink in ActiveState with silence so resuming audio has no click
            self._audio_io.write(bytes(min(free, self._AUDIO_SILENCE)))

    def _on_camera_volume_changed(self, camera_id: str, volume: float):
        """Update per-camera volume and apply to live sink if camera is active."""
        session = self._camera_sessions.get(camera_id)
        if session is None:
            return
        session.audio_volume = volume
        # Keep both buttons in sync
        session.video_widget.volume_btn.set_volume(volume)
        session.matrix_tile.volume_btn.set_volume(volume)
        if self._current_camera_id == camera_id and self._audio_sink is not None:
            self._audio_sink.setVolume(volume)

    def _stop_audio(self):
        """Stop audio playback."""
        self._audio_timer.stop()
        self._audio_buf = bytearray()
        if self._audio_sink is not None:
            self._audio_sink.stop()
            self._audio_sink = None
        self._audio_io = None

    # ---- PTZ Control ----

    def _on_ptz_move(self, mode: str, pan: float, tilt: float, zoom: float):
        session = self._active_session()
        if session is None:
            return

        try:
            if mode == PTZControlWidget.MODE_CONTINUOUS:
                session.loop.run_until_complete(
                    session.client.continuous_move(pan, tilt, zoom)
                )
            elif mode == PTZControlWidget.MODE_RELATIVE:
                session.loop.run_until_complete(
                    session.client.relative_move(
                        pan, tilt, zoom, speed=self.ptz_widget._speed
                    )
                )
            elif mode == PTZControlWidget.MODE_ABSOLUTE:
                session.loop.run_until_complete(
                    session.client.absolute_move(
                        pan, tilt, zoom, speed=self.ptz_widget._speed
                    )
                )
        except Exception as e:
            logger.error(f"PTZ move error: {e}")

    def _on_ptz_stop(self):
        session = self._active_session()
        if session is None:
            return

        try:
            session.loop.run_until_complete(session.client.stop_move())
        except Exception as e:
            logger.error(f"PTZ stop error: {e}")

    # ---- Presets ----

    def _on_goto_preset(self, token: str):
        session = self._active_session()
        if session is None:
            return

        try:
            session.loop.run_until_complete(session.client.goto_preset(token))
        except Exception as e:
            logger.error(f"Goto preset error: {e}")

    def _on_refresh_presets(self):
        session = self._active_session()
        if session is None:
            self.presets_widget.update_presets([])
            return

        try:
            presets = session.loop.run_until_complete(session.client.get_presets())
            self.presets_widget.update_presets(presets)
        except Exception as e:
            logger.error(f"Refresh presets error: {e}")

    def _on_save_preset(self):
        session = self._active_session()
        if session is None:
            return

        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        if ok and name.strip():
            try:
                session.loop.run_until_complete(session.client.set_preset(name.strip()))
                self._on_refresh_presets()
            except Exception as e:
                logger.error(f"Save preset error: {e}")
                QMessageBox.warning(self, "Error", f"Failed to save preset: {e}")

    def _on_delete_preset(self, token: str):
        session = self._active_session()
        if session is None:
            return

        reply = QMessageBox.question(
            self, "Delete Preset",
            "Are you sure you want to delete this preset?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                session.loop.run_until_complete(session.client.remove_preset(token))
                self._on_refresh_presets()
            except Exception as e:
                logger.error(f"Delete preset error: {e}")

    # ---- Status polling ----

    def _poll_status(self):
        session = self._active_session()
        if session is None:
            return

        try:
            status = session.loop.run_until_complete(session.client.get_status())
            session.last_status = status
        except Exception:
            pass

    # ---- Cleanup ----

    def closeEvent(self, event):
        for camera_id in list(self._camera_sessions):
            self._remove_camera_session(camera_id, forget_saved=False)
        super().closeEvent(event)
