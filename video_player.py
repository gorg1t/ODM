"""
RTSP Video Stream Player using PyAV (FFmpeg).
Captures RTSP stream frames and provides them to the PyQt6 UI.

FFmpeg decoding runs in a separate *process* (not just a thread) so that
C-level crashes (e.g. access violations in a codec) cannot kill the main
application.  The QThread acts as a supervisor: it spawns the worker
process, forwards video frames to Qt via signals, and handles crashes
gracefully.
"""

import logging
import multiprocessing as mp
import time
from typing import Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, QMutex
from PyQt6.QtGui import QImage

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
AUDIO_OUTPUT_CHANNELS = 1


# ---------------------------------------------------------------------------
# Subprocess worker — must be a top-level function so Windows 'spawn' can
# pickle it.  All FFmpeg / PyAV / sounddevice code lives here so that any
# native crash is contained within this child process.
# ---------------------------------------------------------------------------

def _rtsp_subprocess_worker(
    rtsp_url: str,
    option_profiles: list,
    max_reconnects: int,
    first_frame_timeout: float,
    stop_event,       # multiprocessing.Event
    cmd_q,            # multiprocessing.Queue  — commands from main process
    frame_q,          # multiprocessing.Queue  — frames/events to main process
) -> None:
    """Isolated FFmpeg/sounddevice worker.  Runs in a child process."""
    import logging as _logging
    import time as _time
    import threading as _threading
    import numpy as _np
    import av as _av
    from av.audio.resampler import AudioResampler as _Resampler

    _log = _logging.getLogger(__name__)

    # Windows WASAPI COM init (needed in every thread/process that uses audio)
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)
    except Exception:
        pass

    import sounddevice as _sd

    _AUDIO_RATE = 48000
    _AUDIO_CH   = 1
    _EMIT_INTERVAL = 1.0 / 30

    current_vol = 0.0
    reconnect_attempts = 0

    def _drain_cmd_q():
        """Apply any pending volume commands from the main process."""
        nonlocal current_vol
        try:
            while True:
                cmd = cmd_q.get_nowait()
                if cmd[0] == 'vol':
                    current_vol = float(cmd[1])
        except Exception:
            pass

    # Drain initial commands (e.g. initial volume sent before process started)
    _drain_cmd_q()

    while not stop_event.is_set() and reconnect_attempts < max_reconnects:
        container = None
        audio_out  = None
        try:
            profile_name, profile_options = option_profiles[
                reconnect_attempts % len(option_profiles)
            ]
            container = _av.open(
                rtsp_url,
                options=dict(profile_options),
                timeout=(10.0, 10.0),
            )
            if not container.streams.video:
                raise RuntimeError("No video stream found in RTSP source")

            video_stream = container.streams.video[0]
            # Use NONE threading to avoid internal FFmpeg race conditions
            # that can cause access violations with certain camera codecs.
            video_stream.thread_type = 'NONE'

            audio_stream = None
            resampler    = None
            demux_streams = [video_stream]
            if getattr(container.streams, 'audio', None):
                audio_stream = container.streams.audio[0]
                demux_streams.append(audio_stream)
                resampler = _Resampler(
                    format='fltp', layout='mono', rate=_AUDIO_RATE
                )

            # Watcher thread: closes the container as soon as stop_event fires
            # so that container.demux() unblocks immediately instead of waiting
            # for the next network packet or the stimeout to expire.
            def _watcher(c):
                stop_event.wait()
                try:
                    c.close()
                except Exception:
                    pass

            _threading.Thread(target=_watcher, args=(container,),
                               daemon=True, name='rtsp-watcher').start()

            last_emit_time       = 0.0
            first_frame_received = False
            opened_at            = _time.monotonic()

            for packet in container.demux(*demux_streams):
                if stop_event.is_set():
                    break

                if (not first_frame_received
                        and packet.stream == video_stream
                        and _time.monotonic() - opened_at > first_frame_timeout):
                    raise TimeoutError(
                        f"No video frame within {first_frame_timeout:.0f}s "
                        f"using {profile_name.upper()} transport"
                    )

                for frame in packet.decode():
                    if stop_event.is_set():
                        break

                    if packet.stream == video_stream:
                        if not first_frame_received:
                            first_frame_received = True
                            reconnect_attempts   = 0
                            frame_q.put(('started',))
                            _log.info("RTSP stream opened successfully")

                        now = _time.monotonic()
                        if now - last_emit_time < _EMIT_INTERVAL:
                            continue

                        rgb = frame.reformat(format='rgb24')
                        arr = rgb.to_ndarray()          # (H, W, 3)  uint8
                        h, w, ch = arr.shape
                        try:
                            frame_q.put_nowait(('frame', w, h, ch, arr.tobytes()))
                        except Exception:
                            pass   # drop frame when queue full (backpressure)
                        last_emit_time = now

                    elif packet.stream == audio_stream and resampler:
                        # Apply any pending volume commands before writing audio
                        _drain_cmd_q()

                        for out_frame in resampler.resample(frame):
                            if stop_event.is_set():
                                break
                            pcm = out_frame.to_ndarray()   # (1, N) float32
                            if pcm.ndim == 2:
                                pcm = pcm.T                # (N, 1)
                            if not pcm.size:
                                continue
                            try:
                                if audio_out is None:
                                    audio_out = _sd.OutputStream(
                                        samplerate=_AUDIO_RATE,
                                        channels=_AUDIO_CH,
                                        dtype='float32',
                                    )
                                    audio_out.start()
                                audio_out.write(pcm * _np.float32(current_vol))
                            except Exception as sd_err:
                                _log.warning(f"Audio output error: {sd_err}")
                                if audio_out is not None:
                                    try:
                                        audio_out.stop()
                                        audio_out.close()
                                    except Exception:
                                        pass
                                    audio_out = None

            if not stop_event.is_set() and not first_frame_received:
                raise RuntimeError(
                    f"RTSP opened via {profile_name.upper()} but no frames decoded"
                )

        except _av.error.ExitError:
            _log.info("Stream closed (ExitError)")
            break
        except Exception as exc:
            if stop_event.is_set():
                break   # intentional stop — do not reconnect
            _log.error(f"Stream error: {exc}")
            frame_q.put(('error', str(exc)))
            reconnect_attempts += 1
            # Interruptible sleep: exits immediately when stop_event is set
            stop_event.wait(3.0)
        finally:
            if audio_out is not None:
                try:
                    audio_out.stop()
                    audio_out.close()
                except Exception:
                    pass
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass

    frame_q.put(('stopped',))


