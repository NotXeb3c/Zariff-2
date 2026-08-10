"""Tests for PilotServer's narration_status/narration_config_update RPC
handlers."""

import pytest

from pilot.config import PilotConfig
from pilot.server import PilotServer


@pytest.mark.asyncio
async def test_status_reports_config_defaults():
    server = PilotServer(PilotConfig())
    result = await server._handle_narration_status({}, ws=None)
    assert result["enabled"] is False
    assert result["narrate_steps"] is True
    assert result["interrupt_on_risk"] is True
    assert result["proactive_review_enabled"] is True
    assert result["live_corrections_enabled"] is True
    assert result["follow_up_enabled"] is True
    assert result["max_auto_revisions"] == 2
    assert result["confirm_timeout_seconds"] == 120.0
    assert result["advisory_timeout_seconds"] == 5.0


@pytest.mark.asyncio
async def test_config_update_persists_enabled_toggle(monkeypatch):
    config = PilotConfig()
    monkeypatch.setattr(config, "save", lambda: None)
    server = PilotServer(config)

    result = await server._handle_narration_config_update({"enabled": True}, ws=None)
    assert result["status"] == "ok"
    assert result["enabled"] is True
    assert server.config.narration.enabled is True


@pytest.mark.asyncio
async def test_config_update_sets_sub_toggles_and_timeout(monkeypatch):
    config = PilotConfig()
    monkeypatch.setattr(config, "save", lambda: None)
    server = PilotServer(config)

    result = await server._handle_narration_config_update(
        {
            "narrate_steps": False,
            "interrupt_on_risk": False,
            "proactive_review_enabled": False,
            "live_corrections_enabled": False,
            "follow_up_enabled": False,
            "max_auto_revisions": 3,
            "confirm_timeout_seconds": 45.0,
            "advisory_timeout_seconds": 4.0,
        },
        ws=None,
    )
    assert result["status"] == "ok"
    assert result["narrate_steps"] is False
    assert result["interrupt_on_risk"] is False
    assert result["proactive_review_enabled"] is False
    assert result["live_corrections_enabled"] is False
    assert result["follow_up_enabled"] is False
    assert result["max_auto_revisions"] == 3
    assert result["confirm_timeout_seconds"] == 45.0
    assert result["advisory_timeout_seconds"] == 4.0


@pytest.mark.asyncio
async def test_status_reflects_updated_config(monkeypatch):
    config = PilotConfig()
    monkeypatch.setattr(config, "save", lambda: None)
    server = PilotServer(config)

    await server._handle_narration_config_update({"enabled": True}, ws=None)
    result = await server._handle_narration_status({}, ws=None)
    assert result["enabled"] is True
