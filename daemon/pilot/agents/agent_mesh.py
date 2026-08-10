"""Capability-driven specialist mesh with bounded delegation and outcomes.

The mesh does not execute tools. It indexes reviewed specialist contracts,
selects among agents that can already handle an action, enforces per-agent
action/latency/concurrency budgets, and learns routing quality only from
observed ActionResult outcomes.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

import aiosqlite

from pilot.actions import ActionType
from pilot.intelligence.experience import (
    ExperienceEventType,
    ExperienceLedger,
    PrivacyClass,
)

if TYPE_CHECKING:
    from pilot.agents.base_agent import BaseAgent
    from pilot.plugins import PluginManifest

MESH_SCHEMA_VERSION = 1
MAX_DELEGATION_DEPTH = 3
MAX_HANDOFF_FANOUT = 4


class AgentMeshError(RuntimeError):
    """Base error for mesh enforcement."""


class AgentBudgetExceeded(AgentMeshError):
    """Raised when a specialist exceeds its bounded assignment budget."""


class HandoffRejected(AgentMeshError):
    """Raised when delegation would exceed depth/fan-out or create a cycle."""


@dataclass(frozen=True, slots=True)
class AgentBudgetPolicy:
    max_tokens_per_task: int = 12_000
    max_actions_per_task: int = 20
    max_latency_ms_per_action: int = 120_000
    max_concurrency: int = 1


@dataclass(frozen=True, slots=True)
class AgentPermissions:
    action_types: tuple[str, ...]
    confirmation_actions: tuple[str, ...] = ()
    filesystem_read: tuple[str, ...] = ()
    filesystem_write: tuple[str, ...] = ()
    network_domains: tuple[str, ...] = ()
    process_names: tuple[str, ...] = ()
    credential_names: tuple[str, ...] = ()
    clipboard: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    authority: str = "agent_gateway"


@dataclass(frozen=True, slots=True)
class AgentContract:
    agent_key: str
    display_name: str
    role: str
    source: str
    description: str
    capabilities: tuple[str, ...]
    keywords: tuple[str, ...]
    permissions: AgentPermissions
    budget: AgentBudgetPolicy
    executable: bool = True
    handoff_contract: str = "bounded_context_refs_and_partial_results"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentPerformance:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    total_latency_ms: int = 0
    last_error: str = ""
    in_flight: int = 0

    @property
    def quality_score(self) -> float:
        # Beta(1, 1) prior prevents a new specialist from outranking a proven
        # one based on self-description alone.
        return round((self.successes + 1) / (self.attempts + 2), 4)

    @property
    def average_latency_ms(self) -> int:
        return round(self.total_latency_ms / self.attempts) if self.attempts else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "quality_score": self.quality_score,
            "average_latency_ms": self.average_latency_ms,
        }


@dataclass(slots=True)
class AssignmentLease:
    task_id: str
    agent_key: str
    action_type: str
    action_count: int = 1
    started_at: float = field(default_factory=time.monotonic)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_mesh_outcomes (
    agent_key TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    total_latency_ms INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agent_mesh_handoffs (
    handoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    depth INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class AgentMesh:
    """Dynamic catalog and outcome-grounded provider selector."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else None
        self._db: aiosqlite.Connection | None = None
        self._contracts: dict[str, AgentContract] = {}
        self._agents: dict[str, BaseAgent] = {}
        self._providers: dict[str, list[str]] = defaultdict(list)
        self._performance: dict[str, AgentPerformance] = defaultdict(AgentPerformance)
        self._task_actions: dict[tuple[str, str], int] = defaultdict(int)
        self._handoff_fanout: dict[str, set[str]] = defaultdict(set)
        self._ledger: ExperienceLedger | None = None

    async def initialize(self) -> None:
        if self._db_path is None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(SCHEMA_SQL)
        cursor = await self._db.execute(
            """
            SELECT agent_key, attempts, successes, failures,
                   total_latency_ms, last_error
            FROM agent_mesh_outcomes
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            self._performance[str(row[0])] = AgentPerformance(
                attempts=int(row[1]),
                successes=int(row[2]),
                failures=int(row[3]),
                total_latency_ms=int(row[4]),
                last_error=str(row[5]),
            )

    def set_experience_ledger(self, ledger: ExperienceLedger) -> None:
        self._ledger = ledger

    def register_agent(
        self,
        agent: BaseAgent,
        *,
        keywords: Iterable[str] = (),
        budget: AgentBudgetPolicy | None = None,
    ) -> AgentContract:
        capabilities = tuple(agent.get_capabilities())
        action_types = tuple(sorted({cap.action_type.value for cap in capabilities}))
        confirmation = tuple(sorted({cap.action_type.value for cap in capabilities if cap.requires_confirmation}))
        configured_budget = budget or getattr(agent, "mesh_budget", None)
        if not isinstance(configured_budget, AgentBudgetPolicy):
            configured_budget = AgentBudgetPolicy()
        agent_key = f"builtin:{agent.__class__.__module__}.{agent.__class__.__name__}"
        descriptive_words = {
            word.lower().strip(".,:;()[]") for cap in capabilities for word in cap.description.split() if len(word) >= 4
        }
        declared_keywords = {
            str(item).lower()
            for item in (
                *keywords,
                *getattr(agent, "mesh_keywords", ()),
            )
        }
        contract = AgentContract(
            agent_key=agent_key,
            display_name=agent.__class__.__name__,
            role=agent.role.value,
            source="builtin",
            description=agent.get_system_prompt()[:500],
            capabilities=action_types,
            keywords=tuple(sorted(descriptive_words | declared_keywords)),
            permissions=AgentPermissions(
                action_types=action_types,
                confirmation_actions=confirmation,
                filesystem_read=tuple(getattr(agent, "mesh_filesystem_read", ())),
                filesystem_write=tuple(getattr(agent, "mesh_filesystem_write", ())),
                network_domains=tuple(getattr(agent, "mesh_network_domains", ())),
                process_names=tuple(getattr(agent, "mesh_process_names", ())),
                credential_names=tuple(getattr(agent, "mesh_credential_names", ())),
                clipboard=tuple(getattr(agent, "mesh_clipboard", ())),
                devices=tuple(getattr(agent, "mesh_devices", ())),
                authority=f"agent_gateway:{agent.role.value}",
            ),
            budget=configured_budget,
        )
        self._register_contract(contract, agent=agent)
        return contract

    def register_plugin(self, manifest: PluginManifest) -> AgentContract:
        capabilities = tuple(sorted(tool.name for tool in manifest.tools))
        plugin_caps = manifest.capabilities
        devices = tuple(
            device
            for device, enabled in (
                ("camera", plugin_caps.media.camera),
                ("microphone", plugin_caps.media.microphone),
            )
            if enabled
        )
        clipboard = tuple(
            direction
            for direction, enabled in (
                ("read", plugin_caps.clipboard.read),
                ("write", plugin_caps.clipboard.write),
            )
            if enabled
        )
        contract = AgentContract(
            agent_key=f"plugin:{manifest.name}",
            display_name=manifest.name,
            role=manifest.agent_type or "plugin",
            source="approved_plugin",
            description=manifest.description[:500],
            capabilities=capabilities,
            keywords=tuple(
                sorted(
                    {
                        manifest.name.lower(),
                        *(word.lower() for word in manifest.description.split() if len(word) >= 4),
                        *(tool.name.lower() for tool in manifest.tools),
                    }
                )
            ),
            permissions=AgentPermissions(
                action_types=tuple(sorted({tool.action_type for tool in manifest.tools})),
                confirmation_actions=tuple(
                    sorted({tool.action_type for tool in manifest.tools if int(tool.permission_tier) >= 2})
                ),
                filesystem_read=plugin_caps.filesystem.read,
                filesystem_write=plugin_caps.filesystem.write,
                network_domains=plugin_caps.network_domains,
                process_names=plugin_caps.processes,
                credential_names=plugin_caps.credentials,
                clipboard=clipboard,
                devices=devices,
                authority="plugin_capability_broker",
            ),
            budget=AgentBudgetPolicy(
                max_tokens_per_task=0,
                max_actions_per_task=8,
                max_latency_ms_per_action=30_000,
                max_concurrency=1,
            ),
            # Plugin tools remain behind PLUGIN_CALL and the capability broker;
            # the mesh may advertise them but never invokes plugin code itself.
            executable=False,
            handoff_contract="guarded_plugin_call_only",
        )
        self._register_contract(contract)
        return contract

    def refresh_plugins(self, manifests: Iterable[PluginManifest]) -> None:
        for key in [key for key in self._contracts if key.startswith("plugin:")]:
            self.unregister(key)
        for manifest in manifests:
            if manifest.enabled:
                self.register_plugin(manifest)

    def _register_contract(
        self,
        contract: AgentContract,
        *,
        agent: BaseAgent | None = None,
    ) -> None:
        self.unregister(contract.agent_key)
        self._contracts[contract.agent_key] = contract
        if agent is not None:
            self._agents[contract.agent_key] = agent
            for action_type in contract.capabilities:
                self._providers[action_type].append(contract.agent_key)

    def unregister(self, agent_key: str) -> None:
        self._contracts.pop(agent_key, None)
        self._agents.pop(agent_key, None)
        for action_type, providers in list(self._providers.items()):
            self._providers[action_type] = [key for key in providers if key != agent_key]
            if not self._providers[action_type]:
                del self._providers[action_type]

    def contract_for(self, agent_key: str) -> AgentContract | None:
        return self._contracts.get(agent_key)

    def agent_for(self, agent_key: str) -> BaseAgent | None:
        return self._agents.get(agent_key)

    def key_for_agent(self, agent: BaseAgent) -> str | None:
        return next((key for key, value in self._agents.items() if value is agent), None)

    def executable_agents(self) -> tuple[BaseAgent, ...]:
        return tuple(self._agents.values())

    def select_provider(
        self,
        action_type: ActionType,
        *,
        task_id: str,
    ) -> tuple[str, BaseAgent] | None:
        eligible: list[tuple[float, float, str, BaseAgent]] = []
        for key in self._providers.get(action_type.value, ()):
            contract = self._contracts[key]
            performance = self._performance[key]
            if performance.in_flight >= contract.budget.max_concurrency:
                continue
            if self._task_actions[(task_id, key)] >= contract.budget.max_actions_per_task:
                continue
            latency_penalty = min(
                performance.average_latency_ms / max(1, contract.budget.max_latency_ms_per_action),
                1.0,
            )
            score = performance.quality_score - (latency_penalty * 0.15)
            # When observed quality is tied, prefer the narrower contract.
            # This makes a domain specialist useful immediately without
            # allowing self-description to outrank verified outcomes.
            specificity = 1 / max(1, len(contract.capabilities))
            eligible.append((score, specificity, key, self._agents[key]))
        if not eligible:
            return None
        _, _, key, agent = max(eligible, key=lambda item: (item[0], item[1], item[2]))
        return key, agent

    def begin_assignment(
        self,
        *,
        task_id: str,
        agent_key: str,
        action_type: str,
        action_count: int = 1,
    ) -> AssignmentLease:
        contract = self._contracts.get(agent_key)
        if contract is None or agent_key not in self._agents:
            raise AgentMeshError(f"unknown executable specialist: {agent_key}")
        performance = self._performance[agent_key]
        task_key = (task_id, agent_key)
        if performance.in_flight >= contract.budget.max_concurrency:
            raise AgentBudgetExceeded(f"{agent_key} reached its concurrency budget")
        bounded_count = max(1, int(action_count))
        if self._task_actions[task_key] + bounded_count > contract.budget.max_actions_per_task:
            raise AgentBudgetExceeded(f"{agent_key} reached its per-task action budget")
        performance.in_flight += 1
        self._task_actions[task_key] += bounded_count
        return AssignmentLease(
            task_id=task_id,
            agent_key=agent_key,
            action_type=action_type,
            action_count=bounded_count,
        )

    async def complete_assignment(
        self,
        lease: AssignmentLease,
        *,
        success: bool,
        error: str = "",
    ) -> AgentPerformance:
        elapsed_ms = max(0, round((time.monotonic() - lease.started_at) * 1000))
        contract = self._contracts[lease.agent_key]
        performance = self._performance[lease.agent_key]
        within_latency_budget = elapsed_ms <= contract.budget.max_latency_ms_per_action
        effective_success = success and within_latency_budget
        performance.in_flight = max(0, performance.in_flight - 1)
        performance.attempts += 1
        performance.successes += int(effective_success)
        performance.failures += int(not effective_success)
        performance.total_latency_ms += elapsed_ms
        performance.last_error = error[:500] if not effective_success else ""
        if not within_latency_budget:
            performance.last_error = (
                f"latency budget exceeded: {elapsed_ms}ms > {contract.budget.max_latency_ms_per_action}ms"
            )
        await self._persist_performance(lease.agent_key, performance)
        await self._record_event(
            ExperienceEventType.AGENT_MESH_OUTCOME,
            f"agent-mesh:{lease.task_id}:{lease.agent_key}:{self._task_actions[(lease.task_id, lease.agent_key)]}",
            {
                "task_id": lease.task_id,
                "agent_key": lease.agent_key,
                "action_type": lease.action_type,
                "action_count": lease.action_count,
                "success": effective_success,
                "latency_ms": elapsed_ms,
                "quality_score": performance.quality_score,
            },
        )
        return performance

    def release_task(self, task_id: str) -> None:
        for key in [key for key in self._task_actions if key[0] == task_id]:
            del self._task_actions[key]
        self._handoff_fanout.pop(task_id, None)

    async def authorize_handoff(
        self,
        *,
        root_id: str,
        sender: str,
        recipient: str,
        depth: int,
        lineage: tuple[str, ...],
    ) -> dict[str, Any]:
        if depth > MAX_DELEGATION_DEPTH:
            raise HandoffRejected(f"delegation depth exceeds {MAX_DELEGATION_DEPTH}")
        if recipient in lineage or recipient == sender:
            raise HandoffRejected("delegation cycle rejected")
        fanout = self._handoff_fanout[root_id]
        if recipient not in fanout and len(fanout) >= MAX_HANDOFF_FANOUT:
            raise HandoffRejected(f"handoff fan-out exceeds {MAX_HANDOFF_FANOUT}")
        if recipient not in self._contracts:
            raise HandoffRejected(f"unknown handoff recipient: {recipient}")
        fanout.add(recipient)
        if self._db is not None:
            await self._db.execute(
                """
                INSERT INTO agent_mesh_handoffs
                    (root_id, sender, recipient, depth)
                VALUES (?, ?, ?, ?)
                """,
                (root_id, sender, recipient, depth),
            )
            await self._db.commit()
        await self._record_event(
            ExperienceEventType.AGENT_MESH_HANDOFF,
            f"agent-mesh:{root_id}:handoff:{sender}:{recipient}:{depth}",
            {
                "root_id": root_id,
                "sender": sender,
                "recipient": recipient,
                "depth": depth,
                "context_contract": "bounded_refs_and_partial_results",
            },
        )
        return {
            "allowed": True,
            "root_id": root_id,
            "sender": sender,
            "recipient": recipient,
            "depth": depth,
        }

    def route_text(self, user_input: str, *, limit: int = 4) -> list[dict[str, Any]]:
        words = {word.strip(".,:;!?()[]").lower() for word in user_input.split()}
        ranked: list[tuple[int, float, AgentContract]] = []
        for contract in self._contracts.values():
            if not contract.executable:
                continue
            matches = len(words & set(contract.keywords))
            if matches:
                ranked.append((matches, self._performance[contract.agent_key].quality_score, contract))
        ranked.sort(key=lambda item: (item[0], item[1], item[2].agent_key), reverse=True)
        return [
            {
                "agent_key": contract.agent_key,
                "role": contract.role,
                "display_name": contract.display_name,
                "matches": matches,
                "quality_score": quality,
            }
            for matches, quality, contract in ranked[: max(1, min(limit, MAX_HANDOFF_FANOUT))]
        ]

    def status(self) -> dict[str, Any]:
        executable = sum(contract.executable for contract in self._contracts.values())
        available_actions = {action_type.value for action_type in ActionType}
        covered_actions = set(self._providers)
        sources: dict[str, int] = defaultdict(int)
        for contract in self._contracts.values():
            sources[contract.source] += 1
        return {
            "enabled": True,
            "schema_version": MESH_SCHEMA_VERSION,
            "total_specialists": len(self._contracts),
            "executable_specialists": executable,
            "external_capability_providers": len(self._contracts) - executable,
            "sources": dict(sorted(sources.items())),
            "registered_action_types": len(self._providers),
            "available_action_types": len(available_actions),
            "coverage_complete": covered_actions == available_actions,
            "uncovered_action_types": sorted(available_actions - covered_actions),
            "delegation": {
                "maximum_depth": MAX_DELEGATION_DEPTH,
                "maximum_fanout": MAX_HANDOFF_FANOUT,
                "cycle_detection": True,
                "full_transcript_handoffs": False,
                "cancellation_propagation": True,
                "partial_result_recovery": True,
                "parallel_only_when_explicitly_independent": True,
            },
            "routing": {
                "fixed_numeric_ceiling": False,
                "selection": "capability_then_verified_outcome",
                "self_reported_success_authority": False,
            },
            "specialists": [
                {
                    **contract.to_dict(),
                    "performance": self._performance[key].to_dict(),
                }
                for key, contract in sorted(self._contracts.items())
            ],
        }

    async def _persist_performance(
        self,
        agent_key: str,
        performance: AgentPerformance,
    ) -> None:
        if self._db is None:
            return
        await self._db.execute(
            """
            INSERT INTO agent_mesh_outcomes (
                agent_key, attempts, successes, failures,
                total_latency_ms, last_error
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_key) DO UPDATE SET
                attempts = excluded.attempts,
                successes = excluded.successes,
                failures = excluded.failures,
                total_latency_ms = excluded.total_latency_ms,
                last_error = excluded.last_error
            """,
            (
                agent_key,
                performance.attempts,
                performance.successes,
                performance.failures,
                performance.total_latency_ms,
                performance.last_error,
            ),
        )
        await self._db.commit()

    async def _record_event(
        self,
        event_type: ExperienceEventType,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        if self._ledger is None:
            return
        await self._ledger.append(
            event_type,
            idempotency_key=idempotency_key,
            source="agent_mesh",
            payload=payload,
            provenance={"component": "AgentMesh"},
            privacy_class=PrivacyClass.SENSITIVE,
        )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
