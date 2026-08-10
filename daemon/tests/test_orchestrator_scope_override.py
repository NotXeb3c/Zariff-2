"""Tests that AgentOrchestrator.execute_plan threads scope_override through
to the specialist agent's handle_task() unchanged, and that omitting it
(existing callers) preserves today's behavior (None)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pilot.actions import Action, ActionPlan, ActionResult, ActionType, CodeExecParams
from pilot.agents.base_agent import AgentRole
from pilot.agents.orchestrator import AgentOrchestrator
from pilot.security.gateway import TaskScopeOverride


def _make_action(action_type=ActionType.SYSTEM_INFO, **kwargs):
    from pilot.actions import Action

    return Action(action_type=action_type, parameters={}, **kwargs)


def _make_plan():
    action = _make_action()
    return ActionPlan(actions=[action], explanation="test", raw_input="test input")


def _stub_agent(role=AgentRole.SYSTEM):
    agent = MagicMock()
    agent.role = role
    agent.get_capabilities = MagicMock(return_value=[])
    agent.attach_orchestrator = MagicMock()
    agent.handle_task = AsyncMock(return_value=[])
    return agent


@pytest.fixture
async def orchestrator():
    o = AgentOrchestrator(model_router=MagicMock())
    yield o
    await o.stop()


@pytest.mark.asyncio
async def test_scope_override_forwarded_to_specialist(orchestrator):
    plan = _make_plan()
    agent = _stub_agent()
    agent.handle_task.return_value = [ActionResult(action=plan.actions[0], success=True, output="ok")]
    orchestrator._action_registry[plan.actions[0].action_type] = AgentRole.SYSTEM
    orchestrator._agents[AgentRole.SYSTEM] = agent

    override = TaskScopeOverride(max_tier={"shell": 0})
    await orchestrator.execute_plan("test", plan, scope_override=override)

    agent.handle_task.assert_awaited_once()
    _, kwargs = agent.handle_task.call_args
    assert kwargs["scope_override"] is override


@pytest.mark.asyncio
async def test_scope_override_defaults_to_none(orchestrator):
    plan = _make_plan()
    agent = _stub_agent()
    agent.handle_task.return_value = [ActionResult(action=plan.actions[0], success=True, output="ok")]
    orchestrator._action_registry[plan.actions[0].action_type] = AgentRole.SYSTEM
    orchestrator._agents[AgentRole.SYSTEM] = agent

    await orchestrator.execute_plan("test", plan)

    agent.handle_task.assert_awaited_once()
    _, kwargs = agent.handle_task.call_args
    assert kwargs["scope_override"] is None


@pytest.mark.asyncio
async def test_previous_output_context_crosses_specialist_agent_boundary(orchestrator):
    system_action = _make_action(ActionType.SYSTEM_INFO, target="os")
    code_action = Action(
        action_type=ActionType.CODE_EXECUTE,
        target="report",
        parameters=CodeExecParams(
            code="print(PREV_OUTPUT)",
            language="python",
        ),
        use_previous_output=True,
    )
    plan = ActionPlan(
        actions=[system_action, code_action],
        explanation="inspect then report",
        raw_input="report the OS version",
    )
    system_agent = _stub_agent(AgentRole.SYSTEM)
    system_agent.handle_task.return_value = [
        ActionResult(
            action=system_action,
            success=True,
            output="Windows 11 version 10.0.26220",
        ),
    ]
    code_agent = _stub_agent(AgentRole.CODE)
    code_agent.handle_task.return_value = [
        ActionResult(action=code_action, success=True, output="10.0.26220"),
    ]
    orchestrator._action_registry[ActionType.SYSTEM_INFO] = AgentRole.SYSTEM
    orchestrator._action_registry[ActionType.CODE_EXECUTE] = AgentRole.CODE
    orchestrator._agents[AgentRole.SYSTEM] = system_agent
    orchestrator._agents[AgentRole.CODE] = code_agent

    results = await orchestrator.execute_plan("report the OS version", plan)

    assert [result.output for result in results] == [
        "Windows 11 version 10.0.26220",
        "10.0.26220",
    ]
    context = code_agent.handle_task.call_args.kwargs["context"]
    assert context["initial_last_output"] == "Windows 11 version 10.0.26220"
    assert context["initial_largest_output"] == "Windows 11 version 10.0.26220"
