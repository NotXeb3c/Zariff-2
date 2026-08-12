"""GitHub-backed plugin marketplace catalog and installer.

The official catalog is read from the public ``main`` branch, so merging an
approved plugin pull request publishes it without coupling plugin releases to
desktop-app releases. A bundled copy remains available for offline use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from pilot.plugins.capabilities import (
    PluginCapabilityError,
    parse_plugin_capabilities,
)

MARKETPLACE_SCHEMA_VERSION = 1
DEFAULT_MARKETPLACE_REGISTRY_URL = (
    "https://raw.githubusercontent.com/NotXeb3c/zariff-2/main/plugins/registry.json"
)
MARKETPLACE_REGISTRY_ENV = "ZARIFF_MARKETPLACE_REGISTRY_URL"
MAX_CATALOG_BYTES = 1_000_000
MAX_PLUGIN_FILE_BYTES = 5_000_000
MAX_PLUGIN_FILES = 64
PLUGIN_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SIGNATURE_METADATA_FILES = frozenset({"plugin.ed25519.pub", "plugin.ed25519.sig"})
CANONICAL_TEXT_SUFFIXES = frozenset({".json", ".py"})


class MarketplaceError(ValueError):
    """Raised when marketplace metadata or a plugin package is unsafe."""


@dataclass(frozen=True)
class MarketplaceCatalog:
    """A validated marketplace catalog plus its provenance."""

    data: dict[str, Any]
    source: str
    registry_url: str
    warning: str = ""


def validate_plugin_name(name: str) -> str:
    """Return a canonical safe plugin slug or raise MarketplaceError."""

    normalized = name.strip()
    if normalized != name or not PLUGIN_NAME_PATTERN.fullmatch(normalized):
        raise MarketplaceError(
            "Plugin names must use 1-64 lowercase letters, numbers, or single hyphens "
            "and cannot start or end with a hyphen"
        )
    return normalized


def _safe_package_path(raw_path: str) -> str:
    """Validate a package-relative POSIX path."""

    path = PurePosixPath(raw_path)
    if (
        not raw_path
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in raw_path
        or path.name in SIGNATURE_METADATA_FILES
    ):
        raise MarketplaceError(f"Unsafe marketplace package path: {raw_path!r}")
    return path.as_posix()


def canonicalize_package_payload(file_path: str, payload: bytes) -> bytes:
    """Return stable package bytes across Git newline conversion.

    Git can check text files out with CRLF on Windows even though GitHub's raw
    endpoint serves the repository blob with LF. Marketplace hashes are defined
    over canonical LF bytes so the same reviewed Python or JSON file verifies
    on every supported operating system.
    """

    suffix = PurePosixPath(file_path).suffix.lower()
    if suffix in CANONICAL_TEXT_SUFFIXES:
        return payload.replace(b"\r\n", b"\n")
    return payload


def package_sha256(file_path: str, payload: bytes) -> str:
    """Return the cross-platform marketplace digest for one package file."""

    canonical_payload = canonicalize_package_payload(file_path, payload)
    return hashlib.sha256(canonical_payload).hexdigest()


def validate_catalog(data: Any) -> dict[str, Any]:
    """Validate the public catalog schema and return it unchanged."""

    if not isinstance(data, dict):
        raise MarketplaceError("Marketplace catalog must be a JSON object")
    if data.get("schema_version") != MARKETPLACE_SCHEMA_VERSION:
        raise MarketplaceError(f"Unsupported marketplace schema version: {data.get('schema_version')!r}")

    repository = data.get("repository")
    ref = data.get("ref")
    plugins = data.get("plugins")
    if not isinstance(repository, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise MarketplaceError("Marketplace repository must be in owner/repository form")
    if not isinstance(ref, str) or not ref or any(char.isspace() for char in ref):
        raise MarketplaceError("Marketplace ref must be a non-empty Git ref")
    if not isinstance(plugins, list):
        raise MarketplaceError("Marketplace plugins must be a list")

    seen_plugins: set[str] = set()
    seen_tools: set[str] = set()
    for plugin in plugins:
        if not isinstance(plugin, dict):
            raise MarketplaceError("Each marketplace plugin must be an object")
        name = validate_plugin_name(str(plugin.get("name", "")))
        if name in seen_plugins:
            raise MarketplaceError(f"Duplicate marketplace plugin: {name}")
        seen_plugins.add(name)

        for field_name in ("version", "description", "author"):
            if not isinstance(plugin.get(field_name), str) or not plugin[field_name].strip():
                raise MarketplaceError(f"Plugin {name!r} requires {field_name}")
        try:
            parse_plugin_capabilities(plugin.get("capabilities"))
        except PluginCapabilityError as exc:
            raise MarketplaceError(f"Plugin {name!r} has unsafe capabilities: {exc}") from exc

        tools = plugin.get("tools")
        if not isinstance(tools, list) or not tools:
            raise MarketplaceError(f"Plugin {name!r} must expose at least one tool")
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise MarketplaceError(f"Plugin {name!r} has an invalid tool")
            tool_name = tool["name"].strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool_name):
                raise MarketplaceError(f"Plugin {name!r} has unsafe tool name: {tool_name!r}")
            if tool_name in seen_tools:
                raise MarketplaceError(f"Duplicate marketplace tool name: {tool_name}")
            seen_tools.add(tool_name)

        package = plugin.get("package")
        if not isinstance(package, dict):
            raise MarketplaceError(f"Plugin {name!r} requires package metadata")
        package_path = _safe_package_path(str(package.get("path", "")))
        if package_path != f"plugins/{name}":
            raise MarketplaceError(f"Plugin {name!r} package path must be exactly 'plugins/{name}'")
        files = package.get("files")
        if not isinstance(files, list) or not files or len(files) > MAX_PLUGIN_FILES:
            raise MarketplaceError(f"Plugin {name!r} must contain 1-{MAX_PLUGIN_FILES} package files")
        file_paths: set[str] = set()
        for file_entry in files:
            if not isinstance(file_entry, dict):
                raise MarketplaceError(f"Plugin {name!r} has invalid file metadata")
            file_path = _safe_package_path(str(file_entry.get("path", "")))
            digest = str(file_entry.get("sha256", "")).lower()
            if file_path in file_paths:
                raise MarketplaceError(f"Plugin {name!r} repeats file {file_path!r}")
            if not SHA256_PATTERN.fullmatch(digest):
                raise MarketplaceError(f"Plugin {name!r} file {file_path!r} requires a lowercase SHA-256")
            file_paths.add(file_path)
        if "manifest.json" not in file_paths:
            raise MarketplaceError(f"Plugin {name!r} package is missing manifest.json")

    submission_url = data.get("submission_url", "")
    if submission_url:
        parsed_submission = urlparse(str(submission_url))
        if parsed_submission.scheme != "https" or parsed_submission.netloc != "github.com":
            raise MarketplaceError("Marketplace submission_url must be an HTTPS GitHub URL")
    return data


class GitHubMarketplace:
    """Fetch the public catalog and install approved packages safely."""

    def __init__(
        self,
        *,
        repo_root: Path,
        plugins_dir: Path,
        registry_url: str | None = None,
        bundled_catalog_dir: Path | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.plugins_dir = plugins_dir
        repository_catalog = repo_root / "plugins"
        packaged_catalog = bundled_catalog_dir or Path(__file__).parent / "marketplace_catalog"
        self.local_catalog_dir = (
            repository_catalog if (repository_catalog / "registry.json").is_file() else packaged_catalog
        )
        self.local_registry_path = self.local_catalog_dir / "registry.json"
        self.registry_url = (
            registry_url or os.environ.get(MARKETPLACE_REGISTRY_ENV, "").strip() or DEFAULT_MARKETPLACE_REGISTRY_URL
        )

    def load_catalog(self) -> MarketplaceCatalog:
        """Load the GitHub catalog, falling back to the bundled snapshot."""

        try:
            data = self._fetch_json(self.registry_url)
            return MarketplaceCatalog(
                data=validate_catalog(data),
                source="github",
                registry_url=self.registry_url,
            )
        except Exception as exc:
            local_data = json.loads(self.local_registry_path.read_text(encoding="utf-8"))
            return MarketplaceCatalog(
                data=validate_catalog(local_data),
                source="bundled",
                registry_url=self.registry_url,
                warning=f"Using bundled marketplace catalog: {exc}",
            )

    def install(self, plugin_name: str) -> dict[str, Any]:
        """Install one exact approved catalog entry."""

        name = validate_plugin_name(plugin_name)
        catalog = self.load_catalog()
        plugin = next(
            (item for item in catalog.data["plugins"] if item.get("name") == name),
            None,
        )
        if plugin is None:
            raise MarketplaceError(f"Plugin is not in the approved marketplace: {name}")

        target = self.plugins_dir / name
        if target.exists():
            raise MarketplaceError(f"Plugin is already installed: {name}")
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

        temp_parent = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=str(self.plugins_dir)))
        staging = temp_parent / name
        staging.mkdir()
        try:
            self._materialize_package(catalog, plugin, staging)
            manifest = self._validate_staged_plugin(
                staging,
                expected_name=name,
                expected_capabilities=plugin["capabilities"],
            )

            from pilot.plugins import sign_plugin_directory

            sign_plugin_directory(staging)
            staging.replace(target)
            return {
                "success": True,
                "plugin": name,
                "version": manifest.get("version", plugin["version"]),
                "source": catalog.source,
                "path": str(target),
            }
        finally:
            shutil.rmtree(temp_parent, ignore_errors=True)

    def _materialize_package(
        self,
        catalog: MarketplaceCatalog,
        plugin: dict[str, Any],
        staging: Path,
    ) -> None:
        package = plugin["package"]
        if catalog.source == "bundled":
            package_parts = PurePosixPath(package["path"]).parts
            source_root = self.local_catalog_dir.joinpath(*package_parts[1:])
            for file_entry in package["files"]:
                source = source_root / Path(file_entry["path"])
                if not source.is_file():
                    raise MarketplaceError(f"Bundled plugin file is missing: {source}")
                payload = source.read_bytes()
                self._write_verified_file(staging, file_entry, payload)
            return

        repository = catalog.data["repository"]
        ref = quote(catalog.data["ref"], safe="/._-")
        package_path = package["path"]
        base_url = f"https://raw.githubusercontent.com/{repository}/{ref}/{package_path}"
        for file_entry in package["files"]:
            file_path = file_entry["path"]
            url = f"{base_url}/{quote(file_path, safe='/._-')}"
            payload = self._fetch_bytes(url, MAX_PLUGIN_FILE_BYTES)
            self._write_verified_file(staging, file_entry, payload)

    @staticmethod
    def _write_verified_file(
        staging: Path,
        file_entry: dict[str, str],
        payload: bytes,
    ) -> None:
        file_path = _safe_package_path(file_entry["path"])
        if len(payload) > MAX_PLUGIN_FILE_BYTES:
            raise MarketplaceError(f"Marketplace file is too large: {file_path}")
        canonical_payload = canonicalize_package_payload(file_path, payload)
        actual_hash = package_sha256(file_path, canonical_payload)
        if actual_hash != file_entry["sha256"]:
            raise MarketplaceError(f"SHA-256 mismatch for marketplace file: {file_path}")
        destination = staging.joinpath(*PurePosixPath(file_path).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(canonical_payload)

    @staticmethod
    def _validate_staged_plugin(
        staging: Path,
        *,
        expected_name: str,
        expected_capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        manifest_path = staging / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MarketplaceError(f"Plugin manifest is invalid: {exc}") from exc
        if manifest.get("name") != expected_name:
            raise MarketplaceError("Plugin manifest name does not match the catalog")
        entry_point = _safe_package_path(str(manifest.get("entry_point", "plugin.py")))
        if not (staging / entry_point).is_file():
            raise MarketplaceError(f"Plugin entry point is missing: {entry_point}")
        if manifest.get("runtime_type", "python") != "python":
            raise MarketplaceError("GitHub marketplace MVP currently supports Python plugins")
        try:
            manifest_capabilities = parse_plugin_capabilities(manifest.get("capabilities"))
            catalog_capabilities = parse_plugin_capabilities(expected_capabilities)
        except PluginCapabilityError as exc:
            raise MarketplaceError(f"Plugin manifest has unsafe capabilities: {exc}") from exc
        if manifest_capabilities != catalog_capabilities:
            raise MarketplaceError("Plugin manifest capabilities do not match the reviewed catalog")
        return manifest

    @staticmethod
    def _fetch_json(url: str) -> Any:
        return json.loads(GitHubMarketplace._fetch_bytes(url, MAX_CATALOG_BYTES))

    @staticmethod
    def _fetch_bytes(url: str, max_bytes: int) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc not in {
            "raw.githubusercontent.com",
            "github.com",
        }:
            raise MarketplaceError(f"Marketplace URL is not an approved HTTPS GitHub URL: {url}")
        request = Request(url, headers={"User-Agent": "Zariff-Marketplace/1"})
        with urlopen(request, timeout=5) as response:
            payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise MarketplaceError(f"Marketplace response exceeded {max_bytes} bytes")
        return payload
