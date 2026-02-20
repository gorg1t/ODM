import logging
import os
from typing import Optional
from dataclasses import dataclass, field
from pathlib import Path

from onvif import ONVIFCamera
from zeep.exceptions import Fault

logger = logging.getLogger(__name__)


@dataclass
class CameraInfo:
    """Camera connection and status information."""
    host: str
    port: int
    username: str
    password: str
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""
    connected: bool = False


@dataclass
class PTZStatus:
    """Current PTZ position and status."""
    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 0.0
    moving: bool = False


@dataclass
class Preset:
    """Camera preset."""
    token: str
    name: str


class ONVIFPTZClient:
    """Client for controlling PTZ cameras via ONVIF protocol."""

    def __init__(self, host: str, port: int, username: str, password: str):
        self.camera_info = CameraInfo(
            host=host, port=port, username=username, password=password
        )
        self._camera: Optional[ONVIFCamera] = None
        self._ptz_service = None
        self._media_service = None
        self._device_service = None
        self._profile_token: Optional[str] = None
        self._ptz_config_token: Optional[str] = None
        self._presets: list[Preset] = []

    async def connect(self) -> bool:
        """Connect to the ONVIF camera and initialize services."""
        try:
            logger.info(f"Connecting to camera at {self.camera_info.host}:{self.camera_info.port}")

            # Locate WSDL files bundled with the onvif package
            import onvif as _onvif_pkg
            wsdl_dir = os.path.join(os.path.dirname(_onvif_pkg.__file__), 'wsdl')

            self._camera = ONVIFCamera(
                self.camera_info.host,
                self.camera_info.port,
                self.camera_info.username,
                self.camera_info.password,
                wsdl_dir=wsdl_dir,
            )
            await self._camera.update_xaddrs()

            # Initialize services
            self._device_service = await self._camera.create_devicemgmt_service()
            self._media_service = await self._camera.create_media_service()
            self._ptz_service = await self._camera.create_ptz_service()

            # Get device info
            device_info = await self._device_service.GetDeviceInformation()
            self.camera_info.manufacturer = device_info.Manufacturer or ""
            self.camera_info.model = device_info.Model or ""
            self.camera_info.firmware = device_info.FirmwareVersion or ""

            # Get media profile
            profiles = await self._media_service.GetProfiles()
            if not profiles:
                logger.error("No media profiles found on camera")
                return False

            self._profile_token = profiles[0].token
            logger.info(f"Using profile: {self._profile_token}")

            # Get PTZ configuration
            if hasattr(profiles[0], 'PTZConfiguration') and profiles[0].PTZConfiguration:
                self._ptz_config_token = profiles[0].PTZConfiguration.token

            self.camera_info.connected = True
            logger.info(
                f"Connected to {self.camera_info.manufacturer} {self.camera_info.model} "
                f"(FW: {self.camera_info.firmware})"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to connect to camera: {e}")
            self.camera_info.connected = False
            return False

    async def disconnect(self):
        """Disconnect from the camera."""
        self._camera = None
        self._ptz_service = None
        self._media_service = None
        self._device_service = None
        self._profile_token = None
        self.camera_info.connected = False
        logger.info("Disconnected from camera")

    async def continuous_move(self, pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0):
        """
        Start continuous PTZ movement.
        
        Args:
            pan: Pan velocity (-1.0 to 1.0, negative=left, positive=right)
            tilt: Tilt velocity (-1.0 to 1.0, negative=down, positive=up)
            zoom: Zoom velocity (-1.0 to 1.0, negative=out, positive=in)
        """
        if not self._ptz_service or not self._profile_token:
            logger.warning("PTZ service not available")
            return

        try:
            request = self._ptz_service.create_type('ContinuousMove')
            request.ProfileToken = self._profile_token
            request.Velocity = {
                'PanTilt': {'x': pan, 'y': tilt},
                'Zoom': {'x': zoom}
            }
            await self._ptz_service.ContinuousMove(request)
            logger.debug(f"ContinuousMove: pan={pan}, tilt={tilt}, zoom={zoom}")
        except Fault as e:
            logger.error(f"ContinuousMove failed: {e}")
        except Exception as e:
            logger.error(f"ContinuousMove error: {e}")

    async def stop_move(self):
        """Stop all PTZ movement."""
        if not self._ptz_service or not self._profile_token:
            return

        try:
            request = self._ptz_service.create_type('Stop')
            request.ProfileToken = self._profile_token
            request.PanTilt = True
            request.Zoom = True
            await self._ptz_service.Stop(request)
            logger.debug("PTZ movement stopped")
        except Fault as e:
            logger.error(f"Stop failed: {e}")
        except Exception as e:
            logger.error(f"Stop error: {e}")

    async def get_status(self) -> PTZStatus:
        """Get current PTZ position and status."""
        status = PTZStatus()
        if not self._ptz_service or not self._profile_token:
            return status

        try:
            request = self._ptz_service.create_type('GetStatus')
            request.ProfileToken = self._profile_token
            result = await self._ptz_service.GetStatus(request)

            if result.Position:
                if result.Position.PanTilt:
                    status.pan = result.Position.PanTilt.x or 0.0
                    status.tilt = result.Position.PanTilt.y or 0.0
                if result.Position.Zoom:
                    status.zoom = result.Position.Zoom.x or 0.0

            if result.MoveStatus:
                pan_status = getattr(result.MoveStatus, 'PanTilt', None)
                zoom_status = getattr(result.MoveStatus, 'Zoom', None)
                status.moving = (
                    (pan_status is not None and str(pan_status) == 'MOVING') or
                    (zoom_status is not None and str(zoom_status) == 'MOVING')
                )

        except Exception as e:
            logger.error(f"GetStatus error: {e}")

        return status

    async def get_presets(self) -> list[Preset]:
        """Get all saved presets from the camera."""
        self._presets = []
        if not self._ptz_service or not self._profile_token:
            return self._presets

        try:
            request = self._ptz_service.create_type('GetPresets')
            request.ProfileToken = self._profile_token
            result = await self._ptz_service.GetPresets(request)

            for preset in result:
                token = preset.token if hasattr(preset, 'token') else str(preset._token)
                name = preset.Name if hasattr(preset, 'Name') and preset.Name else f"Preset {token}"
                self._presets.append(Preset(token=token, name=name))

            logger.info(f"Found {len(self._presets)} presets")
        except Exception as e:
            logger.error(f"GetPresets error: {e}")

        return self._presets

    async def goto_preset(self, preset_token: str, speed: float = 1.0):
        """
        Move camera to a saved preset position.
        
        Args:
            preset_token: The token identifying the preset
            speed: Movement speed (0.0 to 1.0)
        """
        if not self._ptz_service or not self._profile_token:
            logger.warning("PTZ service not available")
            return

        try:
            request = self._ptz_service.create_type('GotoPreset')
            request.ProfileToken = self._profile_token
            request.PresetToken = preset_token
            request.Speed = {
                'PanTilt': {'x': speed, 'y': speed},
                'Zoom': {'x': speed}
            }
            await self._ptz_service.GotoPreset(request)
            logger.info(f"Moving to preset {preset_token}")
        except Fault as e:
            logger.error(f"GotoPreset failed: {e}")
        except Exception as e:
            logger.error(f"GotoPreset error: {e}")

    async def set_preset(self, preset_name: str) -> Optional[str]:
        """
        Save current position as a new preset.
        
        Args:
            preset_name: Name for the new preset
            
        Returns:
            Preset token if successful, None otherwise
        """
        if not self._ptz_service or not self._profile_token:
            return None

        try:
            request = self._ptz_service.create_type('SetPreset')
            request.ProfileToken = self._profile_token
            request.PresetName = preset_name
            result = await self._ptz_service.SetPreset(request)
            token = result
            logger.info(f"Saved preset '{preset_name}' with token {token}")
            return str(token)
        except Exception as e:
            logger.error(f"SetPreset error: {e}")
            return None

    async def remove_preset(self, preset_token: str) -> bool:
        """
        Remove a saved preset.
        
        Args:
            preset_token: Token of the preset to remove
            
        Returns:
            True if successful
        """
        if not self._ptz_service or not self._profile_token:
            return False

        try:
            request = self._ptz_service.create_type('RemovePreset')
            request.ProfileToken = self._profile_token
            request.PresetToken = preset_token
            await self._ptz_service.RemovePreset(request)
            logger.info(f"Removed preset {preset_token}")
            return True
        except Exception as e:
            logger.error(f"RemovePreset error: {e}")
            return False

    async def get_stream_uri(self) -> Optional[str]:
        """Get RTSP stream URI from the camera."""
        if not self._media_service or not self._profile_token:
            return None

        try:
            request = self._media_service.create_type('GetStreamUri')
            request.ProfileToken = self._profile_token
            request.StreamSetup = {
                'Stream': 'RTP-Unicast',
                'Transport': {'Protocol': 'RTSP'}
            }
            result = await self._media_service.GetStreamUri(request)
            uri = result.Uri
            logger.info(f"Stream URI: {uri}")
            return uri
        except Exception as e:
            logger.error(f"GetStreamUri error: {e}")
            return None

    @property
    def is_connected(self) -> bool:
        return self.camera_info.connected

    @property
    def profile_token(self) -> Optional[str]:
        return self._profile_token
