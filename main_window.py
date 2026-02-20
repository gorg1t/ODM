import asyncio
import logging
import sys
from typing import Optional
from functools import partial

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit, QSpinBox, QGroupBox,
    QListWidget, QListWidgetItem, QStatusBar, QMessageBox,
    QSlider, QSplitter, QFrame, QInputDialog, QApplication,
    QSizePolicy, QToolBar
)
from PyQt6.QtCore import Qt, QTimer, pyqtSlot, QSize
from PyQt6.QtGui import QImage, QPixmap, QIcon, QKeySequence, QShortcut, QAction

from onvif_client import ONVIFPTZClient, Preset, PTZStatus
from video_player import VideoStreamThread

logger = logging.getLogger(__name__)

# PTZ speed
DEFAULT_PTZ_SPEED = 0.5


class VideoWidget(QLabel):
    """Widget for displaying the video stream."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: #1a1a2e; border-radius: 8px;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setText("No video stream")
        self.setStyleSheet("""
            QLabel {
                background-color: #1a1a2e;
                color: #888;
                font-size: 18px;
                border: 2px solid #333;
                border-radius: 8px;
            }
        """)

    @pyqtSlot(QImage)
    def update_frame(self, image: QImage):
        """Update displayed frame, scaling to widget size."""
        pixmap = QPixmap.fromImage(image)
        scaled = pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)


class PTZControlWidget(QGroupBox):
    """Widget with PTZ directional controls and zoom."""

    def __init__(self, parent=None):
        super().__init__("PTZ Control", parent)
        self.ptz_callback = None  # async callback for PTZ
        self.stop_callback = None  # async callback for stop

        self._speed = DEFAULT_PTZ_SPEED
        self._init_ui()
        self._setup_shortcuts()

    def _init_ui(self):
        main_layout = QVBoxLayout()

        # Direction controls
        dir_layout = QGridLayout()
        dir_layout.setSpacing(4)

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
        self.btn_stop.setStyleSheet("""
            QPushButton {
                background-color: #c0392b; color: white;
                font-size: 16px; font-weight: bold; 
                border-radius: 22px; min-width: 44px; min-height: 44px;
            }
            QPushButton:hover { background-color: #e74c3c; }
            QPushButton:pressed { background-color: #96281b; }
        """)

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
        self.btn_zoom_in = QPushButton("🔍+  Zoom In")
        self.btn_zoom_out = QPushButton("🔍−  Zoom Out")
        for btn in (self.btn_zoom_in, self.btn_zoom_out):
            btn.setMinimumHeight(36)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #2c3e50; color: white;
                    font-size: 13px; border-radius: 6px; padding: 6px 12px;
                }
                QPushButton:hover { background-color: #34495e; }
                QPushButton:pressed { background-color: #1a252f; }
            """)
        zoom_layout.addWidget(self.btn_zoom_out)
        zoom_layout.addWidget(self.btn_zoom_in)
        main_layout.addLayout(zoom_layout)

        # Speed slider
        speed_layout = QHBoxLayout()
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

        self.setLayout(main_layout)

        # Connect button signals (press = start, release = stop)
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

        # Release = stop for all direction/zoom buttons
        for btn in (self.btn_up, self.btn_down, self.btn_left, self.btn_right,
                    self.btn_up_left, self.btn_up_right, self.btn_down_left,
                    self.btn_down_right, self.btn_zoom_in, self.btn_zoom_out):
            btn.released.connect(self._stop)

    def _make_btn(self, text: str, tooltip: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.setMinimumSize(44, 44)
        btn.setStyleSheet("""
            QPushButton {
                background-color: #2c3e50; color: white;
                font-size: 18px; font-weight: bold; 
                border-radius: 22px; min-width: 44px; min-height: 44px;
            }
            QPushButton:hover { background-color: #34495e; }
            QPushButton:pressed { background-color: #1a252f; }
        """)
        return btn

    def _setup_shortcuts(self):
        """Configure keyboard shortcuts for PTZ."""
        # We'll handle keyboard in the main window via keyPressEvent/keyReleaseEvent
        pass

    def _on_speed_changed(self, value: int):
        self._speed = value / 100.0
        self.speed_label.setText(f"{self._speed:.2f}")

    def _move(self, pan_dir: int, tilt_dir: int, zoom_dir: int):
        if self.ptz_callback:
            pan = pan_dir * self._speed
            tilt = tilt_dir * self._speed
            zoom = zoom_dir * self._speed
            self.ptz_callback(pan, tilt, zoom)

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

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()

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
        self.presets_list.clear()
        for preset in presets:
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
        layout.addWidget(self.port_input, 0, 3)

        layout.addWidget(QLabel("User:"), 1, 0)
        self.user_input = QLineEdit("admin")
        layout.addWidget(self.user_input, 1, 1)

        layout.addWidget(QLabel("Password:"), 1, 2)
        self.pass_input = QLineEdit("Supervisor")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.pass_input, 1, 3)

        # RTSP URL
        layout.addWidget(QLabel("RTSP URL:"), 2, 0)
        self.rtsp_input = QLineEdit(
            "rtsp://172.18.212.18:554/Streaming/Channels/101"
            "?transportmode=unicast&profile=Profile_1"
        )
        layout.addWidget(self.rtsp_input, 2, 1, 1, 3)

        btn_layout = QHBoxLayout()
        self.btn_connect = QPushButton("🔗 Connect")
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

        self.btn_disconnect = QPushButton("✖ Disconnect")
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
        layout.addLayout(btn_layout, 3, 0, 1, 4)

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
                self.rtsp_input.text().strip(),
            )

    def _on_disconnect(self):
        if self.disconnect_callback:
            self.disconnect_callback()

    def set_connected(self, connected: bool):
        self.btn_connect.setEnabled(not connected)
        self.btn_disconnect.setEnabled(connected)
        self.host_input.setEnabled(not connected)
        self.port_input.setEnabled(not connected)
        self.user_input.setEnabled(not connected)
        self.pass_input.setEnabled(not connected)
        self.rtsp_input.setEnabled(not connected)


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ONVIF PTZ Controller — PoC")
        self.setMinimumSize(1024, 700)
        self.resize(1280, 800)

        self._onvif_client: Optional[ONVIFPTZClient] = None
        self._video_thread: Optional[VideoStreamThread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Status polling timer
        self._status_timer = QTimer()
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._poll_status)

        self._init_ui()
        self._apply_dark_theme()
        self._setup_keyboard_shortcuts()

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # Connection panel
        self.connection_widget = ConnectionWidget()
        self.connection_widget.connect_callback = self._on_connect
        self.connection_widget.disconnect_callback = self._on_disconnect
        main_layout.addWidget(self.connection_widget)

        # Splitter: video | controls
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Video display
        self.video_widget = VideoWidget()
        splitter.addWidget(self.video_widget)

        # Right panel: PTZ + Presets
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.ptz_widget = PTZControlWidget()
        self.ptz_widget.ptz_callback = self._on_ptz_move
        self.ptz_widget.stop_callback = self._on_ptz_stop

        self.presets_widget = PresetsWidget()
        self.presets_widget.goto_callback = self._on_goto_preset
        self.presets_widget.refresh_callback = self._on_refresh_presets
        self.presets_widget.save_callback = self._on_save_preset
        self.presets_widget.delete_callback = self._on_delete_preset

        right_layout.addWidget(self.ptz_widget)
        right_layout.addWidget(self.presets_widget)
        right_layout.addStretch()

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        main_layout.addWidget(splitter, 1)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.lbl_connection = QLabel("Disconnected")
        self.lbl_position = QLabel("P: 0.00 T: 0.00 Z: 0.00")
        self.lbl_stream = QLabel("Stream: Off")
        self.status_bar.addWidget(self.lbl_connection)
        self.status_bar.addWidget(self._separator())
        self.status_bar.addWidget(self.lbl_position)
        self.status_bar.addWidget(self._separator())
        self.status_bar.addWidget(self.lbl_stream)

    def _separator(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        return sep

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #0f0f23;
                color: #e0e0e0;
                font-family: 'Segoe UI', 'Ubuntu', sans-serif;
            }
            QGroupBox {
                border: 1px solid #444;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 18px;
                font-weight: bold;
                font-size: 13px;
                color: #3498db;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QLineEdit, QSpinBox {
                background-color: #1a1a2e;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 4px 8px;
                color: #eee;
                font-size: 13px;
            }
            QLineEdit:focus, QSpinBox:focus {
                border-color: #3498db;
            }
            QLabel {
                font-size: 13px;
            }
            QStatusBar {
                background-color: #16213e;
                color: #aaa;
                font-size: 12px;
            }
            QSlider::groove:horizontal {
                background: #444;
                height: 6px;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #3498db;
                width: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
            QSplitter::handle {
                background-color: #333;
                width: 3px;
            }
        """)

    def _setup_keyboard_shortcuts(self):
        """Set up keyboard shortcuts for PTZ control."""
        pass  # Handled via keyPressEvent / keyReleaseEvent

    # ---- Keyboard PTZ ----

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        key = event.key()
        speed = self.ptz_widget._speed
        moved = True
        if key == Qt.Key.Key_W:
            self._on_ptz_move(0, speed, 0)
        elif key == Qt.Key.Key_S:
            self._on_ptz_move(0, -speed, 0)
        elif key == Qt.Key.Key_A:
            self._on_ptz_move(-speed, 0, 0)
        elif key == Qt.Key.Key_D:
            self._on_ptz_move(speed, 0, 0)
        elif key == Qt.Key.Key_Q:
            self._on_ptz_move(-speed, speed, 0)
        elif key == Qt.Key.Key_E:
            self._on_ptz_move(speed, speed, 0)
        elif key == Qt.Key.Key_Z:
            self._on_ptz_move(-speed, -speed, 0)
        elif key == Qt.Key.Key_C:
            self._on_ptz_move(speed, -speed, 0)
        elif key == Qt.Key.Key_Plus or key == Qt.Key.Key_Equal:
            self._on_ptz_move(0, 0, speed)
        elif key == Qt.Key.Key_Minus:
            self._on_ptz_move(0, 0, -speed)
        elif key == Qt.Key.Key_Space:
            self._on_ptz_stop()
        else:
            moved = False
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        key = event.key()
        if key in (Qt.Key.Key_W, Qt.Key.Key_S, Qt.Key.Key_A, Qt.Key.Key_D,
                   Qt.Key.Key_Q, Qt.Key.Key_E, Qt.Key.Key_Z, Qt.Key.Key_C,
                   Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus):
            self._on_ptz_stop()
        else:
            super().keyReleaseEvent(event)

    # ---- Connection ----

    def _on_connect(self, host: str, port: int, user: str, password: str, rtsp_url: str):
        """Handle connect button click."""
        self.lbl_connection.setText("Connecting...")
        QApplication.processEvents()

        self._onvif_client = ONVIFPTZClient(host, port, user, password)

        # Run async connect in a synchronous context
        try:
            loop = asyncio.new_event_loop()
            self._loop = loop
            success = loop.run_until_complete(self._onvif_client.connect())
        except Exception as e:
            logger.error(f"Connection error: {e}")
            QMessageBox.critical(self, "Connection Error", str(e))
            self.lbl_connection.setText("Disconnected")
            return

        if success:
            info = self._onvif_client.camera_info
            self.lbl_connection.setText(
                f"Connected: {info.manufacturer} {info.model}"
            )
            self.connection_widget.set_connected(True)
            self._on_refresh_presets()
            self._status_timer.start()

            # Start video stream — inject credentials into RTSP URL
            self._start_video(rtsp_url, user, password)
        else:
            QMessageBox.warning(self, "Connection Failed",
                                "Could not connect to camera. Check settings and try again.")
            self.lbl_connection.setText("Disconnected")

    def _on_disconnect(self):
        """Handle disconnect."""
        self._status_timer.stop()

        # Stop video
        if self._video_thread and self._video_thread.is_running:
            self._video_thread.stop_stream()
            self._video_thread = None
            self.lbl_stream.setText("Stream: Off")
            self.video_widget.setText("No video stream")
            self.video_widget.setPixmap(QPixmap())

        # Disconnect ONVIF
        if self._onvif_client and self._loop:
            try:
                self._loop.run_until_complete(self._onvif_client.disconnect())
            except Exception:
                pass
            self._onvif_client = None

        if self._loop:
            self._loop.close()
            self._loop = None

        self.connection_widget.set_connected(False)
        self.lbl_connection.setText("Disconnected")
        self.lbl_position.setText("P: 0.00 T: 0.00 Z: 0.00")
        self.presets_widget.presets_list.clear()

    def _start_video(self, rtsp_url: str, username: str = "", password: str = ""):
        """Start the RTSP video stream, injecting credentials into the URL."""
        if self._video_thread and self._video_thread.is_running:
            self._video_thread.stop_stream()

        # Inject credentials into rtsp:// URL if not already present
        if username and password and "rtsp://" in rtsp_url and "@" not in rtsp_url:
            rtsp_url = rtsp_url.replace("rtsp://", f"rtsp://{username}:{password}@", 1)

        self._video_thread = VideoStreamThread()
        self._video_thread.set_url(rtsp_url)
        self._video_thread.frame_ready.connect(self.video_widget.update_frame)
        self._video_thread.stream_started.connect(
            lambda: self.lbl_stream.setText("Stream: Active ✓")
        )
        self._video_thread.stream_stopped.connect(
            lambda: self.lbl_stream.setText("Stream: Stopped")
        )
        self._video_thread.error_occurred.connect(
            lambda msg: self.lbl_stream.setText(f"Stream: Error — {msg}")
        )
        self._video_thread.start()

    # ---- PTZ Control ----

    def _on_ptz_move(self, pan: float, tilt: float, zoom: float):
        if self._onvif_client and self._loop:
            try:
                self._loop.run_until_complete(
                    self._onvif_client.continuous_move(pan, tilt, zoom)
                )
            except Exception as e:
                logger.error(f"PTZ move error: {e}")

    def _on_ptz_stop(self):
        if self._onvif_client and self._loop:
            try:
                self._loop.run_until_complete(self._onvif_client.stop_move())
            except Exception as e:
                logger.error(f"PTZ stop error: {e}")

    # ---- Presets ----

    def _on_goto_preset(self, token: str):
        if self._onvif_client and self._loop:
            try:
                self._loop.run_until_complete(
                    self._onvif_client.goto_preset(token)
                )
            except Exception as e:
                logger.error(f"Goto preset error: {e}")

    def _on_refresh_presets(self):
        if self._onvif_client and self._loop:
            try:
                presets = self._loop.run_until_complete(
                    self._onvif_client.get_presets()
                )
                self.presets_widget.update_presets(presets)
            except Exception as e:
                logger.error(f"Refresh presets error: {e}")

    def _on_save_preset(self):
        if not self._onvif_client or not self._loop:
            return
        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        if ok and name.strip():
            try:
                self._loop.run_until_complete(
                    self._onvif_client.set_preset(name.strip())
                )
                self._on_refresh_presets()
            except Exception as e:
                logger.error(f"Save preset error: {e}")
                QMessageBox.warning(self, "Error", f"Failed to save preset: {e}")

    def _on_delete_preset(self, token: str):
        if not self._onvif_client or not self._loop:
            return
        reply = QMessageBox.question(
            self, "Delete Preset",
            "Are you sure you want to delete this preset?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                self._loop.run_until_complete(
                    self._onvif_client.remove_preset(token)
                )
                self._on_refresh_presets()
            except Exception as e:
                logger.error(f"Delete preset error: {e}")

    # ---- Status polling ----

    def _poll_status(self):
        if self._onvif_client and self._loop:
            try:
                status = self._loop.run_until_complete(
                    self._onvif_client.get_status()
                )
                self.lbl_position.setText(
                    f"P: {status.pan:.2f}  T: {status.tilt:.2f}  Z: {status.zoom:.2f}"
                    + ("  [Moving]" if status.moving else "")
                )
            except Exception:
                pass

    # ---- Cleanup ----

    def closeEvent(self, event):
        self._on_disconnect()
        super().closeEvent(event)
