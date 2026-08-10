"""Tests for capability-driven specialist discovery and bounded routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from pilot.actions import ActionPlan, ActionResult, ActionType
from pilot.agents.agent_mesh import (
    AgentBudgetExceeded,
    AgentBudgetPolicy,
    AgentMesh,
    HandoffRejected,
)
from pilot.agents.base_agent import AgentCapability, AgentRole, BaseAgent
from pilot.agents.orchestrator import AgentOrchestrator
from pilot.intelligence.experience import ExperienceEventType, ExperienceLedger
from pilot.plugins import PluginManifest, PluginTool
from pilot.plugins.capabilities import parse_plugin_capabilities
from pilot.server import PilotServer


def _agent(
    name: str,
    *,
    role: AgentRole = AgentRole.COMMUNICATION,
    action_type: ActionType = ActionType.NOTIFY,
) -> BaseAgent:
    class Specialist(BaseAgent):
        def __init__(self):
            super().__init__(role)

        def get_capabilities(self):
            return [AgentCapability(action_type, f"{name} handles verified {action_type.value}")]

        def get_system_prompt(self):
            return f"{name} specialist"

        async def handle_task(self, user_input, plan, context=None, scope_override=None):
            return [ActionResult(action=action, success=True, output=name) for action in plan.actions]

        def can_handle(self, candidate):
            return candidate == action_type

    Specialist.__name__ = name
    Specialist.__qualname__ = name
    return Specialist()


@pytest.mark.asyncio
async def test_mesh_has_no_fixed_numeric_specialist_ceiling(tmp_path: Path):
    mesh = AgentMesh(tmp_path / "mesh.db")
    await mesh.initialize()

    for index in range(15):
        mesh.register_agent(_agent(f"Specialist{index}"))

    status = mesh.status()
    assert status["total_specialists"] == 15
    assert status["routing"]["fixed_numeric_ceiling"] is False
    assert len(status["specialists"]) == 15
    await mesh.close()


@pytest.mark.asyncio
async def test_provider_selection_uses_observed_outcomes_not_self_report(tmp_path: Path):
    mesh = AgentMesh(tmp_path / "mesh.db")
    await mesh.initialize()
    first = mesh.register_agent(_agent("FirstProvider"))
    second = mesh.register_agent(_agent("SecondProvider"))

    good = mesh.begin_assignment(
        task_id="quality-task",
        agent_key=first.agent_key,
        action_type=ActionType.NOTIFY.value,
    )
    await mesh.complete_assignment(good, success=True)
    bad = mesh.begin_assignment(
        task_id="quality-task",
        agent_key=second.agent_key,
        action_type=ActionType.NOTIFY.value,
    )
    await mesh.complete_assignment(bad, success=False, error="verified failure")

    selected = mesh.select_provider(ActionType.NOTIFY, task_id="next-task")
    assert selected is not None
    assert selected[0] == first.agent_key
    assert mesh.status()["routing"]["self_reported_success_authority"] is False
    await mesh.close()


@pytest.mark.asyncio
async def test_orchestrator_retains_multiple_providers_with_one_gateway_role():
    orchestrator = AgentOrchestrator(model_router=None)
    try:
        notify = _agent("NotificationProvider", action_type=ActionType.NOTIFY)
        email = _agent("EmailProvider", action_type=ActionType.EMAIL_FETCH)
        orchestrator.register_agent(notify)
        orchestrator.register_agent(email)

        assert orchestrator.get_agent(AgentRole.COMMUNICATION) is notify
        assert orchestrator.get_all_stats()["total_agents"] == 2
        notify_provider = orchestrator._mesh.select_provider(ActionType.NOTIFY, task_id="route")
        email_provider = orchestrator._mesh.select_provider(ActionType.EMAIL_FETCH, task_id="route")
        assert notify_provider is not None and notify_provider[1] is notify
        assert email_provider is not None and email_provider[1] is email
    finally:
        await orchestrator.stop()


@pytest.mark.asyncio
async def test_parallel_execution_requires_independence_and_bounded_fanout():
    orchestrator = AgentOrchestrator(model_router=None)
    request = ("inspect", ActionPlan(actions=[], raw_input="inspect"))
    try:
        with pytest.raises(ValueError, match="independence attestation"):
            await orchestrator.execute_independent_plans(
                [request, request],
                independence_attested=False,
            )
        with pytest.raises(ValueError, match="fan-out"):
            await orchestrator.execute_independent_plans(
                [request] * 5,
                independence_attested=True,
            )
        results = await orchestrator.execute_independent_plans(
            [request, request],
            independence_attested=True,
        )
        assert [result["status"] for result in results] == ["completed", "completed"]
    finally:
        await orchestrator.stop()


@pytest.mark.asyncio
async def test_action_and_concurrency_budgets_fail_closed(tmp_path: Path):
    mesh = AgentMesh(tmp_path / "mesh.db")
    await mesh.initialize()
    contract = mesh.register_agent(
        _agent("BoundedProvider"),
        budget=AgentBudgetPolicy(
            max_tokens_per_task=100,
            max_actions_per_task=1,
            max_latency_ms_per_action=1_000,
            max_concurrency=1,
        ),
    )
    lease = mesh.begin_assignment(
        task_id="bounded-task",
        agent_key=contract.agent_key,
        action_type=ActionType.NOTIFY.value,
    )
    with pytest.raises(AgentBudgetExceeded, match="concurrency"):
        mesh.begin_assignment(
            task_id="parallel-task",
            agent_key=contract.agent_key,
            action_type=ActionType.NOTIFY.value,
        )
    await mesh.complete_assignment(lease, success=True)
    with pytest.raises(AgentBudgetExceeded, match="action budget"):
        mesh.begin_assignment(
            task_id="bounded-task",
            agent_key=contract.agent_key,
            action_type=ActionType.NOTIFY.value,
        )
    await mesh.close()


@pytest.mark.asyncio
async def test_handoffs_enforce_depth_fanout_and_cycle(tmp_path: Path):
    mesh = AgentMesh(tmp_path / "mesh.db")
    await mesh.initialize()
    keys = [mesh.register_agent(_agent(f"Handoff{index}")).agent_key for index in range(6)]

    allowed = await mesh.authorize_handoff(
        root_id="root",
        sender=keys[0],
        recipient=keys[1],
        depth=1,
        lineage=(keys[0],),
    )
    assert allowed["allowed"] is True
    with pytest.raises(HandoffRejected, match="cycle"):
        await mesh.authorize_handoff(
            root_id="root",
            sender=keys[1],
            recipient=keys[0],
            depth=2,
            lineage=(keys[0], keys[1]),
        )
    with pytest.raises(HandoffRejected, match="depth"):
        await mesh.authorize_handoff(
            root_id="other",
            sender=keys[0],
            recipient=keys[1],
            depth=4,
            lineage=(keys[0],),
        )
    for recipient in keys[2:5]:
        await mesh.authorize_handoff(
            root_id="root",
            sender=keys[0],
            recipient=recipient,
            depth=1,
            lineage=(keys[0],),
        )
    with pytest.raises(HandoffRejected, match="fan-out"):
        await mesh.authorize_handoff(
            root_id="root",
            sender=keys[0],
            recipient=keys[5],
            depth=1,
            lineage=(keys[0],),
        )
    await mesh.close()


def test_approved_plugin_contract_preserves_exact_broker_grants():
    mesh = AgentMesh()
    manifest = PluginManifest(
        name="research-provider",
        description="Reviewed research provider",
        agent_type="research",
        tools=[
            PluginTool(
                name="research_lookup",
                description="Look up reviewed data",
                permission_tier=2,
                action_type="plugin_call",
            )
        ],
        capabilities=parse_plugin_capabilities(
            {
                "filesystem": {"read": ["C:/Research"], "write": []},
                "network_domains": ["api.example.com"],
                "processes": [],
                "credentials": ["RESEARCH_TOKEN"],
                "clipboard": {"read": False, "write": False},
                "media": {"camera": False, "microphone": False},
                "data_retention": {"mode": "none", "max_days": 0},
                "destructive_actions": False,
            }
        ),
    )

    contract = mesh.register_plugin(manifest)

    assert contract.source == "approved_plugin"
    assert contract.executable is False
    assert contract.capabilities == ("research_lookup",)
    assert contract.permissions.filesystem_read == ("C:/Research",)
    assert contract.permissions.network_domains == ("api.example.com",)
    assert contract.permissions.credential_names == ("RESEARCH_TOKEN",)
    assert mesh.select_provider(ActionType.PLUGIN_CALL, task_id="task") is None


@pytest.mark.asyncio
async def test_verified_outcomes_persist_and_emit_ledger_events(tmp_path: Path):
    ledger = ExperienceLedger(tmp_path / "experience.db")
    await ledger.initialize()
    mesh = AgentMesh(tmp_path / "mesh.db")
    await mesh.initialize()
    mesh.set_experience_ledger(ledger)
    contract = mesh.register_agent(_agent("PersistentProvider"))
    lease = mesh.begin_assignment(
        task_id="persist-task",
        agent_key=contract.agent_key,
        action_type=ActionType.NOTIFY.value,
    )
    await mesh.complete_assignment(lease, success=True)
    events = await ledger.list_events(event_type=ExperienceEventType.AGENT_MESH_OUTCOME)
    assert len(events) == 1
    await mesh.close()

    reloaded = AgentMesh(tmp_path / "mesh.db")
    await reloaded.initialize()
    reloaded.register_agent(_agent("PersistentProvider"))
    specialist = reloaded.status()["specialists"][0]
    assert specialist["performance"]["attempts"] == 1
    assert specialist["performance"]["successes"] == 1
    await reloaded.close()
    await ledger.close()


@pytest.mark.asyncio
async def test_server_exposes_mesh_status_without_mutating_it():
    server = object.__new__(PilotServer)
    mesh = AgentMesh()
    mesh.register_agent(_agent("VisibleProvider"))
    server._agent_mesh = mesh

    status = await server._handle_agent_mesh_status({}, None)

    assert status["total_specialists"] == 1
    assert status["delegation"]["maximum_depth"] == 3
    assert status["routing"]["fixed_numeric_ceiling"] is False
