"""ONVIF WS-Discovery helpers for local network camera search."""

from __future__ import annotations

import json
import ipaddress
import platform
import re
import socket
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import unquote, urlparse


WS_DISCOVERY_ADDRESS = ("239.255.255.250", 3702)
RECEIVE_TIMEOUT_SECONDS = 0.4
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_SIZE = 65535
MAX_UNICAST_TARGETS = 65536
UNICAST_BATCH_SIZE = 256
UNICAST_BATCH_IDLE_SECONDS = 0.02
DEFAULT_AUTO_SUBNET_PREFIX = 24
VIRTUAL_INTERFACE_MARKERS = (
    "virtual",
    "vmware",
    "hyper-v",
    "vethernet",
    "loopback",
    "docker",
    "wsl",
    "tun",
    "tap",
    "tailscale",
    "wireguard",
)

DISCOVERY_NAMESPACES = {
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
}

PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <e:Header>
    <w:MessageID>uuid:{message_id}</w:MessageID>
    <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe />
  </e:Body>
</e:Envelope>
"""


@dataclass(slots=True)
class DiscoveredCameraInfo:
    """Single ONVIF device discovered through WS-Discovery."""

    host: str
    port: int
    xaddr: str
    endpoint_reference: str = ""
    scopes: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        for scope in self.scopes:
            value = _scope_value(scope, "name")
            if value:
                return value
        return ""

    @property
    def hardware(self) -> str:
        for scope in self.scopes:
            value = _scope_value(scope, "hardware")
            if value:
                return value
        return ""

    @property
    def location(self) -> str:
        for scope in self.scopes:
            value = _scope_value(scope, "location")
            if value:
                return value
        return ""

    @property
    def camera_id(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def is_camera(self) -> bool:
        if any(_type_local_name(type_name) == "NetworkVideoTransmitter" for type_name in self.types):
            return True
        return bool(self.scopes) and "/onvif/device_service" in self.xaddr.lower()


def _scope_value(scope: str, scope_name: str) -> str:
    marker = f"/www.onvif.org/{scope_name}/"
    normalized_scope = scope.replace("onvif://", "/", 1)
    if marker not in normalized_scope:
        return ""
    return unquote(normalized_scope.rsplit(marker, 1)[-1]).strip()


def _type_local_name(type_name: str) -> str:
    if not type_name:
        return ""
    return type_name.rsplit(":", 1)[-1].rsplit("}", 1)[-1]


def _is_virtual_interface_alias(interface_alias: str) -> bool:
    alias = interface_alias.strip().casefold()
    if not alias:
        return False
    return any(marker in alias for marker in VIRTUAL_INTERFACE_MARKERS)


def _probe_message() -> bytes:
    return PROBE_TEMPLATE.format(message_id=uuid.uuid4()).encode("utf-8")


def _create_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("", 0))
    sock.settimeout(RECEIVE_TIMEOUT_SECONDS)
    return sock


def _expand_partial_ipv4_target(token: str) -> Optional[list[str]]:
    parts = token.split(".")
    if not 1 <= len(parts) <= 4 or any(not part.isdigit() for part in parts):
        return None

    octets: list[int] = []
    for part in parts:
        value = int(part)
        if not 0 <= value <= 255:
            raise ValueError(f"Invalid IPv4 target: {token}")
        octets.append(value)

    if len(octets) == 4:
        return [token]

    if len(octets) < 2:
        raise ValueError(
            f"Target prefix {token} is too broad. Use at least two octets, for example 172.18, or use CIDR notation."
        )

    prefix_length = len(octets) * 8
    network_octets = octets + ([0] * (4 - len(octets)))
    network = ipaddress.ip_network(
        f"{'.'.join(str(octet) for octet in network_octets)}/{prefix_length}",
        strict=False,
    )
    if network.num_addresses > MAX_UNICAST_TARGETS:
        raise ValueError(
            f"Target range {token} is too large. Limit ranges to {MAX_UNICAST_TARGETS} addresses."
        )

    return [str(address) for address in network]


def parse_discovery_targets(text: str) -> list[str]:
    """Expand a target filter into host addresses for directed WS-Discovery probes."""

    if not text.strip():
        return []

    raw_tokens = [token.strip() for token in re.split(r"[,;\s]+", text) if token.strip()]
    expanded_targets: list[str] = []

    for token in raw_tokens:
        partial_ipv4_targets = _expand_partial_ipv4_target(token)
        if partial_ipv4_targets is not None:
            expanded_targets.extend(partial_ipv4_targets)
            continue

        if "/" not in token:
            expanded_targets.append(token)
            continue

        network = ipaddress.ip_network(token, strict=False)
        if network.version != 4:
            raise ValueError(f"IPv6 targets are not supported yet: {token}")
        if network.num_addresses > MAX_UNICAST_TARGETS:
            raise ValueError(
                f"Target range {token} is too large. Limit ranges to {MAX_UNICAST_TARGETS} addresses."
            )

        for address in network:
            expanded_targets.append(str(address))

    return list(dict.fromkeys(expanded_targets))


def _normalize_auto_subnet(interface: ipaddress.IPv4Interface) -> ipaddress.IPv4Network:
    prefix_length = max(interface.network.prefixlen, DEFAULT_AUTO_SUBNET_PREFIX)
    return ipaddress.ip_network(f"{interface.ip}/{prefix_length}", strict=False)


def _fallback_local_ipv4_interfaces() -> list[ipaddress.IPv4Interface]:
    interfaces: list[ipaddress.IPv4Interface] = []
    try:
        address_info = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except socket.gaierror:
        return []

    for family, _socktype, _proto, _canonname, sockaddr in address_info:
        if family != socket.AF_INET or not sockaddr:
            continue

        host = str(sockaddr[0])
        if host.startswith("127.") or host.startswith("169.254."):
            continue

        try:
            interfaces.append(
                ipaddress.ip_interface(f"{host}/{DEFAULT_AUTO_SUBNET_PREFIX}")
            )
        except ValueError:
            continue

    return interfaces


def _windows_local_ipv4_interfaces() -> list[ipaddress.IPv4Interface]:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-NetIPAddress -AddressFamily IPv4 | "
            "Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*' } | "
            "Select-Object InterfaceAlias, IPAddress, PrefixLength | ConvertTo-Json -Compress"
        ),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return []

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []

    if isinstance(payload, dict):
        records = [payload]
    elif isinstance(payload, list):
        records = payload
    else:
        return []

    physical_interfaces: list[ipaddress.IPv4Interface] = []
    fallback_interfaces: list[ipaddress.IPv4Interface] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        interface_alias = str(record.get("InterfaceAlias", "")).strip()
        ip_address = str(record.get("IPAddress", "")).strip()
        prefix_length = record.get("PrefixLength")
        if not ip_address:
            continue
        try:
            prefix_value = int(prefix_length)
            interface = ipaddress.ip_interface(f"{ip_address}/{prefix_value}")
        except (TypeError, ValueError):
            continue

        fallback_interfaces.append(interface)
        if not _is_virtual_interface_alias(interface_alias):
            physical_interfaces.append(interface)

    return physical_interfaces or fallback_interfaces


def local_discovery_networks() -> list[ipaddress.IPv4Network]:
    """Return local IPv4 networks suitable for automatic camera discovery."""

    if platform.system() == "Windows":
        interfaces = _windows_local_ipv4_interfaces()
    else:
        interfaces = []

    if not interfaces:
        interfaces = _fallback_local_ipv4_interfaces()

    normalized_networks = [
        _normalize_auto_subnet(interface)
        for interface in interfaces
        if interface.version == 4 and not interface.ip.is_loopback and not interface.ip.is_link_local
    ]

    return sorted(
        {
            network
            for network in normalized_networks
            if network.prefixlen >= DEFAULT_AUTO_SUBNET_PREFIX
        },
        key=lambda item: (int(item.network_address), item.prefixlen),
    )


def default_discovery_targets(preferred_host: str = "") -> tuple[list[str], list[str]]:
    """Expand local IPv4 subnets into direct WS-Discovery targets."""

    networks = local_discovery_networks()
    if preferred_host:
        try:
            preferred_ip = ipaddress.ip_address(preferred_host.strip())
        except ValueError:
            preferred_ip = None
        if isinstance(preferred_ip, ipaddress.IPv4Address):
            matching_networks = [network for network in networks if preferred_ip in network]
            if matching_networks:
                networks = matching_networks

    targets: list[str] = []
    for network in networks:
        for address in network:
            targets.append(str(address))
    return list(dict.fromkeys(targets)), [str(network) for network in networks]


def _candidate_xaddrs(text: str) -> list[str]:
    return [candidate.strip() for candidate in text.split() if candidate.strip()]


def _parse_xaddr(xaddr: str) -> tuple[Optional[str], Optional[int]]:
    parsed = urlparse(xaddr)
    if not parsed.hostname:
        return None, None
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.hostname, parsed.port or default_port


def _preferred_xaddr(xaddrs: list[str], sender_host: str) -> Optional[str]:
    if not xaddrs:
        return None
    for candidate in xaddrs:
        host, _port = _parse_xaddr(candidate)
        if host == sender_host:
            return candidate
    return xaddrs[0]


def _parse_probe_match(payload: bytes, sender_host: str) -> Optional[DiscoveredCameraInfo]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None

    xaddrs_element = root.find(".//d:XAddrs", DISCOVERY_NAMESPACES)
    if xaddrs_element is None or not (xaddrs_element.text or "").strip():
        return None

    xaddr = _preferred_xaddr(_candidate_xaddrs(xaddrs_element.text or ""), sender_host)
    if not xaddr:
        return None

    host, port = _parse_xaddr(xaddr)
    if not host or port is None:
        return None

    endpoint_reference = (
        root.findtext(".//wsa:Address", default="", namespaces=DISCOVERY_NAMESPACES)
        or ""
    ).strip()
    scopes_text = (
        root.findtext(".//d:Scopes", default="", namespaces=DISCOVERY_NAMESPACES)
        or ""
    ).strip()
    types_text = (
        root.findtext(".//d:Types", default="", namespaces=DISCOVERY_NAMESPACES)
        or ""
    ).strip()

    return DiscoveredCameraInfo(
        host=host,
        port=port,
        xaddr=xaddr,
        endpoint_reference=endpoint_reference,
        scopes=[item for item in scopes_text.split() if item],
        types=[item for item in types_text.split() if item],
    )


def _store_discovery_entry(
    discovered: dict[str, DiscoveredCameraInfo],
    entry: DiscoveredCameraInfo,
) -> None:
    existing = discovered.get(entry.camera_id)
    if existing is None:
        discovered[entry.camera_id] = entry
        return

    if not existing.endpoint_reference and entry.endpoint_reference:
        existing.endpoint_reference = entry.endpoint_reference
    if len(entry.scopes) > len(existing.scopes):
        existing.scopes = entry.scopes
    if len(entry.types) > len(existing.types):
        existing.types = entry.types
    if not existing.xaddr and entry.xaddr:
        existing.xaddr = entry.xaddr


def _receive_discovery_responses(
    sock: socket.socket,
    discovered: dict[str, DiscoveredCameraInfo],
    idle_timeout_seconds: float,
) -> None:
    if idle_timeout_seconds <= 0:
        return

    deadline = time.monotonic() + idle_timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return

        sock.settimeout(min(RECEIVE_TIMEOUT_SECONDS, remaining))
        try:
            payload, sender = sock.recvfrom(MAX_RESPONSE_SIZE)
        except socket.timeout:
            return
        except OSError:
            return

        sender_host = str(sender[0]) if sender else ""
        entry = _parse_probe_match(payload, sender_host)
        if entry is not None and entry.is_camera:
            _store_discovery_entry(discovered, entry)
        deadline = time.monotonic() + idle_timeout_seconds


def discover_onvif_cameras(
    timeout_seconds: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
    targets: Optional[Iterable[str]] = None,
) -> list[DiscoveredCameraInfo]:
    """Broadcast a WS-Discovery probe and collect ONVIF device responses."""

    discovered: dict[str, DiscoveredCameraInfo] = {}
    started_at = time.monotonic()
    target_list = [str(target).strip() for target in (targets or []) if str(target).strip()]

    with _create_socket() as sock:
        probe_message = _probe_message()
        if target_list:
            sent_count = 0
            invalid_targets: list[str] = []
            for start_index in range(0, len(target_list), UNICAST_BATCH_SIZE):
                batch = target_list[start_index:start_index + UNICAST_BATCH_SIZE]
                for target in batch:
                    try:
                        sock.sendto(probe_message, (target, WS_DISCOVERY_ADDRESS[1]))
                        sent_count += 1
                    except OSError:
                        invalid_targets.append(target)

                _receive_discovery_responses(
                    sock,
                    discovered,
                    UNICAST_BATCH_IDLE_SECONDS,
                )

            if sent_count == 0 and invalid_targets:
                invalid_summary = ", ".join(invalid_targets[:3])
                if len(invalid_targets) > 3:
                    invalid_summary = f"{invalid_summary} ..."
                raise ValueError(f"Could not resolve target address: {invalid_summary}")
            _receive_discovery_responses(sock, discovered, timeout_seconds)
        else:
            sock.sendto(probe_message, WS_DISCOVERY_ADDRESS)
            while time.monotonic() - started_at < timeout_seconds:
                _receive_discovery_responses(sock, discovered, RECEIVE_TIMEOUT_SECONDS)

    return sorted(
        discovered.values(),
        key=lambda item: (item.name.casefold() if item.name else item.host.casefold(), item.port),
    )