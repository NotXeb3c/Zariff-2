"""Fail-closed capability declarations for Heliox plugins."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_CREDENTIAL_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_PROCESS_NAME = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
_DOMAIN_NAME = re.compile(
    r"^(?=.{1,253}$)(?:localhost|"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)$"
)
_RETENTION_MODES = frozenset({"none", "session", "persistent"})


class PluginCapabilityError(ValueError):
    """Raised when a plugin capability declaration is absent or unsafe."""


def _string_list(value: Any, *, field_name: str, pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PluginCapabilityError(f"{field_name} must be a list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise PluginCapabilityError(f"{field_name} entries must be non-empty trimmed strings")
        if "*" in item:
            raise PluginCapabilityError(f"{field_name} does not allow wildcard grants")
        if pattern is not None and not pattern.fullmatch(item):
            raise PluginCapabilityError(f"{field_name} contains an invalid entry: {item!r}")
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _filesystem_paths(value: Any, *, field_name: str) -> tuple[str, ...]:
    paths = _string_list(value, field_name=field_name)
    for raw_path in paths:
        if "\0" in raw_path:
            raise PluginCapabilityError(f"{field_name} contains a null byte")
        path = Path(raw_path).expanduser()
        if ".." in path.parts:
            raise PluginCapabilityError(f"{field_name} does not allow parent traversal: {raw_path!r}")
    return paths


@dataclass(frozen=True)
class FilesystemCapabilities:
    """Filesystem roots granted to a plugin."""

    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {"read": list(self.read), "write": list(self.write)}


@dataclass(frozen=True)
class ClipboardCapabilities:
    """Clipboard directions granted to a plugin."""

    read: bool = False
    write: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {"read": self.read, "write": self.write}


@dataclass(frozen=True)
class MediaCapabilities:
    """Camera and microphone grants."""

    camera: bool = False
    microphone: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {"camera": self.camera, "microphone": self.microphone}


@dataclass(frozen=True)
class RetentionCapabilities:
    """Plugin data-retention declaration."""

    mode: str = "none"
    max_days: int = 0

    def to_dict(self) -> dict[str, str | int]:
        return {"mode": self.mode, "max_days": self.max_days}


@dataclass(frozen=True)
class PluginCapabilities:
    """Complete, explicit, default-deny plugin authority."""

    filesystem: FilesystemCapabilities = field(default_factory=FilesystemCapabilities)
    network_domains: tuple[str, ...] = ()
    processes: tuple[str, ...] = ()
    credentials: tuple[str, ...] = ()
    clipboard: ClipboardCapabilities = field(default_factory=ClipboardCapabilities)
    media: MediaCapabilities = field(default_factory=MediaCapabilities)
    data_retention: RetentionCapabilities = field(default_factory=RetentionCapabilities)
    destructive_actions: bool = False

    @property
    def risk_labels(self) -> list[str]:
        """Return concise user-facing labels for non-empty grants."""

        labels: list[str] = []
        if self.filesystem.read:
            labels.append("filesystem read")
        if self.filesystem.write:
            labels.append("filesystem write")
        if self.network_domains:
            labels.append("network")
        if self.processes:
            labels.append("processes")
        if self.credentials:
            labels.append("credentials")
        if self.clipboard.read or self.clipboard.write:
            labels.append("clipboard")
        if self.media.camera:
            labels.append("camera")
        if self.media.microphone:
            labels.append("microphone")
        if self.data_retention.mode != "none":
            labels.append(f"{self.data_retention.mode} retention")
        if self.destructive_actions:
            labels.append("destructive actions")
        return labels

    def to_dict(self) -> dict[str, Any]:
        return {
            "filesystem": self.filesystem.to_dict(),
            "network_domains": list(self.network_domains),
            "processes": list(self.processes),
            "credentials": list(self.credentials),
            "clipboard": self.clipboard.to_dict(),
            "media": self.media.to_dict(),
            "data_retention": self.data_retention.to_dict(),
            "destructive_actions": self.destructive_actions,
        }


def parse_plugin_capabilities(value: Any) -> PluginCapabilities:
    """Parse a complete capability object or fail closed."""

    if not isinstance(value, dict):
        raise PluginCapabilityError("Plugin manifest requires a capabilities object")
    required = {
        "filesystem",
        "network_domains",
        "processes",
        "credentials",
        "clipboard",
        "media",
        "data_retention",
        "destructive_actions",
    }
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required)
    if missing:
        raise PluginCapabilityError(f"Plugin capabilities are missing: {', '.join(missing)}")
    if unknown:
        raise PluginCapabilityError(f"Plugin capabilities contain unknown fields: {', '.join(unknown)}")

    filesystem = value["filesystem"]
    if not isinstance(filesystem, dict) or set(filesystem) != {"read", "write"}:
        raise PluginCapabilityError("capabilities.filesystem must contain only read and write lists")

    clipboard = value["clipboard"]
    if not isinstance(clipboard, dict) or set(clipboard) != {"read", "write"}:
        raise PluginCapabilityError("capabilities.clipboard must contain only read and write booleans")
    if not all(isinstance(clipboard[key], bool) for key in ("read", "write")):
        raise PluginCapabilityError("capabilities.clipboard values must be booleans")

    media = value["media"]
    if not isinstance(media, dict) or set(media) != {"camera", "microphone"}:
        raise PluginCapabilityError("capabilities.media must contain only camera and microphone booleans")
    if not all(isinstance(media[key], bool) for key in ("camera", "microphone")):
        raise PluginCapabilityError("capabilities.media values must be booleans")

    retention = value["data_retention"]
    if not isinstance(retention, dict) or set(retention) != {"mode", "max_days"}:
        raise PluginCapabilityError("capabilities.data_retention must contain only mode and max_days")
    mode = retention["mode"]
    max_days = retention["max_days"]
    if mode not in _RETENTION_MODES:
        raise PluginCapabilityError(f"Unsupported data-retention mode: {mode!r}")
    if isinstance(max_days, bool) or not isinstance(max_days, int) or not 0 <= max_days <= 3650:
        raise PluginCapabilityError("capabilities.data_retention.max_days must be between 0 and 3650")
    if mode in {"none", "session"} and max_days != 0:
        raise PluginCapabilityError(f"{mode} retention must use max_days 0")
    if mode == "persistent" and max_days == 0:
        raise PluginCapabilityError("persistent retention requires a positive max_days")

    destructive = value["destructive_actions"]
    if not isinstance(destructive, bool):
        raise PluginCapabilityError("capabilities.destructive_actions must be a boolean")

    domains = _string_list(
        value["network_domains"],
        field_name="capabilities.network_domains",
    )
    normalized_domains: list[str] = []
    for domain in domains:
        candidate = domain.lower()
        if "://" in candidate or "/" in candidate or not _DOMAIN_NAME.fullmatch(candidate):
            raise PluginCapabilityError(f"Invalid network domain grant: {domain!r}")
        normalized_domains.append(candidate)

    return PluginCapabilities(
        filesystem=FilesystemCapabilities(
            read=_filesystem_paths(filesystem["read"], field_name="capabilities.filesystem.read"),
            write=_filesystem_paths(filesystem["write"], field_name="capabilities.filesystem.write"),
        ),
        network_domains=tuple(normalized_domains),
        processes=_string_list(
            value["processes"],
            field_name="capabilities.processes",
            pattern=_PROCESS_NAME,
        ),
        credentials=_string_list(
            value["credentials"],
            field_name="capabilities.credentials",
            pattern=_CREDENTIAL_NAME,
        ),
        clipboard=ClipboardCapabilities(read=clipboard["read"], write=clipboard["write"]),
        media=MediaCapabilities(camera=media["camera"], microphone=media["microphone"]),
        data_retention=RetentionCapabilities(mode=mode, max_days=max_days),
        destructive_actions=destructive,
    )


def validate_credential_urls(capabilities: PluginCapabilities, environment: dict[str, str]) -> None:
    """Reject URL-valued credentials that point outside declared domains."""

    allowed = set(capabilities.network_domains)
    for name in capabilities.credentials:
        value = environment.get(name, "").strip()
        if "://" not in value:
            continue
        hostname = (urlparse(value).hostname or "").lower()
        if hostname and hostname not in allowed:
            raise PluginCapabilityError(f"Credential {name} points to undeclared network domain {hostname!r}")
