"""
RTSP Video Stream Player using PyAV (FFmpeg).
Captures RTSP stream frames and provides them to the PyQt6 UI.
"""

import logging
import time
from typing import Optional

import numpy as np
import av
from av.audio.resampler import AudioResampler
from PyQt6.QtCore import QThread, pyqtSignal, QMutex, Qt
from PyQt6.QtGui import QImage, QPixmap

logger = logging.getLogger(__name__)

LOW_LATENCY_RTSP_OPTIONS = {
    'rtsp_flags': 'prefer_tcp',
    'fflags': 'nobuffer+discardcorrupt',
    'flags': 'low_delay',
    'max_delay': '0',
    'reorder_queue_size': '0',
    'analyzeduration': '0',
    'probesize': '32768',
}

DEFAULT_RTSP_OPTIONS = {
    **LOW_LATENCY_RTSP_OPTIONS,
    'stimeout': '10000000',
}

RTSP_OPTION_PROFILES = (
    (
        'udp',
        {
            **DEFAULT_RTSP_OPTIONS,
            'rtsp_transport': 'udp',
            'buffer_size': '524288',
        },
    ),
    (
        'tcp',
        {
            **DEFAULT_RTSP_OPTIONS,
            'rtsp_transport': 'tcp',
        },
    ),
)

AUDIO_OUTPUT_SAMPLE_RATE = 48000
AUDIO_OUTPUT_CHANNELS = 2


class VideoStreamThread(QThread):
    """Thread for capturing RTSP video and audio streams via PyAV."""

    frame_ready = pyqtSignal(QImage)
    audio_format_ready = pyqtSignal(int, int)
    audio_chunk_ready = pyqtSignal(bytes)
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
        self._first_frame_timeout = 20.0

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
            container = None
            try:
                profile_name, profile_options = RTSP_OPTION_PROFILES[
                    reconnect_attempts % len(RTSP_OPTION_PROFILES)
                ]
                logger.info(
                    f"Opening RTSP stream: {self._rtsp_url} using {profile_name.upper()} transport"
                )

                container = av.open(
                    self._rtsp_url,
                    options=dict(profile_options),
                    timeout=(10.0, 10.0),  # (open, read) timeouts in seconds
                )
                if not container.streams.video:
                    raise RuntimeError("No video stream found in RTSP source")

                video_stream = container.streams.video[0]
                video_stream.thread_type = 'SLICE'

                audio_stream = None
                resampler = None
                demux_streams = [video_stream]
                if getattr(container.streams, 'audio', None):
                    audio_stream = container.streams.audio[0]
                    demux_streams.append(audio_stream)
                    resampler = AudioResampler(
                        format='s16p',
                        layout='stereo',
                        rate=AUDIO_OUTPUT_SAMPLE_RATE,
                    )

                EMIT_INTERVAL = 1.0 / 30  # max ~30fps display rate
                last_emit_time = 0.0
                first_frame_received = False
                opened_at = time.monotonic()

                if audio_stream:
                    self.audio_format_ready.emit(
                        AUDIO_OUTPUT_SAMPLE_RATE,
                        AUDIO_OUTPUT_CHANNELS,
                    )

                for packet in container.demux(*demux_streams):
                    if not self._running:
                        break

                    if not first_frame_received and packet.stream == video_stream and time.monotonic() - opened_at > self._first_frame_timeout:
                        raise TimeoutError(
                            "No decodable video frame received within "
                            f"{self._first_frame_timeout:.0f}s using {profile_name.upper()} transport"
                        )

                    for frame in packet.decode():
                        if not self._running:
                            break

                        if packet.stream == video_stream:
                            if not first_frame_received:
                                first_frame_received = True
                                reconnect_attempts = 0
                                self.stream_started.emit()
                                logger.info("RTSP stream opened successfully")

                            now = time.monotonic()
                            if now - last_emit_time < EMIT_INTERVAL:
                                continue

                            rgb_frame = frame.reformat(format='rgb24')
                            arr = rgb_frame.to_ndarray()
                            h, w, ch = arr.shape
                            bytes_per_line = ch * w
                            qt_image = QImage(
                                arr.data, w, h, bytes_per_line,
                                QImage.Format.Format_RGB888,
                            ).copy()

                            self.frame_ready.emit(qt_image)
                            last_emit_time = now
                        elif packet.stream == audio_stream and resampler:
                            for output_frame in resampler.resample(frame):
                                if not self._running:
                                    break
                                planes = output_frame.planes
                                if not planes:
                                    continue
                                # Read raw int16 samples directly from FFmpeg planes.
                                # s16p mono (1 plane) or stereo (2 planes) are both
                                # handled explicitly to avoid to_ndarray() shape surprises.
                                ch0 = np.frombuffer(bytes(planes[0]), dtype=np.int16)
                                if len(planes) >= 2:
                                    ch1 = np.frombuffer(bytes(planes[1]), dtype=np.int16)
                                    n = min(len(ch0), len(ch1))
                                    interleaved = np.empty(n * 2, dtype=np.int16)
                                    interleaved[0::2] = ch0[:n]
                                    interleaved[1::2] = ch1[:n]
                                else:
                                    # Mono output: duplicate to both channels
                                    interleaved = np.empty(len(ch0) * 2, dtype=np.int16)
                                    interleaved[0::2] = ch0
                                    interleaved[1::2] = ch0
                                if not getattr(self, '_audio_fmt_logged', False):
                                    self._audio_fmt_logged = True
                                    logger.info(
                                        f"Audio: fmt={output_frame.format.name} "
                                        f"rate={output_frame.sample_rate} Hz "
                                        f"planes={len(planes)} "
                                        f"samples={output_frame.samples} "
                                        f"→ {len(interleaved) * 2} B/chunk"
                                    )
                                pcm_bytes = interleaved.tobytes()
                                if pcm_bytes:
                                    self.audio_chunk_ready.emit(pcm_bytes)

                if self._running and not first_frame_received:
                    raise RuntimeError(
                        f"RTSP stream opened using {profile_name.upper()} transport, but no video frames were decoded"
                    )

            except av.error.ExitError:
                logger.info("Stream closed")
                break
            except Exception as e:
                logger.error(f"Stream error: {e}")
                self.error_occurred.emit(str(e))
                reconnect_attempts += 1
                if self._running:
                    time.sleep(self._reconnect_delay)
            finally:
                if container is not None:
                    container.close()

        self._running = False
        self.stream_stopped.emit()
        logger.info("Video stream thread stopped")

    @property
    def is_running(self) -> bool:
        return self._running