# ---------------------------------------------------------------------------
# Qt supervisor thread — spawns the worker process and forwards events to Qt
# ---------------------------------------------------------------------------

class VideoStreamThread(QThread):
    """Supervisor QThread for the isolated RTSP worker process.

    Video decoding runs inside a child process so that FFmpeg native crashes
    (e.g. access violations with unusual camera codecs) cannot bring down the
    main application.
    """

    frame_ready    = pyqtSignal(QImage)
    error_occurred = pyqtSignal(str)
    stream_started = pyqtSignal()
    stream_stopped = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rtsp_url: Optional[str] = None
        self._running        = False
        self._stop_requested = False   # set by stop_stream() before run() starts
        self._mutex          = QMutex()
        self._max_reconnect_attempts = 5
        self._first_frame_timeout    = 20.0
        self._volume: float = 0.0

        # Shared state with the worker process; created fresh in run()
        self._stop_event: Optional[mp.Event] = None
        self._cmd_q:      Optional[mp.Queue] = None

    def set_url(self, url: str):
        self._rtsp_url = url

    def set_volume(self, volume: float):
        self._volume = max(0.0, min(1.0, float(volume)))
        if self._cmd_q is not None:
            try:
                self._cmd_q.put_nowait(('vol', self._volume))
                logger.debug(f"Volume command sent to subprocess: {self._volume:.2f}")
            except Exception:
                pass
        else:
            logger.debug(f"Volume stored (thread not running yet): {self._volume:.2f}")

    def stop_stream(self):
        self._mutex.lock()
        self._running        = False
        self._stop_requested = True
        self._mutex.unlock()
        if self._stop_event is not None:
            self._stop_event.set()
        # Only block if the QThread is actually alive; avoids a 7-second hang
        # when stop_stream() is called on a thread that never entered run().
        if self.isRunning():
            self.wait(7000)

    def run(self):
        if not self._rtsp_url:
            self.error_occurred.emit("No RTSP URL configured")
            return

        # stop_stream() may have been called between thread.start() and here.
        self._mutex.lock()
        if self._stop_requested:
            self._mutex.unlock()
            self.stream_stopped.emit()
            return
        self._running = True
        self._mutex.unlock()

        stop_event = mp.Event()
        cmd_q      = mp.Queue()
        frame_q    = mp.Queue(maxsize=4)   # small buffer keeps latency low

        # Send the current volume before starting the process so the worker
        # picks it up from cmd_q immediately on launch.
        cmd_q.put(('vol', self._volume))

        # Store for set_volume() / stop_stream() access from other threads
        self._stop_event = stop_event
        self._cmd_q      = cmd_q

        # Serialise the option profiles as plain lists/dicts for pickling
        option_profiles = [(name, dict(opts)) for name, opts in RTSP_OPTION_PROFILES]

        process = mp.Process(
            target=_rtsp_subprocess_worker,
            args=(
                self._rtsp_url,
                option_profiles,
                self._max_reconnect_attempts,
                self._first_frame_timeout,
                stop_event,
                cmd_q,
                frame_q,
            ),
            daemon=True,
            name='rtsp-worker',
        )
        process.start()
        logger.info(f"Started RTSP worker process PID={process.pid}")

        try:
            while self._running:
                # Detect subprocess crash (segfault, access violation, …)
                if not process.is_alive() and frame_q.empty():
                    exit_code = process.exitcode
                    logger.error(
                        f"RTSP worker process died unexpectedly "
                        f"(exit code {exit_code})"
                    )
                    self.error_occurred.emit(
                        f"Video stream process crashed (exit code {exit_code})"
                    )
                    break

                try:
                    item = frame_q.get(timeout=0.05)
                except Exception:
                    continue   # queue empty — loop back

                kind = item[0]

                if kind == 'frame':
                    _, w, h, ch, data = item
                    qt_image = QImage(
                        data, w, h, ch * w,
                        QImage.Format.Format_RGB888,
                    ).copy()
                    self.frame_ready.emit(qt_image)

                elif kind == 'started':
                    self.stream_started.emit()

                elif kind == 'error':
                    logger.error(f"Stream error from worker: {item[1]}")
                    self.error_occurred.emit(item[1])

                elif kind == 'stopped':
                    break

        finally:
            self._running = False
            stop_event.set()
            process.join(timeout=5)
            if process.is_alive():
                logger.warning("RTSP worker did not stop cleanly; terminating")
                process.terminate()
                process.join(timeout=2)

            self._stop_event = None
            self._cmd_q      = None
            self.stream_stopped.emit()
            logger.info("Video stream thread stopped")

    @property
    def is_running(self) -> bool:
        return self._running


# ---------------------------------------------------------------------------
# Legacy audio-only thread (not used — kept for reference)
# ---------------------------------------------------------------------------

class AudioStreamThread(QThread):
    """Unused — audio is handled inside _rtsp_subprocess_worker."""

    error_occurred = pyqtSignal(str)
    stream_started = pyqtSignal()
    stream_stopped = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rtsp_url: Optional[str] = None
        self._running = False
        self._mutex   = QMutex()
        self._volume: float = 0.0

    def set_url(self, url: str):
        self._rtsp_url = url

    def set_volume(self, volume: float):
        self._volume = max(0.0, min(1.0, volume))

    def stop_stream(self):
        self._mutex.lock()
        self._running = False
        self._mutex.unlock()
        self.wait(5000)

    def run(self):
        pass
