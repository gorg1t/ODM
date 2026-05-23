"""
ONVIF PTZ Camera Client Module.
Handles connection, PTZ control, media profiles, and camera services.
"""

import logging
import os
import inspect
from typing import Any, Optional
from dataclasses import dataclass, field

from onvif import ONVIFCamera
from zeep.exceptions import Fault
from zeep.helpers import serialize_object

logger = logging.getLogger(__name__)


async def _resolve_maybe_awaitable(value):
    """Handle both sync and async ONVIF library methods."""

    if inspect.isawaitable(value):
        return await value
    return value


class _OnvifAsyncCompatProxy:
    """Expose sync-or-async ONVIF service methods behind a consistent awaitable API."""

    _passthrough_methods = {
        'create_type',
        'get_type',
        'get_element',
        'type_factory',
    }

    def __init__(self, target):
        self._target = target

    def __getattr__(self, name: str):
        attribute = getattr(self._target, name)
        if not callable(attribute) or name in self._passthrough_methods:
            return attribute

        async def _call(*args, **kwargs):
            return await _resolve_maybe_awaitable(attribute(*args, **kwargs))

        return _call


def _wrap_onvif_service(service):
    if service is None or isinstance(service, _OnvifAsyncCompatProxy):
        return service
    return _OnvifAsyncCompatProxy(service)


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


@dataclass
class MediaProfileInfo:
    """Camera media profile/stream information."""
    token: str
    name: str
    encoding: str = ""
    width: Optional[int] = None
    height: Optional[int] = None

    @property
    def display_name(self) -> str:
        details = []
        if self.encoding:
            details.append(self.encoding)
        if self.width and self.height:
            details.append(f"{self.width}x{self.height}")
        if details:
            return f"{self.name} ({', '.join(details)})"
        return self.name


@dataclass
class ImagingSettingInfo:
    """Imaging parameter value and supported range."""
    key: str
    label: str
    value: Optional[float] = None
    minimum: float = 0.0
    maximum: float = 100.0
    supported: bool = False


@dataclass
class ServiceEntry:
    """Read-only service object shown in analytics and rules tabs."""
    name: str
    token: str = ""
    item_type: str = ""
    data: Any = None


@dataclass
class ValueRange:
    """Simple numeric range metadata for editor widgets."""
    minimum: float
    maximum: float


@dataclass
class VideoResolutionOption:
    """Available encoder resolution option."""
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass
class VideoEncoderSettings:
    """Current video encoder settings plus camera-supported options."""
    profile_token: str
    profile_name: str
    configuration_token: str
    encoding: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    quality: Optional[float] = None
    frame_rate: Optional[int] = None
    encoding_interval: Optional[int] = None
    bitrate_limit: Optional[int] = None
    gov_length: Optional[int] = None
    encoding_profile: str = ""
    available_encodings: list[str] = field(default_factory=list)
    available_profiles: list[str] = field(default_factory=list)
    available_resolutions: list[VideoResolutionOption] = field(default_factory=list)
    quality_range: Optional[ValueRange] = None
    frame_rate_range: Optional[ValueRange] = None
    encoding_interval_range: Optional[ValueRange] = None
    bitrate_range: Optional[ValueRange] = None
    gov_length_range: Optional[ValueRange] = None


@dataclass
class ImagingSettingsPayload:
    """Serialized imaging settings and their device-specific options."""
    settings: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkSettingsPayload:
    """Flattened network settings shown in the network settings panel."""
    interface_token: Optional[str] = None
    dhcp: Optional[bool] = None
    ip_address: str = ""
    subnet_mask: str = ""
    default_gateway: str = ""
    host_name: str = ""
    host_name_from_dhcp: Optional[bool] = None
    dns_from_dhcp: Optional[bool] = None
    dns_manual: list[str] = field(default_factory=list)
    ntp_from_dhcp: Optional[bool] = None
    ntp_manual: list[str] = field(default_factory=list)
    http_enabled: Optional[bool] = None
    http_port: Optional[int] = None
    https_enabled: Optional[bool] = None
    https_port: Optional[int] = None
    rtsp_enabled: Optional[bool] = None
    rtsp_port: Optional[int] = None
    zero_config_enabled: Optional[bool] = None
    zero_config_addresses: list[str] = field(default_factory=list)
    discovery_mode: str = ""


@dataclass
class UserAccountInfo:
    """Simple user management entry."""
    username: str
    role: str


