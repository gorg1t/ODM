import sys
import traceback
from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
from PyQt6.QtWidgets import QApplication, QMainWindow

def handle_message(msg_type, context, message):
    if 'QFont::setPointSize' in message:
        print("--- Qt Warning Detected ---")
        print(f"Message: {message}")
        print("--- Python Stack Trace ---")
        # Skip the handler frame
        stack = traceback.format_stack()
        for frame in stack[:-1]:
            print(frame.strip())
        print("--- End Stack Trace ---")
        # Exit after first occurrence to keep output clean
        sys.exit(0)

qInstallMessageHandler(handle_message)

app = QApplication(sys.argv)
window = QMainWindow()
# Force the warning by setting a point size <= 0
font = window.font()
font.setPointSize(0)
window.setFont(font)
window.show()
