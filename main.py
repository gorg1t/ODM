"""
ONVIF PTZ Controller — PoC
Entry point for the application.
"""

import sys
import logging
import multiprocessing as mp
import faulthandler

# faulthandler requires sys.stderr; in a windowed PyInstaller build it is None.
if sys.stderr is not None:
    faulthandler.enable()

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont
from main_window import MainWindow


def setup_logging():
    """Configure application logging.

    In a windowed (no-console) build sys.stderr is None, so StreamHandler
    would silently drop everything.  Redirect to a log file instead so that
    diagnostics are still available.
    """
    _frozen = getattr(sys, 'frozen', False)
    if _frozen or sys.stderr is None:
        import os
        log_dir = os.path.dirname(sys.executable) if _frozen else os.getcwd()
        log_path = os.path.join(log_dir, "onvif_ptz_controller.log")
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            filename=log_path,
            filemode='w',
            encoding='utf-8',
        )
    else:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )


def main():
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Starting ONVIF PTZ Controller PoC")

    app = QApplication(sys.argv)
    app.setApplicationName("ONVIF PTZ Controller")
    app.setOrganizationName("MIEM")
    app_font = QFont(app.font())
    app_font.setFamily("Segoe UI")
    if app_font.pointSize() <= 0:
        app_font.setPointSize(9)
    app.setFont(app_font)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    mp.freeze_support()   # required on Windows when using spawn start method
    main()
