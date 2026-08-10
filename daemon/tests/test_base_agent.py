"""Tests for pilot.agents.base_agent.BaseAgent.get_invocation_source().

Covers the fix for a real gap: most specialist agents never declared an
InvocationSource when forwarding to the shared Executor, so they silently
ran with the unrestricted "interactive" gateway floor regardless of which
agent actually issued the action. get_invocation_source() derives the
right source from AgentRole with zero per-subclass boilerplate.
"""

from __future__ import annotations

from pilot.agents.base_agent import AgentRole, BaseAgent
from pilot.security.gateway import InvocationSource


class _StubAgent(BaseAgent):
    """Minimal concrete BaseAgent -- only get_invocation_source() is under
    test here, so every other abstract method is a trivial stub."""

    def get_capabilities(self):
        return []

    def get_system_prompt(self) -> str:
        return ""

    async def handle_task(self, *args, **kwargs):
        return []

    def can_handle(self, action_type) -> bool:
        return False


class TestGetInvocationSource:
    def test_system_role_maps_to_system_agent_source(self):
        agent = _StubAgent(role=AgentRole.SYSTEM)
        assert agent.get_invocation_source() == InvocationSource.SYSTEM_AGENT

    def test_code_role_maps_to_code_agent_source(self):
        agent = _StubAgent(role=AgentRole.CODE)
        assert agent.get_invocation_source() == InvocationSource.CODE_AGENT

    def test_web_role_maps_to_existing_web_agent_source(self):
        """WEB predates this fix -- confirms the new mechanism reproduces
        the same value the hand-written WebAgent already used."""
        agent = _StubAgent(role=AgentRole.WEB)
        assert agent.get_invocation_source() == InvocationSource.WEB_AGENT

    def test_communication_role_maps_to_comm_agent_source(self):
        agent = _StubAgent(role=AgentRole.COMMUNICATION)
        assert agent.get_invocation_source() == InvocationSource.COMMUNICATION_AGENT

    def test_forensics_role_maps_to_forensics_agent_source(self):
        agent = _StubAgent(role=AgentRole.FORENSICS)
        assert agent.get_invocation_source() == InvocationSource.FORENSICS_AGENT

    def test_ssh_role_maps_to_ssh_agent_source(self):
        agent = _StubAgent(role=AgentRole.SSH)
        assert agent.get_invocation_source() == InvocationSource.SSH_AGENT

    def test_monitor_role_maps_to_monitor_agent_source(self):
        agent = _StubAgent(role=AgentRole.MONITOR)
        assert agent.get_invocation_source() == InvocationSource.MONITOR_AGENT

    def test_rss_role_maps_to_rss_agent_source(self):
        agent = _StubAgent(role=AgentRole.RSS)
        assert agent.get_invocation_source() == InvocationSource.RSS_AGENT

    def test_calendar_role_maps_to_calendar_agent_source(self):
        agent = _StubAgent(role=AgentRole.CALENDAR)
        assert agent.get_invocation_source() == InvocationSource.CALENDAR_AGENT

    def test_semantic_search_role_maps_to_semantic_search_agent_source(self):
        agent = _StubAgent(role=AgentRole.SEMANTIC_SEARCH)
        assert agent.get_invocation_source() == InvocationSource.SEMANTIC_SEARCH_AGENT

    def test_roles_with_no_matching_source_fall_back_to_unknown(self):
        """ORCHESTRATOR/GENERAL never call Executor.execute() directly --
        no InvocationSource member matches their AgentRole.value, so this
        must degrade to UNKNOWN rather than raise."""
        assert _StubAgent(role=AgentRole.ORCHESTRATOR).get_invocation_source() == InvocationSource.UNKNOWN
        assert _StubAgent(role=AgentRole.GENERAL).get_invocation_source() == InvocationSource.UNKNOWN

    def test_every_agent_role_has_a_defined_gateway_profile_or_is_unknown(self):
        """Whole-enum sweep: every AgentRole either resolves to a real,
        profiled InvocationSource, or is one of the two known
        non-executing roles that fall back to UNKNOWN."""
        from pilot.security.gateway import DEFAULT_SOURCE_PROFILES

        for role in AgentRole:
            source = _StubAgent(role=role).get_invocation_source()
            if source == InvocationSource.UNKNOWN:
                assert role in (AgentRole.ORCHESTRATOR, AgentRole.GENERAL)
            else:
                assert source.value in DEFAULT_SOURCE_PROFILES
