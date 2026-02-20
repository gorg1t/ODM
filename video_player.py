import logging
import time
from typing import Optional

import cv2
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, QMutex, Qt
from PyQt6.QtGui import QImage, QPixmap

logger = logging.getLogger(__name__)


class VideoStreamThread(QThread):
    """Thread for capturing RTSP video stream frames."""

    frame_ready = pyqtSignal(QImage)
    error_occurred = pyqtSignal(str)
    stream_started = pyqtSignal()
    stream_stopped = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rtsp_url: Optional[str] = None
        self._running = False
        self._mutex = QMutex()
        self._reconnect_delay = 3  # seconds
        self._max_reconnect_attempts = 5

    def set_url(self, url: str):
        """Set the RTSP stream URL."""
        self._rtsp_url = url

    def stop_stream(self):
        """Signal the thread to stop."""
        self._mutex.lock()
        self._running = False
        self._mutex.unlock()
        self.wait(5000)

    def run(self):
        """Main thread loop: capture and emit frames."""
        if not self._rtsp_url:
            self.error_occurred.emit("No RTSP URL configured")
            return

        self._running = True
        reconnect_attempts = 0

        while self._running and reconnect_attempts < self._max_reconnect_attempts:
            cap = None
            try:
                logger.info(f"Opening RTSP stream: {self._rtsp_url}")

                # Set OpenCV capture options for RTSP
                cap = cv2.VideoCapture(self._rtsp_url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                # Reduce latency
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000)
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000)

                if not cap.isOpened():
                    reconnect_attempts += 1
                    logger.warning(
                        f"Failed to open stream (attempt {reconnect_attempts}/"
                        f"{self._max_reconnect_attempts})"
                    )
                    self.error_occurred.emit(
                        f"Cannot open stream (attempt {reconnect_attempts})"
                    )
                    time.sleep(self._reconnect_delay)
                    continue

                reconnect_attempts = 0
                self.stream_started.emit()
                logger.info("RTSP stream opened successfully")

                consecutive_failures = 0
                while self._running:
                    ret, frame = cap.read()
                    if not ret:
                        consecutive_failures += 1
                        if consecutive_failures > 30:
                            logger.warning("Too many consecutive read failures, reconnecting...")
                            break
                        time.sleep(0.01)
                        continue

                    consecutive_failures = 0

                    # Convert BGR (OpenCV) to RGB (Qt)
                    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    h, w, ch = rgb_frame.shape
                    bytes_per_line = ch * w
                    qt_image = QImage(
                        rgb_frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888
                    ).copy()  # .copy() to ensure data ownership

                    self.frame_ready.emit(qt_image)

                    # ~30 FPS cap to reduce CPU usage
                    time.sleep(0.016)

            except Exception as e:
                logger.error(f"Stream error: {e}")
                self.error_occurred.emit(str(e))
                reconnect_attempts += 1
                time.sleep(self._reconnect_delay)
            finally:
                if cap is not None:
                    cap.release()

        self._running = False
        self.stream_stopped.emit()
        logger.info("Video stream thread stopped")

    @property
    def is_running(self) -> bool:
        return self._running


def build_rtsp_url(host: str, port: int = 554, path: str = "",
                   username: str = "", password: str = "") -> str:
    """
    Build an RTSP URL with optional authentication.
    
    Args:
        host: Camera IP address
        port: RTSP port (default 554)
        path: Stream path
        username: Authentication username
        password: Authentication password
        
    Returns:
        Complete RTSP URL string
    """
    auth = ""
    if username and password:
        auth = f"{username}:{password}@"
    
    url = f"rtsp://{auth}{host}:{port}"
    if path:
        if not path.startswith("/"):
            path = "/" + path
        url += path
    
    return url
