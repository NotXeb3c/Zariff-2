"""Tests for the moderated GitHub plugin marketplace."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pilot.plugins import PluginManifest, PluginRegistry, PluginTool, verify_plugin_signature
from pilot.plugins.capabilities import PluginCapabilities
from pilot.plugins.marketplace import (
    GitHubMarketplace,
    MarketplaceError,
    validate_catalog,
    validate_plugin_name,
)
from pilot.server import PilotServer

REPO_ROOT = Path(__file__).parents[2]
PACKAGED_CATALOG = REPO_ROOT / "daemon" / "pilot" / "plugins" / "marketplace_catalog"


def _offline_marketplace(tmp_path: Path, monkeypatch) -> GitHubMarketplace:
    marketplace = GitHubMarketplace(
        repo_root=REPO_ROOT,
        plugins_dir=tmp_path / "installed",
    )

    def fail_remote(_url: str):
        raise OSError("offline")

    monkeypatch.setattr(marketplace, "_fetch_json", fail_remote)
    return marketplace


@pytest.mark.parametrize(
    "name",
    ["../escape", "Uppercase", "ends-", "-starts", "has space", "a" * 65],
)
def test_validate_plugin_name_rejects_unsafe_slugs(name):
    with pytest.raises(MarketplaceError):
        validate_plugin_name(name)


def test_validate_catalog_rejects_path_traversal():
    catalog = json.loads((REPO_ROOT / "plugins" / "registry.json").read_text(encoding="utf-8"))
    catalog["plugins"][0]["package"]["files"][0]["path"] = "../manifest.json"

    with pytest.raises(MarketplaceError, match="Unsafe marketplace package path"):
        validate_catalog(catalog)


def test_validate_catalog_requires_complete_capabilities():
    catalog = json.loads((REPO_ROOT / "plugins" / "registry.json").read_text(encoding="utf-8"))
    del catalog["plugins"][0]["capabilities"]["network_domains"]

    with pytest.raises(MarketplaceError, match="unsafe capabilities"):
        validate_catalog(catalog)


def test_staged_manifest_capabilities_must_match_reviewed_catalog(tmp_path):
    staging = tmp_path / "weather"
    staging.mkdir()
    manifest = json.loads((REPO_ROOT / "plugins" / "weather" / "manifest.json").read_text(encoding="utf-8"))
    manifest["capabilities"]["network_domains"] = ["other.example"]
    (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (staging / "plugin.py").write_text(
        "def handle_tool(tool_name, params):\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    expected = json.loads((REPO_ROOT / "plugins" / "weather" / "manifest.json").read_text(encoding="utf-8"))[
        "capabilities"
    ]

    with pytest.raises(MarketplaceError, match="do not match"):
        GitHubMarketplace._validate_staged_plugin(
            staging,
            expected_name="weather",
            expected_capabilities=expected,
        )


def test_offline_catalog_uses_bundled_snapshot(tmp_path, monkeypatch):
    marketplace = _offline_marketplace(tmp_path, monkeypatch)

    catalog = marketplace.load_catalog()

    assert catalog.source == "bundled"
    assert "offline" in catalog.warning
    assert {plugin["name"] for plugin in catalog.data["plugins"]} == {
        "home-assistant",
        "spotify-control",
        "weather",
    }


def test_packaged_offline_snapshot_matches_public_marketplace():
    public_catalog = REPO_ROOT / "plugins"
    assert (PACKAGED_CATALOG / "registry.json").read_text(encoding="utf-8") == (
        public_catalog / "registry.json"
    ).read_text(encoding="utf-8")

    registry = json.loads((public_catalog / "registry.json").read_text(encoding="utf-8"))
    for plugin in registry["plugins"]:
        name = plugin["name"]
        for file_entry in plugin["package"]["files"]:
            relative_path = Path(file_entry["path"])
            assert (PACKAGED_CATALOG / name / relative_path).read_text(encoding="utf-8") == (
                public_catalog / name / relative_path
            ).read_text(encoding="utf-8")


def test_marketplace_validator_runs_from_clean_checkout():
    result = subprocess.run(
        [sys.executable, "-I", str(REPO_ROOT / "scripts" / "validate_marketplace.py")],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Marketplace valid:" in result.stdout


def test_install_accepts_only_approved_verified_package(tmp_path, monkeypatch):
    marketplace = _offline_marketplace(tmp_path, monkeypatch)

    result = marketplace.install("weather")
    installed = tmp_path / "installed" / "weather"

    assert result["success"] is True
    assert result["source"] == "bundled"
    verify_plugin_signature(installed)

    registry = PluginRegistry(plugin_dirs=[tmp_path / "installed"])
    registry.discover()
    assert registry.find_tool("get_weather") is not None

    with pytest.raises(MarketplaceError, match="not in the approved marketplace"):
        marketplace.install("unreviewed-plugin")


def test_installed_wheel_layout_supports_offline_catalog_and_install(tmp_path, monkeypatch):
    bundled_catalog = tmp_path / "site-packages" / "pilot" / "plugins" / "marketplace_catalog"
    shutil.copytree(REPO_ROOT / "plugins", bundled_catalog)
    marketplace = GitHubMarketplace(
        repo_root=tmp_path / "python-runtime",
        plugins_dir=tmp_path / "installed",
        bundled_catalog_dir=bundled_catalog,
    )

    def fail_remote(_url: str):
        raise OSError("offline")

    monkeypatch.setattr(marketplace, "_fetch_json", fail_remote)

    catalog = marketplace.load_catalog()
    result = marketplace.install("weather")

    assert catalog.source == "bundled"
    assert result["success"] is True
    assert result["source"] == "bundled"
    assert (tmp_path / "installed" / "weather" / "plugin.py").is_file()


def test_hash_mismatch_does_not_leave_partial_install(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "plugins", repo_root / "plugins")
    (repo_root / "plugins" / "weather" / "plugin.py").write_text(
        "def handle_tool(tool_name, params):\n    return {'tampered': True}\n",
        encoding="utf-8",
    )
    marketplace = GitHubMarketplace(
        repo_root=repo_root,
        plugins_dir=tmp_path / "installed",
    )

    def fail_remote(_url: str):
        raise OSError("offline")

    monkeypatch.setattr(marketplace, "_fetch_json", fail_remote)

    with pytest.raises(MarketplaceError, match="SHA-256 mismatch"):
        marketplace.install("weather")
    assert not (tmp_path / "installed" / "weather").exists()


def test_install_normalizes_windows_newlines_before_hash_verification(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "plugins", repo_root / "plugins")
    package_dir = repo_root / "plugins" / "weather"
    for file_name in ("manifest.json", "plugin.py"):
        file_path = package_dir / file_name
        payload = file_path.read_bytes().replace(b"\r\n", b"\n")
        file_path.write_bytes(payload.replace(b"\n", b"\r\n"))

    marketplace = GitHubMarketplace(
        repo_root=repo_root,
        plugins_dir=tmp_path / "installed",
    )

    def fail_remote(_url: str):
        raise OSError("offline")

    monkeypatch.setattr(marketplace, "_fetch_json", fail_remote)

    result = marketplace.install("weather")
    installed = tmp_path / "installed" / "weather"

    assert result["success"] is True
    assert b"\r\n" not in (installed / "manifest.json").read_bytes()
    assert b"\r\n" not in (installed / "plugin.py").read_bytes()
    verify_plugin_signature(installed)


def test_discover_rebuilds_indexes_after_plugin_removal(tmp_path):
    plugin_root = tmp_path / "plugins"
    plugin_dir = plugin_root / "temporary"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "temporary",
                "version": "1.0.0",
                "description": "Temporary test plugin",
                "author": "tests",
                "entry_point": "plugin.py",
                "runtime_type": "python",
                "capabilities": PluginCapabilities().to_dict(),
                "tools": [
                    {
                        "name": "temporary_tool",
                        "description": "Temporary tool",
                        "inputs": [],
                        "outputs": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        "def handle_tool(tool_name, params):\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    registry = PluginRegistry(plugin_dirs=[plugin_root], require_signatures=False)

    registry.discover()
    assert registry.find_tool("temporary_tool") is not None
    shutil.rmtree(plugin_dir)
    registry.discover()

    assert registry.find_tool("temporary_tool") is None
    assert registry.call_tool("temporary_tool", {}) == {"error": "Tool not found: temporary_tool"}


def test_planner_tool_block_explains_plugin_call_shape(tmp_path):
    registry = PluginRegistry(plugin_dirs=[tmp_path], require_signatures=False)
    registry._plugins["sample"] = PluginManifest(
        name="sample",
        version="1.0.0",
        description="Sample",
        author="tests",
        tools=[
            PluginTool(
                name="sample_tool",
                description="Run a sample",
                inputs=["value"],
            )
        ],
    )
    block = registry.get_tools_for_planner()

    assert "action_type plugin_call" in block
    assert '"tool": "<tool name>"' in block
    assert "sample_tool" in block
    assert "require user confirmation" in block


@pytest.mark.asyncio
async def test_marketplace_handlers_install_list_and_uninstall(
    tmp_path,
    monkeypatch,
):
    from pilot import server as server_module

    plugins_dir = tmp_path / "installed"
    marketplace = _offline_marketplace(tmp_path, monkeypatch)
    registry = PluginRegistry(plugin_dirs=[plugins_dir])
    planner = SimpleNamespace(
        contexts=[],
        set_plugin_context=lambda context: planner.contexts.append(context),
    )
    server = object.__new__(PilotServer)
    server._plugin_marketplace = marketplace
    server._plugin_registry = registry
    server._planner = planner
    monkeypatch.setattr(server_module, "PLUGINS_DIR", plugins_dir)

    installed = await server._handle_plugin_install(
        {"plugin_name": "weather"},
        None,
    )
    listed = await server._handle_plugin_market_list({}, None)
    removed = await server._handle_plugin_uninstall(
        {"plugin_name": "weather"},
        None,
    )

    assert installed["success"] is True
    weather = next(plugin for plugin in listed["plugins"] if plugin["name"] == "weather")
    assert weather["installed"] is True
    assert weather["capabilities"]["network_domains"] == ["wttr.in"]
    assert weather["capabilities"]["data_retention"] == {
        "mode": "none",
        "max_days": 0,
    }
    assert listed["source"] == "bundled"
    assert removed == {"success": True, "plugin": "weather"}
    assert registry.find_tool("get_weather") is None
    assert planner.contexts