class ONVIFPTZClient:
    """Client for controlling PTZ cameras via ONVIF protocol."""

    USER_LEVELS = ["Administrator", "Operator", "User", "Anonymous"]

    def __init__(self, host: str, port: int, username: str, password: str):
        self.camera_info = CameraInfo(
            host=host, port=port, username=username, password=password
        )
        self._camera: Optional[ONVIFCamera] = None
        self._ptz_service = None
        self._media_service = None
        self._device_service = None
        self._imaging_service = None
        self._analytics_service = None
        self._profile_token: Optional[str] = None
        self._ptz_config_token: Optional[str] = None
        self._profile_objects: dict[str, object] = {}
        self._profiles: list[MediaProfileInfo] = []
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
            await _resolve_maybe_awaitable(self._camera.update_xaddrs())

            # Initialize services
            self._device_service = _wrap_onvif_service(
                await _resolve_maybe_awaitable(self._camera.create_devicemgmt_service())
            )
            self._media_service = _wrap_onvif_service(
                await _resolve_maybe_awaitable(self._camera.create_media_service())
            )

            try:
                self._ptz_service = _wrap_onvif_service(
                    await _resolve_maybe_awaitable(self._camera.create_ptz_service())
                )
            except Exception as e:
                self._ptz_service = None
                logger.info(f"PTZ service unavailable: {e}")

            try:
                self._imaging_service = _wrap_onvif_service(
                    await _resolve_maybe_awaitable(self._camera.create_imaging_service())
                )
            except Exception as e:
                self._imaging_service = None
                logger.info(f"Imaging service unavailable: {e}")

            try:
                self._analytics_service = _wrap_onvif_service(
                    await _resolve_maybe_awaitable(self._camera.create_analytics_service())
                )
            except Exception as e:
                self._analytics_service = None
                logger.info(f"Analytics service unavailable: {e}")

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

            self._store_profiles(profiles)
            if not self._profiles:
                logger.error("Failed to parse media profiles")
                return False

            self.set_active_profile(self._profiles[0].token)
            logger.info(f"Using profile: {self._profile_token}")

            self.camera_info.connected = True
            logger.info(
                f"Connected to {self.camera_info.manufacturer} {self.camera_info.model} "
                f"(FW: {self.camera_info.firmware})"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to connect to camera: {e}")
            if self._camera is not None:
                try:
                    await _resolve_maybe_awaitable(self._camera.close())
                except Exception as close_error:
                    logger.debug(f"Failed to close camera after connection error: {close_error}")
                finally:
                    self._camera = None
            self.camera_info.connected = False
            return False

    @property
    def has_ptz(self) -> bool:
        return self._ptz_service is not None

    async def disconnect(self):
        """Disconnect from the camera."""
        if self._camera is not None:
            try:
                await _resolve_maybe_awaitable(self._camera.close())
            except Exception as e:
                logger.debug(f"Failed to close camera cleanly: {e}")
        self._camera = None
        self._ptz_service = None
        self._media_service = None
        self._device_service = None
        self._imaging_service = None
        self._analytics_service = None
        self._profile_token = None
        self._ptz_config_token = None
        self._profile_objects = {}
        self._profiles = []
        self.camera_info.connected = False
        logger.info("Disconnected from camera")

    def _store_profiles(self, profiles) -> None:
        self._profile_objects = {}
        self._profiles = []

        for index, profile in enumerate(profiles, start=1):
            token = getattr(profile, 'token', None) or getattr(profile, '_token', None)
            if token is None:
                continue

            token = str(token)
            name = getattr(profile, 'Name', None) or f"Profile {index}"

            width = None
            height = None
            encoding = ""
            video_config = getattr(profile, 'VideoEncoderConfiguration', None)
            if video_config:
                encoding = str(getattr(video_config, 'Encoding', '') or "")
                resolution = getattr(video_config, 'Resolution', None)
                if resolution:
                    width = getattr(resolution, 'Width', None)
                    height = getattr(resolution, 'Height', None)

            self._profile_objects[token] = profile
            self._profiles.append(
                MediaProfileInfo(
                    token=token,
                    name=str(name),
                    encoding=encoding,
                    width=width,
                    height=height,
                )
            )

    def set_active_profile(self, profile_token: str) -> bool:
        """Switch active ONVIF media/PTZ profile."""
        profile = self._profile_objects.get(profile_token)
        if profile is None:
            return False

        self._profile_token = profile_token
        self._ptz_config_token = None
        if hasattr(profile, 'PTZConfiguration') and profile.PTZConfiguration:
            self._ptz_config_token = profile.PTZConfiguration.token
        return True

    async def get_media_profiles(self) -> list[MediaProfileInfo]:
        """Return media profiles discovered during connection."""
        return list(self._profiles)

    def _current_profile(self):
        if not self._profile_token:
            return None
        return self._profile_objects.get(self._profile_token)

    def _video_source_token(self) -> Optional[str]:
        profile = self._current_profile()
        if profile is None:
            return None

        source_config = getattr(profile, 'VideoSourceConfiguration', None)
        if source_config is None:
            return None

        token = getattr(source_config, 'SourceToken', None)
        if token is None:
            token = getattr(source_config, 'token', None)
        return str(token) if token is not None else None

    def _token_from_value(self, value) -> Optional[str]:
        if value is None:
            return None
        token = getattr(value, 'token', None) or getattr(value, '_token', None)
        if token is None and isinstance(value, dict):
            token = value.get('token') or value.get('_token')
        return str(token) if token is not None else None

    def _get_field(self, value, field_name: str, default=None):
        if isinstance(value, dict):
            return value.get(field_name, default)
        return getattr(value, field_name, default)

    def _set_field(self, value, field_name: str, field_value):
        if isinstance(value, dict):
            value[field_name] = field_value
        else:
            setattr(value, field_name, field_value)

    def _range_from_value(self, value) -> Optional[ValueRange]:
        if value is None:
            return None

        minimum = self._get_field(value, 'Min')
        maximum = self._get_field(value, 'Max')
        if minimum is None or maximum is None:
            return None

        return ValueRange(float(minimum), float(maximum))

    def _list_from_value(self, value) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    def _coerce_bool(self, value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {'true', '1', 'yes', 'on'}:
            return True
        if text in {'false', '0', 'no', 'off'}:
            return False
        return None

    def _network_interface_ipv4_addresses(self, interface: Any) -> list[str]:
        ipv4 = self._get_field(interface, 'IPv4')
        config = self._get_field(ipv4, 'Config') if ipv4 is not None else None
        if config is None:
            return []

        addresses: list[str] = []
        for entry in self._list_from_value(self._get_field(config, 'Manual')):
            address = self._entry_to_text(entry)
            if address:
                addresses.append(address)

        dhcp_entry = self._get_field(config, 'FromDHCP')
        dhcp_address = self._entry_to_text(dhcp_entry)
        if dhcp_address:
            addresses.append(dhcp_address)

        return addresses

    def _select_network_interface(self, interfaces: list[Any]) -> Any:
        if not interfaces:
            return None

        target_host = str(self.camera_info.host or '').strip()
        if target_host:
            for interface in interfaces:
                if target_host in self._network_interface_ipv4_addresses(interface):
                    return interface

        for interface in interfaces:
            enabled = self._coerce_bool(self._get_field(interface, 'Enabled'))
            if enabled is not False:
                return interface

        return interfaces[0]

    def _select_zero_configuration(self, value: Any, interface_token: Optional[str]) -> Any:
        direct_token = self._token_from_value(value) or self._get_field(value, 'InterfaceToken')
        if direct_token:
            if not interface_token or str(direct_token) == str(interface_token):
                return value

        entries = self._list_from_value(self._get_field(value, 'ZeroConfiguration'))
        if interface_token:
            for entry in entries:
                entry_token = self._token_from_value(entry) or self._get_field(entry, 'InterfaceToken')
                if entry_token and str(entry_token) == str(interface_token):
                    return entry

        if entries:
            return entries[0]

        return value if direct_token or self._get_field(value, 'Enabled') is not None else None

    def _device_operation_supported(self, operation_name: str) -> bool:
        return self._device_service is not None and hasattr(self._device_service, operation_name)

    def _prefix_to_mask(self, prefix_length: int) -> str:
        prefix = max(0, min(32, int(prefix_length)))
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix > 0 else 0
        return ".".join(str((mask >> shift) & 0xFF) for shift in (24, 16, 8, 0))

    def _mask_to_prefix(self, subnet_mask: str) -> Optional[int]:
        octets = subnet_mask.strip().split('.')
        if len(octets) != 4:
            return None

        try:
            bits = ''.join(f"{int(octet):08b}" for octet in octets)
        except ValueError:
            return None

        if '01' in bits:
            return None
        return bits.count('1')

    def _entry_to_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value

        for field_name in ('IPv4Address', 'IPv6Address', 'DNSname', 'Address', 'Name'):
            field_value = self._get_field(value, field_name)
            if field_value:
                return str(field_value)

        return str(value)

    def _split_multi_value(self, raw_value: str) -> list[str]:
        text = raw_value.replace('\n', ';').replace(',', ';')
        return [item.strip() for item in text.split(';') if item.strip()]

    def _dns_entry_from_text(self, value: str) -> dict[str, Any]:
        if ':' in value and not value.count('.') == 3:
            return {'IPv6Address': value}
        return {'IPv4Address': value}

    def _ntp_entry_from_text(self, value: str) -> dict[str, Any]:
        if any(character.isalpha() for character in value):
            return {'Type': 'DNS', 'DNSname': value}
        if ':' in value and not value.count('.') == 3:
            return {'Type': 'IPv6', 'IPv6Address': value}
        return {'Type': 'IPv4', 'IPv4Address': value}

    def _serialize_for_ui(self, value) -> Any:
        if value is None:
            return None

        serialized = serialize_object(value)
        if isinstance(serialized, dict):
            cleaned: dict[str, Any] = {}
            for key, item in serialized.items():
                key_text = str(key)
                if key_text.startswith('_'):
                    continue

                normalized = self._serialize_for_ui(item)
                if normalized is None:
                    continue
                if isinstance(normalized, dict) and not normalized:
                    continue
                if isinstance(normalized, list) and not normalized:
                    continue
                cleaned[key_text] = normalized
            return cleaned

        if isinstance(serialized, list):
            items = []
            for item in serialized:
                normalized = self._serialize_for_ui(item)
                if normalized is None:
                    continue
                items.append(normalized)
            return items

        return serialized

    def _video_encoder_option_branch(self, options, encoding: str):
        if options is None or not encoding:
            return None, None

        branch = getattr(options, encoding, None)
        extension = getattr(options, 'Extension', None)
        extension_branch = getattr(extension, encoding, None) if extension is not None else None
        return branch, extension_branch

    def _available_video_encodings(self, options) -> list[str]:
        if options is None:
            return []

        encodings: list[str] = []
        for encoding in ('JPEG', 'MPEG4', 'H264'):
            if getattr(options, encoding, None) is not None:
                encodings.append(encoding)
        return encodings

    def _resolution_options(self, branch) -> list[VideoResolutionOption]:
        options: list[VideoResolutionOption] = []
        seen: set[tuple[int, int]] = set()

        for resolution in self._list_from_value(self._get_field(branch, 'ResolutionsAvailable')):
            width = self._get_field(resolution, 'Width')
            height = self._get_field(resolution, 'Height')
            if width is None or height is None:
                continue

            pair = (int(width), int(height))
            if pair in seen:
                continue

            seen.add(pair)
            options.append(VideoResolutionOption(width=pair[0], height=pair[1]))

        return options

    async def refresh_media_profiles(self) -> list[MediaProfileInfo]:
        """Refresh media profiles from the device and keep the active token valid."""
        if not self._media_service:
            return []

        profiles = await self._media_service.GetProfiles()
        self._store_profiles(profiles or [])

        if self._profiles:
            if self._profile_token in self._profile_objects:
                self.set_active_profile(self._profile_token)
            else:
                self.set_active_profile(self._profiles[0].token)
        else:
            self._profile_token = None
            self._ptz_config_token = None

        return list(self._profiles)

    def _analytics_configuration_token(self) -> Optional[str]:
        profile = self._current_profile()
        if profile is None:
            return None

        analytics_config = getattr(profile, 'VideoAnalyticsConfiguration', None)
        if analytics_config is None:
            return None

        token = getattr(analytics_config, 'token', None) or getattr(analytics_config, '_token', None)
        return str(token) if token is not None else None

    def _service_entry_from_object(self, item, fallback_name: str) -> ServiceEntry:
        name = getattr(item, 'Name', None) or getattr(item, 'name', None) or fallback_name
        token = (
            getattr(item, 'token', None)
            or getattr(item, '_token', None)
            or getattr(item, 'Name', None)
            or getattr(item, 'name', None)
            or ""
        )
        item_type = getattr(item, 'Type', None) or getattr(item, 'type', None) or getattr(item, 'Name', None) or ""
        return ServiceEntry(
            name=str(name),
            token=str(token),
            item_type=str(item_type) if item_type else "",
            data=item,
        )

    async def get_imaging_settings(self) -> ImagingSettingsPayload:
        """Get serialized imaging settings and device-specific options for the active profile."""
        payload = ImagingSettingsPayload()

        if not self._imaging_service:
            return payload

        video_source_token = self._video_source_token()
        if not video_source_token:
            return payload

        try:
            options_request = self._imaging_service.create_type('GetOptions')
            options_request.VideoSourceToken = video_source_token
            options = await self._imaging_service.GetOptions(options_request)
            normalized = self._serialize_for_ui(options)
            if isinstance(normalized, dict):
                payload.options = normalized
        except Exception as e:
            logger.info(f"Imaging options unavailable: {e}")

        try:
            settings_request = self._imaging_service.create_type('GetImagingSettings')
            settings_request.VideoSourceToken = video_source_token
            settings = await self._imaging_service.GetImagingSettings(settings_request)
            normalized = self._serialize_for_ui(settings)
            if isinstance(normalized, dict):
                payload.settings = normalized
        except Exception as e:
            logger.info(f"Imaging settings unavailable: {e}")

        return payload

    def _merge_nested_values(self, target, values: dict[str, Any]):
        """Merge UI values into the current ONVIF imaging settings object."""
        for key, value in values.items():
            if not self._field_exists(target, key):
                continue

            current_value = self._get_field(target, key, None)

            if isinstance(value, dict):
                if current_value is None:
                    continue
                self._merge_nested_values(current_value, value)
                continue

            if isinstance(value, list):
                continue

            self._set_field(target, key, value)

    def _field_exists(self, obj, field_name: str) -> bool:
        """Check if a field exists on an ONVIF object."""
        try:
            # For zeep objects, try to access the field
            if hasattr(obj, field_name):
                return True
            # For dict-like objects
            if isinstance(obj, dict):
                return field_name in obj
            return False
        except:
            return False

    async def set_imaging_settings(self, values: dict[str, Any]) -> bool:
        """Apply nested imaging settings for the active profile."""
        if not self._imaging_service:
            return False

        video_source_token = self._video_source_token()
        if not video_source_token:
            return False

        try:
            get_request = self._imaging_service.create_type('GetImagingSettings')
            get_request.VideoSourceToken = video_source_token
            current_settings = await self._imaging_service.GetImagingSettings(get_request)

            self._merge_nested_values(current_settings, values)

            request = self._imaging_service.create_type('SetImagingSettings')
            request.VideoSourceToken = video_source_token
            request.ImagingSettings = current_settings
            request.ForcePersistence = True
            await self._imaging_service.SetImagingSettings(request)
            return True
        except Exception as e:
            logger.error(f"SetImagingSettings error: {e}")
            return False

    async def get_video_encoder_settings(self) -> Optional[VideoEncoderSettings]:
        """Get the active profile's video encoder configuration and available options."""
        if not self._media_service or not self._profile_token:
            return None

        profile = self._current_profile()
        if profile is None:
            return None

        configuration_ref = getattr(profile, 'VideoEncoderConfiguration', None)
        configuration_token = self._token_from_value(configuration_ref)
        if not configuration_token:
            return None

        try:
            config_request = self._media_service.create_type('GetVideoEncoderConfiguration')
            config_request.ConfigurationToken = configuration_token
            config = await self._media_service.GetVideoEncoderConfiguration(config_request)
        except Exception as e:
            logger.info(f"Video encoder configuration unavailable: {e}")
            return None

        options = None
        try:
            options_request = self._media_service.create_type('GetVideoEncoderConfigurationOptions')
            options_request.ConfigurationToken = configuration_token
            options_request.ProfileToken = self._profile_token
            options = await self._media_service.GetVideoEncoderConfigurationOptions(options_request)
        except Exception as e:
            logger.info(f"Video encoder options unavailable: {e}")

        encoding = str(getattr(config, 'Encoding', '') or '')
        resolution = getattr(config, 'Resolution', None)
        rate_control = getattr(config, 'RateControl', None)
        option_branch, extension_branch = self._video_encoder_option_branch(options, encoding)

        gov_length = None
        encoding_profile = ""
        available_profiles: list[str] = []
        codec_config = None
        if encoding == 'H264':
            codec_config = getattr(config, 'H264', None)
            gov_length = getattr(codec_config, 'GovLength', None) if codec_config else None
            encoding_profile = str(getattr(codec_config, 'H264Profile', '') or '') if codec_config else ""
            available_profiles = [
                str(item)
                for item in self._list_from_value(
                    getattr(option_branch, 'H264ProfilesSupported', None) if option_branch else None
                )
            ]
        elif encoding == 'MPEG4':
            codec_config = getattr(config, 'MPEG4', None)
            gov_length = getattr(codec_config, 'GovLength', None) if codec_config else None
            encoding_profile = str(getattr(codec_config, 'Mpeg4Profile', '') or '') if codec_config else ""
            available_profiles = [
                str(item)
                for item in self._list_from_value(
                    getattr(option_branch, 'Mpeg4ProfilesSupported', None) if option_branch else None
                )
            ]

        return VideoEncoderSettings(
            profile_token=self._profile_token,
            profile_name=str(getattr(profile, 'Name', None) or self._profile_token),
            configuration_token=configuration_token,
            encoding=encoding,
            width=int(getattr(resolution, 'Width', 0)) if resolution is not None else None,
            height=int(getattr(resolution, 'Height', 0)) if resolution is not None else None,
            quality=float(getattr(config, 'Quality', 0.0)) if getattr(config, 'Quality', None) is not None else None,
            frame_rate=int(getattr(rate_control, 'FrameRateLimit', 0)) if rate_control is not None and getattr(rate_control, 'FrameRateLimit', None) is not None else None,
            encoding_interval=int(getattr(rate_control, 'EncodingInterval', 0)) if rate_control is not None and getattr(rate_control, 'EncodingInterval', None) is not None else None,
            bitrate_limit=int(getattr(rate_control, 'BitrateLimit', 0)) if rate_control is not None and getattr(rate_control, 'BitrateLimit', None) is not None else None,
            gov_length=int(gov_length) if gov_length is not None else None,
            encoding_profile=encoding_profile,
            available_encodings=self._available_video_encodings(options),
            available_profiles=available_profiles,
            available_resolutions=self._resolution_options(option_branch),
            quality_range=self._range_from_value(getattr(options, 'QualityRange', None) if options is not None else None),
            frame_rate_range=self._range_from_value(getattr(option_branch, 'FrameRateRange', None) if option_branch is not None else None),
            encoding_interval_range=self._range_from_value(getattr(option_branch, 'EncodingIntervalRange', None) if option_branch is not None else None),
            bitrate_range=self._range_from_value(getattr(extension_branch, 'BitrateRange', None) if extension_branch is not None else None),
            gov_length_range=self._range_from_value(getattr(option_branch, 'GovLengthRange', None) if option_branch is not None else None),
        )

    async def set_video_encoder_settings(self, values: dict[str, Any]) -> bool:
        """Update the active profile's video encoder configuration."""
        if not self._media_service or not self._profile_token:
            return False

        profile = self._current_profile()
        if profile is None:
            return False

        configuration_ref = getattr(profile, 'VideoEncoderConfiguration', None)
        configuration_token = self._token_from_value(configuration_ref)
        if not configuration_token:
            return False

        try:
            config_request = self._media_service.create_type('GetVideoEncoderConfiguration')
            config_request.ConfigurationToken = configuration_token
            config = await self._media_service.GetVideoEncoderConfiguration(config_request)

            encoding = str(values.get('encoding') or getattr(config, 'Encoding', '') or '')
            if encoding:
                setattr(config, 'Encoding', encoding)

            width = values.get('width')
            height = values.get('height')
            if width is not None and height is not None:
                resolution = getattr(config, 'Resolution', None)
                if resolution is None:
                    resolution = {'Width': int(width), 'Height': int(height)}
                    setattr(config, 'Resolution', resolution)
                else:
                    self._set_field(resolution, 'Width', int(width))
                    self._set_field(resolution, 'Height', int(height))

            if values.get('quality') is not None:
                setattr(config, 'Quality', float(values['quality']))

            rate_control = getattr(config, 'RateControl', None)
            if rate_control is None and any(
                values.get(field_name) is not None
                for field_name in ('frame_rate', 'encoding_interval', 'bitrate_limit')
            ):
                rate_control = {}
                setattr(config, 'RateControl', rate_control)

            if rate_control is not None:
                if values.get('frame_rate') is not None:
                    self._set_field(rate_control, 'FrameRateLimit', int(values['frame_rate']))
                if values.get('encoding_interval') is not None:
                    self._set_field(rate_control, 'EncodingInterval', int(values['encoding_interval']))
                if values.get('bitrate_limit') is not None:
                    self._set_field(rate_control, 'BitrateLimit', int(values['bitrate_limit']))

            if encoding == 'H264':
                setattr(config, 'MPEG4', None)
                h264_config = getattr(config, 'H264', None) or {}
                if values.get('gov_length') is not None:
                    self._set_field(h264_config, 'GovLength', int(values['gov_length']))
                if values.get('encoding_profile'):
                    self._set_field(h264_config, 'H264Profile', str(values['encoding_profile']))
                setattr(config, 'H264', h264_config)
            elif encoding == 'MPEG4':
                setattr(config, 'H264', None)
                mpeg4_config = getattr(config, 'MPEG4', None) or {}
                if values.get('gov_length') is not None:
                    self._set_field(mpeg4_config, 'GovLength', int(values['gov_length']))
                if values.get('encoding_profile'):
                    self._set_field(mpeg4_config, 'Mpeg4Profile', str(values['encoding_profile']))
                setattr(config, 'MPEG4', mpeg4_config)
            elif encoding == 'JPEG':
                setattr(config, 'H264', None)
                setattr(config, 'MPEG4', None)

            request = self._media_service.create_type('SetVideoEncoderConfiguration')
            request.Configuration = config
            request.ForcePersistence = True
            await self._media_service.SetVideoEncoderConfiguration(request)
            await self.refresh_media_profiles()
            return True
        except Exception as e:
            logger.error(f"SetVideoEncoderConfiguration error: {e}")
            return False

    async def _analytics_configuration_tokens(self) -> list[str]:
        tokens: list[str] = []
        active_token = self._analytics_configuration_token()
        if active_token:
            tokens.append(active_token)

        if self._media_service is not None:
            try:
                configs = await self._media_service.GetVideoAnalyticsConfigurations()
                for config in configs or []:
                    token = self._token_from_value(config)
                    if token and token not in tokens:
                        tokens.append(token)
            except Exception as e:
                logger.info(f"Video analytics configurations unavailable: {e}")

        return tokens

    async def get_video_analytics_configurations(self) -> list[ServiceEntry]:
        """Return all video analytics configurations attached to media profiles."""
        if not self._media_service:
            return []

        try:
            configs = await self._media_service.GetVideoAnalyticsConfigurations()
        except Exception as e:
            logger.info(f"Video analytics configurations unavailable: {e}")
            return []

        return [
            self._service_entry_from_object(item, f"Configuration {index}")
            for index, item in enumerate(configs or [], start=1)
        ]

    async def _get_supported_entries(self, operation_name: str, fallback_label: str) -> list[ServiceEntry]:
        if not self._analytics_service:
            return []

        config_token = self._analytics_configuration_token()
        if not config_token:
            configs = await self.get_video_analytics_configurations()
            config_token = configs[0].token if configs else None
        if not config_token:
            return []

        try:
            request = self._analytics_service.create_type(operation_name)
            request.ConfigurationToken = config_token
            result = await getattr(self._analytics_service, operation_name)(request)
            return [
                self._service_entry_from_object(item, f"{fallback_label} {index}")
                for index, item in enumerate(result or [], start=1)
            ]
        except Exception as e:
            logger.info(f"{operation_name} unavailable: {e}")
            return []

    async def get_supported_rules(self) -> list[ServiceEntry]:
        """Return rule descriptions supported by the current analytics configuration."""
        return await self._get_supported_entries('GetSupportedRules', 'Rule')

    async def get_supported_analytics_modules(self) -> list[ServiceEntry]:
        """Return analytics module descriptions supported by the current analytics configuration."""
        return await self._get_supported_entries('GetSupportedAnalyticsModules', 'Module')

    def _normalize_config_name(self, value: str) -> str:
        return value.strip()

    def _build_config_object(self, name: str, type_name: str, source=None):
        config = self._analytics_service.create_type('Config') if self._analytics_service else {
            'Name': name,
            'Type': type_name,
            'Parameters': {'SimpleItem': []},
        }

        if isinstance(config, dict):
            config['Name'] = name
            config['Type'] = type_name
            parameters = serialize_object(getattr(source, 'Parameters', None)) if source is not None else None
            config['Parameters'] = parameters if parameters is not None else {'SimpleItem': []}
            return config

        config.Name = name
        config.Type = type_name
        if source is not None and getattr(source, 'Parameters', None) is not None:
            parameters = serialize_object(getattr(source, 'Parameters'))
            config.Parameters = parameters if parameters is not None else {'SimpleItem': []}
        else:
            config.Parameters = {'SimpleItem': []}
        return config

    async def _modify_analytics_configurations(
        self,
        operation_name: str,
        item_name_field: str,
        items: list,
    ) -> bool:
        if not self._analytics_service:
            return False

        config_token = self._analytics_configuration_token()
        if not config_token:
            configs = await self.get_video_analytics_configurations()
            config_token = configs[0].token if configs else None
        if not config_token:
            return False

        try:
            request = self._analytics_service.create_type(operation_name)
            request.ConfigurationToken = config_token
            setattr(request, item_name_field, items)
            await getattr(self._analytics_service, operation_name)(request)
            return True
        except Exception as e:
            logger.error(f"{operation_name} error: {e}")
            return False

    async def get_rules(self) -> list[ServiceEntry]:
        """Get analytics rules for the active profile if supported."""
        assigned_rules = await self._collect_analytics_entries('GetRules', 'Rule')
        if assigned_rules:
            return assigned_rules
        return await self.get_supported_rules()

    async def create_rule(self, name: str, type_name: Optional[str] = None) -> bool:
        supported_rules = await self.get_supported_rules()
        chosen_type = type_name or (supported_rules[0].item_type if supported_rules else None)
        if not chosen_type:
            return False

        config = self._build_config_object(self._normalize_config_name(name), chosen_type)
        return await self._modify_analytics_configurations('CreateRules', 'Rule', [config])

    async def delete_rule(self, rule_name: str) -> bool:
        return await self._modify_analytics_configurations('DeleteRules', 'RuleName', [rule_name])

    async def modify_rule(self, rule_name: str, new_name: str) -> bool:
        current_rules = await self.get_rules()
        source = next((item.data for item in current_rules if item.name == rule_name), None)
        if source is None:
            return False

        config = self._build_config_object(self._normalize_config_name(new_name), getattr(source, 'Type', None) or getattr(source, 'type', None) or rule_name, source)
        return await self._modify_analytics_configurations('ModifyRules', 'Rule', [config])

    async def get_analytics_modules(self) -> list[ServiceEntry]:
        """Get analytics modules for the active profile if supported."""
        assigned_modules = await self._collect_analytics_entries('GetAnalyticsModules', 'Module')
        if assigned_modules:
            return assigned_modules
        return await self.get_supported_analytics_modules()

    async def create_analytics_module(self, name: str, type_name: Optional[str] = None) -> bool:
        supported_modules = await self.get_supported_analytics_modules()
        chosen_type = type_name or (supported_modules[0].item_type if supported_modules else None)
        if not chosen_type:
            return False

        config = self._build_config_object(self._normalize_config_name(name), chosen_type)
        return await self._modify_analytics_configurations('CreateAnalyticsModules', 'AnalyticsModule', [config])

    async def delete_analytics_module(self, module_name: str) -> bool:
        return await self._modify_analytics_configurations('DeleteAnalyticsModules', 'AnalyticsModuleName', [module_name])

    async def modify_analytics_module(self, module_name: str, new_name: str) -> bool:
        current_modules = await self.get_analytics_modules()
        source = next((item.data for item in current_modules if item.name == module_name), None)
        if source is None:
            return False

        config = self._build_config_object(self._normalize_config_name(new_name), getattr(source, 'Type', None) or getattr(source, 'type', None) or module_name, source)
        return await self._modify_analytics_configurations('ModifyAnalyticsModules', 'AnalyticsModule', [config])

    async def _collect_analytics_entries(self, operation_name: str, item_label: str) -> list[ServiceEntry]:
        if not self._analytics_service:
            return []

        tokens = await self._analytics_configuration_tokens()
        if not tokens:
            return []

        collected: list[ServiceEntry] = []
        seen: set[tuple[str, str, str]] = set()

        for config_token in tokens:
            try:
                request = self._analytics_service.create_type(operation_name)
                request.ConfigurationToken = config_token
                result = await getattr(self._analytics_service, operation_name)(request)
            except Exception as e:
                logger.info(f"{operation_name} unavailable for {config_token}: {e}")
                continue

            for index, item in enumerate(result or [], start=1):
                entry = self._service_entry_from_object(item, f"{item_label} {index}")
                key = (entry.token, entry.name, entry.item_type)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(entry)

        return collected

    async def _get_profile(self, profile_token: str):
        if not self._media_service:
            return None

        profile = self._profile_objects.get(profile_token)
        if profile is not None:
            return profile

        request = self._media_service.create_type('GetProfile')
        request.ProfileToken = profile_token
        return await self._media_service.GetProfile(request)

    async def _copy_profile_configurations(self, source_profile_token: str, target_profile_token: str):
        source_profile = await self._get_profile(source_profile_token)
        if source_profile is None or not self._media_service:
            return

        operation_map = (
            ('VideoSourceConfiguration', 'AddVideoSourceConfiguration'),
            ('VideoEncoderConfiguration', 'AddVideoEncoderConfiguration'),
            ('AudioSourceConfiguration', 'AddAudioSourceConfiguration'),
            ('AudioEncoderConfiguration', 'AddAudioEncoderConfiguration'),
            ('PTZConfiguration', 'AddPTZConfiguration'),
            ('VideoAnalyticsConfiguration', 'AddVideoAnalyticsConfiguration'),
            ('MetadataConfiguration', 'AddMetadataConfiguration'),
            ('AudioOutputConfiguration', 'AddAudioOutputConfiguration'),
            ('AudioDecoderConfiguration', 'AddAudioDecoderConfiguration'),
        )

        for attribute_name, operation_name in operation_map:
            configuration = getattr(source_profile, attribute_name, None)
            configuration_token = self._token_from_value(configuration)
            if not configuration_token:
                continue

            try:
                request = self._media_service.create_type(operation_name)
                request.ProfileToken = target_profile_token
                request.ConfigurationToken = configuration_token
                await getattr(self._media_service, operation_name)(request)
            except Exception as e:
                logger.info(
                    f"{operation_name} failed while copying profile {source_profile_token}: {e}"
                )

    async def create_profile(
        self,
        name: str,
        token: Optional[str] = None,
        copy_from_token: Optional[str] = None,
    ) -> Optional[MediaProfileInfo]:
        """Create a profile and optionally clone configuration links from another profile."""
        if not self._media_service:
            return None

        try:
            request = self._media_service.create_type('CreateProfile')
            request.Name = name
            if token:
                request.Token = token

            result = await self._media_service.CreateProfile(request)
            created_profile = getattr(result, 'Profile', None) or result
            created_token = self._token_from_value(created_profile)

            if created_token and copy_from_token:
                await self._copy_profile_configurations(copy_from_token, created_token)

            profiles = await self.refresh_media_profiles()
            for profile in profiles:
                if profile.token == created_token:
                    return profile
        except Exception as e:
            logger.error(f"CreateProfile error: {e}")

        return None

    async def delete_profile(self, profile_token: str) -> bool:
        """Delete a media profile and keep the client state consistent."""
        if not self._media_service:
            return False

        try:
            request = self._media_service.create_type('DeleteProfile')
            request.ProfileToken = profile_token
            await self._media_service.DeleteProfile(request)
            await self.refresh_media_profiles()
            return True
        except Exception as e:
            logger.error(f"DeleteProfile error: {e}")
            return False

    async def edit_profile(
        self,
        profile_token: str,
        new_name: str,
        new_token: Optional[str] = None,
    ) -> Optional[MediaProfileInfo]:
        """Rename a profile by recreating it with the same linked configurations."""
        source_profile = self._profile_objects.get(profile_token)
        current_name = str(getattr(source_profile, 'Name', '') or '') if source_profile else ''
        if current_name == new_name and (not new_token or new_token == profile_token):
            for profile in self._profiles:
                if profile.token == profile_token:
                    return profile

        was_active = self._profile_token == profile_token
        replacement = await self.create_profile(
            name=new_name,
            token=new_token,
            copy_from_token=profile_token,
        )
        if replacement is None:
            return None

        deleted = await self.delete_profile(profile_token)
        if not deleted:
            return None

        if was_active:
            self.set_active_profile(replacement.token)

        return replacement

    async def get_network_settings(self) -> NetworkSettingsPayload:
        """Return a flattened snapshot of device-management network settings."""
        payload = NetworkSettingsPayload()
        if not self._device_service:
            return payload

        try:
            interfaces = await self._device_service.GetNetworkInterfaces()
            interface_list = self._list_from_value(interfaces)
            active_interface = self._select_network_interface(interface_list)
            if active_interface is not None:
                payload.interface_token = self._token_from_value(active_interface)
                ipv4 = self._get_field(active_interface, 'IPv4')
                config = self._get_field(ipv4, 'Config') if ipv4 is not None else None
                if config is not None:
                    dhcp_value = self._get_field(config, 'DHCP')
                    payload.dhcp = self._coerce_bool(dhcp_value)
                    manual_entries = self._list_from_value(self._get_field(config, 'Manual'))
                    from_dhcp = self._get_field(config, 'FromDHCP')
                    chosen_entry = from_dhcp if payload.dhcp and from_dhcp is not None else (manual_entries[0] if manual_entries else from_dhcp)
                    if chosen_entry is not None:
                        payload.ip_address = self._entry_to_text(chosen_entry)
                        prefix_length = self._get_field(chosen_entry, 'PrefixLength')
                        if prefix_length is not None:
                            payload.subnet_mask = self._prefix_to_mask(int(prefix_length))
        except Exception as e:
            logger.info(f"GetNetworkInterfaces unavailable: {e}")

        try:
            gateway = await self._device_service.GetNetworkDefaultGateway()
            ipv4_gateways = [
                self._entry_to_text(item)
                for item in self._list_from_value(self._get_field(gateway, 'IPv4Address'))
                if self._entry_to_text(item)
            ]
            ipv6_gateways = [
                self._entry_to_text(item)
                for item in self._list_from_value(self._get_field(gateway, 'IPv6Address'))
                if self._entry_to_text(item)
            ]
            payload.default_gateway = ';'.join(ipv4_gateways + ipv6_gateways)
        except Exception as e:
            logger.info(f"GetNetworkDefaultGateway unavailable: {e}")

        try:
            hostname = await self._device_service.GetHostname()
            payload.host_name = str(self._get_field(hostname, 'Name') or "")
            payload.host_name_from_dhcp = self._coerce_bool(self._get_field(hostname, 'FromDHCP'))
        except Exception as e:
            logger.info(f"GetHostname unavailable: {e}")

        try:
            dns = await self._device_service.GetDNS()
            from_dhcp = self._get_field(dns, 'FromDHCP')
            payload.dns_from_dhcp = self._coerce_bool(from_dhcp)
            payload.dns_manual = [
                self._entry_to_text(item)
                for item in self._list_from_value(self._get_field(dns, 'DNSManual'))
                if self._entry_to_text(item)
            ]
        except Exception as e:
            logger.info(f"GetDNS unavailable: {e}")

        try:
            ntp = await self._device_service.GetNTP()
            from_dhcp = self._get_field(ntp, 'FromDHCP')
            payload.ntp_from_dhcp = self._coerce_bool(from_dhcp)
            payload.ntp_manual = [
                self._entry_to_text(item)
                for item in self._list_from_value(self._get_field(ntp, 'NTPManual'))
                if self._entry_to_text(item)
            ]
        except Exception as e:
            logger.info(f"GetNTP unavailable: {e}")

        try:
            protocols = await self._device_service.GetNetworkProtocols()
            for protocol in self._list_from_value(protocols):
                name = str(self._get_field(protocol, 'Name') or '').upper()
                enabled = self._get_field(protocol, 'Enabled')
                ports = self._list_from_value(self._get_field(protocol, 'Port'))
                port_value = None
                if ports:
                    try:
                        port_value = int(ports[0])
                    except (TypeError, ValueError):
                        port_value = None

                if name == 'HTTP':
                    payload.http_enabled = self._coerce_bool(enabled)
                    payload.http_port = port_value
                elif name == 'HTTPS':
                    payload.https_enabled = self._coerce_bool(enabled)
                    payload.https_port = port_value
                elif name == 'RTSP':
                    payload.rtsp_enabled = self._coerce_bool(enabled)
                    payload.rtsp_port = port_value
        except Exception as e:
            logger.info(f"GetNetworkProtocols unavailable: {e}")

        if payload.interface_token:
            try:
                zero_config_response = await self._device_service.GetZeroConfiguration()
                zero_config = self._select_zero_configuration(
                    zero_config_response,
                    payload.interface_token,
                )
                enabled = self._get_field(zero_config, 'Enabled') if zero_config is not None else None
                payload.zero_config_enabled = self._coerce_bool(enabled)
                payload.zero_config_addresses = [
                    self._entry_to_text(item)
                    for item in self._list_from_value(self._get_field(zero_config, 'Addresses'))
                    if self._entry_to_text(item)
                ]
            except Exception as e:
                logger.info(f"GetZeroConfiguration unavailable: {e}")

        try:
            discovery_mode = await self._device_service.GetDiscoveryMode()
            payload.discovery_mode = str(discovery_mode or '')
        except Exception as e:
            logger.info(f"GetDiscoveryMode unavailable: {e}")

        return payload

    async def set_network_settings(self, values: dict[str, Any]) -> tuple[bool, list[str]]:
        """Apply edited network settings with best-effort per-section updates."""
        if not self._device_service:
            return False, ["Device management service unavailable."]

        errors: list[str] = []
        interface_token = str(values.get('interface_token') or '').strip()

        if interface_token and values.get('dhcp') is not None:
            try:
                request = self._device_service.create_type('SetNetworkInterfaces')
                request.InterfaceToken = interface_token
                interface_config: dict[str, Any] = {
                    'IPv4': {
                        'Enabled': True,
                        'DHCP': bool(values.get('dhcp')),
                    },
                }
                if not bool(values.get('dhcp')):
                    ip_address = str(values.get('ip_address') or '').strip()
                    subnet_mask = str(values.get('subnet_mask') or '').strip()
                    prefix_length = self._mask_to_prefix(subnet_mask) if subnet_mask else None
                    if ip_address and prefix_length is not None:
                        interface_config['IPv4']['Manual'] = [
                            {
                                'Address': ip_address,
                                'PrefixLength': prefix_length,
                            }
                        ]
                request.NetworkInterface = interface_config
                await self._device_service.SetNetworkInterfaces(request)
            except Exception as e:
                logger.error(f"SetNetworkInterfaces error: {e}")
                errors.append('IPv4 settings')

        default_gateway = str(values.get('default_gateway') or '').strip()
        if default_gateway:
            try:
                request = self._device_service.create_type('SetNetworkDefaultGateway')
                ipv4_addresses: list[str] = []
                ipv6_addresses: list[str] = []
                for item in self._split_multi_value(default_gateway):
                    if ':' in item and not item.count('.') == 3:
                        ipv6_addresses.append(item)
                    else:
                        ipv4_addresses.append(item)
                request.IPv4Address = ipv4_addresses
                request.IPv6Address = ipv6_addresses
                await self._device_service.SetNetworkDefaultGateway(request)
            except Exception as e:
                logger.error(f"SetNetworkDefaultGateway error: {e}")
                errors.append('Default gateway')

        host_name_from_dhcp = values.get('host_name_from_dhcp')
        host_name = str(values.get('host_name') or '').strip()
        if host_name_from_dhcp is not None:
            if self._device_operation_supported('SetHostnameFromDHCP'):
                try:
                    request = self._device_service.create_type('SetHostnameFromDHCP')
                    request.FromDHCP = bool(host_name_from_dhcp)
                    await self._device_service.SetHostnameFromDHCP(request)
                except Exception as e:
                    logger.error(f"SetHostnameFromDHCP error: {e}")
                    errors.append('Host name mode')
            elif bool(host_name_from_dhcp):
                errors.append('Host name mode')

        if host_name and not bool(host_name_from_dhcp):
            try:
                request = self._device_service.create_type('SetHostname')
                request.Name = host_name
                await self._device_service.SetHostname(request)
            except Exception as e:
                logger.error(f"SetHostname error: {e}")
                errors.append('Host name')

        if values.get('dns_from_dhcp') is not None:
            try:
                request = self._device_service.create_type('SetDNS')
                request.FromDHCP = bool(values.get('dns_from_dhcp'))
                request.SearchDomain = []
                if not request.FromDHCP:
                    request.DNSManual = [
                        self._dns_entry_from_text(item)
                        for item in self._split_multi_value(str(values.get('dns_manual') or ''))
                    ]
                await self._device_service.SetDNS(request)
            except Exception as e:
                logger.error(f"SetDNS error: {e}")
                errors.append('DNS')

        if values.get('ntp_from_dhcp') is not None:
            try:
                request = self._device_service.create_type('SetNTP')
                request.FromDHCP = bool(values.get('ntp_from_dhcp'))
                if not request.FromDHCP:
                    request.NTPManual = [
                        self._ntp_entry_from_text(item)
                        for item in self._split_multi_value(str(values.get('ntp_manual') or ''))
                    ]
                await self._device_service.SetNTP(request)
            except Exception as e:
                logger.error(f"SetNTP error: {e}")
                errors.append('NTP')

        protocol_items: list[dict[str, Any]] = []
        for protocol_name, enabled_key, port_key in (
            ('HTTP', 'http_enabled', 'http_port'),
            ('HTTPS', 'https_enabled', 'https_port'),
            ('RTSP', 'rtsp_enabled', 'rtsp_port'),
        ):
            enabled_value = values.get(enabled_key)
            port_value = values.get(port_key)
            if enabled_value is None and port_value in {None, ''}:
                continue
            protocol_items.append(
                {
                    'Name': protocol_name,
                    'Enabled': bool(enabled_value),
                    'Port': [int(port_value)] if port_value not in {None, ''} else [],
                }
            )

        if protocol_items:
            try:
                request = self._device_service.create_type('SetNetworkProtocols')
                request.NetworkProtocols = protocol_items
                await self._device_service.SetNetworkProtocols(request)
            except Exception as e:
                logger.error(f"SetNetworkProtocols error: {e}")
                errors.append('Network protocols')

        if interface_token and values.get('zero_config_enabled') is not None:
            try:
                request = self._device_service.create_type('SetZeroConfiguration')
                request.InterfaceToken = interface_token
                request.Enabled = bool(values.get('zero_config_enabled'))
                await self._device_service.SetZeroConfiguration(request)
            except Exception as e:
                logger.error(f"SetZeroConfiguration error: {e}")
                errors.append('Zero configuration')

        discovery_mode = str(values.get('discovery_mode') or '').strip()
        if discovery_mode:
            try:
                request = self._device_service.create_type('SetDiscoveryMode')
                request.DiscoveryMode = discovery_mode
                await self._device_service.SetDiscoveryMode(request)
            except Exception as e:
                logger.error(f"SetDiscoveryMode error: {e}")
                errors.append('Discovery mode')

        return len(errors) == 0, errors

    async def get_user_accounts(self) -> list[UserAccountInfo]:
        """Return device users with username and role."""
        if not self._device_service:
            return []

        try:
            users = await self._device_service.GetUsers()
        except Exception as e:
            logger.error(f"GetUsers error: {e}")
            return []

        accounts: list[UserAccountInfo] = []
        for user in self._list_from_value(users):
            username = str(self._get_field(user, 'Username') or '').strip()
            if not username:
                continue
            role = str(self._get_field(user, 'UserLevel') or 'User')
            accounts.append(UserAccountInfo(username=username, role=role))

        accounts.sort(key=lambda item: item.username.lower())
        return accounts

    async def create_user_account(self, username: str, password: str, role: str) -> bool:
        if not self._device_service:
            return False

        try:
            request = self._device_service.create_type('CreateUsers')
            request.User = [
                {
                    'Username': username,
                    'Password': password,
                    'UserLevel': role,
                }
            ]
            await self._device_service.CreateUsers(request)
            return True
        except Exception as e:
            logger.error(f"CreateUsers error: {e}")
            return False

    async def update_user_account(self, username: str, password: Optional[str], role: str) -> bool:
        if not self._device_service:
            return False

        try:
            request = self._device_service.create_type('SetUser')
            user_payload: dict[str, Any] = {
                'Username': username,
                'UserLevel': role,
            }
            if password:
                user_payload['Password'] = password
            request.User = [user_payload]
            await self._device_service.SetUser(request)
            return True
        except Exception as e:
            logger.error(f"SetUser error: {e}")
            return False

    async def delete_user_account(self, username: str) -> bool:
        if not self._device_service:
            return False

        try:
            request = self._device_service.create_type('DeleteUsers')
            request.Username = [username]
            await self._device_service.DeleteUsers(request)
            return True
        except Exception as e:
            logger.error(f"DeleteUsers error: {e}")
            return False

    async def system_reboot(self) -> tuple[bool, str]:
        """Trigger device reboot."""
        if not self._device_service:
            return False, 'Device management service unavailable.'

        try:
            result = await self._device_service.SystemReboot()
            return True, str(result or 'Device reboot requested.')
        except Exception as e:
            logger.error(f"SystemReboot error: {e}")
            return False, str(e)

    async def system_factory_reset(self, factory_mode: str) -> tuple[bool, str]:
        """Trigger soft or hard factory reset."""
        if not self._device_service:
            return False, 'Device management service unavailable.'

        try:
            request = self._device_service.create_type('SetSystemFactoryDefault')
            request.FactoryDefault = factory_mode
            await self._device_service.SetSystemFactoryDefault(request)
            return True, f'{factory_mode} factory reset requested.'
        except Exception as e:
            logger.error(f"SetSystemFactoryDefault error: {e}")
            return False, str(e)

    async def upgrade_firmware(self, firmware_path: str) -> tuple[bool, str]:
        """Best-effort firmware upload through the ONVIF device management service."""
        if not self._device_service:
            return False, 'Device management service unavailable.'

        try:
            with open(firmware_path, 'rb') as firmware_file:
                firmware_data = firmware_file.read()
        except Exception as e:
            return False, f'Could not read firmware file: {e}'

        start_error: Optional[Exception] = None
        if self._device_operation_supported('StartFirmwareUpgrade'):
            try:
                request = self._device_service.create_type('StartFirmwareUpgrade')
                await self._device_service.StartFirmwareUpgrade(request)
            except Exception as e:
                start_error = e
                logger.info(f"StartFirmwareUpgrade unavailable or failed: {e}")

        if self._device_operation_supported('UpgradeSystemFirmware'):
            try:
                request = self._device_service.create_type('UpgradeSystemFirmware')
                request.Firmware = firmware_data
                await self._device_service.UpgradeSystemFirmware(request)
                return True, 'Firmware upgrade request sent.'
            except Exception as e:
                logger.error(f"UpgradeSystemFirmware error: {e}")
                return False, str(e)

        if start_error is not None:
            return False, str(start_error)
        return False, 'Firmware upgrade is not supported by this device.'

    async def get_rules_for_configuration(self, configuration_token: str) -> list[ServiceEntry]:
        if not self._analytics_service:
            return []

        try:
            request = self._analytics_service.create_type('GetRules')
            request.ConfigurationToken = configuration_token
            result = await self._analytics_service.GetRules(request)
            return [
                self._service_entry_from_object(item, f"Rule {index}")
                for index, item in enumerate(result or [], start=1)
            ]
        except Exception as e:
            logger.info(f"GetRules for {configuration_token} unavailable: {e}")
            return []

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

    async def absolute_move(self, pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0,
                            speed: float = 1.0):
        """
        Move camera to an absolute position.

        Args:
            pan: Absolute pan position (-1.0 to 1.0)
            tilt: Absolute tilt position (-1.0 to 1.0)
            zoom: Absolute zoom position (0.0 to 1.0)
            speed: Movement speed (0.0 to 1.0)
        """
        if not self._ptz_service or not self._profile_token:
            logger.warning("PTZ service not available")
            return

        try:
            request = self._ptz_service.create_type('AbsoluteMove')
            request.ProfileToken = self._profile_token
            request.Position = {
                'PanTilt': {'x': pan, 'y': tilt},
                'Zoom': {'x': zoom}
            }
            request.Speed = {
                'PanTilt': {'x': speed, 'y': speed},
                'Zoom': {'x': speed}
            }
            await self._ptz_service.AbsoluteMove(request)
            logger.debug(f"AbsoluteMove: pan={pan}, tilt={tilt}, zoom={zoom}")
        except Fault as e:
            logger.error(f"AbsoluteMove failed: {e}")
        except Exception as e:
            logger.error(f"AbsoluteMove error: {e}")

    async def relative_move(self, pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0,
                            speed: float = 1.0):
        """
        Move camera by a relative offset from current position.

        Args:
            pan: Relative pan offset (-1.0 to 1.0)
            tilt: Relative tilt offset (-1.0 to 1.0)
            zoom: Relative zoom offset (-1.0 to 1.0)
            speed: Movement speed (0.0 to 1.0)
        """
        if not self._ptz_service or not self._profile_token:
            logger.warning("PTZ service not available")
            return

        try:
            request = self._ptz_service.create_type('RelativeMove')
            request.ProfileToken = self._profile_token
            request.Translation = {
                'PanTilt': {'x': pan, 'y': tilt},
                'Zoom': {'x': zoom}
            }
            request.Speed = {
                'PanTilt': {'x': speed, 'y': speed},
                'Zoom': {'x': speed}
            }
            await self._ptz_service.RelativeMove(request)
            logger.debug(f"RelativeMove: pan={pan}, tilt={tilt}, zoom={zoom}")
        except Fault as e:
            logger.error(f"RelativeMove failed: {e}")
        except Exception as e:
            logger.error(f"RelativeMove error: {e}")

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

    async def get_stream_uri(self, profile_token: Optional[str] = None) -> Optional[str]:
        """Get RTSP stream URI from the camera."""
        if not self._media_service or not self._profile_token:
            return None

        try:
            if profile_token and not self.set_active_profile(profile_token):
                logger.warning(f"Unknown media profile requested: {profile_token}")
                return None

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
