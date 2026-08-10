"""Tests for fail-closed plugin capabilities and native isolation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pilot.plugins import PluginRegistry
from pilot.plugins.capabilities import (
    PluginCapabilities,
    PluginCapabilityError,
    parse_plugin_capabilities,
    validate_credential_urls,
)


def _capabilities(**overrides: object) -> dict:
    capabilities = PluginCapabilities().to_dict()
    capabilities.update(overrides)
    return capabilities


def _write_plugin(
    root: Path,
    *,
    code: str,
    capabilities: dict | None = None,
    destructive: bool = False,
) -> PluginRegistry:
    plugin_dir = root / "isolated"
    plugin_dir.mkdir(parents=True)
    grants = capabilities or _capabilities()
    grants["destructive_actions"] = destructive
    (plugin_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "isolated",
                "version": "1.0.0",
                "description": "Isolation test",
                "author": "tests",
                "entry_point": "plugin.py",
                "runtime_type": "python",
                "tools": [
                    {
                        "name": "isolated_tool",
                        "description": "Run isolation test",
                        "inputs": ["path"],
                        "outputs": ["result"],
                    }
                ],
                "capabilities": grants,
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(code, encoding="utf-8")
    registry = PluginRegistry(plugin_dirs=[root], require_signatures=False)
    assert registry.discover() == 1
    return registry


def test_manifest_without_capabilities_is_rejected(tmp_path: Path):
    plugin_dir = tmp_path / "missing"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(
        json.dumps({"name": "missing", "tools": []}),
        encoding="utf-8",
    )
    registry = PluginRegistry(plugin_dirs=[tmp_path], require_signatures=False)

    assert registry.discover() == 0


def test_capability_parser_rejects_wildcards_and_unknown_fields():
    wildcard = _capabilities(network_domains=["*.example.com"])
    with pytest.raises(PluginCapabilityError, match="wildcard"):
        parse_plugin_capabilities(wildcard)

    unknown = _capabilities(unreviewed=True)
    with pytest.raises(PluginCapabilityError, match="unknown fields"):
        parse_plugin_capabilities(unknown)


def test_url_credential_must_match_declared_domain():
    capabilities = parse_plugin_capabilities(
        _capabilities(
            network_domains=["homeassistant.local"],
            credentials=["HA_URL"],
        )
    )
    with pytest.raises(PluginCapabilityError, match="undeclared network domain"):
        validate_credential_urls(capabilities, {"HA_URL": "https://other.example"})


def test_native_plugin_runs_outside_daemon_and_receives_only_declared_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PLUGIN_ALLOWED_TOKEN", "allowed")
    monkeypatch.setenv("PLUGIN_SECRET_NOT_DECLARED", "hidden")
    registry = _write_plugin(
        tmp_path,
        capabilities=_capabilities(credentials=["PLUGIN_ALLOWED_TOKEN"]),
        code=(
            "import os\n"
            "def handle_tool(tool_name, params):\n"
            "    return {'pid': os.getpid(), "
            "'allowed': os.environ.get('PLUGIN_ALLOWED_TOKEN'), "
            "'hidden': os.environ.get('PLUGIN_SECRET_NOT_DECLARED')}\n"
        ),
    )

    result = registry.call_tool("isolated_tool", {})

    assert result["pid"] != os.getpid()
    assert result["allowed"] == "allowed"
    assert result["hidden"] is None


def test_native_broker_denies_undeclared_filesystem_read(tmp_path: Path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    registry = _write_plugin(
        tmp_path / "plugins",
        code=(
            "def handle_tool(tool_name, params):\n"
            "    with open(params['path'], encoding='utf-8') as handle:\n"
            "        return {'result': handle.read()}\n"
        ),
    )

    result = registry.call_tool("isolated_tool", {"path": str(secret)})

    assert "error" in result
    assert "filesystem read denied" in result["error"]


def test_native_broker_allows_declared_filesystem_read(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    source = shared / "allowed.txt"
    source.write_text("allowed", encoding="utf-8")
    registry = _write_plugin(
        tmp_path / "plugins",
        capabilities=_capabilities(
            filesystem={"read": [str(shared)], "write": []},
        ),
        code=(
            "def handle_tool(tool_name, params):\n"
            "    with open(params['path'], encoding='utf-8') as handle:\n"
            "        return {'result': handle.read()}\n"
        ),
    )

    assert registry.call_tool("isolated_tool", {"path": str(source)}) == {"result": "allowed"}


def test_native_broker_denies_undeclared_network_and_process(tmp_path: Path):
    network_registry = _write_plugin(
        tmp_path / "network",
        code=(
            "import socket\n"
            "def handle_tool(tool_name, params):\n"
            "    socket.getaddrinfo('example.com', 443)\n"
            "    return {'result': 'unexpected'}\n"
        ),
    )
    process_registry = _write_plugin(
        tmp_path / "process",
        code=(
            "import subprocess\n"
            "def handle_tool(tool_name, params):\n"
            "    subprocess.run(['git', '--version'], check=False)\n"
            "    return {'result': 'unexpected'}\n"
        ),
    )

    network_result = network_registry.call_tool("isolated_tool", {})
    process_result = process_registry.call_tool("isolated_tool", {})

    assert "network domain denied" in network_result["error"]
    assert "process denied" in process_result["error"]


def test_native_broker_awaits_async_tool_handler(tmp_path: Path):
    registry = _write_plugin(
        tmp_path,
        code=(
            "async def isolated_tool(path=''):\n"
            "    return {'result': 'awaited'}\n"
            "TOOL_HANDLERS = {'isolated_tool': isolated_tool}\n"
        ),
    )

    assert registry.call_tool("isolated_tool", {}) == {"result": "awaited"}


def test_destructive_plugin_requires_guarded_approval(tmp_path: Path):
    registry = _write_plugin(
        tmp_path,
        destructive=True,
        code=("def handle_tool(tool_name, params):\n    return {'result': 'approved'}\n"),
    )

    denied = registry.call_tool("isolated_tool", {})
    allowed = registry.call_tool("isolated_tool", {}, approved=True)

    assert "guarded planner approval flow" in denied["error"]
    assert allowed == {"result": "approved"}
