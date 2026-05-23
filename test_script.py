import sys
from PyQt6.QtWidgets import QApplication
from main_window import LocalSearchDialog, DiscoveredCameraInfo, CameraCredentialsDialog

app = QApplication(sys.argv)

dialog = LocalSearchDialog(None)

fake_cameras = [
    DiscoveredCameraInfo('192.168.1.10', 'Camera 1', 'f3511116-6211-4560-84c6-8a02d8478950'),
    DiscoveredCameraInfo('192.168.1.11', 'Camera 2', 'f3511116-6211-4560-84c6-8a02d8478951'),
    DiscoveredCameraInfo('192.168.1.12', 'Camera 3', 'f3511116-6211-4560-84c6-8a02d8478952'),
]
dialog._on_discovery_finished(fake_cameras)

dialog._select_all_cameras()
add_enabled = dialog.add_button.isEnabled()

# Monkeypatching for PyQt6
CameraCredentialsDialog.exec = lambda self: 1
CameraCredentialsDialog.credentials = lambda self: ('admin', 'Supervisor')

dialog._add_selected_cameras()
configs = dialog.selected_cameras

print(f'Count: {len(configs)}')
if configs:
    print(f'Sample: {configs[0].username}:{configs[0].password}')
