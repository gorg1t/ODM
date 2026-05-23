"""
ONVIF PTZ Controller — PoC
Entry point for the application.
"""

import sys
import logging
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont
from main_window import MainWindow


def setup_logging():
    """Configure application logging."""
    logging.basicConfig(
        level=logging.INFO,
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
    main()
