"""Confirms SystemAgent/CodeAgent/CommunicationAgent/ForensicsAgent each
pass their own get_invocation_source() through to the shared Executor.

Before this fix, all four called `self._executor.execute(sub_plan,
scope_override=scope_override)` with no invocation_source at all, silently
defaulting to InvocationSource.INTERACTIVE (the unrestricted gateway
floor) regardless of which specialist agent actually issued the action.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pilot.actions import Action, ActionPlan, ActionResult, ActionType, EmptyParams
from pilot.agents.code_agent import CodeAgent
from pilot.agents.comm_agent import CommunicationAgent
from pilot.agents.forensics_agent import ForensicsAgent
from pilot.agents.system_agent import SystemAgent
from pilot.security.gateway import InvocationSource


class _FakeExecutor:
    def __init__(self):
        self.execute = AsyncMock(return_value=[])


def _action(action_type: ActionType) -> Action:
    return Action(action_type=action_type, target="x", parameters=EmptyParams())


def _plan(action_type: ActionType) -> ActionPlan:
    return ActionPlan(actions=[_action(action_type)], raw_input="test")


class TestSystemAgentInvocationSource:
    @pytest.mark.asyncio
    async def test_passes_system_agent_source(self):
        executor = _FakeExecutor()
        agent = SystemAgent(model_router=None, executor=executor)

        await agent.handle_task("test", _plan(ActionType.FILE_READ))

        executor.execute.assert_awaited_once()
        assert executor.execute.call_args.kwargs["invocation_source"] == InvocationSource.SYSTEM_AGENT


class TestCodeAgentInvocationSource:
    @pytest.mark.asyncio
    async def test_passes_code_agent_source(self):
        executor = _FakeExecutor()
        agent = CodeAgent(model_router=None, executor=executor)

        await agent.handle_task("test", _plan(ActionType.SHELL_COMMAND))

        executor.execute.assert_awaited_once()
        assert executor.execute.call_args.kwargs["invocation_source"] == InvocationSource.CODE_AGENT


class TestCommunicationAgentInvocationSource:
    @pytest.mark.asyncio
    async def test_passes_comm_agent_source(self):
        executor = _FakeExecutor()
        agent = CommunicationAgent(model_router=None, executor=executor)

        await agent.handle_task("test", _plan(ActionType.API_SEND_EMAIL))

        executor.execute.assert_awaited_once()
        assert executor.execute.call_args.kwargs["invocation_source"] == InvocationSource.COMMUNICATION_AGENT


class TestForensicsAgentInvocationSource:
    @pytest.mark.asyncio
    async def test_passes_forensics_agent_source(self):
        executor = _FakeExecutor()
        agent = ForensicsAgent(model_router=None, executor=executor)

        await agent.handle_task("test", _plan(ActionType.LOG_ANALYZE))

        executor.execute.assert_awaited_once()
        assert executor.execute.call_args.kwargs["invocation_source"] == InvocationSource.FORENSICS_AGENT


class TestNoOutOfScopeActionsReachTheExecutor:
    @pytest.mark.asyncio
    async def test_system_agent_ignores_actions_outside_its_capabilities(self):
        """CALENDAR_CREATE_EVENT belongs to CalendarAgent, not SystemAgent --
        confirms the existing can_handle() filter (which the invocation_source
        fix sits on top of) still drops it before it ever reaches the executor."""
        executor = _FakeExecutor()
        agent = SystemAgent(model_router=None, executor=executor)

        results = await agent.handle_task("test", _plan(ActionType.CALENDAR_CREATE_EVENT))

        executor.execute.assert_not_awaited()
        assert results == []