class AudioStreamThread(QThread):
    """Thread for decoding RTSP audio into PCM samples via PyAV."""

    audio_format_ready = pyqtSignal(int, int)
    audio_chunk_ready = pyqtSignal(bytes)
    error_occurred = pyqtSignal(str)
    stream_started = pyqtSignal()
    stream_stopped = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rtsp_url: Optional[str] = None
        self._running = False
        self._mutex = QMutex()
        self._reconnect_delay = 3
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
        """Decode RTSP audio and emit PCM audio chunks."""
        if not self._rtsp_url:
            self.error_occurred.emit("No RTSP URL configured")
            return

        self._running = True
        reconnect_attempts = 0
        resampler = AudioResampler(
            format='s16p',
            layout='stereo',
            rate=AUDIO_OUTPUT_SAMPLE_RATE,
        )

        while self._running and reconnect_attempts < self._max_reconnect_attempts:
            container = None
            try:
                logger.info(f"Opening RTSP audio stream: {self._rtsp_url}")

                options = dict(DEFAULT_RTSP_OPTIONS)
                options['rtsp_transport'] = 'tcp'

                container = av.open(
                    self._rtsp_url,
                    options=options,
                    timeout=(10.0, 5.0),
                )

                if not container.streams.audio:
                    self.error_occurred.emit("No audio stream found")
                    logger.warning("No audio stream found in RTSP source")
                    break

                audio_stream = container.streams.audio[0]

                reconnect_attempts = 0
                self.audio_format_ready.emit(
                    AUDIO_OUTPUT_SAMPLE_RATE,
                    AUDIO_OUTPUT_CHANNELS,
                )
                self.stream_started.emit()
                logger.info("RTSP audio stream opened successfully")

                for packet in container.demux(audio_stream):
                    if not self._running:
                        break

                    for frame in packet.decode():
                        if not self._running:
                            break

                        for output_frame in resampler.resample(frame):
                            if not self._running:
                                break
                            planes = output_frame.planes
                            if not planes:
                                continue
                            ch0 = np.frombuffer(bytes(planes[0]), dtype=np.int16)
                            if len(planes) >= 2:
                                ch1 = np.frombuffer(bytes(planes[1]), dtype=np.int16)
                                n = min(len(ch0), len(ch1))
                                interleaved = np.empty(n * 2, dtype=np.int16)
                                interleaved[0::2] = ch0[:n]
                                interleaved[1::2] = ch1[:n]
                            else:
                                interleaved = np.empty(len(ch0) * 2, dtype=np.int16)
                                interleaved[0::2] = ch0
                                interleaved[1::2] = ch0
                            pcm_bytes = interleaved.tobytes()
                            if pcm_bytes:
                                self.audio_chunk_ready.emit(pcm_bytes)

            except av.error.ExitError:
                logger.info("Audio stream closed")
                break
            except Exception as e:
                logger.error(f"Audio stream error: {e}")
                self.error_occurred.emit(str(e))
                reconnect_attempts += 1
                if self._running:
                    time.sleep(self._reconnect_delay)
            finally:
                if container is not None:
                    container.close()

        self._running = False
        self.stream_stopped.emit()
        logger.info("Audio stream thread stopped")

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
