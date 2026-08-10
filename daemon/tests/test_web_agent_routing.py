from __future__ import annotations

import pytest

from pilot.actions import ActionType
from pilot.agents.base_agent import AgentRole
from pilot.agents.orchestrator import AgentOrchestrator
from pilot.agents.web_agent import WEB_ACTION_TYPES, WebAgent


def _web_agent() -> WebAgent:
    return WebAgent(model_router=None, executor=object())  # type: ignore[arg-type]


def test_web_agent_advertises_every_action_it_can_handle() -> None:
    agent = _web_agent()

    advertised = {capability.action_type for capability in agent.get_capabilities()}

    assert advertised == WEB_ACTION_TYPES
    assert ActionType.BROWSER_CLICK_TEXT in advertised


@pytest.mark.asyncio
async def test_orchestrator_routes_every_browser_action_to_web_agent() -> None:
    orchestrator = AgentOrchestrator(model_router=None)  # type: ignore[arg-type]
    try:
        orchestrator.register_agent(_web_agent())

        for action_type in WEB_ACTION_TYPES:
            assert orchestrator._action_registry[action_type] == AgentRole.WEB
    finally:
        await orchestrator.stop()
