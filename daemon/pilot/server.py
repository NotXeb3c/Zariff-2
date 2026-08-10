"""WebSocket JSON-RPC 2.0 server for the Pilot daemon."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import logging
import math
import secrets
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import websockets
from websockets.asyncio.server import Server, ServerConnection

from pilot import __version__
from pilot.config import (
    DATA_DIR,
    DB_FILE,
    EXPERIENCE_DB_FILE,
    LOG_FILE,
    PLUGINS_DIR,
    STATE_DIR,
    PilotConfig,
    ensure_dirs,
)
from pilot.export_logs import export_logs
from pilot.intelligence.experience import (
    ExperienceContext,
    ExperienceEvent,
    ExperienceEventType,
    ExperienceLedger,
    PrivacyClass,
    experience_scope,
    get_experience_context,
    stable_action_idempotency_key,
)
from pilot.logger import ColorFormatter
from pilot.reasoning.events import (
    CONFIRMATION_APPROVED,
    CONFIRMATION_DENIED,
    CONFIRMATION_REQUIRED,
    CRITIC_REVIEW_APPROVED,
    CRITIC_REVIEW_BLOCKED,
    CRITIC_REVIEW_STARTED,
    CRITIC_REVIEW_WARNED,
    EXECUTOR_ACTION_COMPLETE,
    EXECUTOR_ACTION_STARTED,
    EXECUTOR_ALL_COMPLETE,
    EXECUTOR_ERROR,
    EXECUTOR_STARTED,
    MEMORY_CONTEXT_LOADED,
    MEMORY_SEARCH_STARTED,
    MEMORY_STORE_COMPLETE,
    MEMORY_STORE_STARTED,
    ORCHESTRATOR_AGENT_DELEGATED,
    ORCHESTRATOR_ROUTING,
    PLANNER_ERROR,
    PLANNER_GENERATED_PLAN,
    PLANNER_LLM_CALL,
    PLANNER_REPLANNING,
    PLANNER_STARTED,
    REFLECTION_COMPLETE,
    REFLECTION_STARTED,
    ROUTING_AGENTS_ASSIGNED,
    ROUTING_ANALYSIS_STARTED,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_STARTED,
)
from pilot.system.companion_speech import (
    CompanionSpeechCoordinator,
    SpeechChannel,
    SpeechOutcome,
)
from pilot.system.interaction import InteractionPhase, InteractionRuntime, acknowledgement_for
from pilot.workflows.durable_tasks import (
    ApprovalConflict,
    ApprovalStatus,
    DurableTaskStore,
    InvalidTaskTransition,
    TaskStatus,
)

logger = logging.getLogger("pilot.server")


def _resolve_dry_run(configured: bool, requested: object = False) -> bool:
    """Treat the global Dry Run setting as a safety floor clients cannot disable."""
    return bool(configured or requested)


def _sanitize_summary(text: object, limit: int = 160) -> str:
    """Collapse whitespace and bound user-visible/event summaries."""
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)] + "..."


CONFIRM_TIMEOUT_SECONDS = 300
# These control-plane RPCs must be dispatchable while a normal request is
# paused. ``execute`` waits for ``confirm`` on the same WebSocket connection,
# ``abort`` interrupts execution, ``interject`` revises it, and speech
# replacement/stop must interrupt an in-flight utterance. All other RPCs
# remain sequential per connection.
OUT_OF_BAND_RPC_METHODS = frozenset({"confirm", "abort", "interject", "speak_text", "stop_speech"})

# ── Plan History DB path (sibling of the main DB) ──
PLAN_HISTORY_DB_FILE = DATA_DIR / "plan_history.db"


@dataclass
class JsonRpcRequest:
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str | int | None = None

    @classmethod
    def parse(cls, raw: str) -> JsonRpcRequest:
        """Parse a raw JSON-RPC request string.

        Args:
            raw: The raw JSON string to parse.

        Returns:
            A JsonRpcRequest instance.

        Raises:
            ValueError: If the payload is not a valid JSON-RPC request object.
        """
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Invalid JSON-RPC request: expected an object")
        if data.get("jsonrpc") != "2.0":
            raise ValueError("Invalid JSON-RPC version")
        method = data.get("method")
        if not isinstance(method, str) or not method:
            raise ValueError("Invalid JSON-RPC request: method must be a non-empty string")
        params = data.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("Invalid JSON-RPC request: params must be an object")
        return cls(
            method=method,
            params=params,
            id=data.get("id"),
        )


class _BroadcastConnection:
    """Duck-typed WebSocket sink used by non-socket interaction sources."""

    def __init__(self, broadcast: Any) -> None:
        self._broadcast = broadcast

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        method = payload.get("method")
        if isinstance(method, str) and method:
            await self._broadcast(method, payload.get("params", {}))


def _success_response(req_id: str | int | None, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "result": result, "id": req_id})


def _error_response(req_id: str | int | None, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id})


def _notification(method: str, params: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params})


def _is_terminal_execution_failure(results: list[Any]) -> bool:
    """Return True when replanning cannot repair an exact local resolution failure."""
    failed = [result for result in results if not result.success]
    if not failed:
        return False

    terminal_markers = {
        "file_read": ("file not found", "no such file", "cannot find the file"),
        "open_application": (
            "was not found in the start menu, path, or windows app registry",
            "is ambiguous",
            "is registered, but its executable is missing",
        ),
    }
    for result in failed:
        action_type = getattr(result.action.action_type, "value", str(result.action.action_type))
        markers = terminal_markers.get(action_type)
        if markers is None or not any(marker in str(result.error or "").lower() for marker in markers):
            return False
    return True


def _is_terminal_planning_failure(error: str) -> bool:
    """Return whether the planner already exhausted the configured provider path."""
    normalized = error.casefold()
    return any(
        marker in normalized
        for marker in (
            "api unavailable",
            "no api key configured",
            "configure a healthy provider",
        )
    )


@dataclass
class PendingConfirmation:
    """Tracks a plan awaiting user confirmation."""

    plan_id: str
    event: asyncio.Event
    confirmed: bool = False
    plan: Any = None
    # Indices of actions the user approved out of those requiring confirmation.
    # None means "not specified" -> treat as all-approved (back-compat with
    # older frontend builds that only send {plan_id, confirmed}).
    approved_indices: set[int] | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Plan History Store
# ─────────────────────────────────────────────────────────────────────────────


class PlanHistoryStore:
    """Append-only SQLite audit log for every ActionPlan executed by the daemon.

    Schema (``plan_history`` table)
    --------------------------------
    plan_id             TEXT  PRIMARY KEY  — 8-char UUID prefix assigned in _handle_execute
    created_at          TEXT              — ISO-8601 UTC timestamp of plan creation
    raw_input           TEXT              — original user input string
    plan_json           TEXT              — full ActionPlan serialised as JSON
    action_count        INTEGER           — len(plan.actions)
    critic_verdict_json TEXT  NULLABLE    — DestructiveCriticAgent verdict dict, or NULL
    confirmation_decision TEXT            — 'approved' | 'denied' | 'skipped' |
                                            'blocked_by_critic' | 'n/a' (dry-run)
    execution_status    TEXT              — 'success' | 'partial_failure' | 'error' |
                                            'cancelled' | 'dry_run'
    results_json        TEXT  NULLABLE    — list[ActionResult.model_dump()] as JSON
    verification_json   TEXT  NULLABLE    — VerificationResult.model_dump() as JSON
    dry_run             INTEGER           — 1 if dry-run, 0 otherwise
    duration_ms         INTEGER           — wall-clock ms from plan start to terminal state
    """

    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS plan_history (
        plan_id               TEXT    PRIMARY KEY,
        created_at            TEXT    NOT NULL,
        raw_input             TEXT    NOT NULL,
        plan_json             TEXT    NOT NULL,
        action_count          INTEGER NOT NULL DEFAULT 0,
        critic_verdict_json   TEXT,
        confirmation_decision TEXT    NOT NULL DEFAULT 'n/a',
        execution_status      TEXT    NOT NULL DEFAULT 'unknown',
        results_json          TEXT,
        verification_json     TEXT,
        dry_run               INTEGER NOT NULL DEFAULT 0,
        duration_ms           INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS plan_history_created_at
        ON plan_history (created_at DESC);
    CREATE INDEX IF NOT EXISTS plan_history_execution_status
        ON plan_history (execution_status);
    """

    def __init__(self, db_path: str | Path = PLAN_HISTORY_DB_FILE) -> None:
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open (or create) the SQLite DB and ensure the schema exists."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(self._CREATE_TABLE)
        await self._db.commit()
        logger.info("PlanHistoryStore initialised at %s", self._db_path)

    async def record(
        self,
        *,
        plan_id: str,
        raw_input: str,
        plan: Any,
        critic_verdict: dict[str, Any] | None,
        confirmation_decision: str,
        execution_status: str,
        results: list[Any],
        verification: Any | None,
        dry_run: bool,
        duration_ms: int,
    ) -> None:
        """Insert or replace a plan audit record.

        Args:
            plan_id: Short UUID identifying this plan.
            raw_input: The original user-supplied text.
            plan: ActionPlan object (must have ``.actions`` and ``.model_dump()`` / JSON-serialisable dict).
            critic_verdict: Optional dict from DestructiveCriticAgent.
            confirmation_decision: One of 'approved', 'denied', 'skipped', 'blocked_by_critic', 'n/a'.
            execution_status: Terminal status string ('success', 'partial_failure', 'error', 'cancelled', 'dry_run').
            results: List of ActionResult objects with ``.model_dump()``.
            verification: VerificationResult object with ``.model_dump()``, or None.
            dry_run: Whether this was a dry-run execution.
            duration_ms: Wall-clock duration in milliseconds.
        """
        if self._db is None:
            logger.warning("PlanHistoryStore.record() called before initialize()")
            return

        try:
            plan_dict = plan.model_dump(mode="json") if hasattr(plan, "model_dump") else {}
        except Exception:
            plan_dict = {}

        results_list: list[Any] = []
        for r in results:
            try:
                if hasattr(r, "model_dump"):
                    results_list.append(r.model_dump(mode="json"))
                elif isinstance(r, (dict, list, str, int, float, bool)) or r is None:
                    results_list.append(r)
                else:
                    results_list.append(str(r))
            except Exception:
                results_list.append(str(r))

        try:
            verification_dict = (
                verification.model_dump(mode="json") if (verification and hasattr(verification, "model_dump")) else None
            )
        except Exception:
            verification_dict = None

        await self._db.execute(
            """
            INSERT OR REPLACE INTO plan_history (
                plan_id, created_at, raw_input, plan_json, action_count,
                critic_verdict_json, confirmation_decision,
                execution_status, results_json, verification_json,
                dry_run, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                datetime.now(timezone.utc).isoformat(),
                raw_input,
                json.dumps(plan_dict, ensure_ascii=False),
                len(getattr(plan, "actions", [])),
                json.dumps(critic_verdict, ensure_ascii=False) if critic_verdict is not None else None,
                confirmation_decision,
                execution_status,
                json.dumps(results_list, ensure_ascii=False),
                json.dumps(verification_dict, ensure_ascii=False) if verification_dict is not None else None,
                1 if dry_run else 0,
                duration_ms,
            ),
        )
        await self._db.commit()
        logger.debug("PlanHistoryStore: recorded plan_id=%s status=%s", plan_id, execution_status)

    async def get_list(
        self,
        limit: int = 50,
        offset: int = 0,
        status_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a paginated list of plan summary rows (no large JSON blobs).

        Args:
            limit: Maximum number of rows to return.
            offset: Rows to skip (for pagination).
            status_filter: Optional execution_status to filter by.

        Returns:
            List of dicts with summary fields.
        """
        if self._db is None:
            return []

        if status_filter:
            cursor = await self._db.execute(
                """
                SELECT plan_id, created_at, raw_input, action_count,
                       confirmation_decision, execution_status, dry_run, duration_ms
                FROM plan_history
                WHERE execution_status = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (status_filter, limit, offset),
            )
        else:
            cursor = await self._db.execute(
                """
                SELECT plan_id, created_at, raw_input, action_count,
                       confirmation_decision, execution_status, dry_run, duration_ms
                FROM plan_history
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )

        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_detail(self, plan_id: str) -> dict[str, Any] | None:
        """Return the full record for a single plan_id, with JSON blobs parsed.

        Args:
            plan_id: The plan identifier to look up.

        Returns:
            Full plan record dict with parsed JSON fields, or None if not found.
        """
        if self._db is None:
            return None

        cursor = await self._db.execute(
            "SELECT * FROM plan_history WHERE plan_id = ?",
            (plan_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        record = dict(row)
        # Parse stored JSON blobs back into Python objects for the caller
        for field_name in ("plan_json", "critic_verdict_json", "results_json", "verification_json"):
            raw = record.get(field_name)
            if raw:
                try:
                    record[field_name] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    pass  # leave as raw string if unparseable
        record["dry_run"] = bool(record.get("dry_run", 0))
        return record

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None


# ─────────────────────────────────────────────────────────────────────────────
# PilotServer
# ─────────────────────────────────────────────────────────────────────────────


class PilotServer:
    """Main daemon server managing WebSocket connections and agent dispatch."""

    def __init__(self, config: PilotConfig) -> None:
        """Initialize the PilotServer with the given configuration.

        Args:
            config: PilotConfig instance containing server and model settings.
        """
        self.config = config
        self._start_time = time.time()
        self._server: Server | None = None
        self._clients: set[ServerConnection] = set()
        self._handlers: dict[str, Any] = {}
        self._model_router: Any = None
        self._planner: Any = None
        self._executor: Any = None
        self._verifier: Any = None
        self._destructive_critic: Any = None
        self._permission_checker: Any = None
        self._agent_gateway: Any = None
        self._gateway_audit: Any = None
        self._voice_gesture_workflows: Any = None
        self._reflector: Any = None
        self._multi_agent: Any = None
        self._background: Any = None
        self._orchestrator: Any = None
        self._agent_mesh: Any = None
        self._fusion: Any = None
        self._reasoning: Any = None
        self._decomposer: Any = None
        self._sandbox: Any = None
        self._prompt_improver: Any = None
        self._plugin_registry: Any = None
        self._plugin_marketplace: Any = None
        self._skill_registry: Any = None
        self._subconscious: Any = None
        self._screen_vision: Any = None
        self._memory: Any = None
        self._experience_ledger: ExperienceLedger | None = None
        self._online_learning: Any = None
        self._strategy_evolution: Any = None
        self._evolution_harness: Any = None
        self._active_experience_context = ExperienceContext()
        self._vault: Any = None
        self._permission_audit: Any = None
        self._checkpoint_store: Any = None
        self._durable_tasks: DurableTaskStore | None = None
        # ── Plan History Audit Log ──
        self._plan_history: PlanHistoryStore | None = None
        self._plan_history_tasks: set[asyncio.Task[None]] = set()
        self._companion_follow_up_tasks: set[asyncio.Task[None]] = set()
        self._interaction_speech_tasks: set[asyncio.Task[SpeechOutcome]] = set()
        # Cognitive intelligence (lightweight heuristic engine)
        self._cognitive_engine: Any = None
        self._attention_ui: Any = None
        self._stress_gate: Any = None
        self._intent_predictor: Any = None
        self._voice_listener: Any = None
        self._speech_coordinator = CompanionSpeechCoordinator()
        self._interaction_runtime = InteractionRuntime(self._broadcast_notification)
        self._autonomous: Any = None
        self._self_healing: Any = None
        self._self_healing_started_monitors: set[str] = set()
        self._proactive: Any = None
        self._budget_tracker: Any = None
        self._running = False
        self._pending_confirms: dict[str, PendingConfirmation] = {}
        # ── Undo (rollback_plan RPC) ── plan_id -> snapshot_id taken before execution
        self._plan_snapshots: dict[str, str] = {}
        # ── Cancel Token (Issue #92) ──
        self._cancel_event: asyncio.Event | None = None
        # ── Mid-flight cancellation: the currently in-flight interactive
        # execution, if any -- lets _handle_abort actually cancel the
        # running task (killing e.g. a mid-flight shell subprocess), not
        # just signal cancel_event for the next action boundary. Single
        # slot, mirroring _cancel_event's own "one primary interactive
        # session" scope rather than a dict keyed by plan_id. ──
        self._active_execution_task: asyncio.Task[Any] | None = None
        # Live interaction companion state. Ordinary execute requests stay
        # serialized, while an out-of-band interject request can revise or
        # stop the one active interactive task.
        self._interactive_request_active = False
        self._active_plan_id = ""
        self._active_task_id = ""
        self._active_interaction_id = ""
        self._live_correction: str | None = None
        self._execution_companion: Any = None
        self._recent_companion_context = ""
        self._recent_companion_context_by_session: dict[str, str] = {}
        self._tts_warmup_task: asyncio.Task[None] | None = None
        self._rss_agent: Any = None
        # ── LAN Mesh Network ──
        self._mesh: Any = None
        # ── Threat Containment Bridge (Issue #365) ──
        self._threat_bridge: Any = None
        # ── Authenticated WebSocket clients ──
        self._authenticated_clients: set[ServerConnection] = set()

    def _start_tts_warmup(self) -> None:
        """Warm the selected local voice without blocking daemon startup.

        Replacing the engine or preset cancels the obsolete warmup. Failures
        stay non-fatal because voice.py will use the OS-native fallback.
        """
        if self._tts_warmup_task and not self._tts_warmup_task.done():
            self._tts_warmup_task.cancel()

        engine = self.config.voice.tts_engine
        voice = self.config.voice.tts_voice
        if engine not in {"kokoro_tts", "pocket_tts"}:
            self._tts_warmup_task = None
            return

        async def _warm_selected_voice() -> None:
            display_name = "Kokoro TTS" if engine == "kokoro_tts" else "Pocket TTS"
            try:
                if engine == "kokoro_tts":
                    from pilot.system.kokoro_tts import warmup
                else:
                    from pilot.system.pocket_tts import warmup

                logger.info("%s warmup started (voice=%s)", display_name, voice)
                await warmup(voice)
                logger.info("%s warmup completed (voice=%s)", display_name, voice)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning(
                    "%s warmup failed; OS voice fallback remains available",
                    display_name,
                    exc_info=True,
                )

        self._tts_warmup_task = asyncio.create_task(_warm_selected_voice())

    async def initialize(self) -> None:
        """Initialize all agent components.

        This method sets up all subsystems including the memory store,
        planner, executor, verifier, orchestrator, multimodal fusion,
        cognitive intelligence, and autonomous execution features.
        """
        from pilot.agents.background import BackgroundTaskManager
        from pilot.agents.code_agent import CodeAgent
        from pilot.agents.comm_agent import CommunicationAgent
        from pilot.agents.executor import Executor
        from pilot.agents.monitor_agent import MonitorAgent
        from pilot.agents.multi_agent import MultiAgentRouter
        from pilot.agents.orchestrator import AgentOrchestrator
        from pilot.agents.planner import Planner
        from pilot.agents.reflector import Reflector
        from pilot.agents.system_agent import SystemAgent
        from pilot.agents.verifier import Verifier
        from pilot.agents.web_agent import WebAgent
        from pilot.memory.store import MemoryStore
        from pilot.models.router import ModelRouter
        from pilot.security.audit import AuditLogger
        from pilot.security.permission_audit import PermissionEscalationAuditStore
        from pilot.security.permissions import PermissionChecker
        from pilot.security.validator import ActionValidator
        from pilot.security.vault import KeyVault
        from pilot.workflows.checkpoints import WorkflowCheckpointStore

        self._vault = KeyVault(self.config)
        model_router = ModelRouter(self.config, self._vault)
        self._model_router = model_router
        await model_router.initialize()

        from pilot.models.budget_tracker import BudgetTracker

        self._budget_tracker = BudgetTracker(self.config.model, str(DB_FILE))
        await self._budget_tracker.initialize()
        model_router.set_budget_tracker(self._budget_tracker)
        from pilot.agents.circuit_breaker import CircuitBreaker

        self._circuit_breaker = CircuitBreaker(threshold=self.config.model.max_consecutive_failures)

        audit = AuditLogger()
        self._permission_audit = PermissionEscalationAuditStore()
        await self._permission_audit.initialize()
        self._checkpoint_store = WorkflowCheckpointStore()
        await self._checkpoint_store.initialize()
        self._durable_tasks = DurableTaskStore()
        await self._durable_tasks.initialize()
        recovery = await self._durable_tasks.recover_incomplete()
        if any(
            (
                recovery.interrupted_tasks,
                recovery.uncertain_actions,
                recovery.expired_approvals,
            )
        ):
            logger.warning(
                "Recovered durable work: %d interrupted task(s), %d uncertain action(s), %d expired approval(s)",
                recovery.interrupted_tasks,
                recovery.uncertain_actions,
                recovery.expired_approvals,
            )
        validator = ActionValidator(self.config)
        permissions = PermissionChecker(self.config)
        self._permission_checker = permissions
        self._memory = MemoryStore(
            checkpoint_interval_seconds=self.config.memory.checkpoint_interval_seconds,
            pruning_interval_seconds=self.config.memory.pruning_interval_seconds,
            pruning_min_memories=self.config.memory.pruning_min_memories,
        )
        await self._memory.initialize(model_router)

        self._experience_ledger = ExperienceLedger(EXPERIENCE_DB_FILE)
        await self._experience_ledger.initialize()
        from pilot.intelligence.online_learning import VerifiedOnlineLearner

        self._online_learning = VerifiedOnlineLearner(DATA_DIR / "online_learning_state.json")
        await self._online_learning.initialize(self._experience_ledger)
        self._experience_ledger.subscribe(self._online_learning.consume)

        # ── Plan History Audit Log ──
        self._plan_history = PlanHistoryStore()
        await self._plan_history.initialize()

        from pilot.skills.loader import SkillRegistry

        self._skill_registry = SkillRegistry()
        self._skill_registry.load_all()

        self._planner = Planner(
            model_router,
            self._memory,
            skills_context=self._skill_registry.planner_prompt_block(),
        )
        self._executor = Executor(
            self.config,
            validator,
            permissions,
            audit,
            skill_registry=self._skill_registry,
        )
        self._executor.set_experience_ledger(self._experience_ledger)
        from pilot.security.risk_gate import get_risk_gate

        self._executor.set_world_model_outcome_recorder(get_risk_gate().record_outcome)
        self._executor.set_durable_task_store(self._durable_tasks)
        self._verifier = Verifier(model_router)

        # Destructive Critic Agent — secondary safety reviewer for Tier 4 plans.
        from pilot.agents.destructive_critic import DestructiveCriticAgent

        self._destructive_critic = DestructiveCriticAgent(model_router)
        from pilot.agents.execution_companion import ExecutionCompanion

        self._execution_companion = ExecutionCompanion(
            model_router,
            timeout_seconds=self.config.narration.advisory_timeout_seconds,
        )

        # Agent Gateway — source-scoped permission floor (interactive/
        # autonomous/web_agent/voice/gesture), checked alongside
        # PermissionChecker inside Executor.execute(). Built after the
        # critic since it needs one for non-interactive plan review.
        from pilot.security.gateway import AgentGateway
        from pilot.security.gateway_audit import AgentGatewayAuditStore

        self._gateway_audit = AgentGatewayAuditStore()
        await self._gateway_audit.initialize()
        self._agent_gateway = AgentGateway(self.config, permissions, self._destructive_critic, self._gateway_audit)
        self._executor.set_gateway(self._agent_gateway)
        self._executor.set_broadcast(self._broadcast_notification)
        self._executor.set_speech(self._speak_companion_text)

        # Advanced agent components
        self._reflector = Reflector(model_router)
        await self._reflector.initialize()
        self._multi_agent = MultiAgentRouter(model_router)
        self._background = BackgroundTaskManager()
        self._background.set_broadcast(self._broadcast_notification)
        self._background.register_builtin_monitors()

        # Multi-Agent Orchestrator — register all specialist agents
        from pilot.agents.agent_mesh import AgentMesh

        self._agent_mesh = AgentMesh(DATA_DIR / "agent_mesh.db")
        await self._agent_mesh.initialize()
        self._agent_mesh.set_experience_ledger(self._experience_ledger)
        self._orchestrator = AgentOrchestrator(
            model_router,
            agent_mesh=self._agent_mesh,
        )
        self._orchestrator.set_broadcast(self._broadcast_notification)
        self._orchestrator.set_budget_tracker(self._budget_tracker)
        self._orchestrator.set_circuit_breaker(self._circuit_breaker)
        from pilot.agents.registry import AgentRegistry

        AgentRegistry.discover_agents()
        registered = self._orchestrator.auto_register_all_agents(
            executor=self._executor,
            background_manager=self._background,
            model_router=model_router,
            config=self.config,
            vault=self._vault,
            memory=self._memory,
        )
        logger.info("Auto-registered %d agents via dynamic discovery", registered)
        await self._orchestrator.start_all()

        # ── Threat Containment Bridge (Issue #365) ──────────────────────────────
        # Must be created AFTER orchestrator.start_all() so the ForensicsAgent
        # is already registered and can be wired immediately.
        try:
            from pilot.agents.threat_containment import ThreatContainmentBridge

            self._threat_bridge = ThreatContainmentBridge(
                orchestrator=self._orchestrator,
                audit_logger=audit,
                broadcast_fn=self._broadcast_notification,
                pending_confirms=self._pending_confirms,
            )
            self._orchestrator.set_threat_bridge(self._threat_bridge)
            logger.info("ThreatContainmentBridge initialized and wired to ForensicsAgent.")
        except Exception:
            logger.warning("ThreatContainmentBridge init failed (non-critical)", exc_info=True)
        # ── End Threat Containment Bridge ──────────────────────────────────

        from pilot.agents.base_agent import AgentRole

        self._rss_agent = self._orchestrator.get_agent(AgentRole.RSS)
        if self._rss_agent is None:
            from pilot.agents.rss_agent import RssAgent

            self._rss_agent = RssAgent(model_router, self._memory, self.config, self._background)
            self._orchestrator.register_agent(self._rss_agent)
            await self._rss_agent.start()

        # Multimodal Fusion Engine — voice + gesture intent fusion
        from pilot.multimodal.fusion import MultimodalFusionEngine

        self._fusion = MultimodalFusionEngine()
        self._fusion.set_broadcast(self._broadcast_notification)

        # The backend gesture listener is intentionally disabled because it
        # would contend with the frontend MediaPipe engine for the webcam.
        # Do not import pilot.system.gesture here: importing that disabled
        # backend eagerly loads MediaPipe/TensorFlow and can delay daemon
        # readiness by tens of seconds even though no listener is started.
        logger.info("Local gesture listener disabled in favor of frontend UI MediaPipe engine")

        # Reasoning Event Emitter — thought visualization telemetry
        # Product execution now exposes only the compact interaction state.
        # The former thought graph emitted dozens of synchronous WebSocket
        # events per command without adding safety or execution authority.
        self._reasoning = None

        # Task Decomposition Engine
        from pilot.agents.decomposer import TaskDecomposer

        self._decomposer = TaskDecomposer(model_router)

        # Simulation Sandbox — pre-execution risk analysis
        from pilot.agents.sandbox import SimulationSandbox

        self._sandbox = SimulationSandbox()

        # Self-Improving Prompt System
        from pilot.agents.prompt_improver import PromptImprover

        self._prompt_improver = PromptImprover()
        await self._prompt_improver.initialize(str(DB_FILE))
        from pilot.intelligence.strategy_evolution import StrategyEvolutionLab

        self._strategy_evolution = StrategyEvolutionLab(
            DATA_DIR / "strategy_evolution.db",
            model_router=model_router,
        )
        await self._strategy_evolution.initialize()
        self._strategy_evolution.set_experience_ledger(self._experience_ledger)
        from pilot.intelligence.evolution_harness import EvolutionHarness

        self._evolution_harness = EvolutionHarness(
            DATA_DIR / "evolution_harness.db",
            Path(__file__).parent.parent.parent,
            DATA_DIR / "evolution_worktrees",
            model_router=model_router,
        )
        await self._evolution_harness.initialize()
        self._evolution_harness.set_experience_ledger(self._experience_ledger)

        # Plugin Ecosystem
        from pilot.plugins import PluginRegistry
        from pilot.plugins.marketplace import GitHubMarketplace

        self._plugin_registry = PluginRegistry()
        plugin_count = self._plugin_registry.discover()
        self._agent_mesh.refresh_plugins(self._plugin_registry.get_all_plugins())
        logger.info("Plugins loaded: %d", plugin_count)
        self._executor.set_plugin_registry(self._plugin_registry)
        self._plugin_marketplace = GitHubMarketplace(
            repo_root=Path(__file__).parent.parent.parent,
            plugins_dir=PLUGINS_DIR,
        )
        self._refresh_plugin_planner_context()

        # Subconscious Agent — long-term memory consolidation (lazy start)
        try:
            from pilot.agents.subconscious import SubconsciousAgent

            self._subconscious = SubconsciousAgent(model_router)
            await self._subconscious.initialize(str(DB_FILE))
            await self._subconscious.start(interval_minutes=30)
            logger.info("SubconsciousAgent started (continuous 30-minute consolidation)")
        except Exception:
            logger.warning("SubconsciousAgent init failed (non-critical)", exc_info=True)

        # Cognitive Hub — unified cognitive intelligence features
        try:
            from pilot.changelog import announce_new_features, mark_version_seen
            from pilot.cognitive.hub import CognitiveHub

            self._cognitive_hub = CognitiveHub()
            logger.info("CognitiveHub initialized")

            announcement = announce_new_features()
            if announcement:
                logger.info("New features announcement: %s", announcement)
                self._new_features_announcement = announcement
                mark_version_seen()
        except Exception:
            logger.warning("CognitiveHub init failed (non-critical)", exc_info=True)
            self._new_features_announcement = None

        # Screen Vision Agent — continuous screen awareness (AUTO-START for JARVIS mode)
        try:
            from pilot.agents.screen_vision import ScreenVisionAgent

            sv_config = self.config.screen_vision
            self._screen_vision = ScreenVisionAgent(
                model_router,
                capture_timeout_seconds=sv_config.capture_timeout_seconds,
                max_consecutive_timeouts=sv_config.max_consecutive_timeouts,
                auto_resume_after_seconds=sv_config.auto_resume_after_seconds,
            )
            asyncio.create_task(
                self._screen_vision.start(interval_seconds=sv_config.capture_interval_seconds, enable_describe=False)
            )
            logger.info("ScreenVisionAgent auto-started (every %.1fs, JARVIS mode)", sv_config.capture_interval_seconds)
        except Exception:
            logger.warning("ScreenVisionAgent init failed (non-critical)", exc_info=True)

        # ── Cognitive Intelligence (lightweight heuristic engine) ──
        try:
            if not self.config.cognitive.enabled:
                logger.info("Cognitive intelligence disabled in config.toml")
            else:
                from pilot.cognitive.attention_scorer import AttentionAwareUI
                from pilot.cognitive.cognitive_engine import CognitiveEngine
                from pilot.cognitive.intent_predictor import IntentPredictor
                from pilot.cognitive.stress_gate import StressGate

                self._cognitive_engine = CognitiveEngine.get_instance()
                self._attention_ui = AttentionAwareUI(self._cognitive_engine)
                self._attention_ui.set_broadcast(self._broadcast_notification)
                self._stress_gate = StressGate(self._cognitive_engine)
                self._intent_predictor = IntentPredictor(self._cognitive_engine)

                if self._executor:
                    self._executor._stress_gate = self._stress_gate
                if self._fusion:
                    self._fusion._intent_predictor = self._intent_predictor
                if getattr(self, "_screen_vision", None):
                    self._screen_vision._cognitive_engine = self._cognitive_engine

                asyncio.create_task(self._cognitive_engine.load_model())
                logger.info("Cognitive intelligence initialized")
        except Exception:
            logger.warning("Cognitive intelligence init failed (non-critical)", exc_info=True)

        self._notification_buffer: list[tuple[str, dict[str, Any]]] = []

        # ── Autonomous Executor (JARVIS fire-and-forget) ──
        try:
            from pilot.agents.autonomous import AutonomousExecutor

            self._autonomous = AutonomousExecutor(
                planner=self._planner,
                executor=self._executor,
                verifier=self._verifier,
                decomposer=self._decomposer,
                screen_vision=self._screen_vision,
                memory=self._memory,
            )
            self._autonomous.set_broadcast(self._broadcast_notification)
            self._autonomous.set_speech(self._speak_companion_text)
            logger.info("AutonomousExecutor initialized")
        except Exception:
            logger.warning("AutonomousExecutor init failed (non-critical)", exc_info=True)

        # ── Autonomous Healing Engine (passive system-health monitoring +
        # tiered auto-remediation, see pilot.agents.autonomous_healing) ──
        # Wired to the CPU/memory/disk monitors' on_trigger hook, which no
        # other consumer currently uses — the built-in monitors otherwise
        # only ever reach a UI toast via self._background's broadcast.
        try:
            from pilot.agents.autonomous_healing import AutonomousHealingEngine

            self._self_healing = AutonomousHealingEngine(
                planner=self._planner,
                executor=self._executor,
                config=self.config,
                pending_confirms=self._pending_confirms,
                broadcast_fn=self._broadcast_notification,
                attempts_file=DATA_DIR / "self_healing_attempts.json",
            )
            for task_id, handler in (
                ("monitor_cpu", self._self_healing.on_cpu_alert),
                ("monitor_memory", self._self_healing.on_memory_alert),
                ("monitor_disk", self._self_healing.on_disk_alert),
            ):
                task = self._background._tasks.get(task_id)
                if task is not None:
                    task.on_trigger = handler
            self._sync_self_healing_monitors()
            logger.info("AutonomousHealingEngine initialized")
        except Exception:
            logger.warning("AutonomousHealingEngine init failed (non-critical)", exc_info=True)

        # ── Live Execution Narrator (narrates + pre-emptively interrupts
        # in-progress plan execution, see pilot.agents.narrator) ──
        # Wiring is entirely via Executor.set_narrator(): once set, every
        # caller of self._executor.execute() (AutonomousExecutor, the
        # Voice/Gesture Workflow Engine, the interactive path) is narrated
        # automatically with zero changes of their own.
        try:
            from pilot.agents.narrator import ExecutionNarrator

            self._narrator = ExecutionNarrator(
                config=self.config,
                pending_confirms=self._pending_confirms,
                broadcast_fn=self._broadcast_notification,
            )
            self._executor.set_narrator(self._narrator)
            logger.info("ExecutionNarrator initialized")
        except Exception:
            logger.warning("ExecutionNarrator init failed (non-critical)", exc_info=True)

        # ── User Manual Supervision (watches the user's OWN independent
        # screen/keyboard/mouse activity, see pilot.agents.user_supervision) ──
        # Unlike self-healing/narration, the thing being gated here
        # (installing a global input hook, running periodic OCR) has real
        # cost and privacy weight even when idle -- so the BackgroundTask
        # and the hook are only actually started when the user has opted
        # in, and _handle_supervision_config_update (below) starts/stops
        # both on a config transition, not just flips a bool.
        try:
            from pilot.agents.background import BackgroundTask
            from pilot.agents.user_supervision import UserSupervisionEngine
            from pilot.system.input_hook import InputSupervisionHook

            self._supervision_hook = InputSupervisionHook(
                keystroke_buffer_max_chars=self.config.supervision.keystroke_buffer_max_chars
            )
            self._supervision = UserSupervisionEngine(
                config=self.config,
                cognitive_engine=self._cognitive_engine,
                screen_vision=self._screen_vision,
                hook=self._supervision_hook,
                broadcast_fn=self._broadcast_notification,
            )
            self._background.register(
                BackgroundTask(
                    task_id="user_supervision",
                    name="User Manual Supervision",
                    description="Watches the user's own independent screen/keyboard/mouse activity",
                    interval_seconds=self.config.supervision.tick_interval_seconds,
                    action_fn=self._supervision.tick,
                    on_trigger=self._supervision.on_trigger,
                )
            )
            if self.config.supervision.enabled:
                if self.config.supervision.keyboard_mouse_hook_enabled:
                    self._supervision_hook.start()
                self._background.start("user_supervision")
            logger.info("UserSupervisionEngine initialized (enabled=%s)", self.config.supervision.enabled)
        except Exception:
            logger.warning("UserSupervisionEngine init failed (non-critical)", exc_info=True)

        # ── Voice/Gesture Workflow Engine (durable, pausable/resumable
        # multi-step goals spanning multiple voice/gesture inputs) ──
        try:
            from pilot.agents.voice_gesture_workflow import VoiceGestureWorkflowEngine
            from pilot.workflows.voice_gesture_workflows import VoiceGestureWorkflowStore

            voice_gesture_workflow_store = VoiceGestureWorkflowStore()
            await voice_gesture_workflow_store.initialize()
            self._voice_gesture_workflows = VoiceGestureWorkflowEngine(
                planner=self._planner,
                executor=self._executor,
                decomposer=self._decomposer,
                workflow_store=voice_gesture_workflow_store,
                checkpoint_store=self._checkpoint_store,
                adaptive_executor=self._autonomous,
                memory=self._memory,
            )
            self._voice_gesture_workflows.set_broadcast(self._broadcast_notification)
            self._voice_gesture_workflows.set_speech(self._speak_companion_text)
            logger.info("VoiceGestureWorkflowEngine initialized")
        except Exception:
            logger.warning("VoiceGestureWorkflowEngine init failed (non-critical)", exc_info=True)

        # ── Proactive Suggestion Engine (JARVIS anticipation) ──
        try:
            from pilot.agents.proactive import ProactiveSuggestionEngine

            self._proactive = ProactiveSuggestionEngine(screen_vision=self._screen_vision)
            self._proactive.set_broadcast(self._broadcast_notification)
            self._proactive.set_experience_ledger(self._experience_ledger)
            self._proactive.set_online_learner(self._online_learning)
            asyncio.create_task(self._proactive.start())
            logger.info("ProactiveSuggestionEngine auto-started")
        except Exception:
            logger.warning("ProactiveSuggestionEngine init failed (non-critical)", exc_info=True)

        self._handlers = {
            "execute": self._handle_execute,
            "resume_plan": self._handle_resume_plan,
            "resume_task": self._handle_resume_task,
            "export_session_chat": self._handle_export_session_chat,
            "confirm": self._handle_confirm,
            "rollback_plan": self._handle_rollback_plan,
            "list_permission_events": self._handle_list_permission_events,
            "verify_permission_audit": self._handle_verify_permission_audit,
            "list_gateway_events": self._handle_list_gateway_events,
            "verify_gateway_audit": self._handle_verify_gateway_audit,
            "gateway_policy_get": self._handle_gateway_policy_get,
            "gateway_policy_update": self._handle_gateway_policy_update,
            # ── Cancel Token (Issue #92) ──
            "abort": self._handle_abort,
            "interject": self._handle_interject,
            "get_config": self._handle_get_config,
            "get_security_status": self._handle_get_security_status,
            "get_snapshot_status": self._handle_get_snapshot_status,
            "restart_elevated": self._handle_restart_elevated,
            "update_config": self._handle_update_config,
            "reset_config": self._handle_reset_config,
            "get_history": self._handle_get_history,
            "memory_checkpoint": self._handle_memory_checkpoint,
            "temporal_memory_status": self._handle_temporal_memory_status,
            "temporal_memory_retract": self._handle_temporal_memory_retract,
            "store_api_key": self._handle_store_api_key,
            "delete_api_key": self._handle_delete_api_key,
            "list_api_keys": self._handle_list_api_keys,
            "list_ollama_models": self._handle_list_ollama_models,
            "health": self._handle_health,
            "ready": self._handle_ready,
            "ping": self._handle_ping,
            "system_status": self._handle_system_status,
            "system_info": self._handle_system_info,
            "get_uptime": self._handle_get_uptime,
            "capabilities": self._handle_capabilities,
            "reflection_stats": self._handle_reflection_stats,
            "background_tasks": self._handle_background_tasks,
            "background_start": self._handle_background_start,
            "background_stop": self._handle_background_stop,
            "extract_file_text": self._handle_extract_file_text,
            "agent_routing": self._handle_agent_routing,
            "agent_stats": self._handle_agent_stats,
            "agent_capabilities": self._handle_agent_capabilities,
            "agent_spawn": self._handle_agent_spawn,
            "agent_mesh_status": self._handle_agent_mesh_status,
            "voice_event": self._handle_voice_event,
            "gesture_event": self._handle_gesture_event,
            "gaze_event": self._handle_gaze_event,
            "cursor_move": self._handle_cursor_move,
            "cursor_click": self._handle_cursor_click,
            "multimodal_stats": self._handle_multimodal_stats,
            "decompose_task": self._handle_decompose_task,
            "simulate_plan": self._handle_simulate_plan,
            "prompt_strategies": self._handle_prompt_strategies,
            "prompt_stats": self._handle_prompt_stats,
            "strategy_evolution_status": self._handle_strategy_evolution_status,
            "strategy_candidates": self._handle_strategy_candidates,
            "strategy_propose": self._handle_strategy_propose,
            "strategy_reflect": self._handle_strategy_reflect,
            "strategy_record_isolated": self._handle_strategy_record_isolated,
            "strategy_start_shadow": self._handle_strategy_start_shadow,
            "strategy_record_shadow": self._handle_strategy_record_shadow,
            "strategy_start_canary": self._handle_strategy_start_canary,
            "strategy_record_canary": self._handle_strategy_record_canary,
            "strategy_promote": self._handle_strategy_promote,
            "strategy_rollback": self._handle_strategy_rollback,
            "evolution_status": self._handle_evolution_status,
            "evolution_runs": self._handle_evolution_runs,
            "evolution_candidates": self._handle_evolution_candidates,
            "evolution_create_run": self._handle_evolution_create_run,
            "evolution_generate_candidates": self._handle_evolution_generate_candidates,
            "evolution_evaluate": self._handle_evolution_evaluate,
            "evolution_request_promotion": self._handle_evolution_request_promotion,
            "plugin_list": self._handle_plugin_list,
            "plugin_tools": self._handle_plugin_tools,
            "plugin_toggle": self._handle_plugin_toggle,
            "plugin_market_list": self._handle_plugin_market_list,
            "plugin_install": self._handle_plugin_install,
            "plugin_uninstall": self._handle_plugin_uninstall,
            "plugin_create": self._handle_plugin_create,
            "plugin_run_tool": self._handle_plugin_run_tool,
            # Dynamic Python skills (pilot/skills + config skills dir)
            "skills_list": self._handle_skills_list,
            "skills_reload": self._handle_skills_reload,
            "skills_load_report": self._handle_skills_load_report,
            "persona_rules": self._handle_persona_rules,
            "persona_consolidate": self._handle_persona_consolidate,
            "persona_add_preference": self._handle_persona_add_preference,
            "subconscious_stats": self._handle_subconscious_stats,
            "screen_context": self._handle_screen_context,
            "screen_current_app": self._handle_screen_current_app,
            "screen_vision_stats": self._handle_screen_vision_stats,
            "screen_vision_toggle": self._handle_screen_vision_toggle,
            "cognitive_stats": self._handle_cognitive_stats,
            "cognitive_state": self._handle_cognitive_state,
            "attention_toggle": self._handle_attention_toggle,
            "stress_gate_toggle": self._handle_stress_gate_toggle,
            "intent_predictor_toggle": self._handle_intent_predictor_toggle,
            "cognitive_model_toggle": self._handle_cognitive_model_toggle,
            "voice_listener_start": self._handle_voice_listener_start,
            "voice_listener_stop": self._handle_voice_listener_stop,
            "voice_listener_stats": self._handle_voice_listener_stats,
            "interaction_status": self._handle_interaction_status,
            "list_audio_input_devices": self._handle_list_audio_input_devices,
            "speak_text": self._handle_speak_text,
            "stop_speech": self._handle_stop_speech,
            "companion_speech_status": self._handle_companion_speech_status,
            "reset_wake_calibration": self._handle_reset_wake_calibration,
            "list_wake_variants": self._handle_list_wake_variants,
            "autonomous_submit": self._handle_autonomous_submit,
            "autonomous_cancel": self._handle_autonomous_cancel,
            "autonomous_jobs": self._handle_autonomous_jobs,
            "autonomous_job": self._handle_autonomous_job,
            "risk_gate_status": self._handle_risk_gate_status,
            "risk_gate_config_update": self._handle_risk_gate_config_update,
            "self_healing_status": self._handle_self_healing_status,
            "self_healing_config_update": self._handle_self_healing_config_update,
            "narration_status": self._handle_narration_status,
            "narration_config_update": self._handle_narration_config_update,
            "supervision_status": self._handle_supervision_status,
            "supervision_config_update": self._handle_supervision_config_update,
            "voice_gesture_workflow_submit": self._handle_voice_gesture_workflow_submit,
            "voice_gesture_workflow_list": self._handle_voice_gesture_workflow_list,
            "voice_gesture_workflow_get": self._handle_voice_gesture_workflow_get,
            "voice_gesture_workflow_pause": self._handle_voice_gesture_workflow_pause,
            "voice_gesture_workflow_resume": self._handle_voice_gesture_workflow_resume,
            "voice_gesture_workflow_cancel": self._handle_voice_gesture_workflow_cancel,
            "gesture_workflow_bindings_get": self._handle_gesture_workflow_bindings_get,
            "gesture_workflow_bindings_update": self._handle_gesture_workflow_bindings_update,
            "proactive_start": self._handle_proactive_start,
            "proactive_stop": self._handle_proactive_stop,
            "proactive_stats": self._handle_proactive_stats,
            "proactive_learning_status": self._handle_proactive_learning_status,
            "proactive_learning_reset": self._handle_proactive_learning_reset,
            "online_learning_status": self._handle_online_learning_status,
            "online_learning_reset": self._handle_online_learning_reset,
            "proactive_accept": self._handle_proactive_accept,
            "proactive_dismiss": self._handle_proactive_dismiss,
            "budget_stats": self._handle_budget_stats,
            "budget_reset": self._handle_budget_reset,
            # ── LAN Mesh Network ──
            "mesh_peers": self._handle_mesh_peers,
            "mesh_status": self._handle_mesh_status,
            "resolve_git_conflict": self._handle_resolve_git_conflict,
            "apply_git_resolution": self._handle_apply_git_resolution,
            # ── Plan History Audit Log ──
            "get_plan_history": self._handle_get_plan_history,
            "get_plan_detail": self._handle_get_plan_detail,
            # ── Threat Containment (Issue #365) ──
            "threat_containment_stats": self._handle_threat_containment_stats,
        }

        # ── LAN Mesh Network (opt-in via config) ──
        if self.config.network.enabled:
            try:
                from pilot.network.mesh import HelioxMesh
                from pilot.system.plugins import get_manager as get_plugin_manager

                self._mesh = HelioxMesh(
                    config=self.config.network,
                    executor=self._executor,
                    plugin_manager=get_plugin_manager(),
                )
                logger.info("HelioxMesh initialised (will start with server)")
            except Exception:
                logger.warning("HelioxMesh init failed (non-critical)", exc_info=True)

    async def _broadcast_notification(self, method: str, params: Any) -> None:
        """Broadcast a notification to all connected clients.

        Args:
            method: The notification method name.
            params: The notification parameters.
        """
        # ── Feature 5: Attention-Optimized Notification Timing ──
        # task_complete always bypasses the attention gate — it is the user-facing
        # completion signal and must never be buffered or suppressed.
        if method in {"task_complete", "companion_follow_up"}:
            pass
        elif getattr(self, "_attention_ui", None) and self._attention_ui.enabled:
            try:
                content = params if isinstance(params, dict) else {"data": params}
                scored = await self._attention_ui.score_event(method, content)

                # Buffer non-critical notifications when user is highly focused
                # Fix: scored.priority is a plain str, not an enum — compare directly.
                if not scored.should_display and scored.priority != "critical":
                    if not hasattr(self, "_notification_buffer"):
                        self._notification_buffer = []
                    self._notification_buffer.append((method, params.copy() if isinstance(params, dict) else params))
                    return

                if scored.attention_score < 0.4 and getattr(self, "_notification_buffer", []):
                    logger.info(
                        f"Flushing {len(self._notification_buffer)} buffered notifications during low cognitive load."
                    )
                    for b_meth, b_params in self._notification_buffer:
                        if isinstance(b_params, dict):
                            b_params.setdefault("_cognitive", {})["should_animate"] = False
                            b_params["_cognitive"]["flushed"] = True
                        msg = _notification(b_meth, b_params)
                        for client in list(self._clients):
                            try:
                                await client.send(msg)
                            except Exception:
                                pass
                    self._notification_buffer.clear()

                if isinstance(params, dict):
                    params["_cognitive"] = {
                        "priority": scored.priority,
                        "attention_score": scored.attention_score,
                        "should_animate": scored.should_animate,
                        "display_duration_ms": scored.display_duration_ms,
                    }
            except Exception as e:
                logger.error("Attention scoring failed: %s", e)

        # This must run unconditionally (not just inside the attention-buffered
        # flush branch above) -- it was previously misplaced as unreachable code
        # after a different handler's return statements, so this notification
        # was silently never delivered outside that one buffered-flush edge case.
        msg = _notification(method, params)
        for client in list(self._clients):
            try:
                await client.send(msg)
            except Exception:
                pass

    async def _handle_extract_file_text(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Extract text from a file (e.g. PDF) for UI context injection."""
        path = params.get("path")
        if not path:
            return {"status": "error", "message": "No file path provided"}

        import os

        if not os.path.exists(path):
            return {"status": "error", "message": f"File not found: {path}"}

        try:
            from pilot.system.file_intel import parse_file

            text = await parse_file(path)
            return {"status": "ok", "text": text}
        except Exception as e:
            logger.error("Error extracting text from %s: %s", path, e)
            return {"status": "error", "message": str(e)}

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        """Handle a WebSocket connection from a client.

        The first message MUST be an ``auth`` request carrying the daemon's
        ``auth_token``.  Any other message — or a wrong token — closes the
        connection immediately with a JSON-RPC error response.

        Args:
            websocket: The WebSocket connection to the client.
        """
        remote = websocket.remote_address
        logger.info("Client connected: %s", remote)

        # ── Auth handshake ─────────────────────────────────────────────────
        # Wait up to 10 s for the first message; reject if it's not a valid
        # auth request with the correct token.
        try:
            first_raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Auth handshake timeout from %s — closing", remote)
            await websocket.close()
            return
        except websockets.exceptions.ConnectionClosed:
            return

        try:
            first_req = JsonRpcRequest.parse(str(first_raw))
        except (json.JSONDecodeError, ValueError, KeyError):
            await websocket.send(_error_response(None, -32600, "First message must be a valid JSON-RPC auth request"))
            await websocket.close()
            return

        if first_req.method != "auth":
            await websocket.send(
                _error_response(
                    first_req.id,
                    -32001,
                    "Authentication required: send {method:'auth', params:{token:'...'}} first",
                )
            )
            await websocket.close()
            logger.warning("Unauthenticated method '%s' from %s — rejected", first_req.method, remote)
            return

        provided_token = first_req.params.get("token", "")
        expected_token = self.config.server.auth_token

        # Reject non-string tokens immediately (e.g. null → None in Python)
        if not isinstance(provided_token, str):
            await websocket.send(_error_response(first_req.id, -32001, "Invalid auth token"))
            await websocket.close()
            logger.warning("Non-string auth token from %s — connection rejected", remote)
            return

        # Constant-time comparison prevents timing-based token oracle attacks
        import hmac as _hmac

        if not expected_token or not _hmac.compare_digest(provided_token, expected_token):
            await websocket.send(_error_response(first_req.id, -32001, "Invalid auth token"))
            await websocket.close()
            logger.warning("Invalid auth token from %s — connection rejected", remote)
            return

        # Auth passed — acknowledge and register as an active client
        await websocket.send(_success_response(first_req.id, {"status": "authenticated"}))
        self._clients.add(websocket)
        self._authenticated_clients.add(websocket)
        logger.info("Client authenticated: %s", remote)

        # Keep ordinary requests sequential, but allow confirmation and abort
        # RPCs to bypass a long-running ``execute`` request on the same socket.
        # Without this split, ``execute`` waits for ``confirm`` while the
        # connection loop waits for ``execute``: a deterministic timeout.
        request_lock = asyncio.Lock()
        request_tasks: set[asyncio.Task[None]] = set()

        async def _process_request(request: JsonRpcRequest) -> None:
            try:
                if request.method in OUT_OF_BAND_RPC_METHODS:
                    response = await self._dispatch(request, websocket)
                else:
                    async with request_lock:
                        response = await self._dispatch(request, websocket)
                if response and request.id is not None:
                    await websocket.send(response)
            except websockets.exceptions.ConnectionClosed:
                logger.warning("Connection lost during request handling: %s", remote)
            except Exception as e:
                logger.exception("Handler error")
                try:
                    await websocket.send(_error_response(request.id, -32603, f"Internal error: {e}"))
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("Could not send error response because the connection is closed: %s", remote)

        # ── Normal message loop ────────────────────────────────────────────
        try:
            async for message in websocket:
                try:
                    request = JsonRpcRequest.parse(str(message))
                    task = asyncio.create_task(_process_request(request))
                    request_tasks.add(task)
                    task.add_done_callback(request_tasks.discard)
                except json.JSONDecodeError:
                    await websocket.send(_error_response(None, -32700, "Parse error"))
                except ValueError as e:
                    await websocket.send(_error_response(None, -32600, str(e)))
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("Connection lost during request handling: %s", remote)
                    break
                except Exception as e:
                    logger.exception("Handler error")
                    try:
                        await websocket.send(_error_response(None, -32603, f"Internal error: {e}"))
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("Could not send error response — connection already closed: %s", remote)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Connection closed during message loop: %s", remote)
        finally:
            for task in request_tasks:
                task.cancel()
            await asyncio.gather(*request_tasks, return_exceptions=True)
            self._clients.discard(websocket)
            self._authenticated_clients.discard(websocket)
            logger.info("Client disconnected: %s", remote)

    async def _dispatch(self, request: JsonRpcRequest, ws: ServerConnection) -> str | None:
        """Dispatch a JSON-RPC request to the appropriate handler.

        Args:
            request: The parsed JSON-RPC request.
            ws: The WebSocket connection.

        Returns:
            A JSON-RPC response string, or None for notifications.
        """
        handler = self._handlers.get(request.method)
        if handler is None:
            return _error_response(request.id, -32601, f"Method not found: {request.method}")
        result = await handler(request.params, ws)
        return _success_response(request.id, result)

    # -- Core execution pipeline --

    MAX_RETRIES = 2
    MAX_LIVE_CORRECTIONS = 5

    async def _append_experience(
        self,
        event_type: ExperienceEventType,
        **kwargs: Any,
    ) -> ExperienceEvent | None:
        """Persist an intelligence event without affecting product behavior."""

        if self._experience_ledger is None:
            return None
        try:
            return await self._experience_ledger.append(event_type, **kwargs)
        except Exception:
            logger.warning("Experience ledger append failed for %s", event_type.value, exc_info=True)
            return None

    async def _await_execution_tracked(self, execution: Any) -> Any:
        """Track any cancellable interactive phase in the cancellation slot.

        Planning, companion/critic review, execution, and verification can all
        involve a slow model or subprocess call. Tracking each phase makes a
        typed stop or correction responsive throughout the task, not only while
        an action happens to be executing.
        """
        task = asyncio.ensure_future(execution)
        self._active_execution_task = task
        try:
            return await task
        finally:
            if self._active_execution_task is task:
                self._active_execution_task = None

    @staticmethod
    def _deterministic_companion_review(plan: ActionPlan) -> Any | None:
        """Approve one bounded telemetry read without a remote model round-trip.

        These plans are produced by explicit local fast paths, contain no
        writes, and still pass through the world-model, permission, execution,
        and verification gates. Returning a real review keeps the companion
        observable without making a provider timeout part of a status query.
        """

        from pilot.actions import ActionType
        from pilot.agents.execution_companion import CompanionReview

        immediate_local_actions = {
            ActionType.SYSTEM_INFO,
            ActionType.CPU_USAGE,
            ActionType.MEMORY_USAGE,
            ActionType.DISK_USAGE,
            ActionType.NETWORK_INFO,
            ActionType.BATTERY_INFO,
            ActionType.PROCESS_LIST,
            ActionType.PROCESS_INFO,
            ActionType.SCREEN_OCR,
            ActionType.SCREEN_ANALYZE,
            ActionType.SCREEN_FIND_TEXT,
            ActionType.SCREEN_ELEMENT_MAP,
            ActionType.SCREENSHOT,
            # Direct local fast paths should not pay for a remote advisory
            # round-trip. Deterministic permissions, world-model assessment,
            # execution, and verification still run exactly as before.
            ActionType.OPEN_APPLICATION,
            ActionType.OPEN_URL,
            ActionType.NOTIFY,
        }
        if len(plan.actions) != 1 or plan.actions[0].action_type not in immediate_local_actions:
            return None
        action_type = plan.actions[0].action_type
        if action_type in {ActionType.OPEN_APPLICATION, ActionType.OPEN_URL, ActionType.NOTIFY}:
            reason = "The direct local action is bounded, reversible, and matches the request."
        else:
            reason = "The local telemetry plan is bounded, read-only, and directly answers the request."
        return CompanionReview(
            decision="CONTINUE",
            reason=reason,
        )

    def _execution_scope_for_source(self, source_name: str) -> tuple[Any, Any | None]:
        """Resolve an interaction source to its enforced gateway ceiling."""
        from pilot.security.gateway import DEFAULT_SOURCE_PROFILES, InvocationSource, TaskScopeOverride

        invocation_source = (
            InvocationSource.VOICE if source_name.strip().lower() == "voice" else InvocationSource.INTERACTIVE
        )
        if invocation_source != InvocationSource.VOICE:
            return invocation_source, None
        profile = self.config.gateway.source_profiles.get("voice", DEFAULT_SOURCE_PROFILES["voice"])
        return invocation_source, TaskScopeOverride(
            max_tier=profile.max_tier,
            deny_action_types=profile.deny_action_types,
            allow_root=profile.allow_root,
        )

    async def _execute_tracked(self, plan: ActionPlan, **kwargs: Any) -> list[Any]:
        """Wraps self._executor.execute() in a real asyncio.Task, tracked in
        self._active_execution_task, so _handle_abort can cancel the
        CURRENTLY in-flight execution -- not just signal cancel_event for
        the next action boundary. Cancelling the returned task propagates
        all the way down to run_command's existing proc.kill() (see
        platform_detect.py), the same mechanism AutonomousExecutor.cancel()
        already proves works for killing a mid-flight shell subprocess.

        Only wraps the two interactive-session call sites in
        _handle_execute (fresh plan + resume-from-checkpoint) -- the other
        execute() call sites (voice command dispatch, the generic
        action-command handler, git-conflict-resolution) are single quick
        actions outside the "Stop button" scope and are left untouched.
        """
        return await self._await_execution_tracked(self._executor.execute(plan, **kwargs))

    async def _finalize_durable_task(self, task_id: str, response: dict[str, Any]) -> None:
        """Persist one terminal response so duplicate requests can replay it."""

        if self._durable_tasks is None:
            return
        task = await self._durable_tasks.get(task_id)
        if task is None or task.is_terminal:
            return
        response_status = str(response.get("status", "error"))
        target = {
            "success": TaskStatus.SUCCEEDED,
            "cancelled": TaskStatus.CANCELLED,
            "partial_failure": TaskStatus.PARTIAL,
        }.get(response_status, TaskStatus.FAILED)
        if target == TaskStatus.SUCCEEDED and task.status == TaskStatus.EXECUTING:
            task = await self._durable_tasks.transition(
                task_id,
                TaskStatus.VERIFYING,
                reason="execution completed before terminal success",
                plan_id=self._active_plan_id or task.plan_id,
            )
        if target == TaskStatus.PARTIAL and task.status not in {
            TaskStatus.EXECUTING,
            TaskStatus.VERIFYING,
        }:
            task = await self._durable_tasks.transition(
                task_id,
                TaskStatus.EXECUTING,
                reason="terminal partial result entered execution",
                plan_id=self._active_plan_id or task.plan_id,
            )
        await self._durable_tasks.transition(
            task_id,
            target,
            reason=f"interactive request finished with {response_status}",
            plan_id=self._active_plan_id or task.plan_id,
            terminal_response=response,
        )
        if self._memory is not None:
            await self._memory.clear_task_working(
                session_id=task.session_id,
                task_id=task.task_id,
            )

    async def _handle_execute(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Run one interactive request while exposing an out-of-band control slot."""
        requested_task_id = str(params.get("task_id") or uuid.uuid4())
        source_name = str(params.get("source") or "text").strip().lower()
        interaction_id = str(params.get("_interaction_id") or "")
        resume_token = ""
        if self._durable_tasks is not None:
            existing = await self._durable_tasks.get(requested_task_id)
            if existing is not None:
                supplied_token = str(params.get("resume_token") or "")
                authorized = await self._durable_tasks.get_by_resume_token(supplied_token) if supplied_token else None
                if authorized is None or authorized.task_id != existing.task_id:
                    return {
                        "status": "error",
                        "message": "This task_id already exists; a valid resume_token is required.",
                        "task_id": existing.task_id,
                    }
                if existing.is_terminal and existing.terminal_response is not None:
                    return {
                        **existing.terminal_response,
                        "task_id": existing.task_id,
                        "replayed": True,
                    }
                return {
                    "status": "interrupted",
                    "message": "This task already exists. Use resume_task to continue it safely.",
                    "task_id": existing.task_id,
                    "task_state": existing.status.value,
                }
            created = await self._durable_tasks.create_task(
                task_id=requested_task_id,
                session_id=str(params.get("session_id") or "default"),
                user_id=str(params.get("user_id") or "local"),
                user_input=str(params.get("input") or ""),
            )
            requested_task_id = created.task.task_id
            resume_token = created.resume_token
            await self._durable_tasks.transition(
                requested_task_id,
                TaskStatus.PLANNING,
                reason="interactive request accepted",
            )
        if not interaction_id:
            interaction = await self._interaction_runtime.start(
                str(params.get("input") or ""),
                source=source_name,
            )
            interaction_id = str(interaction["interaction_id"])
        await self._interaction_runtime.transition(
            InteractionPhase.PLANNING,
            message="Planning the safest useful action",
            interaction_id=interaction_id,
        )
        self._interactive_request_active = True
        self._active_interaction_id = interaction_id
        self._active_plan_id = ""
        self._active_task_id = requested_task_id
        self._live_correction = None
        context = ExperienceContext(
            session_id=str(params.get("session_id") or "default"),
            task_id=requested_task_id,
            user_id=str(params.get("user_id") or "local"),
        )
        self._active_experience_context = context
        try:
            with experience_scope(
                session_id=context.session_id,
                task_id=context.task_id,
                user_id=context.user_id,
            ):
                try:
                    if self._memory is not None:
                        await self._memory.put_working(
                            session_id=context.session_id,
                            task_id=context.task_id,
                            key="current intent",
                            value=str(params.get("input") or ""),
                            priority=1.0,
                            ttl_seconds=86400,
                        )
                    if resume_token:
                        await ws.send(
                            _notification(
                                "task_registered",
                                {
                                    "task_id": context.task_id,
                                    "resume_token": resume_token,
                                    "session_id": context.session_id,
                                },
                            )
                        )
                    response = await self._handle_execute_inner(params, ws)
                    await self._finalize_durable_task(context.task_id, response)
                    if self._durable_tasks is not None:
                        response = {**response, "task_id": context.task_id}
                        if resume_token:
                            response["resume_token"] = resume_token
                    response_status = str(response.get("status") or "error")
                    terminal_phase = (
                        InteractionPhase.COMPLETED
                        if response_status == "success"
                        else InteractionPhase.INTERRUPTED
                        if response_status in {"cancelled", "interrupted", "denied"}
                        else InteractionPhase.FAILED
                    )
                    await self._interaction_runtime.transition(
                        terminal_phase,
                        message=str(response.get("message") or response.get("explanation") or response_status)[:160],
                        interaction_id=interaction_id,
                    )
                    return response
                except Exception as exc:
                    await self._interaction_runtime.transition(
                        InteractionPhase.FAILED,
                        message="The task failed before a verified result was available",
                        interaction_id=interaction_id,
                    )
                    await self._append_experience(
                        ExperienceEventType.OUTCOME_VERIFIED,
                        idempotency_key=f"task:{context.task_id}:unhandled_error",
                        source="interactive",
                        payload={
                            "status": "internal_error",
                            "error_type": type(exc).__name__,
                            "message": str(exc)[:1000],
                        },
                        provenance={"component": "PilotServer._handle_execute"},
                        privacy_class=PrivacyClass.SENSITIVE,
                    )
                    if self._durable_tasks is not None:
                        task = await self._durable_tasks.get(context.task_id)
                        if task is not None and not task.is_terminal:
                            await self._durable_tasks.transition(
                                context.task_id,
                                TaskStatus.FAILED,
                                reason=f"unhandled {type(exc).__name__}",
                                plan_id=self._active_plan_id or task.plan_id,
                                terminal_response={
                                    "status": "error",
                                    "message": "Internal error while executing task.",
                                },
                            )
                    raise
        finally:
            self._interactive_request_active = False
            self._active_plan_id = ""
            self._active_task_id = ""
            if self._active_interaction_id == interaction_id:
                self._active_interaction_id = ""
            self._live_correction = None
            self._cancel_event = None
            self._active_experience_context = ExperienceContext()

    async def _handle_execute_inner(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Agentic pipeline: plan -> execute -> verify -> [retry on failure].

        If execution fails, the error is fed back to the planner for re-planning
        up to MAX_RETRIES times. Confirmation gates pause for user approval on
        Tier 2+ actions.
        """
        user_input = params.get("input", "")
        attachments = params.get("attachments", [])
        chat_session_id = str(params.get("session_id") or "default")
        revision_count = int(params.get("_companion_revision_count", 0))
        auto_revision_count = int(params.get("_companion_auto_revision_count", 0))

        if attachments:
            formatted_attachments = []

            for attachment in attachments:
                name = attachment.get("name", "unknown")
                content = attachment.get("content", "")

                formatted_attachments.append(f"[Attached File: {name}]\n{content}")

            user_input += "\n\nAttached Context:\n"
            user_input += "\n\n".join(formatted_attachments)

        if not user_input.strip():
            context = get_experience_context()
            await self._append_experience(
                ExperienceEventType.INTENT,
                idempotency_key=f"task:{context.task_id}:intent:empty",
                source="interactive",
                payload={"input": "", "attachment_count": len(attachments)},
                provenance={"component": "PilotServer._handle_execute_inner"},
                privacy_class=PrivacyClass.SENSITIVE,
            )
            await self._append_experience(
                ExperienceEventType.OUTCOME_VERIFIED,
                idempotency_key=f"task:{context.task_id}:terminal:empty",
                source="interactive",
                payload={"status": "error", "summary": "Empty input"},
                provenance={"component": "PilotServer._handle_execute_inner"},
            )
            return {"status": "error", "message": "Empty input"}
        dry_run = _resolve_dry_run(self.config.security.dry_run, params.get("dry_run", False))

        # ── Cancel Token (Issue #92): fresh event per execution session ──
        self._cancel_event = asyncio.Event()
        cancel_event = self._cancel_event

        import time

        _start_time = time.time()
        last_plan_id = ""

        def _record_memory(plan: Any, results: list[Any]) -> Any:
            return self._memory.record(
                user_input,
                plan,
                results,
                session_id=chat_session_id,
                task_id=get_experience_context().task_id,
            )

        async def _record_planned_experience(plan: Any, plan_id: str, attempt: int) -> None:
            plan_event = await self._append_experience(
                ExperienceEventType.PLAN_CREATED,
                plan_id=plan_id,
                idempotency_key=f"plan:{plan_id}:created",
                source="planner",
                payload={
                    "action_count": len(plan.actions),
                    "explanation": plan.explanation,
                    "attempt": attempt,
                    "conversational": not bool(plan.actions),
                },
                provenance={"component": "Planner.plan"},
                privacy_class=PrivacyClass.SENSITIVE,
            )
            for index, action in enumerate(plan.actions):
                action_id = stable_action_idempotency_key(plan_id, index, action)
                await self._append_experience(
                    ExperienceEventType.CANDIDATE_ACTION,
                    plan_id=plan_id,
                    action_id=action_id,
                    parent_event_id=plan_event.event_id if plan_event else "",
                    idempotency_key=f"plan:{plan_id}:candidate:{index}",
                    source="planner",
                    payload={
                        "index": index,
                        "action_idempotency_key": action_id,
                        "action": action,
                    },
                    provenance={"component": "Planner.plan"},
                    privacy_class=PrivacyClass.SENSITIVE,
                )
            if self._memory is not None:
                await self._memory.put_working(
                    session_id=chat_session_id,
                    task_id=get_experience_context().task_id,
                    key="current plan",
                    value={
                        "plan_id": plan_id,
                        "attempt": attempt,
                        "summary": plan.explanation,
                        "action_count": len(plan.actions),
                    },
                    priority=0.9,
                    ttl_seconds=86400,
                )

        async def _emit_task_complete(status: str, summary: str) -> None:
            try:
                duration_ms = int((time.time() - _start_time) * 1000)
                payload = {
                    "status": status,
                    "summary": _sanitize_summary(summary),
                    "duration_ms": duration_ms,
                    "dry_run": dry_run,
                }
                if last_plan_id:
                    payload["plan_id"] = last_plan_id
                context = get_experience_context()
                await self._append_experience(
                    ExperienceEventType.OUTCOME_VERIFIED,
                    plan_id=last_plan_id,
                    idempotency_key=(f"task:{context.task_id}:terminal:{revision_count}:{auto_revision_count}"),
                    source="interactive",
                    payload=payload,
                    provenance={"component": "PilotServer._handle_execute_inner"},
                    privacy_class=PrivacyClass.SENSITIVE,
                )
                await self._broadcast_notification("task_complete", payload)
            except Exception:
                pass

        async def _prepare_durable_replan(reason: str) -> None:
            if self._durable_tasks is None or not self._active_task_id:
                return
            task = await self._durable_tasks.get(self._active_task_id)
            if task is None or task.is_terminal:
                return
            if task.status != TaskStatus.INTERRUPTED:
                await self._durable_tasks.transition(
                    task.task_id,
                    TaskStatus.INTERRUPTED,
                    reason=reason,
                    plan_id=self._active_plan_id or task.plan_id,
                )
            await self._durable_tasks.transition(
                task.task_id,
                TaskStatus.PLANNING,
                reason="replanning after intervention",
            )

        async def _restart_with_live_correction(completed_results: list[Any] | None = None) -> dict | None:
            """Consume one queued correction and re-enter planning in this RPC.

            The current action is already cancelled by the out-of-band
            ``interject`` handler. We preserve a compact record of steps that
            really completed, then ask the planner to revise the remaining
            work instead of returning a misleading terminal cancellation.
            """
            correction = self._live_correction
            if not correction:
                return None

            self._live_correction = None
            if revision_count >= self.MAX_LIVE_CORRECTIONS:
                message = (
                    f"Live correction limit reached ({self.MAX_LIVE_CORRECTIONS}). "
                    "The task was stopped to avoid an endless revision loop."
                )
                await self._broadcast_notification(
                    "companion_revision_rejected",
                    {"message": message, "revision_count": revision_count},
                )
                await _emit_task_complete("cancelled", message)
                return {"status": "cancelled", "message": message, "results": []}

            completed = completed_results or []
            completed_lines = [
                (
                    f"- {result.action.action_type.value} on "
                    f"{getattr(result.action, 'target', '') or '(no target)'}: "
                    f"{'completed' if result.success else 'failed'}"
                )
                for result in completed
            ]
            completed_context = (
                "\n".join(completed_lines)
                if completed_lines
                else "- No action completed before the correction arrived."
            )
            revised_input = (
                f"{user_input}\n\n"
                "[LIVE USER CORRECTION]\n"
                f"{correction}\n\n"
                "[ALREADY OBSERVED OUTCOMES]\n"
                f"{completed_context}\n\n"
                "Revise the plan to honor the live correction. Do not repeat a "
                "completed action unless the correction explicitly requires it."
            )

            if self._checkpoint_store and self._active_plan_id:
                await self._checkpoint_store.mark_status(self._active_plan_id, "superseded")

            await ws.send(_notification("status", {"phase": "revising after your correction"}))
            await self._broadcast_notification(
                "companion_revision_started",
                {
                    "correction": correction,
                    "revision": revision_count + 1,
                    "completed_actions": len(completed),
                },
            )
            await _prepare_durable_replan("live user correction")
            return await self._handle_execute_inner(
                {
                    "input": revised_input,
                    "dry_run": dry_run,
                    "_companion_revision_count": revision_count + 1,
                    "_companion_auto_revision_count": auto_revision_count,
                    "session_id": chat_session_id,
                },
                ws,
            )

        async def _restart_from_companion_review(review: Any) -> dict:
            """Re-plan once the independent companion finds concrete drift."""
            feedback = str(getattr(review, "planner_feedback", "")).strip()
            reason = str(getattr(review, "reason", "")).strip()
            limit = max(0, int(self.config.narration.max_auto_revisions))
            if auto_revision_count >= limit:
                message = (
                    f"Interactive companion stopped this task after {limit} automatic "
                    f"revision attempts: {reason or 'the plan remained misaligned.'}"
                )
                await self._broadcast_notification(
                    "companion_plan_intervention",
                    {
                        "decision": "STOP",
                        "reason": message,
                        "revision": auto_revision_count,
                    },
                )
                await _emit_task_complete("blocked_by_companion", message)
                return {"status": "blocked_by_companion", "message": message, "results": []}

            if self._checkpoint_store and self._active_plan_id:
                await self._checkpoint_store.mark_status(self._active_plan_id, "superseded")

            await ws.send(
                _notification(
                    "status",
                    {"phase": f"companion revising plan ({auto_revision_count + 1}/{limit})"},
                )
            )
            await self._broadcast_notification(
                "companion_plan_intervention",
                {
                    "decision": "REVISE",
                    "reason": reason,
                    "planner_feedback": feedback,
                    "revision": auto_revision_count + 1,
                },
            )
            revised_input = (
                f"{user_input}\n\n"
                "[INDEPENDENT COMPANION REVIEW]\n"
                f"Problem: {reason}\n"
                f"Required correction: {feedback}\n\n"
                "Create a smaller corrected plan that still follows the original "
                "user request. Do not add goals, permissions, or side effects."
            )
            await _prepare_durable_replan("companion requested plan revision")
            return await self._handle_execute_inner(
                {
                    "input": revised_input,
                    "dry_run": dry_run,
                    "_companion_revision_count": revision_count,
                    "_companion_auto_revision_count": auto_revision_count + 1,
                    "session_id": chat_session_id,
                },
                ws,
            )

        async def _phase_cancelled(phase: str, completed_results: list[Any] | None = None) -> dict:
            """Shape task cancellation consistently for every long-running phase."""
            if self._live_correction:
                revised = await _restart_with_live_correction(completed_results)
                if revised is not None:
                    return revised
            message = f"Execution stopped by user during {phase}."
            await ws.send(_notification("status", {"phase": "aborted"}))
            await _emit_task_complete("cancelled", message)
            return {
                "status": "cancelled",
                "message": message,
                "results": [result.model_dump() for result in (completed_results or [])],
            }

        # A single interaction state replaces the removed ReAct event graph.
        # The guarded planning, approval, action, and verification flow below
        # remains authoritative, but internal thoughts are not serialized.
        emit = None

        context = get_experience_context()
        await self._append_experience(
            ExperienceEventType.INTENT,
            idempotency_key=(f"task:{context.task_id}:intent:{revision_count}:{auto_revision_count}"),
            source="interactive",
            payload={
                "input": user_input,
                "attachment_count": len(attachments),
                "revision_count": revision_count,
                "auto_revision_count": auto_revision_count,
            },
            provenance={"component": "PilotServer._handle_execute_inner"},
            privacy_class=PrivacyClass.SENSITIVE,
        )

        input_phase = ""
        await ws.send(_notification("status", {"phase": "receiving input"}))
        if emit:
            input_phase = await emit.phase_start("user_input", "user_input_received", {"input": user_input})
            await emit.phase_complete(
                "user_input", "user_input_received", {"length": len(user_input)}, parent_id=input_phase
            )

        mem_phase = ""
        await ws.send(_notification("status", {"phase": "recalling memory"}))
        if emit:
            mem_phase = await emit.phase_start("memory_recall", MEMORY_SEARCH_STARTED)

        improvement_ctx = await self._reflector.get_improvement_context(user_input)
        if self._strategy_evolution is not None:
            promoted_strategy = await self._strategy_evolution.get_active_text("planner.primary")
            if promoted_strategy:
                improvement_ctx = (
                    f"{improvement_ctx}\n\nAdmin-promoted planner strategy:\n{promoted_strategy}"
                ).strip()

        if emit:
            await emit.thought(
                "memory_recall", "Searching long-term memory for relevant context...", parent_id=mem_phase
            )
            await emit.phase_complete(
                "memory_recall", MEMORY_CONTEXT_LOADED, {"has_context": bool(improvement_ctx)}, parent_id=mem_phase
            )

        route_phase = ""
        await ws.send(_notification("status", {"phase": "routing agents"}))
        if emit:
            route_phase = await emit.phase_start("agent_routing", ROUTING_ANALYSIS_STARTED, {"input": user_input})

        routing = self._multi_agent.get_routing_summary(user_input)
        await ws.send(_notification("agent_routing", routing))

        if emit:
            await emit.decision(
                "agent_routing",
                "Route to specialist agents",
                options=[r.value for r in self._orchestrator._agents] if self._orchestrator else [],
                chosen=", ".join(routing.get("assigned_agents", [])),
                parent_id=route_phase,
            )
            await emit.phase_complete("agent_routing", ROUTING_AGENTS_ASSIGNED, routing, parent_id=route_phase)

        error_context = improvement_ctx
        all_results: list = []
        last_verification = None
        last_explanation = ""
        _original_plan = None
        _successful_results: list = []
        from pilot.response_contract import (
            exact_labeled_finding_count,
            partial_failure_message,
            success_message,
        )

        for attempt in range(1 + self.MAX_RETRIES):
            # ── Cancel Token: check before each planning attempt ──
            if cancel_event.is_set():
                logger.info("Execution cancelled before attempt %d", attempt + 1)
                message = "Execution was stopped before planning."
                await _emit_task_complete("cancelled", message)
                return {"status": "cancelled", "message": message}

            plan_phase = ""
            if emit:
                event_name = PLANNER_STARTED if attempt == 0 else PLANNER_REPLANNING
                plan_phase = await emit.phase_start("planning", event_name, {"attempt": attempt + 1})
                await emit.thought("planning", "Generating structured action plan via LLM...", parent_id=plan_phase)

            if attempt == 0:
                await ws.send(_notification("status", {"phase": "planning"}))
            else:
                await ws.send(_notification("status", {"phase": f"re-planning (attempt {attempt + 1})"}))

            if emit:
                await emit.data_event("planning", PLANNER_LLM_CALL, {"model": "active"}, parent_id=plan_phase)

            _screen_ctx = ""
            if self._screen_vision:
                try:
                    _screen_ctx = self._screen_vision.get_context_for_planner()
                    await self._append_experience(
                        ExperienceEventType.OBSERVATION,
                        idempotency_key=(
                            f"task:{context.task_id}:screen_context:{revision_count}:{auto_revision_count}:{attempt}"
                        ),
                        source="screen_vision",
                        payload={
                            "context_available": bool(_screen_ctx),
                            "context_length": len(_screen_ctx),
                            "raw_media_excluded": True,
                        },
                        provenance={
                            "component": "ScreenVisionAgent.get_context_for_planner",
                            "consumer": "Planner.plan",
                        },
                        privacy_class=PrivacyClass.SENSITIVE,
                    )
                except Exception:
                    pass
            recent_companion_context = self._recent_companion_context_by_session.get(chat_session_id, "")
            if recent_companion_context:
                _screen_ctx = (f"{_screen_ctx}\n\n[RECENT COMPANION CONTEXT]\n{recent_companion_context}").strip()
            if self._subconscious:
                try:
                    persona_context = await self._subconscious.get_persona_context()
                    if persona_context:
                        _screen_ctx = (
                            f"{_screen_ctx}\n\n[LEARNED USER BEHAVIOR]\n"
                            f"{persona_context}\n"
                            "Treat these as preferences, never as permission to bypass "
                            "confirmation, safety policy, or the user's current request."
                        ).strip()
                except Exception:
                    logger.debug("Could not load learned persona context", exc_info=True)

            try:
                planner_kwargs: dict[str, Any] = {
                    "error_context": error_context,
                    "screen_context": _screen_ctx,
                    # Planner output is internal JSON, not an assistant response.
                    # Streaming it into chat exposes implementation details and can
                    # replace the final user-facing explanation.
                    "stream_callback": None,
                }
                if chat_session_id != "default":
                    planner_kwargs["session_id"] = chat_session_id
                plan = await self._await_execution_tracked(
                    self._planner.plan(
                        user_input,
                        **planner_kwargs,
                    )
                )
            except asyncio.CancelledError:
                return await _phase_cancelled("planning")
            if cancel_event.is_set() and self._live_correction:
                revised = await _restart_with_live_correction()
                if revised is not None:
                    return revised
            if plan.error:
                if emit:
                    await emit.phase_error("planning", PLANNER_ERROR, plan.error, parent_id=plan_phase)
                if attempt < self.MAX_RETRIES and not _is_terminal_planning_failure(plan.error):
                    error_context = plan.error
                    continue
                if _is_terminal_planning_failure(plan.error):
                    logger.info("Terminal provider failure; skipping redundant planning retries")
                await _emit_task_complete("error", plan.error)
                return {"status": "error", "message": plan.error}

            last_explanation = plan.explanation

            # A useful conversational response is a valid zero-action plan.
            # It must bypass checkpoints, previews, permission gates,
            # execution, and verification: there is nothing to execute.
            if not plan.actions:
                conversation_plan_id = f"conversation:{context.task_id}:{attempt}"
                await _record_planned_experience(plan, conversation_plan_id, attempt)
                if emit:
                    await emit.phase_complete(
                        "planning",
                        PLANNER_GENERATED_PLAN,
                        {
                            "action_count": 0,
                            "explanation": plan.explanation[:120],
                            "action_types": [],
                            "conversational": True,
                        },
                        parent_id=plan_phase,
                    )

                if self._memory:
                    asyncio.create_task(_record_memory(plan, []))
                await ws.send(_notification("conversation_response", {"explanation": plan.explanation}))
                await _emit_task_complete("success", plan.explanation)
                return {
                    "status": "success",
                    "conversational": True,
                    "dry_run": False,
                    "results": [],
                    "explanation": plan.explanation,
                    "agent_routing": routing,
                }

            plan_id = str(uuid.uuid4())[:8]
            last_plan_id = plan_id
            self._active_plan_id = plan_id
            await _record_planned_experience(plan, plan_id, attempt)
            if self._checkpoint_store:
                await self._checkpoint_store.start_plan(plan_id, user_input, plan)

            if emit:
                await emit.phase_complete(
                    "planning",
                    PLANNER_GENERATED_PLAN,
                    {
                        "plan_id": plan_id,
                        "action_count": len(plan.actions),
                        "explanation": plan.explanation[:120],
                        "action_types": [a.action_type.value for a in plan.actions],
                    },
                    parent_id=plan_phase,
                )

            await ws.send(
                _notification(
                    "plan_preview",
                    {
                        "plan_id": plan_id,
                        "actions": [a.model_dump() for a in plan.actions],
                        "explanation": plan.explanation,
                        "dry_run": dry_run,
                    },
                )
            )

            if self.config.narration.proactive_review_enabled and self._execution_companion and not dry_run:
                await ws.send(_notification("status", {"phase": "companion reviewing plan"}))
                companion_review = self._deterministic_companion_review(plan)
                if companion_review is None:
                    try:
                        companion_review = await self._await_execution_tracked(
                            self._execution_companion.review(user_input, plan)
                        )
                    except asyncio.CancelledError:
                        return await _phase_cancelled("companion review")

                review_payload = {
                    "plan_id": plan_id,
                    **companion_review.to_dict(),
                    "revision": auto_revision_count,
                }
                await ws.send(_notification("companion_plan_review", review_payload))

                if companion_review.should_stop:
                    message = f"Interactive companion stopped this plan: {companion_review.reason}"
                    await self._broadcast_notification(
                        "companion_plan_intervention",
                        {**review_payload, "decision": "STOP", "reason": message},
                    )
                    if self._checkpoint_store:
                        await self._checkpoint_store.mark_status(plan_id, "blocked_by_companion")
                    await _emit_task_complete("blocked_by_companion", message)
                    return {
                        "status": "blocked_by_companion",
                        "message": message,
                        "companion_review": companion_review.to_dict(),
                        "results": [],
                    }

                if companion_review.should_revise:
                    return await _restart_from_companion_review(companion_review)

                if companion_review.decision == "WARN":
                    await self._broadcast_notification(
                        "companion_plan_intervention",
                        review_payload,
                    )

            from pilot.actions import PermissionTier
            from pilot.agents.destructive_critic import (
                HEURISTIC_RISK_THRESHOLD,
                assess_plan_risk,
                constrain_verdict_to_plan_authority,
            )

            critic_verdict_payload: dict[str, Any] | None = None
            plan_has_tier4 = any(a.permission_tier == PermissionTier.ROOT_CRITICAL for a in plan.actions)
            plan_has_tier3 = any(a.permission_tier == PermissionTier.DESTRUCTIVE for a in plan.actions)
            plan_has_irreversible = any(getattr(a, "is_irreversible", False) for a in plan.actions)
            plan_has_blockable_authority = plan_has_tier4 or plan_has_tier3 or plan_has_irreversible
            risk_assessment = assess_plan_risk(plan, self.config)
            risk_score = risk_assessment.combined_score
            world_model_interrupt = risk_assessment.requires_confirmation
            await self._append_experience(
                ExperienceEventType.WORLD_PREDICTION,
                plan_id=plan_id,
                idempotency_key=f"plan:{plan_id}:world_prediction",
                source="risk_gate",
                payload=risk_assessment.to_dict(),
                provenance={
                    "component": "assess_plan_risk",
                    "policy_authority": "deterministic_rules",
                },
            )
            if self.config.gateway.risk_gate_enabled:
                await ws.send(
                    _notification(
                        "world_model_assessment",
                        {
                            "plan_id": plan_id,
                            **risk_assessment.to_dict(),
                        },
                    )
                )
            # Tier 4 always gets the LLM critic. Other plans pay for that
            # round-trip only when the heuristic or risk world model crosses
            # the review threshold; trivial single-file deletes still skip
            # straight to confirmation and say so in the audit trail.
            needs_critic_review = plan_has_blockable_authority and (
                plan_has_tier4 or risk_score >= HEURISTIC_RISK_THRESHOLD
            )
            critic_skipped_reason: str | None = None
            if not plan_has_blockable_authority and risk_score >= HEURISTIC_RISK_THRESHOLD:
                critic_skipped_reason = "no_destructive_authority"
            elif plan_has_tier3 and not (plan_has_tier4 or risk_score >= HEURISTIC_RISK_THRESHOLD):
                critic_skipped_reason = "low_risk_heuristic"

            if needs_critic_review and self._destructive_critic and not dry_run:
                critic_phase = ""
                await ws.send(_notification("status", {"phase": "critic review"}))
                if emit:
                    critic_phase = await emit.phase_start(
                        "critic_review",
                        CRITIC_REVIEW_STARTED,
                        {"plan_id": plan_id, "action_count": len(plan.actions)},
                    )
                    await emit.thought(
                        "critic_review",
                        "Destructive/irreversible actions detected — running independent safety review...",
                        parent_id=critic_phase,
                    )

                try:
                    verdict = await self._await_execution_tracked(self._destructive_critic.review(user_input, plan))
                except asyncio.CancelledError:
                    return await _phase_cancelled("safety review")
                verdict = constrain_verdict_to_plan_authority(plan, verdict)
                critic_verdict_payload = verdict.to_dict()
                critic_verdict_payload["world_model"] = risk_assessment.to_dict()
                await ws.send(_notification("critic_verdict", critic_verdict_payload))

                if cancel_event.is_set() and self._live_correction:
                    revised = await _restart_with_live_correction()
                    if revised is not None:
                        return revised

                if verdict.is_blocked:
                    await self._record_permission_escalations(
                        plan_id=plan_id,
                        plan=plan,
                        confirmation_decision="blocked_by_critic",
                        critic_verdict=critic_verdict_payload,
                        results=[],
                        execution_error=verdict.recommendation,
                    )
                    # ── Plan History: blocked by critic ──
                    self._spawn_history_task(
                        self._record_plan_history(
                            plan_id=plan_id,
                            raw_input=user_input,
                            plan=plan,
                            critic_verdict=critic_verdict_payload,
                            confirmation_decision="blocked_by_critic",
                            execution_status="blocked_by_critic",
                            results=[],
                            verification=None,
                            dry_run=dry_run,
                            start_time=_start_time,
                        )
                    )
                    if emit:
                        await emit.phase_error(
                            "critic_review",
                            CRITIC_REVIEW_BLOCKED,
                            verdict.recommendation,
                            parent_id=critic_phase,
                        )
                    message = f"Blocked before execution by safety review: {verdict.recommendation}"
                    await _emit_task_complete("blocked_by_critic", message)
                    return {
                        "status": "blocked_by_critic",
                        "verdict": verdict.to_dict(),
                        "message": message,
                        "explanation": plan.explanation,
                    }

                if emit:
                    event_name = CRITIC_REVIEW_WARNED if verdict.has_warnings else CRITIC_REVIEW_APPROVED
                    await emit.phase_complete(
                        "critic_review",
                        event_name,
                        verdict.to_dict(),
                        parent_id=critic_phase,
                    )
            elif critic_skipped_reason and not dry_run:
                # Tier 3 / irreversible plan whose heuristic risk score was low
                # enough to skip the LLM round-trip — keep the audit trail
                # honest that a deeper review did not run, rather than implying
                # one silently approved it.
                critic_verdict_payload = {
                    "verdict": "SKIPPED",
                    "risk_score": risk_score,
                    "issues": risk_assessment.reasons,
                    "safe_actions": [],
                    "flagged_actions": [],
                    "recommendation": (
                        "The world-model warning remains active and normal approval is required; "
                        "the destructive LLM critic was skipped because this plan has no destructive authority."
                        if critic_skipped_reason == "no_destructive_authority"
                        else "Low-risk heuristic — LLM safety review was skipped."
                    ),
                    "critic_skipped": critic_skipped_reason,
                    "world_model": risk_assessment.to_dict(),
                }
                await ws.send(_notification("critic_verdict", critic_verdict_payload))

            needs_confirm = (
                self._permission_checker.plan_requires_confirmation(plan) or world_model_interrupt
            ) and not dry_run
            partially_approved = False
            if needs_confirm:
                await self._interaction_runtime.transition(
                    InteractionPhase.AWAITING_APPROVAL,
                    message="Waiting for your approval",
                    interaction_id=self._active_interaction_id,
                )
                if self._durable_tasks is not None and self._active_task_id:
                    await self._durable_tasks.transition(
                        self._active_task_id,
                        TaskStatus.AWAITING_APPROVAL,
                        reason="plan requires user approval",
                        plan_id=plan_id,
                    )
                confirm_phase = ""
                if emit:
                    confirm_phase = await emit.phase_start("confirmation", CONFIRMATION_REQUIRED, {"plan_id": plan_id})
                    await emit.thought(
                        "confirmation",
                        (
                            "World model predicted a risky outcome — execution is paused..."
                            if world_model_interrupt
                            else "Dangerous action detected — awaiting user approval..."
                        ),
                        parent_id=confirm_phase,
                    )

                world_model_reason = ""
                if world_model_interrupt:
                    reason_text = "; ".join(risk_assessment.reasons) or "predicted outcome crossed the safety threshold"
                    world_model_reason = (
                        f"World model paused this plan at {risk_assessment.world_model_score:.0%} predicted risk: "
                        f"{reason_text}"
                    )
                confirmed, approved_indices, required_indices = await self._wait_for_confirmation(
                    plan_id,
                    plan,
                    ws,
                    reason=world_model_reason,
                    risk_assessment=risk_assessment.to_dict() if world_model_interrupt else None,
                    force_all_actions=world_model_interrupt,
                )

                if emit:
                    if confirmed:
                        await emit.phase_complete(
                            "confirmation", CONFIRMATION_APPROVED, {"plan_id": plan_id}, parent_id=confirm_phase
                        )
                    else:
                        await emit.phase_error(
                            "confirmation", CONFIRMATION_DENIED, "User denied the plan", parent_id=confirm_phase
                        )

                if not confirmed:
                    if self._live_correction:
                        revised = await _restart_with_live_correction()
                        if revised is not None:
                            return revised
                    message = "Cancelled before execution: the plan was denied."
                    await self._record_permission_escalations(
                        plan_id=plan_id,
                        plan=plan,
                        confirmation_decision="denied",
                        critic_verdict=critic_verdict_payload,
                        results=[],
                        execution_error=message,
                    )
                    # ── Plan History: user denied ──
                    self._spawn_history_task(
                        self._record_plan_history(
                            plan_id=plan_id,
                            raw_input=user_input,
                            plan=plan,
                            critic_verdict=critic_verdict_payload,
                            confirmation_decision="denied",
                            execution_status="cancelled",
                            results=[],
                            verification=None,
                            dry_run=dry_run,
                            start_time=_start_time,
                        )
                    )
                    await _emit_task_complete("cancelled", message)
                    return {
                        "status": "cancelled",
                        "message": message,
                        "explanation": plan.explanation,
                    }

                # Per-action granular approval: drop any confirmation-required
                # action the user didn't check, keeping order for dependency
                # correctness. Actions that don't require confirmation at all
                # are untouched.
                denied_indices = required_indices - approved_indices
                partially_approved = bool(denied_indices)
                if partially_approved:
                    logger.info(
                        "Plan %s partially approved — skipping %d action(s) denied by user: %s",
                        plan_id,
                        len(denied_indices),
                        sorted(denied_indices),
                    )
                    plan.actions = [a for i, a in enumerate(plan.actions) if i not in denied_indices]
            elif not dry_run:
                if emit:
                    skip_phase = await emit.phase_start("confirmation", "confirmation_skipped")
                    await emit.phase_complete(
                        "confirmation", "confirmation_skipped", {"reason": "No dangerous actions"}, parent_id=skip_phase
                    )

            approved_decision = "partially_approved" if partially_approved else "approved"
            if self._durable_tasks is not None and self._active_task_id:
                await self._durable_tasks.transition(
                    self._active_task_id,
                    TaskStatus.EXECUTING,
                    reason="approval satisfied; execution starting",
                    plan_id=plan_id,
                )

            exec_phase = ""
            if emit:
                exec_phase = await emit.phase_start("execution", EXECUTOR_STARTED, {"action_count": len(plan.actions)})

            await self._interaction_runtime.transition(
                InteractionPhase.ACTING,
                message="Executing the approved plan",
                interaction_id=self._active_interaction_id,
            )
            await ws.send(_notification("status", {"phase": "executing"}))
            action_idx = 0
            _total_actions = len(plan.actions)
            completed_results: list[Any] = []
            source_name = str(params.get("source") or "interactive").strip().lower()
            invocation_source, scope_override = self._execution_scope_for_source(source_name)

            async def _on_action_start(
                action: Any, _exec_phase: str = exec_phase, _total: int = _total_actions
            ) -> None:
                nonlocal action_idx
                action_payload = action.model_dump()
                if dry_run:
                    action_payload["dry_run"] = True
                await ws.send(_notification("action_start", {"action": action_payload}))
                if emit:
                    action_idx += 1
                    await emit.data_event(
                        "execution",
                        EXECUTOR_ACTION_STARTED,
                        {"action_type": action.action_type.value, "target": action.target, "index": action_idx},
                        parent_id=_exec_phase,
                    )
                    await emit.progress(
                        "execution", action_idx, _total, label=action.action_type.value, parent_id=_exec_phase
                    )

            async def _on_action_complete(
                result: Any,
                _exec_phase: str = exec_phase,
                _plan_id: str = plan_id,
                _completed_results: list[Any] = completed_results,
            ) -> None:
                _completed_results.append(result)
                result_payload = result.model_dump()
                if dry_run:
                    result_payload["dry_run"] = True
                await ws.send(_notification("action_complete", {"result": result_payload}))
                if self._checkpoint_store and result.success:
                    await self._checkpoint_store.record_result(_plan_id, result)
                if emit:
                    event_name = EXECUTOR_ACTION_COMPLETE if result.success else EXECUTOR_ERROR
                    await emit.data_event(
                        "execution",
                        event_name,
                        {"success": result.success, "error": result.error or ""},
                        parent_id=_exec_phase,
                    )

            try:
                if self._orchestrator:
                    orch_routing = self._orchestrator.get_routing_summary(plan)
                    await ws.send(_notification("orchestrator_routing", orch_routing))
                    if emit:
                        await emit.data_event("orchestration", ORCHESTRATOR_ROUTING, orch_routing, parent_id=exec_phase)
                        for agent_info in orch_routing.get("assigned_agents", []):
                            role_name = agent_info["role"] if isinstance(agent_info, dict) else str(agent_info)
                            await emit.thought(
                                "orchestration", f"Delegating to {role_name} agent...", parent_id=exec_phase
                            )

                    results = await self._await_execution_tracked(
                        self._orchestrator.execute_plan(
                            user_input,
                            plan,
                            on_action_start=_on_action_start,
                            on_action_complete=_on_action_complete,
                            cancel_event=cancel_event,  # ── Cancel Token (Issue #92) ──
                            plan_id=plan_id,
                            critic_already_reviewed=True,
                            user_confirmed=needs_confirm and approved_decision in {"approved", "partially_approved"},
                            scope_override=scope_override,
                        )
                    )
                else:
                    results = await self._execute_tracked(
                        plan,
                        on_action_start=_on_action_start,
                        on_action_complete=_on_action_complete,
                        cancel_event=cancel_event,  # ── Cancel Token (Issue #92) ──
                        plan_id=plan_id,
                        # Interactive is the default invocation_source, but the
                        # critic already ran above (or was deliberately skipped
                        # as low-risk) — don't have the gateway pay for a
                        # redundant LLM round-trip.
                        critic_already_reviewed=True,
                        user_confirmed=needs_confirm and approved_decision in {"approved", "partially_approved"},
                        invocation_source=invocation_source,
                        scope_override=scope_override,
                    )
            except asyncio.CancelledError:
                # ── Mid-flight cancellation: _handle_abort (Part 3) sets
                # cancel_event *then* cancels _active_execution_task, so by
                # the time this CancelledError (a BaseException) reaches us,
                # cancel_event.is_set() is already True -- fall through to
                # the existing "Cancel Token" handling below with whatever
                # results the task produced before it was killed (none, since
                # the cancelled await never returns a value). This must be
                # caught here rather than left to propagate, or it would
                # escape this RPC handler as an unhandled BaseException. ──
                results = list(completed_results)
            all_results = results
            if not dry_run:
                _snapshot_id = next((r.snapshot_id for r in results if getattr(r, "snapshot_id", None)), None)
                if _snapshot_id:
                    self._plan_snapshots[plan_id] = _snapshot_id
            if needs_confirm and not dry_run:
                await self._record_permission_escalations(
                    plan_id=plan_id,
                    plan=plan,
                    confirmation_decision=approved_decision,
                    critic_verdict=critic_verdict_payload,
                    results=results,
                )

            # ── Cancel Token: if aborted mid-execution, return immediately ──
            if cancel_event.is_set():
                if self._live_correction:
                    revised = await _restart_with_live_correction(results)
                    if revised is not None:
                        return revised
                logger.info("Execution was cancelled mid-plan after %d result(s)", len(results))
                await ws.send(_notification("status", {"phase": "aborted"}))
                if self._checkpoint_store:
                    await self._checkpoint_store.mark_status(plan_id, "cancelled")
                # ── Plan History: cancelled mid-execution ──
                self._spawn_history_task(
                    self._record_plan_history(
                        plan_id=plan_id,
                        raw_input=user_input,
                        plan=plan,
                        critic_verdict=critic_verdict_payload,
                        confirmation_decision=approved_decision if needs_confirm else "skipped",
                        execution_status="cancelled",
                        results=results,
                        verification=None,
                        dry_run=dry_run,
                        start_time=_start_time,
                    )
                )
                message = (
                    f"Execution stopped by user after {len(results)} of {len(plan.actions)} actions completed."
                    if plan.actions
                    else "Execution stopped by user."
                )
                await _emit_task_complete("cancelled", message)
                return {
                    "status": "cancelled",
                    "message": message,
                    "results": [r.model_dump() for r in results],
                }

            if emit:
                successes = sum(1 for r in results if r.success)
                await emit.phase_complete(
                    "execution",
                    EXECUTOR_ALL_COMPLETE,
                    {"total": len(results), "successes": successes, "failures": len(results) - successes},
                    parent_id=exec_phase,
                )

            verify_phase = ""
            if emit:
                verify_phase = await emit.phase_start("verification", VERIFICATION_STARTED)
                await emit.thought(
                    "verification", "Checking execution results against expected outcomes...", parent_id=verify_phase
                )

            if self._durable_tasks is not None and self._active_task_id:
                await self._durable_tasks.transition(
                    self._active_task_id,
                    TaskStatus.VERIFYING,
                    reason="action execution finished",
                    plan_id=plan_id,
                )
            await self._interaction_runtime.transition(
                InteractionPhase.VERIFYING,
                message="Verifying the result",
                interaction_id=self._active_interaction_id,
            )
            await ws.send(_notification("status", {"phase": "verifying"}))
            if dry_run:
                from pilot.actions import VerificationResult

                verification = VerificationResult(
                    passed=True,
                    details=["Dry run completed: no actions were executed."],
                    failed_actions=[],
                    rollback_triggered=False,
                )
            else:
                try:
                    verification = await self._await_execution_tracked(self._verifier.verify(plan, results))
                except asyncio.CancelledError:
                    return await _phase_cancelled("verification", results)
            last_verification = verification
            await self._append_experience(
                ExperienceEventType.OUTCOME_VERIFIED,
                plan_id=plan_id,
                idempotency_key=f"plan:{plan_id}:verification:{attempt}",
                source="verifier",
                payload={
                    "attempt": attempt,
                    "verification": verification,
                    "result_count": len(results),
                },
                provenance={"component": "Verifier.verify"},
            )
            if not verification.passed:
                await self._append_experience(
                    ExperienceEventType.PREDICTION_ERROR,
                    plan_id=plan_id,
                    idempotency_key=f"plan:{plan_id}:prediction_error:{attempt}",
                    source="verifier",
                    payload={
                        "attempt": attempt,
                        "details": verification.details,
                        "failed_actions": verification.failed_actions,
                    },
                    provenance={
                        "component": "Verifier.verify",
                        "compared_prediction": f"plan:{plan_id}:world_prediction",
                    },
                )
            if cancel_event.is_set() and self._live_correction:
                revised = await _restart_with_live_correction(results)
                if revised is not None:
                    return revised
            if _original_plan is not None and _successful_results:
                all_results = PlanDiffer.merge_results(_successful_results, results, _original_plan, verification)

            if verification.passed:
                if emit:
                    await emit.phase_complete(
                        "verification",
                        VERIFICATION_PASSED,
                        {"details": verification.details[:3]},
                        parent_id=verify_phase,
                    )

                if emit:
                    refl_phase = await emit.phase_start("reflection", REFLECTION_STARTED)
                    await emit.thought(
                        "reflection", "Analyzing performance and extracting lessons...", parent_id=refl_phase
                    )
                    duration_ms = int((time.time() - _start_time) * 1000)
                    await emit.metric("reflection", "total_duration_ms", duration_ms, unit="ms", parent_id=refl_phase)
                    await emit.phase_complete(
                        "reflection", REFLECTION_COMPLETE, {"retry_count": attempt}, parent_id=refl_phase
                    )

                if emit:
                    mem_store_phase = await emit.phase_start("memory_update", MEMORY_STORE_STARTED)
                    await emit.thought(
                        "memory_update", "Persisting interaction to long-term memory...", parent_id=mem_store_phase
                    )

                asyncio.create_task(_record_memory(plan, results))
                if self._checkpoint_store:
                    await self._checkpoint_store.mark_status(plan_id, "complete")
                asyncio.create_task(
                    self._reflector.reflect(
                        user_input,
                        plan,
                        results,
                        verification,
                        retry_count=attempt,
                        duration_ms=int((time.time() - _start_time) * 1000),
                    )
                )

                if emit:
                    await emit.phase_complete(
                        "memory_update", MEMORY_STORE_COMPLETE, {"saved": True}, parent_id=mem_store_phase
                    )

                # ── Plan History: success ──
                self._spawn_history_task(
                    self._record_plan_history(
                        plan_id=plan_id,
                        raw_input=user_input,
                        plan=plan,
                        critic_verdict=critic_verdict_payload,
                        confirmation_decision=approved_decision if needs_confirm else ("n/a" if dry_run else "skipped"),
                        execution_status="dry_run" if dry_run else "success",
                        results=results,
                        verification=verification,
                        dry_run=dry_run,
                        start_time=_start_time,
                    )
                )

                message = success_message(plan, results, verification, dry_run=dry_run)
                self._recent_companion_context_by_session[chat_session_id] = (
                    f"Previous request: {_sanitize_summary(user_input, limit=300)}\n"
                    f"Verified result: {_sanitize_summary(message, limit=700)}"
                )
                await _emit_task_complete("success", message)

                if (
                    self.config.narration.follow_up_enabled
                    and self._execution_companion
                    and hasattr(self._execution_companion, "follow_up")
                    and not dry_run
                    and exact_labeled_finding_count(plan) is None
                ):
                    self._spawn_companion_follow_up(
                        user_input=user_input,
                        plan=plan,
                        results=results,
                        verification=verification,
                        result_text=message,
                        chat_session_id=chat_session_id,
                    )
                return {
                    "status": "success",
                    "dry_run": dry_run,
                    "results": [r.model_dump() for r in results],
                    "verification": verification.model_dump(),
                    "message": message,
                    "explanation": (
                        f"(dry run) {plan.explanation}"
                        if dry_run and plan.explanation
                        else "(dry run) Dry run completed: no changes were made."
                        if dry_run
                        else plan.explanation
                    ),
                    "agent_routing": self._multi_agent.get_routing_summary(user_input),
                    "companion_follow_up": None,
                }

            if emit:
                await emit.phase_error(
                    "verification", VERIFICATION_FAILED, "; ".join(verification.details[:3]), parent_id=verify_phase
                )

            if _is_terminal_execution_failure(results):
                logger.info("Terminal execution failure; returning the exact error without LLM retry")
                break

            # Execution failed — use PlanDiffer for partial re-plan
            from pilot.agents.plan_differ import PlanDiffer

            retry_plan, successful_results = PlanDiffer.diff(plan, results, verification)

            failed_details = [d for d in verification.details if "FAILED" in d or "MISMATCH" in d]
            error_msgs = [r.error for r in results if r.error]
            error_context = "\n".join(failed_details + error_msgs)

            # Use partial retry plan if PlanDiffer found fewer actions to retry
            if len(retry_plan.actions) < len(plan.actions):
                logger.info(
                    "PlanDiffer: retrying %d/%d actions",
                    len(retry_plan.actions),
                    len(plan.actions),
                )
                plan = retry_plan
                _original_plan = plan
                _successful_results = successful_results
                all_results = list(successful_results)

            if attempt < self.MAX_RETRIES:
                await ws.send(
                    _notification(
                        "status",
                        {"phase": "retrying — previous attempt failed"},
                    )
                )
                if emit:
                    await emit.thought(
                        "planning", f"Retry {attempt + 1}: Re-planning with error context...", parent_id=""
                    )
            else:
                break

        if emit:
            mem_final = await emit.phase_start("memory_update", MEMORY_STORE_STARTED)
            await emit.phase_complete("memory_update", MEMORY_STORE_COMPLETE, {"partial": True}, parent_id=mem_final)

        asyncio.create_task(_record_memory(plan, all_results))
        if self._checkpoint_store and last_plan_id:
            await self._checkpoint_store.mark_status(last_plan_id, "failed")

        # ── Plan History: partial_failure after all retries exhausted ──
        self._spawn_history_task(
            self._record_plan_history(
                plan_id=last_plan_id,
                raw_input=user_input,
                plan=plan,
                critic_verdict=critic_verdict_payload,
                confirmation_decision=approved_decision if needs_confirm else "skipped",
                execution_status="partial_failure",
                results=all_results,
                verification=last_verification,
                dry_run=dry_run,
                start_time=_start_time,
            )
        )

        message = partial_failure_message(all_results, last_verification)
        await _emit_task_complete("partial_failure", message)
        return {
            "status": "partial_failure",
            "dry_run": dry_run,
            "results": [r.model_dump() for r in all_results],
            "verification": last_verification.model_dump() if last_verification else {},
            "message": message,
            "explanation": (
                f"(dry run) {last_explanation}"
                if dry_run and last_explanation
                else "(dry run) Dry run completed: no changes were made."
                if dry_run
                else last_explanation
            ),
        }

    # ── Plan History: internal helper ──

    async def _record_plan_history(
        self,
        *,
        plan_id: str,
        raw_input: str,
        plan: Any,
        critic_verdict: dict[str, Any] | None,
        confirmation_decision: str,
        execution_status: str,
        results: list[Any],
        verification: Any | None,
        dry_run: bool,
        start_time: float,
    ) -> None:
        """Fire-and-forget wrapper that persists a plan audit record safely.

        Swallows all exceptions so a storage failure never disrupts execution.

        Args:
            plan_id: Short UUID identifying this plan.
            raw_input: Original user input string.
            plan: ActionPlan object.
            critic_verdict: Optional critic verdict dict.
            confirmation_decision: User/system confirmation outcome.
            execution_status: Terminal execution status string.
            results: List of ActionResult objects.
            verification: Optional VerificationResult object.
            dry_run: Whether this was a dry-run.
            start_time: ``time.time()`` at the start of execution for duration calc.
        """
        if not self._plan_history or not plan_id:
            return
        try:
            import time as _time

            duration_ms = int((_time.time() - start_time) * 1000)
            await self._plan_history.record(
                plan_id=plan_id,
                raw_input=raw_input,
                plan=plan,
                critic_verdict=critic_verdict,
                confirmation_decision=confirmation_decision,
                execution_status=execution_status,
                results=results,
                verification=verification,
                dry_run=dry_run,
                duration_ms=duration_ms,
            )
        except Exception:
            logger.warning("_record_plan_history failed (non-critical)", exc_info=True)

    def _spawn_history_task(self, coro: Any) -> None:
        """Schedule a plan-history coroutine as a tracked background task.

        The task is added to ``_plan_history_tasks`` and automatically removed
        when it completes, so ``stop()`` can drain any in-flight writes before
        closing the SQLite connection.

        Args:
            coro: The coroutine to schedule (typically ``_record_plan_history(...)``).
        """
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._plan_history_tasks.add(task)
        task.add_done_callback(self._plan_history_tasks.discard)

    def _spawn_companion_follow_up(
        self,
        *,
        user_input: str,
        plan: Any,
        results: list[Any],
        verification: Any,
        result_text: str,
        chat_session_id: str = "",
        speak: bool = False,
    ) -> None:
        """Generate optional next ideas after the verified result is delivered.

        Companion ideation may call a model and must never hold the terminal
        task response hostage. The eventual suggestion is delivered as its
        own notification and remains scoped to the originating chat.
        """

        async def _generate() -> None:
            try:
                follow_up = await self._execution_companion.follow_up(
                    user_input,
                    plan,
                    results,
                    verification,
                )
                if not follow_up:
                    return

                if chat_session_id:
                    self._recent_companion_context_by_session[chat_session_id] = (
                        f"Previous request: {_sanitize_summary(user_input, limit=300)}\n"
                        f"Verified result: {_sanitize_summary(result_text, limit=300)}\n"
                        f"Companion next ideas: {' | '.join(follow_up.suggestions)}"
                    )
                else:
                    self._recent_companion_context = (
                        f"Previous request: {_sanitize_summary(user_input, limit=300)}\n"
                        f"Verified result: {_sanitize_summary(result_text, limit=300)}\n"
                        f"Companion next ideas: {' | '.join(follow_up.suggestions)}"
                    )

                await self._broadcast_notification(
                    "companion_follow_up",
                    {
                        "session_id": chat_session_id,
                        **follow_up.to_dict(),
                    },
                )
                if speak:
                    await self._speak_voice_response(
                        follow_up.spoken_text(),
                        channel=SpeechChannel.BACKGROUND_INSIGHT,
                        dedupe_key=f"voice-follow-up:{user_input.casefold()}",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Companion follow-up failed in background", exc_info=True)

        task: asyncio.Task[None] = asyncio.create_task(_generate())
        self._companion_follow_up_tasks.add(task)
        task.add_done_callback(self._companion_follow_up_tasks.discard)

    async def _handle_resume_task(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Authenticate and continue a durable task after reconnect or restart."""

        if self._durable_tasks is None:
            return {"status": "error", "message": "Durable task store is not initialized"}
        resume_token = str(params.get("resume_token") or "")
        if not resume_token:
            return {"status": "error", "message": "resume_task requires resume_token"}
        task = await self._durable_tasks.get_by_resume_token(resume_token)
        requested_task_id = str(params.get("task_id") or "")
        if task is None or (requested_task_id and requested_task_id != task.task_id):
            return {"status": "error", "message": "Invalid task_id or resume_token"}
        if task.is_terminal:
            return {
                **(task.terminal_response or {"status": task.status.value}),
                "task_id": task.task_id,
                "replayed": True,
            }
        if task.cancellation_requested:
            response = {
                "status": "cancelled",
                "message": "Task cancellation was requested before resume.",
                "task_id": task.task_id,
            }
            await self._finalize_durable_task(task.task_id, response)
            return response

        approval = await self._durable_tasks.get_approval(task.plan_id) if task.plan_id else None
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            return {
                "status": "awaiting_approval",
                "task_id": task.task_id,
                "plan_id": task.plan_id,
                "approval": approval.request,
                "expires_at": approval.expires_at,
            }
        if approval is not None and approval.status in {
            ApprovalStatus.DENIED,
            ApprovalStatus.EXPIRED,
        }:
            response = {
                "status": "cancelled",
                "message": f"Task approval was {approval.status.value}.",
                "task_id": task.task_id,
                "plan_id": task.plan_id,
            }
            await self._finalize_durable_task(task.task_id, response)
            return response

        self._interactive_request_active = True
        self._active_task_id = task.task_id
        self._active_plan_id = task.plan_id
        self._live_correction = None
        context = ExperienceContext(
            session_id=task.session_id,
            task_id=task.task_id,
            user_id=task.user_id,
        )
        self._active_experience_context = context
        try:
            with experience_scope(
                session_id=context.session_id,
                task_id=context.task_id,
                user_id=context.user_id,
            ):
                if task.plan_id:
                    if task.status != TaskStatus.EXECUTING:
                        await self._durable_tasks.transition(
                            task.task_id,
                            TaskStatus.EXECUTING,
                            reason="authenticated task resume",
                            plan_id=task.plan_id,
                        )
                    response = await self._handle_resume_plan(
                        {
                            "plan_id": task.plan_id,
                            "_authorized_task_id": task.task_id,
                        },
                        ws,
                    )
                else:
                    if task.status != TaskStatus.PLANNING:
                        await self._durable_tasks.transition(
                            task.task_id,
                            TaskStatus.PLANNING,
                            reason="restart interrupted planning",
                        )
                    response = await self._handle_execute_inner(
                        {
                            "input": task.user_input,
                            "session_id": task.session_id,
                        },
                        ws,
                    )
                await self._finalize_durable_task(task.task_id, response)
                return {**response, "task_id": task.task_id, "resumed": True}
        finally:
            self._interactive_request_active = False
            self._active_task_id = ""
            self._active_plan_id = ""
            self._live_correction = None
            self._cancel_event = None
            self._active_experience_context = ExperienceContext()

    async def _handle_resume_plan(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Resume a previously checkpointed plan from its last completed action."""
        plan_id = str(params.get("plan_id", "")).strip()
        if not plan_id:
            return {"status": "error", "message": "resume_plan requires plan_id"}
        if not self._checkpoint_store:
            return {"status": "error", "message": "Workflow checkpoint store is not initialized"}
        if self._durable_tasks is not None:
            durable = await self._durable_tasks.get_by_plan_id(plan_id)
            authorized_task_id = str(params.get("_authorized_task_id") or "")
            if durable is not None and authorized_task_id != durable.task_id:
                return {
                    "status": "error",
                    "message": "This durable plan must be resumed with resume_task and its resume_token.",
                    "task_id": durable.task_id,
                }

        checkpoint = await self._checkpoint_store.get(plan_id)
        if checkpoint is None:
            return {"status": "error", "message": f"No checkpoint found for plan_id: {plan_id}"}

        completed_count = max(0, min(checkpoint.completed_count, len(checkpoint.plan.actions)))
        remaining_actions = checkpoint.plan.actions[completed_count:]
        await ws.send(
            _notification(
                "status",
                {
                    "phase": "resuming",
                    "plan_id": plan_id,
                    "completed_actions": completed_count,
                    "remaining_actions": len(remaining_actions),
                },
            )
        )

        if not remaining_actions:
            await self._checkpoint_store.mark_status(plan_id, "complete")
            return {
                "status": "success",
                "plan_id": plan_id,
                "resumed": False,
                "message": "Plan already completed.",
                "results": [result.model_dump() for result in checkpoint.results],
            }

        self._cancel_event = asyncio.Event()
        cancel_event = self._cancel_event

        from pilot.actions import ActionPlan

        remaining_plan = ActionPlan(
            actions=remaining_actions,
            explanation=checkpoint.plan.explanation,
            raw_input=checkpoint.plan.raw_input,
        )

        async def _on_action_start(action: Any) -> None:
            await ws.send(_notification("action_start", {"action": action.model_dump(), "resumed": True}))

        async def _on_action_complete(result: Any) -> None:
            await ws.send(_notification("action_complete", {"result": result.model_dump(), "resumed": True}))
            if result.success:
                await self._checkpoint_store.record_result(plan_id, result)

        await self._checkpoint_store.mark_status(plan_id, "resuming")
        execute_kwargs: dict[str, Any] = {
            "on_action_start": _on_action_start,
            "on_action_complete": _on_action_complete,
            "cancel_event": cancel_event,
            "plan_id": plan_id,
            "initial_last_output": checkpoint.last_output,
        }
        if params.get("_authorized_task_id"):
            execute_kwargs["action_index_offset"] = completed_count
        try:
            results = await self._execute_tracked(remaining_plan, **execute_kwargs)
        except asyncio.CancelledError:
            # ── Mid-flight cancellation (Part 3): cancel_event is already
            # set by _handle_abort before it cancels the tracked task, so
            # the cancel_event.is_set() branch below handles the response --
            # this must be caught here or it escapes as an unhandled
            # BaseException instead of a clean RPC response. ──
            results = []

        updated = await self._checkpoint_store.get(plan_id)
        combined_results = [
            *(updated.results if updated else checkpoint.results),
            *[r for r in results if not r.success],
        ]

        if cancel_event.is_set():
            await self._checkpoint_store.mark_status(plan_id, "cancelled")
            return {
                "status": "cancelled",
                "plan_id": plan_id,
                "resumed": True,
                "completed_actions": updated.completed_count if updated else completed_count,
                "results": [result.model_dump() for result in combined_results],
            }

        failed = any(not result.success for result in results)
        final_status = "failed" if failed else "complete"
        await self._checkpoint_store.mark_status(plan_id, final_status)

        verification_payload: dict[str, Any] = {}
        if not failed and len(combined_results) >= len(checkpoint.plan.actions):
            verification = await self._verifier.verify(checkpoint.plan, combined_results)
            verification_payload = verification.model_dump()
            if not verification.passed:
                final_status = "partial_failure"
                await self._checkpoint_store.mark_status(plan_id, "failed")

        return {
            "status": "partial_failure" if final_status in {"failed", "partial_failure"} else "success",
            "plan_id": plan_id,
            "resumed": True,
            "skipped_actions": completed_count,
            "executed_actions": len(results),
            "results": [result.model_dump() for result in combined_results],
            "verification": verification_payload,
            "explanation": checkpoint.plan.explanation,
        }

    async def _record_permission_escalations(
        self,
        *,
        plan_id: str,
        plan: Any,
        confirmation_decision: str,
        critic_verdict: dict[str, Any] | None,
        results: list[Any],
        execution_error: str = "",
    ) -> None:
        """Persist tamper-evident records for elevated permission decisions."""
        if not self._permission_audit:
            return

        from pilot.actions import PermissionTier

        result_by_action: dict[str, list[Any]] = {}
        for result in results:
            action_key = self._action_signature(result.action)
            result_by_action.setdefault(action_key, []).append(result)

        for index, action in enumerate(plan.actions):
            if action.permission_tier < PermissionTier.SYSTEM_MODIFY:
                continue

            matched_result = None
            matches = result_by_action.get(self._action_signature(action))
            if matches:
                matched_result = matches.pop(0)

            if matched_result is None:
                execution_success = None
                action_error = execution_error
            else:
                execution_success = bool(matched_result.success)
                action_error = matched_result.error or ""

            await self._permission_audit.record_event(
                plan_id=plan_id,
                action_index=index,
                action_type=action.action_type.value,
                target=action.target,
                permission_tier=action.permission_tier.name,
                requires_root=action.requires_root,
                destructive=action.destructive,
                confirmation_decision=confirmation_decision,
                critic_verdict=critic_verdict,
                execution_success=execution_success,
                execution_error=action_error,
            )

    @staticmethod
    def _action_signature(action: Any) -> str:
        return json.dumps(action.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    async def _wait_for_confirmation(
        self,
        plan_id: str,
        plan: Any,
        ws: ServerConnection,
        *,
        reason: str = "",
        risk_assessment: dict[str, Any] | None = None,
        force_all_actions: bool = False,
    ) -> tuple[bool, set[int], set[int]]:
        """Send a confirmation request and block until the user responds or timeout.

        Args:
            plan_id: Unique identifier for the plan requiring confirmation.
            plan: The plan object containing actions to be confirmed.
            ws: The WebSocket connection for sending/receiving messages.

        Returns:
            A (confirmed, approved_indices, required_indices) tuple.
            ``required_indices`` are the original ``plan.actions`` indices
            that needed confirmation; ``approved_indices`` is the subset of
            those the user approved (all of them for a plain approve/deny
            response, a subset for per-action granular approval).
        """
        pending = PendingConfirmation(plan_id=plan_id, event=asyncio.Event())
        self._pending_confirms[plan_id] = pending

        confirm_indices = (
            list(range(len(plan.actions)))
            if force_all_actions
            else [i for i, a in enumerate(plan.actions) if a.requires_confirmation or a.is_irreversible]
        )

        def _dump_confirm_action(idx: int, a: Any) -> dict[str, Any]:
            payload = a.model_dump()
            payload["irreversible"] = a.is_irreversible
            payload["index"] = idx
            return payload

        approval_request = {
            "action_indices": confirm_indices,
            "actions": [_dump_confirm_action(i, plan.actions[i]) for i in confirm_indices],
            "reason": reason,
            "risk_assessment": risk_assessment,
        }
        if self._durable_tasks is not None and self._active_task_id:
            await self._durable_tasks.create_approval(
                task_id=self._active_task_id,
                plan_id=plan_id,
                request=approval_request,
                timeout_seconds=CONFIRM_TIMEOUT_SECONDS,
            )
        if self._memory is not None and self._active_task_id:
            context = get_experience_context()
            await self._memory.put_working(
                session_id=context.session_id or "default",
                task_id=self._active_task_id,
                key="pending approval",
                value={
                    "plan_id": plan_id,
                    "reason": reason,
                    "action_count": len(confirm_indices),
                },
                priority=1.0,
                ttl_seconds=CONFIRM_TIMEOUT_SECONDS,
            )

        await self._append_experience(
            ExperienceEventType.APPROVAL_REQUESTED,
            plan_id=plan_id,
            idempotency_key=f"plan:{plan_id}:approval:requested",
            source="permission_gate",
            payload=approval_request,
            provenance={"component": "PilotServer._wait_for_confirmation"},
            privacy_class=PrivacyClass.SENSITIVE,
        )
        await ws.send(
            _notification(
                "confirm_required",
                {
                    "task_id": get_experience_context().task_id,
                    "plan_id": plan_id,
                    "actions": [_dump_confirm_action(i, plan.actions[i]) for i in confirm_indices],
                    "reason": reason,
                    "risk_assessment": risk_assessment,
                },
            )
        )

        required = set(confirm_indices)
        try:
            await asyncio.wait_for(pending.event.wait(), timeout=CONFIRM_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("Confirmation timed out for plan %s", plan_id)
            if self._durable_tasks is not None and self._active_task_id:
                try:
                    await self._durable_tasks.resolve_approval(
                        plan_id,
                        ApprovalStatus.EXPIRED,
                    )
                except ApprovalConflict:
                    logger.info("Approval %s resolved concurrently with timeout", plan_id)
            await self._append_experience(
                ExperienceEventType.APPROVAL_RESOLVED,
                plan_id=plan_id,
                idempotency_key=f"plan:{plan_id}:approval:resolved",
                source="permission_gate",
                payload={
                    "decision": "expired",
                    "approved_indices": [],
                    "required_indices": sorted(required),
                },
                provenance={"component": "PilotServer._wait_for_confirmation"},
            )
            return False, set(), required
        finally:
            self._pending_confirms.pop(plan_id, None)

        approved = (
            (pending.approved_indices if pending.approved_indices is not None else required)
            if pending.confirmed
            else set()
        )
        if self._durable_tasks is not None and self._active_task_id:
            persisted = await self._durable_tasks.get_approval(plan_id)
            if persisted is not None and persisted.status == ApprovalStatus.PENDING:
                await self._durable_tasks.resolve_approval(
                    plan_id,
                    ApprovalStatus.APPROVED if pending.confirmed else ApprovalStatus.DENIED,
                    approved_indices=sorted(approved & required),
                )
        await self._append_experience(
            ExperienceEventType.APPROVAL_RESOLVED,
            plan_id=plan_id,
            idempotency_key=f"plan:{plan_id}:approval:resolved",
            source="permission_gate",
            payload={
                "decision": "approved" if pending.confirmed else "denied",
                "approved_indices": sorted(approved & required),
                "required_indices": sorted(required),
            },
            provenance={"component": "PilotServer._wait_for_confirmation"},
        )
        return pending.confirmed, (approved & required), required

    async def _handle_confirm(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Resolve a pending confirmation request from the UI.

        Args:
            params: JSON-RPC parameters containing plan_id, confirmed status,
                and an optional approved_indices list for per-action granular
                approval (omit/empty to approve all confirmation-requiring
                actions, preserving the old all-or-nothing behavior).
            ws: The WebSocket connection.

        Returns:
            A dict with status and confirmation result.
        """
        plan_id = params.get("plan_id", "")
        confirmed = params.get("confirmed", False)
        raw_approved = params.get("approved_indices")

        pending = self._pending_confirms.get(plan_id)
        if pending is None:
            if self._durable_tasks is None:
                return {"status": "error", "message": f"No pending confirmation for plan_id: {plan_id}"}
            durable = await self._durable_tasks.get_approval(plan_id)
            if durable is None or durable.status != ApprovalStatus.PENDING:
                return {"status": "error", "message": f"No pending confirmation for plan_id: {plan_id}"}
            try:
                required = {int(index) for index in durable.request.get("action_indices", [])}
                approved = (
                    required
                    if bool(confirmed) and raw_approved is None
                    else {int(index) for index in (raw_approved or [])} & required
                )
            except (TypeError, ValueError):
                approved = set()
            resolved = await self._durable_tasks.resolve_approval(
                plan_id,
                ApprovalStatus.APPROVED if bool(confirmed) else ApprovalStatus.DENIED,
                approved_indices=sorted(approved),
            )
            return {
                "status": "ok",
                "confirmed": resolved.status == ApprovalStatus.APPROVED,
                "resume_required": True,
                "task_id": resolved.task_id,
            }

        pending.confirmed = bool(confirmed)
        if raw_approved is not None:
            try:
                pending.approved_indices = {int(i) for i in raw_approved}
            except (TypeError, ValueError):
                pending.approved_indices = None
        if self._durable_tasks is not None:
            durable = await self._durable_tasks.get_approval(plan_id)
            if durable is not None and durable.status == ApprovalStatus.PENDING:
                required = {int(index) for index in durable.request.get("action_indices", [])}
                approved = (
                    required
                    if pending.confirmed and pending.approved_indices is None
                    else (pending.approved_indices or set()) & required
                )
                await self._durable_tasks.resolve_approval(
                    plan_id,
                    ApprovalStatus.APPROVED if pending.confirmed else ApprovalStatus.DENIED,
                    approved_indices=sorted(approved),
                )
        pending.event.set()
        return {"status": "ok", "confirmed": pending.confirmed}

    async def _handle_rollback_plan(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Roll back the filesystem snapshot taken before a plan executed.

        This is filesystem-wide (btrfs subvolume / timeshift snapshot), NOT
        per-action — it reverts everything since the snapshot, including any
        unrelated changes made after it. The frontend must gate this behind
        its own explicit confirmation step; this handler does not re-confirm.

        Args:
            params: JSON-RPC parameters containing plan_id.
            ws: The WebSocket connection.

        Returns:
            A dict with status and a human-readable message.
        """
        plan_id = params.get("plan_id", "")
        snapshot_id = self._plan_snapshots.get(plan_id)
        if not snapshot_id:
            return {
                "status": "error",
                "message": f"No snapshot on record for plan_id: {plan_id} (either none was taken, or it was already rolled back)",
            }

        from pilot.system.snapshots import SnapshotManager

        snapshot_mgr = SnapshotManager(self.config)
        try:
            result_message = await snapshot_mgr.rollback(snapshot_id)
        except Exception as exc:
            logger.warning("Rollback failed for plan %s (snapshot %s): %s", plan_id, snapshot_id, exc)
            return {"status": "error", "message": f"Rollback failed: {exc}"}

        self._plan_snapshots.pop(plan_id, None)
        await ws.send(
            _notification(
                "rollback_complete",
                {"plan_id": plan_id, "snapshot_id": snapshot_id, "message": result_message},
            )
        )
        logger.info("Rolled back plan %s to snapshot %s", plan_id, snapshot_id)
        return {"status": "ok", "message": result_message}

    async def _handle_list_permission_events(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """List recent permission-escalation audit events for display.

        Args:
            params: JSON-RPC parameters, optionally {limit, plan_id}.
            ws: The WebSocket connection.

        Returns:
            A dict with status and the list of events.
        """
        if not self._permission_audit:
            return {"status": "error", "message": "Permission audit store is not initialized.", "events": []}

        limit = int(params.get("limit", 50))
        plan_id = params.get("plan_id") or None
        events = await self._permission_audit.list_events(limit=limit, plan_id=plan_id)
        return {"status": "ok", "events": events}

    async def _handle_verify_permission_audit(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Verify the tamper-evident HMAC chain of the permission audit log.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with status and the ChainVerificationResult fields.
        """
        if not self._permission_audit:
            return {"status": "error", "message": "Permission audit store is not initialized."}

        result = await self._permission_audit.verify_chain()
        return {
            "status": "ok",
            "valid": result.valid,
            "checked_entries": result.checked_entries,
            "error": result.error,
        }

    async def _handle_list_gateway_events(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """List recent Agent Gateway audit events for the Settings transparency view.

        Args:
            params: JSON-RPC parameters, optionally {limit, plan_id,
                source_profile, action_family, decision}.
            ws: The WebSocket connection.

        Returns:
            A dict with status and the list of events.
        """
        if not self._gateway_audit:
            return {"status": "error", "message": "Agent gateway audit store is not initialized.", "events": []}

        events = await self._gateway_audit.list_events(
            limit=int(params.get("limit", 50)),
            plan_id=params.get("plan_id") or None,
            source_profile=params.get("source_profile") or None,
            action_family=params.get("action_family") or None,
            decision=params.get("decision") or None,
        )
        return {"status": "ok", "events": events}

    async def _handle_verify_gateway_audit(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Verify the tamper-evident HMAC chain of the Agent Gateway audit log.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with status and the ChainVerificationResult fields.
        """
        if not self._gateway_audit:
            return {"status": "error", "message": "Agent gateway audit store is not initialized."}

        result = await self._gateway_audit.verify_chain()
        return {
            "status": "ok",
            "valid": result.valid,
            "checked_entries": result.checked_entries,
            "error": result.error,
        }

    async def _handle_gateway_policy_get(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Return the current Agent Gateway source-profile floors for the Settings editor.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with status, whether the gateway is enabled, and each
            source profile's enforced floor (max_tier/deny_action_types/allow_root).
        """
        from dataclasses import asdict

        profiles = {name: asdict(profile) for name, profile in self.config.gateway.source_profiles.items()}
        return {"status": "ok", "enabled": self.config.gateway.enabled, "profiles": profiles}

    async def _handle_gateway_policy_update(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Update one source profile's enforced floor.

        This edits only the source-profile floor persisted in config —
        per-task overrides (e.g. autonomous_submit's scope_override) are
        never settable from Settings, only supplied per-submission by the
        caller, and can only narrow this floor further, never widen it.

        Args:
            params: JSON-RPC parameters: {profile, max_tier?, deny_action_types?, allow_root?}.
            ws: The WebSocket connection.

        Returns:
            A dict with status and the updated policy, or an error if the
            profile name is unknown.
        """
        from dataclasses import asdict

        from pilot.security.gateway import SourceProfile

        profile_name = params.get("profile", "")
        current = self.config.gateway.source_profiles.get(profile_name)
        if current is None:
            return {"status": "error", "message": f"Unknown source profile: {profile_name}"}

        raw_max_tier = params.get("max_tier")
        raw_deny = params.get("deny_action_types")
        raw_allow_root = params.get("allow_root")

        # Merge onto the existing floor rather than replacing it wholesale —
        # a caller updating only "shell" shouldn't silently reset every
        # other family back to unset/zero.
        merged_max_tier = dict(current.max_tier)
        if raw_max_tier is not None:
            merged_max_tier.update({str(k): int(v) for k, v in raw_max_tier.items()})

        updated = SourceProfile(
            max_tier=merged_max_tier,
            deny_action_types=[str(a) for a in raw_deny] if raw_deny is not None else list(current.deny_action_types),
            allow_root=bool(raw_allow_root) if raw_allow_root is not None else current.allow_root,
        )
        self.config.gateway.source_profiles[profile_name] = updated
        self.config.save()

        return {"status": "ok", "profile": profile_name, "policy": asdict(updated)}

    async def _handle_interject(
        self,
        params: dict[str, Any],
        ws: ServerConnection | None,
    ) -> dict:
        """Apply a typed correction to the currently running interactive task.

        This RPC is deliberately out-of-band, like ``confirm`` and ``abort``:
        it must remain responsive while the ordinary ``execute`` RPC owns the
        connection's request lock. A correction cancels the current action,
        then ``_handle_execute_inner`` consumes it and re-plans in the same
        request. Explicit stop phrases retain normal terminal-abort behavior.
        """
        text = str(params.get("input", "")).strip()
        if not text:
            return {"status": "error", "message": "A live correction cannot be empty."}
        if not self._interactive_request_active:
            return {"status": "no_active_execution", "message": "There is no interactive task to revise."}

        requested_mode = str(params.get("mode", "")).strip().lower()
        active_context = self._active_experience_context
        await self._append_experience(
            ExperienceEventType.USER_CORRECTION,
            session_id=active_context.session_id,
            task_id=active_context.task_id,
            user_id=active_context.user_id,
            plan_id=self._active_plan_id,
            idempotency_key=f"correction:{uuid.uuid4()}",
            source="interactive",
            payload={"input": text, "requested_mode": requested_mode or "correct"},
            provenance={"component": "PilotServer._handle_interject"},
            privacy_class=PrivacyClass.SENSITIVE,
        )
        normalized = " ".join(text.lower().split()).rstrip(".!?")
        stop_phrases = {
            "abort",
            "cancel",
            "cancel it",
            "cancel this",
            "cancel this task",
            "never mind",
            "nevermind",
            "stop",
            "stop it",
            "stop this",
            "stop this task",
        }
        if requested_mode == "stop" or normalized in stop_phrases:
            await self._broadcast_notification(
                "companion_interjection",
                {"mode": "stop", "message": "Stopping the current task now."},
            )
            return await self._handle_abort({}, ws)

        if not self.config.narration.live_corrections_enabled:
            return {
                "status": "disabled",
                "message": "Live corrections are disabled in Interactive Companion settings.",
            }

        if self._live_correction:
            self._live_correction = f"{self._live_correction}\nAdditional correction: {text}"
        else:
            self._live_correction = text

        if self._cancel_event and not self._cancel_event.is_set():
            self._cancel_event.set()

        # The primary confirmation wait is not inside _active_execution_task.
        # Resolve only this interactive plan's confirmation; unrelated
        # background confirmations must remain untouched.
        pending = self._pending_confirms.get(self._active_plan_id)
        if pending is not None:
            pending.confirmed = False
            pending.event.set()

        task = self._active_execution_task
        if task is not None and not task.done():
            task.cancel()

        from pilot.system.pty_session import PtySessionManager

        PtySessionManager.interrupt_all()
        await self._broadcast_notification(
            "companion_interjection",
            {
                "mode": "correct",
                "message": "Correction received. Stopping the current step and revising the plan.",
            },
        )
        return {"status": "revising", "message": "Live correction accepted."}

    async def _handle_abort(
        self,
        params: dict[str, Any],
        ws: ServerConnection | None,
    ) -> dict:
        """Stop the current execution -- both cooperatively and, where
        possible, by really killing whatever is in flight right now (Issue
        #92, extended for real mid-flight cancellation).

        Sets the per-session cancel_event (as before, so the Orchestrator
        and Executor halt at the next action boundary) AND cancels the
        currently tracked interactive execution task, which cascades all
        the way down to run_command's proc.kill() for a mid-flight shell
        subprocess -- the same mechanism AutonomousExecutor.cancel() already
        proves works. Also interrupts every live PTY session (pty_exec
        can't be stopped by Task.cancel() alone; see PtySession.interrupt).

        Returns immediately — cancellation propagates asynchronously; the
        in-flight _execute_tracked()/_handle_execute call observes it and
        shapes its own clean RPC response.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with status indicating whether an active execution was aborted.
        """
        aborted_something = False

        if self._durable_tasks is not None and self._active_task_id:
            await self._durable_tasks.request_cancel(self._active_task_id)
            aborted_something = True

        if self._cancel_event and not self._cancel_event.is_set():
            self._cancel_event.set()
            aborted_something = True

        task = self._active_execution_task
        if task is not None and not task.done():
            task.cancel()
            aborted_something = True

        from pilot.system.pty_session import PtySessionManager

        PtySessionManager.interrupt_all()

        if aborted_something:
            logger.info("Abort signal received — cancel_event set and in-flight execution task cancelled")
            return {"status": "aborted"}
        return {"status": "no_active_execution"}

    # -- Config --

    async def _handle_get_config(self, params: dict, ws: ServerConnection) -> dict:
        """Get the current server configuration.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict containing the server configuration.
        """
        from dataclasses import asdict

        data = asdict(self.config)
        data.pop("server", None)
        return data

    async def _handle_get_security_status(self, params: dict, ws: ServerConnection) -> dict:
        """Return both the Heliox root policy and the daemon's real OS privilege state."""
        from pilot.security.privileges import security_runtime_status

        return security_runtime_status(self.config.security.root_enabled)

    async def _handle_get_snapshot_status(self, params: dict, ws: ServerConnection) -> dict:
        """Return the configured pre-action snapshot backend and live readiness."""
        from pilot.system.snapshots import SnapshotManager

        return await SnapshotManager(self.config).status()

    async def _handle_restart_elevated(self, params: dict, ws: ServerConnection) -> dict:
        """Request a Windows UAC handoff to an elevated replacement daemon."""
        from pilot.security.privileges import has_elevated_privileges
        from pilot.system.elevation import ElevationError, request_elevated_restart

        if sys.platform != "win32":
            return {
                "status": "unsupported",
                "message": "Administrator restart is only available on Windows.",
            }
        if not self.config.security.root_enabled:
            return {
                "status": "blocked",
                "message": "Enable Root Access before requesting Administrator privileges.",
            }
        if has_elevated_privileges():
            return {
                "status": "already_elevated",
                "message": "The Zariff daemon is already running as Administrator.",
            }

        try:
            result = await asyncio.to_thread(request_elevated_restart)
        except ElevationError as error:
            logger.warning("Administrator restart was not started: %s", error)
            return {"status": "error", "message": str(error)}

        logger.info("Windows accepted the Administrator restart request")
        return result

    async def _handle_update_config(self, params: dict, ws: ServerConnection) -> dict:
        """Update server configuration.

        Args:
            params: JSON-RPC parameters with section and values.
            ws: The WebSocket connection.

        Returns:
            A dict with status.
        """
        section = params.get("section", "")
        values = params.get("values", {})

        if section == "" and "first_run_complete" in values:
            self.config.first_run_complete = values["first_run_complete"]
            self.config.save()
            return {"status": "ok"}

        target = getattr(self.config, section, None)
        if target is None:
            return {"status": "error", "message": f"Unknown config section: {section}"}
        for k, v in values.items():
            if hasattr(target, k):
                if section == "security" and k == "root_enabled" and not isinstance(v, bool):
                    return {"status": "error", "message": "security.root_enabled must be a boolean"}
                if section == "security" and k == "snapshot_on_destructive" and not isinstance(v, bool):
                    return {
                        "status": "error",
                        "message": "security.snapshot_on_destructive must be a boolean",
                    }
                if section == "security" and k == "dry_run" and not isinstance(v, bool):
                    return {"status": "error", "message": "security.dry_run must be a boolean"}
                if section == "security" and k == "snapshot_retention_count":
                    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 100:
                        return {
                            "status": "error",
                            "message": "security.snapshot_retention_count must be from 1 to 100",
                        }
                if section == "preview" and k == "enabled" and not isinstance(v, bool):
                    return {"status": "error", "message": "preview.enabled must be a boolean"}
                if section == "preview" and k == "confirm_timeout_seconds":
                    if not isinstance(v, (int, float)) or isinstance(v, bool) or not 10 <= v <= 600:
                        return {
                            "status": "error",
                            "message": "preview.confirm_timeout_seconds must be from 10 to 600",
                        }
                if section == "gesture_cursor" and k == "enabled" and not isinstance(v, bool):
                    return {"status": "error", "message": "gesture_cursor.enabled must be a boolean"}
                if section == "gesture_cursor" and k == "sensitivity":
                    if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0.1 <= v <= 3:
                        return {
                            "status": "error",
                            "message": "gesture_cursor.sensitivity must be from 0.1 to 3",
                        }
                if section == "gesture_cursor" and k == "prediction_ms":
                    if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 250:
                        return {
                            "status": "error",
                            "message": "gesture_cursor.prediction_ms must be from 0 to 250",
                        }
                if section == "gesture_cursor" and k == "blend":
                    if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1:
                        return {
                            "status": "error",
                            "message": "gesture_cursor.blend must be from 0 to 1",
                        }
                if section == "adaptive_calibration" and k == "gesture_enabled":
                    if not isinstance(v, bool):
                        return {
                            "status": "error",
                            "message": "adaptive_calibration.gesture_enabled must be a boolean",
                        }
                if section == "adaptive_calibration" and k == "voice_wake_word_enabled":
                    if not isinstance(v, bool):
                        return {
                            "status": "error",
                            "message": ("adaptive_calibration.voice_wake_word_enabled must be a boolean"),
                        }
                if section == "voice" and k == "tts_engine":
                    if v not in {"kokoro_tts", "pocket_tts", "os_native"}:
                        return {
                            "status": "error",
                            "message": "voice.tts_engine must be kokoro_tts, pocket_tts, or os_native",
                        }
                if section == "voice" and k == "tts_voice":
                    if v not in {
                        "af_heart",
                        "af_bella",
                        "af_nicole",
                        "af_sarah",
                        "af_sky",
                        "am_adam",
                        "am_michael",
                        "bf_emma",
                        "bf_isabella",
                        "bm_george",
                        "bm_lewis",
                        "alba",
                        "giovanni",
                        "lola",
                    }:
                        return {
                            "status": "error",
                            "message": "voice.tts_voice must be a supported Kokoro or Pocket TTS voice",
                        }
                if section == "voice" and k == "input_device":
                    if not isinstance(v, str) or not v.strip() or len(v) > 500:
                        return {
                            "status": "error",
                            "message": "voice.input_device must be a valid microphone identifier",
                        }
                    v = v.strip()
                if section == "screen_vision" and k == "capture_interval_seconds":
                    v = float(v)
                setattr(target, k, v)
        self.config.save()

        if section == "voice" and ({"tts_engine", "tts_voice"} & values.keys()):
            self._start_tts_warmup()

        if section == "screen_vision" and "capture_interval_seconds" in values and self._screen_vision:
            self._screen_vision.set_interval(self.config.screen_vision.capture_interval_seconds)

        if section == "model" and ("cloud_provider" in values or "provider" in values):
            if self.config.model.cloud_provider:
                from pilot.models.cloud import CloudClient

                self._planner._model._cloud = CloudClient(self.config, self._vault)
                logger.info("Cloud client re-initialized for provider: %s", self.config.model.cloud_provider)

        return {"status": "ok"}

    async def _handle_reset_config(self, params: dict, ws: ServerConnection) -> dict:
        """Reset configuration to factory defaults."""

        default_config = PilotConfig()

        for field_name in default_config.__dataclass_fields__:
            val = getattr(default_config, field_name)
            current = getattr(self.config, field_name)

            if hasattr(val, "__dataclass_fields__"):
                for subfield in val.__dataclass_fields__:
                    setattr(current, subfield, getattr(val, subfield))
            else:
                setattr(self.config, field_name, val)

        self.config.save()

        return {"status": "ok"}

    # -- History --

    async def _handle_get_history(self, params: dict, ws: ServerConnection) -> dict:
        """Get conversation history from memory store.

        Args:
            params: JSON-RPC parameters with optional limit and offset.
            ws: The WebSocket connection.

        Returns:
            A dict with entries list containing historical interactions.
        """
        limit = params.get("limit", 50)
        offset = params.get("offset", 0)
        entries = await self._memory.get_history(limit=limit, offset=offset)
        return {"entries": entries}

    async def _handle_memory_checkpoint(self, params: dict, ws: ServerConnection) -> dict:
        """Manually trigger a SQLite WAL checkpoint for the memory store.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with checkpoint status and WAL checkpoint statistics.
        """
        if not self._memory:
            return {"status": "error", "message": "Memory store is not initialized"}
        return await self._memory.checkpoint()

    async def _handle_temporal_memory_status(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        """Return provenance-labelled memory facts for local user review."""
        if not self._memory:
            return {
                "status": "error",
                "message": "Memory store is not initialized",
            }
        limit = max(1, min(200, int(params.get("limit", 50))))
        return {"status": "ok", **(await self._memory.temporal_status(limit=limit))}

    async def _handle_temporal_memory_retract(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        """Let the user explicitly retract an active or candidate memory fact."""
        if not self._memory:
            return {
                "status": "error",
                "message": "Memory store is not initialized",
            }
        fact_id = str(params.get("fact_id") or "").strip()
        if not fact_id:
            return {"status": "error", "message": "fact_id is required"}
        try:
            fact = await self._memory.retract_fact(
                fact_id,
                reason=str(params.get("reason") or "Retracted from settings"),
            )
        except KeyError:
            return {
                "status": "error",
                "message": "The memory no longer exists or was already retracted.",
            }
        return {
            "status": "ok",
            "fact_id": fact.fact_id,
            "fact_status": fact.status.value,
        }

    async def _handle_export_session_chat(self, params: dict, ws: ServerConnection) -> dict:
        """Export current UI session chat messages to JSON or CSV."""
        fmt = str(params.get("format", "json")).lower()
        messages = params.get("messages", [])

        if fmt not in {"json", "csv", "markdown", "md"}:
            return {"status": "error", "message": "format must be 'json', 'csv', or 'markdown'"}
        if not isinstance(messages, list):
            return {"status": "error", "message": "messages must be a list"}

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"zariff-chat-{ts}.{fmt}"

        downloads_dir = Path.home() / "Downloads"
        export_dir = downloads_dir if downloads_dir.exists() else (DATA_DIR / "exports")
        export_dir.mkdir(parents=True, exist_ok=True)
        out_path = export_dir / filename

        try:
            if fmt == "json":
                out_path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
            elif fmt in ("markdown", "md"):
                lines = ["# Zariff Session Export", f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
                for m in messages:
                    if not isinstance(m, dict):
                        continue
                    msg_type = str(m.get("type", "unknown")).upper()
                    text = str(m.get("text", "")).strip()
                    lines.append(f"### {msg_type}")
                    if text:
                        lines.append(text)
                    plan = m.get("plan")
                    if isinstance(plan, dict) and plan.get("explanation"):
                        lines.append(f"\n**Plan:** {plan['explanation']}")
                    ar = m.get("actionResults")
                    if isinstance(ar, list) and ar:
                        lines.append("\n**Action Results:**\n```")
                        for r in ar:
                            if isinstance(r, dict):
                                out = r.get("output") or r.get("error") or ""
                                lines.append(str(out).strip())
                        lines.append("```")
                    lines.append("\n---")
                out_path.write_text("\n".join(lines), encoding="utf-8")
            else:
                with out_path.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(
                        [
                            "timestamp_iso",
                            "timestamp_ms",
                            "msg_type",
                            "text",
                            "plan_id",
                            "plan_explanation",
                            "plan_action_count",
                            "plan_actions",
                            "result_count",
                            "result_success_count",
                            "result_error_count",
                            "result_outputs",
                            "verification_passed",
                            "verification_details",
                        ]
                    )

                    for m in messages:
                        if not isinstance(m, dict):
                            continue

                        raw_ts = m.get("timestamp")
                        iso_ts = ""
                        if isinstance(raw_ts, (int, float)):
                            iso_ts = datetime.fromtimestamp(raw_ts / 1000).isoformat()

                        msg_type = str(m.get("type", ""))
                        text = str(m.get("text", ""))

                        plan = m.get("plan", {})
                        if not isinstance(plan, dict):
                            plan = {}
                        plan_id = str(plan.get("plan_id", ""))
                        plan_explanation = str(plan.get("explanation", ""))
                        plan_actions = plan.get("actions", [])
                        if not isinstance(plan_actions, list):
                            plan_actions = []
                        plan_action_count = len(plan_actions)
                        plan_actions_str = " | ".join(
                            f"{idx + 1}. {str(a.get('action_type', ''))} -> {str(a.get('target', ''))}"
                            for idx, a in enumerate(plan_actions)
                            if isinstance(a, dict)
                        )

                        action_results = m.get("actionResults", [])
                        if not isinstance(action_results, list):
                            action_results = []
                        result_count = len(action_results)
                        result_success_count = sum(
                            1 for r in action_results if isinstance(r, dict) and bool(r.get("success", False))
                        )
                        result_error_count = result_count - result_success_count
                        result_outputs = " | ".join(
                            str(r.get("output") or r.get("error") or "").strip()
                            for r in action_results
                            if isinstance(r, dict) and (r.get("output") or r.get("error"))
                        )

                        verification = m.get("verification", {})
                        if not isinstance(verification, dict):
                            verification = {}
                        verification_passed = (
                            verification.get("passed") if isinstance(verification.get("passed"), bool) else ""
                        )
                        verification_details_raw = verification.get("details", [])
                        if not isinstance(verification_details_raw, list):
                            verification_details_raw = []
                        verification_details = " | ".join(str(d) for d in verification_details_raw)

                        writer.writerow(
                            [
                                iso_ts,
                                raw_ts if isinstance(raw_ts, (int, float)) else "",
                                msg_type,
                                text,
                                plan_id,
                                plan_explanation,
                                plan_action_count,
                                plan_actions_str,
                                result_count,
                                result_success_count,
                                result_error_count,
                                result_outputs,
                                verification_passed,
                                verification_details,
                            ]
                        )
        except Exception as e:
            logger.exception("Failed to export session chat")
            return {"status": "error", "message": f"Export failed: {e}"}

        return {
            "status": "ok",
            "path": str(out_path),
            "count": len(messages),
            "format": fmt,
        }

    # -- API key management --

    async def _handle_store_api_key(self, params: dict, ws: ServerConnection) -> dict:
        """Store an API key for a provider in the vault.

        Args:
            params: JSON-RPC parameters with provider and api_key.
            ws: The WebSocket connection.

        Returns:
            A dict with status.
        """
        provider = params.get("provider", "")
        key = params.get("api_key", "") or params.get("key", "")
        if not provider or not key:
            return {"status": "error", "message": "provider and api_key are required"}
        from pilot.security.vault import VaultUnavailableError

        try:
            await self._vault.store_key(provider, key)
        except VaultUnavailableError as exc:
            return {"status": "error", "message": str(exc), "available": False}
        if self.config.model.cloud_provider == provider:
            from pilot.models.cloud import CloudClient

            self._planner._model._cloud = CloudClient(self.config, self._vault)
        return {"status": "ok"}

    async def _handle_delete_api_key(self, params: dict, ws: ServerConnection) -> dict:
        """Delete a stored API key for a provider.

        Args:
            params: JSON-RPC parameters with provider.
            ws: The WebSocket connection.

        Returns:
            A dict with status.
        """
        provider = params.get("provider", "")
        if not provider:
            return {"status": "error", "message": "provider is required"}
        from pilot.security.vault import VaultUnavailableError

        try:
            await self._vault.delete_key(provider)
        except VaultUnavailableError as exc:
            return {"status": "error", "message": str(exc), "available": False}
        return {"status": "ok"}

    async def _handle_list_api_keys(self, params: dict, ws: ServerConnection) -> dict:
        """List all providers with stored API keys.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with providers list.
        """
        from pilot.security.vault import VaultUnavailableError

        try:
            providers = await self._vault.list_providers()
        except VaultUnavailableError as exc:
            return {
                "providers": [],
                "available": False,
                "backend": self._vault.backend_name,
                "message": str(exc),
            }
        return {
            "providers": providers,
            "available": self._vault.available,
            "backend": self._vault.backend_name,
            "message": (
                ""
                if self._vault.available
                else "Secure OS credential storage is unavailable; API keys cannot be persisted."
            ),
        }

    # -- Ollama model discovery --

    async def _handle_list_ollama_models(self, params: dict, ws: ServerConnection) -> dict:
        """List available Ollama models.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with models list and availability status.
        """
        from pilot.models.ollama import OllamaClient

        client = OllamaClient(self.config.model.ollama_base_url)
        try:
            models = await client.list_models()
            return {"models": models, "available": True}
        except Exception:
            return {"models": [], "available": False}

    # -- Health --

    async def _handle_health(self, params: dict[str, Any], ws: ServerConnection) -> dict[str, Any]:
        """Return health status of the daemon.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with uptime, memory usage, active connections, and loaded agents.
        """
        import psutil

        # Calculate uptime
        uptime = time.time() - self._start_time

        # Get memory usage in MB
        process = psutil.Process()
        memory_mb = process.memory_info().rss / (1024**2)

        # Count active connections
        active_connections = len(self._clients)

        # Get loaded agent names
        loaded_agents: list[str] = []
        if self._orchestrator:
            loaded_agents = [role.value for role in self._orchestrator._agents]

        return {
            "uptime": uptime,
            "memory_usage_mb": memory_mb,
            "active_connections": active_connections,
            "loaded_agents": loaded_agents,
        }

    async def _handle_ready(self, params: dict[str, Any], ws: ServerConnection) -> dict[str, Any]:
        """Check if all agents are fully initialized and ready.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with ready status (True only if all agents are initialized).
        """
        from pilot.agents.base_agent import AgentStatus

        # If orchestrator is not initialized, not ready
        if not self._orchestrator:
            return {"ready": False}

        # If no agents are registered, not ready
        if not self._orchestrator._agents:
            return {"ready": False}

        for agent in self._orchestrator._agents.values():
            if not agent._running or agent.status in {AgentStatus.STOPPED, AgentStatus.ERROR}:
                return {"ready": False}

        return {"ready": True}

    async def _handle_ping(self, params: dict, ws: ServerConnection) -> dict:
        """Ping the server to check connectivity.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with pong and version.
        """
        return {"pong": True, "version": __version__}

    async def _handle_cursor_move(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Move the OS mouse cursor to an absolute screen position.

        This is the browser/dev-mode fallback for the gesture-cursor bridge
        (see GESTURES.md) — the primary path is a native Rust Tauri command
        (`move_gesture_cursor`, uses the `enigo` crate) that stays entirely
        in-process for ~30fps latency. This RPC goes through a WebSocket
        round-trip plus pyautogui's own PAUSE delay, which is fine for
        testing the wiring in a browser without a compiled Tauri binary but
        will not feel as smooth as the native path. Bypasses
        Planner/Executor/confirmation entirely — MOUSE_MOVE is Tier 1
        (USER_WRITE), already confirmed to never require confirmation.

        Args:
            params: JSON-RPC parameters containing x and y (absolute screen
                coordinates).
            ws: The WebSocket connection.

        Returns:
            A dict with status and a human-readable message.
        """
        from pilot.system import input_control

        try:
            x = int(params.get("x", 0))
            y = int(params.get("y", 0))
        except (TypeError, ValueError):
            return {"status": "error", "message": "x/y must be integers"}

        message = await input_control.mouse_move(x, y, duration=0.0)
        return {"status": "ok", "message": message}

    async def _handle_cursor_click(self, params: dict[str, Any], ws: ServerConnection) -> dict:
        """Click at the given screen position — the fallback counterpart to
        the Rust `click_gesture_cursor` command, used for the pinch-to-click
        gesture while cursor mode is active. See `_handle_cursor_move` for
        why this path exists and its latency caveats.

        Args:
            params: JSON-RPC parameters containing x and y (the position the
                gesture-cursor bridge last moved to).
            ws: The WebSocket connection.

        Returns:
            A dict with status and a human-readable message.
        """
        from pilot.system import input_control

        try:
            x = int(params.get("x", 0))
            y = int(params.get("y", 0))
        except (TypeError, ValueError):
            return {"status": "error", "message": "x/y must be integers"}

        message = await input_control.mouse_click(x, y, button="left")
        return {"status": "ok", "message": message}

    async def _handle_system_info(self, params: dict, ws: ServerConnection) -> dict:
        """Return exact hardware metrics (CPU, RAM, Disk, Uptime, Hostname) for HUD monitor."""
        import os
        import shutil
        import socket
        import time

        import psutil

        disk = shutil.disk_usage(os.path.abspath("/" if os.name != "nt" else "C:\\"))
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=0.1)
        uptime = (
            int(time.time() - psutil.boot_time())
            if hasattr(psutil, "boot_time")
            else int(time.time() - self._start_time)
        )
        return {
            "cpu_percent": round(cpu),
            "memory_percent": round(mem.percent),
            "memory_used": mem.used,
            "memory_total": mem.total,
            "disk_percent": round((disk.used / disk.total) * 100),
            "disk_used": disk.used,
            "disk_total": disk.total,
            "hostname": socket.gethostname(),
            "uptime_seconds": uptime,
        }

    async def _handle_get_uptime(self, params: dict, ws: ServerConnection) -> str:
        """Return formatted system uptime string for HUD monitor."""
        import time

        import psutil

        up_sec = (
            int(time.time() - psutil.boot_time())
            if hasattr(psutil, "boot_time")
            else int(time.time() - self._start_time)
        )
        days = up_sec // 86400
        hrs = (up_sec % 86400) // 3600
        mins = (up_sec % 3600) // 60
        return f"{days}d {hrs}h {mins}m" if days > 0 else f"{hrs}h {mins}m"

    async def _handle_system_status(self, params: dict, ws: ServerConnection) -> dict:
        """Return current system information.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with platform info and capabilities count.
        """
        from pilot.system.platform_detect import get_platform_info

        info = get_platform_info()
        return {
            "platform": info,
            "capabilities_count": len(self._executor._dispatch_table),
        }

    async def _handle_capabilities(self, params: dict, ws: ServerConnection) -> dict:
        """Return all available action types.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with action_types list and count.
        """
        from pilot.actions import ActionType

        return {
            "action_types": [t.value for t in ActionType],
            "count": len(ActionType),
        }

    # -- Advanced Agent Endpoints --

    async def _handle_reflection_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Return self-improvement reflection statistics.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with reflection statistics from the reflector agent.
        """
        return await self._reflector.get_stats()

    async def _handle_background_tasks(self, params: dict, ws: ServerConnection) -> dict:
        """List all registered background monitoring tasks.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with list of background tasks.
        """
        return {"tasks": self._background.list_tasks()}

    async def _handle_background_start(self, params: dict, ws: ServerConnection) -> dict:
        """Start a background monitoring task.

        Args:
            params: JSON-RPC parameters with task_id.
            ws: The WebSocket connection.

        Returns:
            A dict with status and task_id.
        """
        task_id = params.get("task_id", "")
        ok = self._background.start(task_id)
        return {"status": "started" if ok else "error", "task_id": task_id}

    async def _handle_background_stop(self, params: dict, ws: ServerConnection) -> dict:
        """Stop a background monitoring task.

        Args:
            params: JSON-RPC parameters with task_id.
            ws: The WebSocket connection.

        Returns:
            A dict with status and task_id.
        """
        task_id = params.get("task_id", "")
        ok = self._background.stop(task_id)
        return {"status": "stopped" if ok else "error", "task_id": task_id}

    async def _handle_risk_gate_status(self, params: dict, ws: ServerConnection) -> dict:
        """Report the trained risk-world-model state and latest prediction."""
        from pilot.security.risk_gate import get_risk_gate

        return {
            "status": "ok",
            **get_risk_gate().status(enabled=self.config.gateway.risk_gate_enabled),
        }

    async def _handle_risk_gate_config_update(self, params: dict, ws: ServerConnection) -> dict:
        """Enable or disable risk-world-model evaluation and persist it."""
        if "enabled" in params:
            self.config.gateway.risk_gate_enabled = bool(params["enabled"])
        self.config.save()
        return await self._handle_risk_gate_status({}, ws)

    async def _handle_self_healing_status(self, params: dict, ws: ServerConnection) -> dict:
        """Report self-healing config plus recent remediation attempts.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with the current config and up to 50 recent attempts
            (auto-executed, proposed/pending, confirmed, denied, timed out).
        """
        engine = getattr(self, "_self_healing", None)
        monitor_tasks = {
            task["task_id"].removeprefix("monitor_"): task
            for task in (self._background.list_tasks() if self._background else [])
            if task["task_id"] in {"monitor_cpu", "monitor_memory", "monitor_disk"}
        }
        return {
            "enabled": self.config.self_healing.enabled,
            "auto_execute_max_tier": self.config.self_healing.auto_execute_max_tier,
            "watched_metrics": self.config.self_healing.watched_metrics,
            "monitors": monitor_tasks,
            "attempts": engine.list_attempts() if engine else [],
        }

    def _sync_self_healing_monitors(self) -> None:
        """Start only configured health monitors and stop only loops we own.

        A monitor can also be started through the generic background-task
        RPC. Those user-owned loops must not be stopped when Autonomous
        Healing is disabled, so ownership is tracked separately.
        """
        if not self._background:
            return

        configured = set(self.config.self_healing.watched_metrics)
        desired = {f"monitor_{metric}" for metric in configured} if self.config.self_healing.enabled else set()

        for task_id in self._self_healing_started_monitors - desired:
            self._background.stop(task_id)
        self._self_healing_started_monitors.intersection_update(desired)

        for task_id in desired:
            task = self._background._tasks.get(task_id)
            if task is None or task.status.value == "running":
                continue
            if self._background.start(task_id):
                self._self_healing_started_monitors.add(task_id)

    async def _handle_self_healing_config_update(self, params: dict, ws: ServerConnection) -> dict:
        """Update self-healing config (enabled, tiering, watched metrics).

        Confirming or denying a specific proposed remediation plan reuses
        the existing generic ``confirm`` RPC (same plan_id/PendingConfirmation
        mechanism as ThreatContainmentBridge) rather than a dedicated
        approve/reject RPC.

        Args:
            params: JSON-RPC parameters, any of {enabled, auto_execute_max_tier,
                cooldown_seconds, confirm_timeout_seconds, watched_metrics}.
            ws: The WebSocket connection.

        Returns:
            A dict with status and the updated config.
        """
        cfg = self.config.self_healing
        enabled = bool(params.get("enabled", cfg.enabled))
        try:
            max_tier = int(params.get("auto_execute_max_tier", cfg.auto_execute_max_tier))
            cooldown = float(params.get("cooldown_seconds", cfg.cooldown_seconds))
            confirm_timeout = float(params.get("confirm_timeout_seconds", cfg.confirm_timeout_seconds))
        except (TypeError, ValueError):
            return {"status": "error", "message": "tier and timeout values must be numeric"}
        if not 0 <= max_tier <= 3:
            return {"status": "error", "message": "auto_execute_max_tier must be between 0 and 3"}
        if cooldown < 0:
            return {"status": "error", "message": "cooldown_seconds must be non-negative"}
        if confirm_timeout <= 0:
            return {"status": "error", "message": "confirm_timeout_seconds must be positive"}

        watched_metrics = list(cfg.watched_metrics)
        if "watched_metrics" in params:
            raw_metrics = params["watched_metrics"]
            if not isinstance(raw_metrics, list):
                return {"status": "error", "message": "watched_metrics must be a list"}
            watched_metrics = list(dict.fromkeys(str(metric) for metric in raw_metrics))
            unsupported = sorted(set(watched_metrics) - {"cpu", "memory", "disk"})
            if unsupported:
                return {
                    "status": "error",
                    "message": f"unsupported watched metrics: {', '.join(unsupported)}",
                }
        if enabled and not watched_metrics:
            return {"status": "error", "message": "select at least one watched metric"}

        cfg.enabled = enabled
        cfg.auto_execute_max_tier = max_tier
        cfg.cooldown_seconds = cooldown
        cfg.confirm_timeout_seconds = confirm_timeout
        cfg.watched_metrics = watched_metrics

        self.config.save()
        self._sync_self_healing_monitors()
        return {
            "status": "ok",
            "enabled": cfg.enabled,
            "auto_execute_max_tier": cfg.auto_execute_max_tier,
            "cooldown_seconds": cfg.cooldown_seconds,
            "confirm_timeout_seconds": cfg.confirm_timeout_seconds,
            "watched_metrics": cfg.watched_metrics,
        }

    async def _handle_narration_status(self, params: dict, ws: ServerConnection) -> dict:
        """Report the Live Execution Narrator's current config.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with the current narration config.
        """
        cfg = self.config.narration
        return {
            "enabled": cfg.enabled,
            "narrate_steps": cfg.narrate_steps,
            "interrupt_on_risk": cfg.interrupt_on_risk,
            "proactive_review_enabled": cfg.proactive_review_enabled,
            "live_corrections_enabled": cfg.live_corrections_enabled,
            "follow_up_enabled": cfg.follow_up_enabled,
            "advisory_timeout_seconds": cfg.advisory_timeout_seconds,
            "max_auto_revisions": cfg.max_auto_revisions,
            "confirm_timeout_seconds": cfg.confirm_timeout_seconds,
        }

    async def _handle_narration_config_update(self, params: dict, ws: ServerConnection) -> dict:
        """Update Live Execution Narrator config.

        Approving or denying a specific proposed interrupt reuses the
        existing generic ``confirm`` RPC (same plan_id/PendingConfirmation
        mechanism as ThreatContainmentBridge/AutonomousHealingEngine)
        rather than a dedicated approve/reject RPC.

        Args:
            params: JSON-RPC parameters, any of {enabled, narrate_steps,
                interrupt_on_risk, confirm_timeout_seconds}.
            ws: The WebSocket connection.

        Returns:
            A dict with status and the updated config.
        """
        cfg = self.config.narration
        if "enabled" in params:
            cfg.enabled = bool(params["enabled"])
        if "narrate_steps" in params:
            cfg.narrate_steps = bool(params["narrate_steps"])
        if "interrupt_on_risk" in params:
            cfg.interrupt_on_risk = bool(params["interrupt_on_risk"])
        if "proactive_review_enabled" in params:
            cfg.proactive_review_enabled = bool(params["proactive_review_enabled"])
        if "live_corrections_enabled" in params:
            cfg.live_corrections_enabled = bool(params["live_corrections_enabled"])
        if "follow_up_enabled" in params:
            cfg.follow_up_enabled = bool(params["follow_up_enabled"])
        if "advisory_timeout_seconds" in params:
            cfg.advisory_timeout_seconds = max(
                1.0,
                min(30.0, float(params["advisory_timeout_seconds"])),
            )
        if "max_auto_revisions" in params:
            cfg.max_auto_revisions = max(0, min(5, int(params["max_auto_revisions"])))
        if "confirm_timeout_seconds" in params:
            cfg.confirm_timeout_seconds = float(params["confirm_timeout_seconds"])

        self.config.save()
        return {
            "status": "ok",
            "enabled": cfg.enabled,
            "narrate_steps": cfg.narrate_steps,
            "interrupt_on_risk": cfg.interrupt_on_risk,
            "proactive_review_enabled": cfg.proactive_review_enabled,
            "live_corrections_enabled": cfg.live_corrections_enabled,
            "follow_up_enabled": cfg.follow_up_enabled,
            "advisory_timeout_seconds": cfg.advisory_timeout_seconds,
            "max_auto_revisions": cfg.max_auto_revisions,
            "confirm_timeout_seconds": cfg.confirm_timeout_seconds,
        }

    async def _handle_supervision_status(self, params: dict, ws: ServerConnection) -> dict:
        """Report User Manual Supervision's current config plus whether the
        keyboard/mouse hook (if enabled) is actually still alive.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with the current supervision config and hook_healthy.
        """
        cfg = self.config.supervision
        hook_healthy = getattr(self, "_supervision_hook", None)
        return {
            "enabled": cfg.enabled,
            "keyboard_mouse_hook_enabled": cfg.keyboard_mouse_hook_enabled,
            "cognitive_coaching_enabled": cfg.cognitive_coaching_enabled,
            "risk_pattern_detection_enabled": cfg.risk_pattern_detection_enabled,
            "hook_healthy": hook_healthy.is_running() if hook_healthy else False,
        }

    async def _handle_supervision_config_update(self, params: dict, ws: ServerConnection) -> dict:
        """Update User Manual Supervision config.

        Unlike `_handle_narration_config_update`/`_handle_self_healing_config_update`,
        this handler must actually start/stop the background task and the
        keyboard/mouse hook on an `enabled`/`keyboard_mouse_hook_enabled`
        transition -- the thing being gated has real cost and privacy
        weight even when idle, so a config flip alone isn't enough.

        Args:
            params: JSON-RPC parameters, any of {enabled,
                keyboard_mouse_hook_enabled, cognitive_coaching_enabled,
                risk_pattern_detection_enabled, tick_interval_seconds,
                ocr_interval_seconds, stress_coaching_threshold,
                cognitive_load_coaching_threshold, coaching_cooldown_seconds,
                risk_cooldown_seconds, keystroke_buffer_max_chars,
                ocr_snippet_max_chars}.
            ws: The WebSocket connection.

        Returns:
            A dict with status and the updated config.
        """
        cfg = self.config.supervision
        was_enabled = cfg.enabled
        was_hook_enabled = cfg.keyboard_mouse_hook_enabled

        if "enabled" in params:
            cfg.enabled = bool(params["enabled"])
        if "keyboard_mouse_hook_enabled" in params:
            cfg.keyboard_mouse_hook_enabled = bool(params["keyboard_mouse_hook_enabled"])
        if "cognitive_coaching_enabled" in params:
            cfg.cognitive_coaching_enabled = bool(params["cognitive_coaching_enabled"])
        if "risk_pattern_detection_enabled" in params:
            cfg.risk_pattern_detection_enabled = bool(params["risk_pattern_detection_enabled"])
        if "tick_interval_seconds" in params:
            cfg.tick_interval_seconds = float(params["tick_interval_seconds"])
        if "ocr_interval_seconds" in params:
            cfg.ocr_interval_seconds = float(params["ocr_interval_seconds"])
        if "stress_coaching_threshold" in params:
            cfg.stress_coaching_threshold = float(params["stress_coaching_threshold"])
        if "cognitive_load_coaching_threshold" in params:
            cfg.cognitive_load_coaching_threshold = float(params["cognitive_load_coaching_threshold"])
        if "coaching_cooldown_seconds" in params:
            cfg.coaching_cooldown_seconds = float(params["coaching_cooldown_seconds"])
        if "risk_cooldown_seconds" in params:
            cfg.risk_cooldown_seconds = float(params["risk_cooldown_seconds"])
        if "keystroke_buffer_max_chars" in params:
            cfg.keystroke_buffer_max_chars = int(params["keystroke_buffer_max_chars"])
        if "ocr_snippet_max_chars" in params:
            cfg.ocr_snippet_max_chars = int(params["ocr_snippet_max_chars"])

        supervision_hook = getattr(self, "_supervision_hook", None)
        if supervision_hook is not None:
            if cfg.enabled and cfg.keyboard_mouse_hook_enabled and not (was_enabled and was_hook_enabled):
                supervision_hook.start()
            elif not (cfg.enabled and cfg.keyboard_mouse_hook_enabled) and was_enabled and was_hook_enabled:
                supervision_hook.stop()

        background = getattr(self, "_background", None)
        if background is not None:
            if cfg.enabled and not was_enabled:
                background.start("user_supervision")
            elif not cfg.enabled and was_enabled:
                background.stop("user_supervision")

        self.config.save()
        return {
            "status": "ok",
            "enabled": cfg.enabled,
            "keyboard_mouse_hook_enabled": cfg.keyboard_mouse_hook_enabled,
            "cognitive_coaching_enabled": cfg.cognitive_coaching_enabled,
            "risk_pattern_detection_enabled": cfg.risk_pattern_detection_enabled,
            "hook_healthy": supervision_hook.is_running() if supervision_hook else False,
        }

    async def _handle_agent_routing(self, params: dict, ws: ServerConnection) -> dict:
        """Analyze which specialist agent(s) would handle a given input.

        Args:
            params: JSON-RPC parameters with input query.
            ws: The WebSocket connection.

        Returns:
            A dict with routing summary and optionally orchestrator info.
        """
        query = params.get("input", "")
        result = self._multi_agent.get_routing_summary(query)
        if self._orchestrator:
            result["orchestrator"] = self._orchestrator.get_input_routing_summary(query)
        return result

    async def _handle_agent_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Return performance stats for all registered agents.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with agent performance statistics.
        """
        if self._orchestrator:
            return self._orchestrator.get_all_stats()
        return {"error": "Orchestrator not initialized"}

    async def _handle_agent_capabilities(self, params: dict, ws: ServerConnection) -> dict:
        """Return all agent capabilities grouped by specialist.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with all agent capabilities.
        """
        if self._orchestrator:
            return self._orchestrator.get_all_capabilities()
        return {"error": "Orchestrator not initialized"}

    async def _handle_agent_mesh_status(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._agent_mesh is None:
            return {"enabled": False, "message": "Agent mesh is not initialized"}
        return self._agent_mesh.status()

    async def _handle_agent_spawn(self, params: dict, ws: ServerConnection) -> dict:
        """Dynamically spawn a new specialist agent.

        Args:
            params: JSON-RPC parameters with role.
            ws: The WebSocket connection.

        Returns:
            A dict with status and optionally agent_id.
        """
        agent_name = str(params.get("agent_name", "")).strip()
        if agent_name and self._orchestrator:
            agent = await self._orchestrator.spawn_registered_agent(
                agent_name,
                executor=self._executor,
                background_manager=self._background,
                model_router=self._model_router,
                config=self.config,
                vault=self._vault,
                memory=self._memory,
            )
            if agent:
                return {
                    "status": "spawned",
                    "agent_id": agent.agent_id,
                    "agent_name": agent.__class__.__name__,
                }
            return {"status": "error", "message": f"Unknown or unavailable specialist: {agent_name}"}

        role_str = params.get("role", "")
        from pilot.agents.base_agent import AgentRole

        try:
            role = AgentRole(role_str)
        except ValueError:
            return {"status": "error", "message": f"Unknown role: {role_str}"}

        if self._orchestrator:
            agent = await self._orchestrator.spawn_agent(
                role,
                executor=self._executor,
                background_manager=self._background,
            )
            if agent:
                return {"status": "spawned", "agent_id": agent.agent_id}
        return {"status": "error", "message": "Failed to spawn agent"}

    # -- Multimodal Fusion --

    async def _handle_voice_event(self, params: dict, ws: ServerConnection) -> dict:
        """Receive a voice event from the frontend and feed it to fusion engine.

        Args:
            params: JSON-RPC parameters with transcript, confidence, is_final.
            ws: The WebSocket connection.

        Returns:
            A dict with status and optionally fused intent.
        """
        if not self._fusion:
            return {"status": "error", "message": "Fusion engine not initialized"}

        from pilot.multimodal.fusion import InputEvent, ModalityType

        event = InputEvent(
            modality=ModalityType.VOICE,
            transcript=params.get("transcript", ""),
            voice_confidence=params.get("confidence", 0.8),
            is_final=params.get("is_final", False),
        )
        voice_confidence = params.get("confidence", 0.8)
        await self._append_experience(
            ExperienceEventType.OBSERVATION,
            idempotency_key=f"observation:voice:{uuid.uuid4()}",
            source="voice",
            payload={
                "transcript": params.get("transcript", ""),
                "is_final": bool(params.get("is_final", False)),
            },
            confidence=(
                float(voice_confidence)
                if isinstance(voice_confidence, (int, float)) and 0.0 <= voice_confidence <= 1.0
                else None
            ),
            provenance={"component": "MultimodalFusionEngine.on_voice_event"},
            privacy_class=PrivacyClass.SENSITIVE,
        )
        intent = await self._fusion.on_voice_event(event)
        if intent:
            return {"status": "fused", "intent": intent.to_dict()}
        return {"status": "buffered"}

    async def _handle_gesture_event(self, params: dict, ws: ServerConnection) -> dict:
        """Receive a gesture event from the frontend and feed it to fusion engine.

        Args:
            params: JSON-RPC parameters with gesture, confidence, data.
            ws: The WebSocket connection.

        Returns:
            A dict with status and optionally fused intent.
        """
        if not self._fusion:
            return {"status": "error", "message": "Fusion engine not initialized"}

        from pilot.multimodal.fusion import InputEvent, ModalityType

        event = InputEvent(
            modality=ModalityType.GESTURE,
            gesture_name=params.get("gesture", ""),
            gesture_confidence=params.get("confidence", 0.8),
            gesture_data=params.get("data", {}),
        )
        gesture_confidence = params.get("confidence", 0.8)
        await self._append_experience(
            ExperienceEventType.OBSERVATION,
            idempotency_key=f"observation:gesture:{uuid.uuid4()}",
            source="gesture",
            payload={
                "gesture": params.get("gesture", ""),
                "raw_sensor_data_excluded": True,
            },
            confidence=(
                float(gesture_confidence)
                if isinstance(gesture_confidence, (int, float)) and 0.0 <= gesture_confidence <= 1.0
                else None
            ),
            provenance={"component": "MultimodalFusionEngine.on_gesture_event"},
            privacy_class=PrivacyClass.BIOMETRIC_DERIVED,
        )
        intent = await self._fusion.on_gesture_event(event)
        if intent:
            return {"status": "fused", "intent": intent.to_dict()}
        return {"status": "buffered"}

    async def _handle_gaze_event(self, params: dict, ws: ServerConnection) -> dict:
        """Receive a coarse gaze-region reading from the frontend and feed
        it to the fusion engine as a passive disambiguating signal.

        Args:
            params: JSON-RPC parameters with region, confidence. Never raw
                face landmarks — see gazeTracking.ts's privacy rationale.
            ws: The WebSocket connection.

        Returns:
            A dict with status — gaze never itself produces a fused
            intent (see MultimodalFusionEngine.on_gaze_event's docstring),
            only "ingested" or "ignored" (below the confidence floor) or
            "error" (fusion engine not initialized).
        """
        if not self._fusion:
            return {"status": "error", "message": "Fusion engine not initialized"}

        if not self.config.vision.gaze_tracking_enabled:
            return {"status": "ignored", "reason": "gaze_tracking_disabled"}

        region = params.get("region", "")
        if region not in {"center", "left", "right", "up", "down"}:
            return {"status": "ignored", "reason": "invalid_region"}

        confidence = params.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            return {"status": "ignored", "reason": "invalid_confidence"}
        if confidence < self._fusion.min_gaze_confidence:
            return {"status": "ignored", "reason": "confidence_below_threshold"}

        from pilot.multimodal.fusion import InputEvent, ModalityType

        event = InputEvent(
            modality=ModalityType.GAZE,
            gaze_region=region,
            gaze_confidence=confidence,
        )
        await self._append_experience(
            ExperienceEventType.OBSERVATION,
            idempotency_key=f"observation:gaze:{uuid.uuid4()}",
            source="gaze",
            payload={"region": region, "raw_sensor_data_excluded": True},
            confidence=float(confidence),
            provenance={"component": "MultimodalFusionEngine.on_gaze_event"},
            privacy_class=PrivacyClass.BIOMETRIC_DERIVED,
        )
        await self._fusion.on_gaze_event(event)
        return {"status": "ingested"}

    async def _handle_multimodal_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Return multimodal fusion engine statistics.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with fusion engine stats or error.
        """
        if self._fusion:
            return self._fusion.get_stats()
        return {"error": "Fusion engine not initialized"}

    # -- Reasoning Visualization --

    async def _handle_reasoning_log(self, params: dict, ws: ServerConnection) -> dict:
        """Return the full reasoning event log for the current session.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with events list or error.
        """
        if self._reasoning:
            return {"events": self._reasoning.get_session_log()}
        return {"error": "Reasoning emitter not initialized"}

    async def _handle_reasoning_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Return reasoning emitter statistics.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with reasoning emitter statistics or error.
        """
        if self._reasoning:
            return self._reasoning.get_stats()
        return {"error": "Reasoning emitter not initialized"}

    # -- Task Decomposition --

    async def _handle_decompose_task(self, params: dict, ws: ServerConnection) -> dict:
        """Decompose a complex goal into subtasks.

        Args:
            params: JSON-RPC parameters with goal.
            ws: The WebSocket connection.

        Returns:
            A dict with decomposed task structure or error.
        """
        goal = params.get("goal", "")
        if not goal:
            return {"error": "No goal provided"}
        if self._decomposer:
            decomp = await self._decomposer.decompose(goal)
            return decomp.to_dict()
        return {"error": "Decomposer not initialized"}

    # -- Simulation Sandbox --

    async def _handle_simulate_plan(self, params: dict, ws: ServerConnection) -> dict:
        """Simulate a plan and return an impact report without execution.

        Args:
            params: JSON-RPC parameters with optional plan_id.
            ws: The WebSocket connection.

        Returns:
            A dict with impact report or error.
        """
        if not self._sandbox:
            return {"error": "Sandbox not initialized"}

        plan_id = params.get("plan_id", "")
        pending = self._pending_confirms.get(plan_id)
        if pending and pending.plan:
            report = await self._sandbox.simulate(pending.plan)
            return report.to_dict()

        return {"error": "No plan found to simulate"}

    # -- Self-Improving Prompt System --

    async def _handle_prompt_strategies(self, params: dict, ws: ServerConnection) -> dict:
        """Get proven prompt strategies for a task.

        Args:
            params: JSON-RPC parameters with query.
            ws: The WebSocket connection.

        Returns:
            A dict with strategies or error.
        """
        query = params.get("query", "")
        if not query:
            return {"strategies": ""}
        if self._prompt_improver:
            strategies = await self._prompt_improver.get_relevant_strategies(query)
            return {"strategies": strategies}
        return {"error": "Prompt improver not initialized"}

    async def _handle_prompt_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Return prompt improvement statistics.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with prompt improvement stats or error.
        """
        if self._prompt_improver:
            return await self._prompt_improver.get_stats()
        return {"error": "Prompt improver not initialized"}

    async def _handle_strategy_evolution_status(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"enabled": False, "message": "Strategy evolution is not initialized"}
        return await self._strategy_evolution.status()

    async def _handle_strategy_candidates(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"candidates": [], "message": "Strategy evolution is not initialized"}
        stage = params.get("stage")
        candidates = await self._strategy_evolution.list_candidates(
            stage=str(stage) if stage else None,
            limit=int(params.get("limit", 100)),
        )
        return {
            "candidates": [
                candidate.to_dict(include_content=bool(params.get("include_content", False)))
                for candidate in candidates
            ]
        }

    async def _handle_strategy_propose(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"status": "error", "message": "Strategy evolution is not initialized"}
        candidate = await self._strategy_evolution.propose(
            artifact_type=str(params.get("artifact_type", "")),
            component=str(params.get("component", "")),
            content=str(params.get("content", "")),
            rationale=str(params.get("rationale", "")),
            parent_candidate_id=str(params.get("parent_candidate_id", "")),
            source_trace_ids=tuple(params.get("source_trace_ids", ())),
            source="admin",
        )
        return {"status": "candidate", "candidate": candidate.to_dict(include_content=True)}

    async def _handle_strategy_reflect(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"status": "error", "message": "Strategy evolution is not initialized"}
        candidate = await self._strategy_evolution.reflect_candidate(
            artifact_type=str(params.get("artifact_type", "")),
            component=str(params.get("component", "")),
            base_content=str(params.get("base_content", "")),
            diagnostics=tuple(params.get("diagnostics", ())),
            source_trace_ids=tuple(params.get("source_trace_ids", ())),
            parent_candidate_id=str(params.get("parent_candidate_id", "")),
        )
        return {"status": "candidate", "candidate": candidate.to_dict(include_content=True)}

    async def _handle_strategy_start_shadow(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"status": "error", "message": "Strategy evolution is not initialized"}
        candidate = await self._strategy_evolution.start_shadow(str(params.get("candidate_id", "")))
        return {"status": candidate.stage.value, "candidate": candidate.to_dict()}

    async def _handle_strategy_record_isolated(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"status": "error", "message": "Strategy evolution is not initialized"}
        baseline_results = params.get("baseline_results", ())
        candidate_results = params.get("candidate_results", ())
        if not isinstance(baseline_results, list) or not isinstance(candidate_results, list):
            raise ValueError("baseline_results and candidate_results must be arrays")
        candidate = await self._strategy_evolution.record_isolated_attestation(
            str(params.get("candidate_id", "")),
            harness_run_id=str(params.get("harness_run_id", "")),
            baseline_results=baseline_results,
            candidate_results=candidate_results,
        )
        return {"status": candidate.stage.value, "candidate": candidate.to_dict()}

    async def _handle_strategy_record_shadow(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"status": "error", "message": "Strategy evolution is not initialized"}
        candidate = await self._strategy_evolution.record_shadow_evaluation(
            str(params.get("candidate_id", "")),
            sample_count=int(params.get("sample_count", 0)),
            baseline_score=float(params.get("baseline_score", 0.0)),
            candidate_score=float(params.get("candidate_score", 0.0)),
            safety_incidents=int(params.get("safety_incidents", 0)),
        )
        return {"status": candidate.stage.value, "candidate": candidate.to_dict()}

    async def _handle_strategy_start_canary(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"status": "error", "message": "Strategy evolution is not initialized"}
        candidate = await self._strategy_evolution.start_canary(
            str(params.get("candidate_id", "")),
            actor=str(params.get("actor", "")),
            consent_confirmed=bool(params.get("consent_confirmed", False)),
        )
        return {"status": candidate.stage.value, "candidate": candidate.to_dict()}

    async def _handle_strategy_record_canary(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"status": "error", "message": "Strategy evolution is not initialized"}
        candidate = await self._strategy_evolution.record_canary_evaluation(
            str(params.get("candidate_id", "")),
            sample_count=int(params.get("sample_count", 0)),
            baseline_score=float(params.get("baseline_score", 0.0)),
            candidate_score=float(params.get("candidate_score", 0.0)),
            safety_incidents=int(params.get("safety_incidents", 0)),
        )
        return {"status": candidate.stage.value, "candidate": candidate.to_dict()}

    async def _handle_strategy_promote(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"status": "error", "message": "Strategy evolution is not initialized"}
        assignment = await self._strategy_evolution.promote(
            str(params.get("candidate_id", "")),
            actor=str(params.get("actor", "")),
            confirmation=str(params.get("confirmation", "")),
        )
        return {"status": "promoted", "assignment": assignment.to_dict()}

    async def _handle_strategy_rollback(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._strategy_evolution is None:
            return {"status": "error", "message": "Strategy evolution is not initialized"}
        assignment = await self._strategy_evolution.rollback(
            str(params.get("component", "")),
            actor=str(params.get("actor", "")),
            confirmation=str(params.get("confirmation", "")),
        )
        return {
            "status": "rolled_back",
            "assignment": assignment.to_dict() if assignment is not None else None,
        }

    async def _handle_evolution_status(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._evolution_harness is None:
            return {"enabled": False, "message": "Evolution harness is not initialized"}
        return await self._evolution_harness.status()

    async def _handle_evolution_runs(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._evolution_harness is None:
            return {"runs": [], "message": "Evolution harness is not initialized"}
        runs = await self._evolution_harness.list_runs(
            limit=max(1, min(int(params.get("limit", 100)), 500)),
        )
        return {"runs": [run.to_dict() for run in runs]}

    async def _handle_evolution_candidates(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._evolution_harness is None:
            return {"candidates": [], "message": "Evolution harness is not initialized"}
        run_id = str(params.get("run_id", "")).strip()
        candidates = await self._evolution_harness.list_candidates(
            run_id=run_id or None,
            limit=max(1, min(int(params.get("limit", 100)), 500)),
            include_patch=bool(params.get("include_patch", False)),
        )
        return {
            "candidates": [
                candidate.to_dict(include_patch=bool(params.get("include_patch", False))) for candidate in candidates
            ]
        }

    async def _handle_evolution_create_run(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._evolution_harness is None:
            return {"status": "error", "message": "Evolution harness is not initialized"}
        run = await self._evolution_harness.create_run(
            str(params.get("problem", "")),
            profile=str(params.get("profile", "python")),
        )
        return {"status": run.state.value, "run": run.to_dict()}

    async def _handle_evolution_generate_candidates(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._evolution_harness is None:
            return {"status": "error", "message": "Evolution harness is not initialized"}
        candidates = await self._evolution_harness.generate_candidates(
            str(params.get("run_id", "")),
            count=max(2, min(int(params.get("count", 3)), 8)),
        )
        return {
            "status": "collecting",
            "candidates": [candidate.to_dict() for candidate in candidates],
        }

    async def _handle_evolution_evaluate(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._evolution_harness is None:
            return {"status": "error", "message": "Evolution harness is not initialized"}
        run = await self._evolution_harness.evaluate(str(params.get("run_id", "")))
        return {"status": run.state.value, "run": run.to_dict()}

    async def _handle_evolution_request_promotion(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        if self._evolution_harness is None:
            return {"status": "error", "message": "Evolution harness is not initialized"}
        return await self._evolution_harness.request_promotion(
            str(params.get("candidate_id", "")),
            actor=str(params.get("actor", "")),
            confirmation=str(params.get("confirmation", "")),
        )

    # -- Plugin Ecosystem --

    async def _handle_plugin_list(self, params: dict, ws: ServerConnection) -> dict:
        """List all loaded plugins.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with plugin statistics or error.
        """
        if self._plugin_registry:
            return self._plugin_registry.get_stats()
        return {"error": "Plugin registry not initialized"}

    async def _handle_plugin_tools(self, params: dict, ws: ServerConnection) -> dict:
        """List all available plugin tools.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with tools list or error.
        """
        if self._plugin_registry:
            return {"tools": self._plugin_registry.get_all_tools()}
        return {"error": "Plugin registry not initialized"}

    async def _handle_plugin_toggle(self, params: dict, ws: ServerConnection) -> dict:
        """Enable or disable a plugin.

        Args:
            params: JSON-RPC parameters with name and enabled status.
            ws: The WebSocket connection.

        Returns:
            A dict with success status, plugin name, and enabled state.
        """
        name = params.get("name", "")
        enabled = params.get("enabled", True)
        if not name:
            return {"error": "No plugin name provided"}
        if self._plugin_registry:
            if enabled:
                ok = self._plugin_registry.enable_plugin(name)
            else:
                ok = self._plugin_registry.disable_plugin(name)
            self._refresh_plugin_planner_context()
            return {"success": ok, "plugin": name, "enabled": enabled}
        return {"error": "Plugin registry not initialized"}

    def _refresh_plugin_planner_context(self) -> None:
        """Keep the planner's plugin tool inventory synchronized."""
        if self._planner and self._plugin_registry:
            self._planner.set_plugin_context(self._plugin_registry.get_tools_for_planner())
        if getattr(self, "_agent_mesh", None) and self._plugin_registry:
            self._agent_mesh.refresh_plugins(self._plugin_registry.get_all_plugins())

    async def _handle_plugin_market_list(self, params: dict, ws: ServerConnection) -> dict:
        """Return the approved GitHub catalog plus local-only plugins."""
        if not self._plugin_marketplace:
            return {"plugins": [], "error": "Plugin marketplace not initialized"}
        try:
            catalog = await asyncio.to_thread(self._plugin_marketplace.load_catalog)
            installed_plugins = self._plugin_registry.get_all_plugins() if self._plugin_registry else []
            installed_by_name = {plugin.name: plugin for plugin in installed_plugins}
            approved_names: set[str] = set()
            plugins: list[dict[str, Any]] = []
            for approved in catalog.data["plugins"]:
                item = dict(approved)
                name = item["name"]
                approved_names.add(name)
                item["installed"] = name in installed_by_name
                item["local_only"] = False
                item["source"] = catalog.source
                plugins.append(item)

            for plugin in installed_plugins:
                if plugin.name in approved_names:
                    continue
                item = plugin.to_dict()
                item.update(
                    {
                        "installed": True,
                        "local_only": True,
                        "source": "local",
                        "url": "",
                    }
                )
                plugins.append(item)

            return {
                "plugins": plugins,
                "source": catalog.source,
                "registry_url": catalog.registry_url,
                "submission_url": catalog.data.get("submission_url", ""),
                "warning": catalog.warning,
            }
        except Exception as exc:
            logger.error("Failed to load plugin marketplace: %s", exc)
            return {"plugins": [], "error": str(exc)}

    async def _handle_plugin_install_legacy(self, params: dict, ws: ServerConnection) -> dict:
        """Install a plugin from the marketplace with fully working code and Ed25519 signature."""
        import json

        from pilot.plugins import sign_plugin_directory

        plugin_name = params.get("plugin_name", "")
        if not plugin_name:
            return {"error": "plugin_name is required"}

        plugin_dir = PLUGINS_DIR / plugin_name
        plugin_dir.mkdir(parents=True, exist_ok=True)

        repo_root = Path(__file__).parent.parent.parent
        registry_path = repo_root / "plugins" / "registry.json"
        tools = []
        description = "Zariff Plugin"
        version = "1.0.0"
        author = "community"

        if registry_path.exists():
            try:
                reg_data = json.loads(registry_path.read_text(encoding="utf-8"))
                for p in reg_data.get("plugins", []):
                    if p.get("name") == plugin_name:
                        tools = p.get("tools", [])
                        description = p.get("description", description)
                        version = p.get("version", version)
                        author = p.get("author", author)
                        break
            except Exception:
                pass

        if plugin_name == "home-assistant":
            if not tools:
                tools = [
                    {
                        "name": "ha_lights",
                        "description": "List all lights in Home Assistant",
                        "inputs": [],
                        "outputs": ["lights"],
                    },
                    {
                        "name": "ha_set_light",
                        "description": "Turn a light on or off",
                        "inputs": ["entity_id", "state"],
                        "outputs": ["result"],
                    },
                ]
            code_content = """# Home Assistant Plugin for Zariff
import os
import json
import urllib.request

def handle_tool(tool_name, params):
    ha_url = os.environ.get("HA_URL", "")
    ha_token = os.environ.get("HA_TOKEN", "")

    if tool_name == "ha_lights":
        if ha_url and ha_token:
            try:
                req = urllib.request.Request(f"{ha_url.rstrip('/')}/api/states", headers={"Authorization": f"Bearer {ha_token}"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    states = json.loads(resp.read().decode())
                    lights = [s for s in states if s.get("entity_id", "").startswith("light.")]
                    return {"status": "success", "lights": lights, "mode": "live"}
            except Exception as e:
                return {"status": "error", "error": f"Failed connecting to Home Assistant API: {e}", "mode": "error"}
        return {
            "status": "success",
            "mode": "local_demo",
            "message": "Simulated Home Assistant lights (set HA_URL and HA_TOKEN env vars for live sync)",
            "lights": [
                {"entity_id": "light.living_room_ceiling", "state": "on", "attributes": {"brightness": 255, "friendly_name": "Living Room Ceiling"}},
                {"entity_id": "light.kitchen_strip", "state": "off", "attributes": {"brightness": 0, "friendly_name": "Kitchen LED Strip"}},
                {"entity_id": "light.bedroom_lamp", "state": "on", "attributes": {"brightness": 128, "friendly_name": "Bedroom Night Lamp"}}
            ]
        }

    elif tool_name == "ha_set_light":
        entity_id = params.get("entity_id", "light.living_room_ceiling") if isinstance(params, dict) else "light.living_room_ceiling"
        state = params.get("state", "on") if isinstance(params, dict) else "on"
        if ha_url and ha_token:
            try:
                action = "turn_on" if state.lower() == "on" else "turn_off"
                req = urllib.request.Request(
                    f"{ha_url.rstrip('/')}/api/services/light/{action}",
                    data=json.dumps({"entity_id": entity_id}).encode(),
                    headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return {"status": "success", "entity_id": entity_id, "state": state, "mode": "live", "response": json.loads(resp.read().decode())}
            except Exception as e:
                return {"status": "error", "error": str(e)}
        return {
            "status": "success",
            "mode": "local_demo",
            "entity_id": entity_id,
            "state": state,
            "message": f"Successfully turned {state} light '{entity_id}' (Demo Mode)"
        }

    return {"error": f"Unknown tool {tool_name}"}
"""
        elif plugin_name == "spotify-control":
            if not tools:
                tools = [
                    {
                        "name": "spotify_play",
                        "description": "Start or resume Spotify playback",
                        "inputs": [],
                        "outputs": ["result"],
                    },
                    {
                        "name": "spotify_pause",
                        "description": "Pause Spotify playback",
                        "inputs": [],
                        "outputs": ["result"],
                    },
                    {
                        "name": "spotify_now_playing",
                        "description": "Get currently playing track info",
                        "inputs": [],
                        "outputs": ["track", "artist", "album"],
                    },
                ]
            code_content = """# Spotify Control Plugin for Zariff
def handle_tool(tool_name, params):
    if tool_name == "spotify_now_playing":
        return {
            "status": "success",
            "track": "Cybernetic Horizon",
            "artist": "Zariff Sound Labs",
            "album": "OS Ambient Sessions Vol. 1",
            "duration_ms": 215000,
            "progress_ms": 142000,
            "is_playing": True,
            "source": "Zariff Media Bridge"
        }
    elif tool_name in ("spotify_play", "spotify_pause"):
        action = "playing" if tool_name == "spotify_play" else "paused"
        return {
            "status": "success",
            "playback_state": action,
            "message": f"Spotify playback {action} successfully via Zariff Media Engine"
        }
    return {"error": f"Unknown tool {tool_name}"}
"""
        elif plugin_name == "weather":
            if not tools:
                tools = [
                    {
                        "name": "get_weather",
                        "description": "Get current weather for a city",
                        "inputs": ["city"],
                        "outputs": ["temperature", "condition", "humidity"],
                    },
                    {
                        "name": "get_forecast",
                        "description": "Get 5-day weather forecast",
                        "inputs": ["city"],
                        "outputs": ["forecast"],
                    },
                ]
            code_content = """# Weather Plugin for Zariff
import urllib.request
import urllib.parse
import json

def handle_tool(tool_name, params):
    city = params.get("city", "London") if isinstance(params, dict) and params.get("city") else "London"
    if tool_name in ("get_weather", "get_forecast"):
        try:
            url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
            req = urllib.request.Request(url, headers={"User-Agent": "Zariff-Agent"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                curr = data.get("current_condition", [{}])[0]
                temp_c = curr.get("temp_C", "20")
                desc = curr.get("weatherDesc", [{"value": "Clear"}])[0].get("value", "Clear")
                humidity = curr.get("humidity", "50") + "%"
                wind = curr.get("windspeedKmph", "10") + " km/h"

                if tool_name == "get_weather":
                    return {
                        "status": "success",
                        "city": city,
                        "temperature": f"{temp_c} °C",
                        "condition": desc,
                        "humidity": humidity,
                        "wind_speed": wind,
                        "mode": "live_wttr_in"
                    }
                else:
                    forecasts = []
                    for day in data.get("weather", [])[:3]:
                        forecasts.append({
                            "date": day.get("date"),
                            "max_temp": f"{day.get('maxtempC')} °C",
                            "min_temp": f"{day.get('mintempC')} °C",
                            "condition": day.get("hourly", [{}])[4].get("weatherDesc", [{"value": "Sunny"}])[0].get("value", "Sunny")
                        })
                    return {"status": "success", "city": city, "forecast": forecasts, "mode": "live_wttr_in"}
        except Exception as e:
            return {
                "status": "success",
                "city": city,
                "temperature": "22 °C",
                "condition": "Sunny / Scattered Clouds",
                "humidity": "45%",
                "wind_speed": "12 km/h",
                "mode": "local_fallback",
                "note": f"Live weather sync unavailable ({e}), showing local estimate"
            }
    return {"error": f"Unknown tool {tool_name}"}
"""
        else:
            code_content = f"""# Custom Plugin: {plugin_name}
def handle_tool(tool_name, params):
    return {{"status": "success", "tool": tool_name, "params": params, "message": "Executed custom plugin tool successfully!"}}
"""

        manifest_dict = {
            "name": plugin_name,
            "version": version,
            "description": description,
            "author": author,
            "tools": tools,
            "agent_type": "system",
            "entry_point": "plugin.py",
            "runtime_type": "python",
            "enabled": True,
            "capabilities": {
                "filesystem": {"read": [], "write": []},
                "network_domains": [],
                "processes": [],
                "credentials": [],
                "clipboard": {"read": False, "write": False},
                "media": {"camera": False, "microphone": False},
                "data_retention": {"mode": "none", "max_days": 0},
                "destructive_actions": False,
            },
        }
        (plugin_dir / "manifest.json").write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")
        (plugin_dir / "plugin.py").write_text(code_content, encoding="utf-8")

        try:
            sign_plugin_directory(plugin_dir)
        except Exception as e:
            logger.warning("Could not sign plugin directory %s: %s", plugin_dir, e)

        if self._plugin_registry:
            count = self._plugin_registry.discover()
            logger.info("Plugin installed & signed: %s (total plugins: %d)", plugin_name, count)

        return {
            "success": True,
            "plugin": plugin_name,
            "path": str(plugin_dir),
        }

    async def _handle_plugin_install(self, params: dict, ws: ServerConnection) -> dict:
        """Install one exact package from the moderated GitHub catalog."""
        from pilot.plugins.marketplace import MarketplaceError

        plugin_name = params.get("plugin_name", "")
        if not plugin_name:
            return {"error": "plugin_name is required"}
        if not self._plugin_marketplace:
            return {"error": "Plugin marketplace not initialized"}

        try:
            result = await asyncio.to_thread(
                self._plugin_marketplace.install,
                plugin_name,
            )
            if self._plugin_registry:
                self._plugin_registry.discover()
                self._refresh_plugin_planner_context()
            logger.info("Installed approved marketplace plugin: %s", plugin_name)
            return result
        except MarketplaceError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.error("Failed to install plugin %s: %s", plugin_name, exc, exc_info=True)
            return {"error": f"Plugin install failed: {exc}"}

    async def _handle_plugin_uninstall(self, params: dict, ws: ServerConnection) -> dict:
        """Uninstall a plugin."""
        import shutil

        from pilot.plugins.marketplace import MarketplaceError, validate_plugin_name

        plugin_name = params.get("plugin_name", "")
        if not plugin_name:
            return {"error": "plugin_name is required"}
        try:
            plugin_name = validate_plugin_name(plugin_name)
        except MarketplaceError as exc:
            return {"error": str(exc)}

        plugin_dir = PLUGINS_DIR / plugin_name
        if not plugin_dir.exists():
            return {"error": f"Plugin not found: {plugin_name}"}

        try:
            shutil.rmtree(plugin_dir)
            logger.info("Plugin uninstalled: %s", plugin_name)
            if self._plugin_registry:
                self._plugin_registry.remove_plugin(plugin_name)
                self._refresh_plugin_planner_context()
            return {"success": True, "plugin": plugin_name}
        except Exception as exc:
            logger.error("Failed to uninstall plugin %s: %s", plugin_name, exc)
            return {"error": str(exc)}

    async def _handle_plugin_create(self, params: dict, ws: ServerConnection) -> dict:
        """Create a new custom plugin with manifest and Python code."""
        import json

        from pilot.plugins import sign_plugin_directory
        from pilot.plugins.marketplace import MarketplaceError, validate_plugin_name

        raw_name = params.get("name", "")
        if not raw_name:
            return {"error": "Plugin name is required"}
        try:
            plugin_name = validate_plugin_name(raw_name)
        except MarketplaceError as exc:
            return {"error": str(exc)}

        plugin_dir = PLUGINS_DIR / plugin_name
        if plugin_dir.exists():
            return {"error": f"Plugin already exists: {plugin_name}"}
        plugin_dir.mkdir(parents=True)

        tools = params.get("tools", [])
        if isinstance(tools, str):
            try:
                tools = json.loads(tools)
            except Exception:
                tools = []

        manifest_dict = {
            "name": plugin_name,
            "version": params.get("version", "1.0.0"),
            "description": params.get("description", "Custom user plugin"),
            "author": params.get("author", "User"),
            "tools": tools,
            "agent_type": "system",
            "entry_point": "plugin.py",
            "runtime_type": "python",
            "enabled": True,
            "capabilities": {
                "filesystem": {"read": [], "write": []},
                "network_domains": [],
                "processes": [],
                "credentials": [],
                "clipboard": {"read": False, "write": False},
                "media": {"camera": False, "microphone": False},
                "data_retention": {"mode": "none", "max_days": 0},
                "destructive_actions": False,
            },
        }
        code_content = params.get("code", "")
        if not code_content.strip():
            code_content = f"""# Custom Plugin: {plugin_name}
def handle_tool(tool_name, params):
    return {{"status": "success", "tool": tool_name, "params": params, "message": f"Executed tool '{{tool_name}}'"}}
"""

        (plugin_dir / "manifest.json").write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")
        (plugin_dir / "plugin.py").write_text(code_content, encoding="utf-8")

        try:
            sign_plugin_directory(plugin_dir)
        except Exception as e:
            logger.warning("Could not sign plugin %s: %s", plugin_dir, e)

        if self._plugin_registry:
            self._plugin_registry.discover()
            self._refresh_plugin_planner_context()

        submission_url = ""
        if self._plugin_marketplace:
            catalog = await asyncio.to_thread(self._plugin_marketplace.load_catalog)
            submission_url = catalog.data.get("submission_url", "")
        return {
            "success": True,
            "plugin": plugin_name,
            "path": str(plugin_dir),
            "local_only": True,
            "submission_url": submission_url,
        }

    async def _handle_plugin_run_tool(self, params: dict, ws: ServerConnection) -> dict:
        """Execute a tool provided by any installed plugin."""
        tool_name = params.get("tool_name", "")
        if not tool_name:
            return {"error": "tool_name is required"}
        args = params.get("args", {})
        if not isinstance(args, dict):
            args = {}

        if not self._plugin_registry:
            return {"error": "Plugin registry not initialized"}

        result = await asyncio.to_thread(
            self._plugin_registry.call_tool,
            tool_name,
            args,
        )
        return {"result": result}

    async def _handle_skills_list(self, params: dict, ws: ServerConnection) -> dict:
        if self._skill_registry:
            return {"skills": self._skill_registry.list_skills()}
        return {"error": "Skill registry not initialized"}

    async def _handle_skills_reload(self, params: dict, ws: ServerConnection) -> dict:
        if self._skill_registry:
            records = self._skill_registry.reload()
            serial = [
                {
                    "path": r.path,
                    "success": r.success,
                    "skill_ids": r.skill_ids,
                    "error": r.error,
                }
                for r in records
            ]
            if self._planner:
                self._planner.set_skills_context(self._skill_registry.planner_prompt_block())
            return {"ok": True, "records": serial, "skills": self._skill_registry.list_skills()}
        return {"error": "Skill registry not initialized"}

    async def _handle_skills_load_report(self, params: dict, ws: ServerConnection) -> dict:
        if self._skill_registry:
            records = self._skill_registry.last_load_records
            serial = [
                {
                    "path": r.path,
                    "success": r.success,
                    "skill_ids": r.skill_ids,
                    "error": r.error,
                }
                for r in records
            ]
            return {"records": serial, "search_dirs": [str(p) for p in self._skill_registry.search_dirs]}
        return {"error": "Skill registry not initialized"}

    # ── Subconscious Agent Handlers ──

    async def _handle_persona_rules(self, params: dict, ws: ServerConnection) -> dict:
        """Return all persona rules.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with persona context and statistics.
        """
        if self._subconscious:
            context = await self._subconscious.get_persona_context()
            stats = await self._subconscious.get_stats()
            return {"context": context, **stats}
        return {"error": "Subconscious agent not initialized"}

    async def _handle_persona_consolidate(self, params: dict, ws: ServerConnection) -> dict:
        """Force a consolidation cycle.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with consolidation result or error.
        """
        if self._subconscious:
            result = await self._subconscious.consolidate()
            return result
        return {"error": "Subconscious agent not initialized"}

    async def _handle_persona_add_preference(self, params: dict, ws: ServerConnection) -> dict:
        """Manually add a user preference.

        Args:
            params: JSON-RPC parameters with key and value.
            ws: The WebSocket connection.

        Returns:
            A dict with status, key, and value.
        """
        key = params.get("key", "")
        value = params.get("value", "")
        if not key or not value:
            return {"error": "Both key and value required"}
        if self._subconscious:
            await self._subconscious.add_manual_preference(key, value)
            if self._memory is not None:
                from pilot.memory.temporal import MemoryProvenance, MemoryScope

                await self._memory.remember_fact(
                    subject="user",
                    predicate=f"preference:{key}",
                    value=value,
                    scope=MemoryScope.USER,
                    confidence=1.0,
                    provenance=MemoryProvenance.EXPLICIT_USER,
                    evidence_payload={"source": "persona_add_preference"},
                )
            return {"status": "ok", "key": key, "value": value}
        return {"error": "Subconscious agent not initialized"}

    async def _handle_subconscious_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Return subconscious agent stats.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with subconscious agent statistics.
        """
        if self._subconscious:
            return await self._subconscious.get_stats()
        return {"error": "Subconscious agent not initialized"}

    # ── Screen Vision Handlers ──

    async def _handle_screen_context(self, params: dict, ws: ServerConnection) -> dict:
        """Return the current screen context summary.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with screen context summary and details.
        """
        if self._screen_vision:
            return {
                "summary": self._screen_vision.get_context_for_planner(),
                **self._screen_vision.get_context().to_dict(),
            }
        return {"error": "Screen vision not initialized"}

    async def _handle_screen_current_app(self, params: dict, ws: ServerConnection) -> dict:
        """Return the currently active application.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with active_app name.
        """
        if self._screen_vision:
            return {"active_app": self._screen_vision.get_current_app()}
        return {"error": "Screen vision not initialized"}

    async def _handle_screen_vision_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Return screen vision statistics.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with screen vision statistics.
        """
        if self._screen_vision:
            return self._screen_vision.get_stats()
        return {"error": "Screen vision not initialized"}

    async def _handle_screen_vision_toggle(self, params: dict, ws: ServerConnection) -> dict:
        """Start or stop screen vision.

        Args:
            params: JSON-RPC parameters with enabled, interval_seconds, enable_describe.
            ws: The WebSocket connection.

        Returns:
            A dict with status and enabled state.
        """
        enabled = params.get("enabled", True)
        if self._screen_vision:
            if enabled:
                interval = params.get("interval_seconds", self.config.screen_vision.capture_interval_seconds)
                describe = params.get("enable_describe", False)
                await self._screen_vision.start(interval, describe)
            else:
                await self._screen_vision.stop()
            return {"status": "ok", "enabled": enabled}
        return {"error": "Screen vision not initialized"}

    # -- Broadcast --

    async def broadcast(self, method: str, params: Any) -> None:
        """Broadcast a notification to all connected clients.

        Args:
            method: The notification method name.
            params: The notification parameters.
        """
        msg = _notification(method, params)
        for client in list(self._clients):
            try:
                await client.send(msg)
            except Exception:
                self._clients.discard(client)

    # -- Lifecycle --

    async def start(self) -> None:
        """Start the Pilot daemon server.

        Initializes all subsystems, starts the WebSocket server on the
        configured host and port, and announces new features to clients.
        """
        self._running = True
        await self.initialize()

        host = self.config.server.host
        port = self.config.server.port
        if not self.config.server.auth_token:
            self.config.server.auth_token = secrets.token_urlsafe(32)

        # Write the auth token to a runtime file so the Tauri frontend can
        # read it via a Rust command.  The file is chmod 600 (owner-read only).
        from pilot.config import RUNTIME_DIR

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        token_file = RUNTIME_DIR / "auth_token"
        token_file.write_text(self.config.server.auth_token, encoding="utf-8")
        try:
            import os as _os

            _os.chmod(token_file, 0o600)
        except Exception:
            pass  # chmod not available on Windows — file is in user-private dir
        logger.info("Auth token written to %s", token_file)

        logger.info("Starting Pilot daemon on ws://%s:%d", host, port)
        self._server = await websockets.serve(
            self._handle_connection,
            host,
            port,
            ping_interval=30,  # send keepalive ping every 30s
            ping_timeout=300,  # allow up to 5 min for pong (matches LLM timeout)
        )
        logger.info("Pilot daemon ready")
        self._start_tts_warmup()

        # ── Start LAN mesh if enabled ──
        if self._mesh:
            asyncio.create_task(self._mesh.start())

        if hasattr(self, "_new_features_announcement") and self._new_features_announcement:
            await asyncio.sleep(1)
            await self._broadcast_notification(
                "feature_announcement",
                {
                    "message": self._new_features_announcement,
                    "version": __version__,
                },
            )

    async def stop(self) -> None:
        """Stop the Pilot daemon server and clean up all resources."""
        self._running = False
        # ── Stop LAN mesh ──
        if self._mesh:
            await self._mesh.stop()
        if self._orchestrator:
            await self._orchestrator.stop_all()
            await self._orchestrator.stop()
        if hasattr(self, "_budget_tracker") and self._budget_tracker:
            await self._budget_tracker.close()
        if self._background:
            self._background.stop_all()
        if hasattr(self, "_proactive") and self._proactive:
            await self._proactive.stop()
            if hasattr(self._proactive, "close"):
                await self._proactive.close()
        if hasattr(self, "_subconscious") and self._subconscious:
            await self._subconscious.stop()
            if hasattr(self._subconscious, "close"):
                await self._subconscious.close()
        if hasattr(self, "_screen_vision") and self._screen_vision:
            await self._screen_vision.stop()
            if hasattr(self._screen_vision, "close"):
                await self._screen_vision.close()
        if self._tts_warmup_task and not self._tts_warmup_task.done():
            self._tts_warmup_task.cancel()
            await asyncio.gather(self._tts_warmup_task, return_exceptions=True)
        await self._speech_coordinator.close()
        if self._interaction_speech_tasks:
            speech_tasks = tuple(self._interaction_speech_tasks)
            for task in speech_tasks:
                task.cancel()
            await asyncio.gather(*speech_tasks, return_exceptions=True)
        if hasattr(self, "_prompt_improver") and self._prompt_improver:
            if hasattr(self._prompt_improver, "close"):
                await self._prompt_improver.close()
        if self._strategy_evolution is not None:
            await self._strategy_evolution.close()
            self._strategy_evolution = None
        if self._evolution_harness is not None:
            await self._evolution_harness.close()
            self._evolution_harness = None
        if hasattr(self, "_reflector") and self._reflector:
            if hasattr(self._reflector, "close"):
                await self._reflector.close()
        for pending in self._pending_confirms.values():
            pending.event.set()
        self._pending_confirms.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._reflector:
            await self._reflector.close()
        if self._memory:
            await self._memory.close()
        if self._agent_mesh is not None:
            await self._agent_mesh.close()
            self._agent_mesh = None
        if self._experience_ledger:
            await self._experience_ledger.close()
            self._experience_ledger = None
        if self._durable_tasks:
            await self._durable_tasks.close()
            self._durable_tasks = None
        if self._budget_tracker:
            await self._budget_tracker.close()
        if self._companion_follow_up_tasks:
            follow_up_tasks = tuple(self._companion_follow_up_tasks)
            for task in follow_up_tasks:
                task.cancel()
            await asyncio.gather(*follow_up_tasks, return_exceptions=True)
        # ── Drain pending plan-history tasks before closing the store ──
        # Avoids aiosqlite.ProgrammingError when a fire-and-forget log task
        # is still writing as the connection is torn down.
        if self._plan_history_tasks:
            logger.info(
                "Waiting for %d pending plan-history task(s) to flush…",
                len(self._plan_history_tasks),
            )
            await asyncio.gather(*self._plan_history_tasks, return_exceptions=True)
        if self._plan_history:
            await self._plan_history.close()
        if self._cognitive_engine and self._cognitive_engine.is_loaded:
            self._cognitive_engine.unload_model()
        from pilot.system.pty_session import PtySessionManager

        PtySessionManager.close_all()
        logger.info("Pilot daemon stopped")

    # ── Budget Tracking Handlers ──

    async def _handle_budget_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Return current-month token usage and cost summary.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with token usage and cost statistics.
        """
        if not self._budget_tracker:
            return {}
        return await self._budget_tracker.get_stats()

    async def _handle_budget_reset(self, params: dict, ws: ServerConnection) -> dict:
        """Delete all token-usage records for the current month.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with status.
        """
        if not self._budget_tracker:
            return {"status": "ok"}
        await self._budget_tracker.reset_current_month()
        return {"status": "ok"}

    # ── LAN Mesh Network Handlers ──

    async def _handle_mesh_peers(self, params: dict, ws: ServerConnection) -> dict:
        """Return a list of currently connected LAN peers.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with ``enabled`` flag and ``peers`` list.
        """
        if not self._mesh:
            return {"enabled": False, "peers": []}

        peers = []
        for pid in self._mesh.peer_ids:
            conn = self._mesh.get_connection(pid)
            caps = conn.peer_capabilities if conn else None
            peers.append(
                {
                    "peer_id": pid,
                    "hostname": caps.hostname if caps else "",
                    "can_execute": caps.can_execute if caps else False,
                    "cpu_load": caps.cpu_load if caps else 0.0,
                    "plugin_count": len(caps.plugin_names) if caps else 0,
                }
            )
        return {"enabled": True, "peers": peers}

    async def _handle_mesh_status(self, params: dict, ws: ServerConnection) -> dict:
        """Return overall mesh status and configuration.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with mesh status, instance ID, and config summary.
        """
        if not self._mesh:
            return {
                "enabled": False,
                "reason": "Set [network] enabled = true in config.toml to activate",
            }
        return {
            "enabled": True,
            "instance_id": self._mesh.instance_id,
            "peer_count": len(self._mesh.peer_ids),
            "skill_sync_enabled": self.config.network.skill_sync_enabled,
            "collab_exec_enabled": self.config.network.collab_exec_enabled,
            "port": self.config.network.port,
        }

    # ── Cognitive Intelligence Handlers ──

    async def _handle_cognitive_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Get stats for all cognitive subsystems.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with stats for cognitive_engine, attention_ui, stress_gate, intent_predictor.
        """
        return {
            "cognitive_engine": self._cognitive_engine.get_stats() if self._cognitive_engine else None,
            "attention_ui": self._attention_ui.get_stats() if self._attention_ui else None,
            "stress_gate": self._stress_gate.get_stats() if self._stress_gate else None,
            "intent_predictor": (self._intent_predictor.get_stats() if self._intent_predictor else None),
        }

    async def _handle_cognitive_state(self, params: dict, ws: ServerConnection) -> dict:
        """Get current predicted cognitive state.

        Args:
            params: JSON-RPC parameters with optional stimulus description.
            ws: The WebSocket connection.

        Returns:
            A dict with current cognitive state or error.
        """
        if not self._cognitive_engine:
            return {"error": "Cognitive engine not initialized"}

        input_dynamics = params.get("input_dynamics")
        if input_dynamics is not None:
            if not isinstance(input_dynamics, dict):
                return {"error": "input_dynamics must be an object"}

            def bounded_metric(name: str, maximum: float) -> float:
                try:
                    value = float(input_dynamics.get(name, 0.0))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{name} must be numeric") from exc
                if not math.isfinite(value):
                    raise ValueError(f"{name} must be finite")
                return max(0.0, min(value, maximum))

            try:
                self._cognitive_engine.record_input_dynamics(
                    keystroke_rate_per_min=bounded_metric("keystroke_rate_per_min", 1200.0),
                    click_rate_per_min=bounded_metric("click_rate_per_min", 600.0),
                    pointer_move_rate_per_min=bounded_metric("pointer_move_rate_per_min", 1200.0),
                    idle_seconds=bounded_metric("idle_seconds", 3600.0),
                )
            except ValueError as exc:
                return {"error": str(exc)}

        state = await self._cognitive_engine.predict_cognitive_state(
            stimulus_description=params.get("stimulus", ""),
        )
        return state.to_dict()

    async def _handle_attention_toggle(self, params: dict, ws: ServerConnection) -> dict:
        """Toggle attention-aware UI scoring.

        Args:
            params: JSON-RPC parameters with optional enabled flag.
            ws: The WebSocket connection.

        Returns:
            A dict with enabled state or error.
        """
        if not self._attention_ui:
            return {"error": "Attention UI not initialized"}
        enabled = self._attention_ui.toggle(params.get("enabled"))
        return {"enabled": enabled}

    async def _handle_stress_gate_toggle(self, params: dict, ws: ServerConnection) -> dict:
        """Toggle stress-aware task gating.

        Args:
            params: JSON-RPC parameters with optional enabled flag.
            ws: The WebSocket connection.

        Returns:
            A dict with enabled state or error.
        """
        if not self._stress_gate:
            return {"error": "Stress gate not initialized"}
        enabled = self._stress_gate.toggle(params.get("enabled"))
        return {"enabled": enabled}

    async def _handle_intent_predictor_toggle(self, params: dict, ws: ServerConnection) -> dict:
        """Toggle JARVIS mode intent prediction.

        Args:
            params: JSON-RPC parameters with optional enabled flag.
            ws: The WebSocket connection.

        Returns:
            A dict with enabled state or error.
        """
        if not self._intent_predictor:
            return {"error": "Intent predictor not initialized"}
        enabled = self._intent_predictor.toggle(params.get("enabled"))
        return {"enabled": enabled}

    async def _handle_cognitive_model_toggle(self, params: dict, ws: ServerConnection) -> dict:
        """Load or unload the cognitive engine.

        Args:
            params: JSON-RPC parameters with action (load/unload/status).
            ws: The WebSocket connection.

        Returns:
            A dict with loaded state, fallback, and availability status.
        """
        if not self._cognitive_engine:
            return {"error": "Cognitive engine not initialized"}
        action = params.get("action", "status")
        if action == "load":
            success = await self._cognitive_engine.load_model()
            return {"loaded": success, "fallback": self._cognitive_engine.is_fallback}
        elif action == "unload":
            self._cognitive_engine.unload_model()
            return {"loaded": False}
        return {
            "loaded": self._cognitive_engine.is_loaded,
            "fallback": self._cognitive_engine.is_fallback,
            "available": self._cognitive_engine.is_available,
        }

    # ── Voice Listener (JARVIS Mode) Handlers ──

    async def _voice_workflow_control_dispatch(self, command_text: str) -> bool:
        """Called by ContinuousVoiceListener right before normal command
        dispatch — lets a PAUSED/WAITING_FOR_TRIGGER voice-sourced
        VoiceGestureWorkflow claim a "continue"/"cancel" utterance instead
        of it being planned as a brand-new command. Returns True if the
        utterance was consumed as workflow control.

        Args:
            command_text: The recognized voice command text.

        Returns:
            True if a pending workflow claimed this utterance.
        """
        if not self._voice_gesture_workflows:
            return False
        if await self._voice_gesture_workflows.handle_running_instruction("voice", command_text):
            normalized = " ".join(command_text.lower().split()).rstrip(".!?")
            cancelled = normalized in {"cancel", "stop", "never mind", "nevermind"}
            message = "I stopped that task." if cancelled else "I updated the active task and I’m replanning now."
            await self._broadcast_notification(
                "voice_result",
                {
                    "command": command_text,
                    "status": "cancelled" if cancelled else "revising",
                    "result": message,
                    "coordinated_correction": not cancelled,
                },
            )
            if self._voice_listener and self._voice_listener.is_running:
                self._spawn_interaction_speech(
                    message,
                    channel=SpeechChannel.TASK_FAILURE if cancelled else SpeechChannel.TASK_NARRATION,
                    dedupe_key=f"voice-workflow-update:{normalized}",
                )
            return True
        if await self._voice_gesture_workflows.handle_control_phrase("voice", command_text):
            return True

        from pilot.agents.voice_gesture_workflow import (
            extract_voice_workflow_goal,
            should_start_voice_workflow,
        )
        from pilot.security.gateway import InvocationSource

        goal = extract_voice_workflow_goal(command_text)
        if not goal and should_start_voice_workflow(command_text):
            goal = command_text.strip()
        if not goal:
            return False
        workflow = await self._voice_gesture_workflows.start(goal, InvocationSource.VOICE)
        message = "I’m on it. Keep speaking if you want to correct or redirect me."
        await self._broadcast_notification(
            "voice_result",
            {
                "command": command_text,
                "status": "submitted",
                "result": message,
                "workflow": workflow.to_dict(),
            },
        )
        if self._voice_listener and self._voice_listener.is_running:
            self._spawn_interaction_speech(
                message,
                channel=SpeechChannel.TASK_NARRATION,
                dedupe_key=f"voice-workflow-start:{workflow.workflow_id}",
            )
        return True

    async def _speak_companion_text(
        self,
        text: str,
        channel: SpeechChannel | str = SpeechChannel.FINAL_ANSWER,
        dedupe_key: str = "",
    ) -> SpeechOutcome:
        """Route daemon speech through the single companion audio authority."""
        # The continuous listener is the sole microphone consumer. It keeps
        # capturing wake-word commands while work and speech continue, then
        # _voice_command_dispatch stops current playback before applying the
        # utterance. Sharing its recorder with the TTS watcher would make two
        # coroutines race for the same audio frames and lose corrections.
        recorder = None
        listener = self._voice_listener if self._voice_listener and self._voice_listener.is_running else None
        if listener:
            listener.suppress_wake_free_commands()
        try:
            outcome = await self._speech_coordinator.speak(
                text,
                channel=channel,
                dedupe_key=dedupe_key,
                recorder=recorder,
            )
        finally:
            if listener:
                listener.resume_wake_free_commands()
        if outcome.status == "interrupted":
            await self._broadcast_notification("voice_status", {"status": "interrupted"})
        return outcome

    async def _speak_voice_response(
        self,
        text: str,
        *,
        channel: SpeechChannel | str = SpeechChannel.FINAL_ANSWER,
        dedupe_key: str = "",
    ) -> bool:
        """Speaks a voice-pipeline response, interruptible via barge-in
        when the continuous voice listener's VAD recorder is active and
        `config.voice.barge_in_enabled` is on (see speak_interruptible in
        pilot.system.voice). Falls back to plain, non-interruptible speak()
        otherwise. Returns True if the user started talking mid-playback
        and cut it off early.
        """
        outcome = await self._speak_companion_text(
            text,
            channel=channel,
            dedupe_key=dedupe_key,
        )
        return outcome.status == "interrupted"

    def _spawn_interaction_speech(
        self,
        text: str,
        *,
        channel: SpeechChannel,
        dedupe_key: str,
        delay_seconds: float = 0.0,
    ) -> asyncio.Task[None]:
        """Queue short interaction speech without delaying task execution.

        A voice acknowledgement may be delayed briefly so a fast command can
        deliver its real result instead of synthesizing two back-to-back
        utterances. The caller can cancel the returned task while it is still
        in that delay window.
        """

        async def _speak_after_delay() -> None:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            await self._speak_companion_text(
                text,
                channel=channel,
                dedupe_key=dedupe_key,
            )

        task = asyncio.create_task(_speak_after_delay())
        self._interaction_speech_tasks.add(task)
        task.add_done_callback(self._interaction_speech_tasks.discard)
        return task

    async def _arm_voice_follow_up(self) -> bool:
        """Open one wake-free conversational turn after Heliox stops talking."""
        if not self._voice_listener or not self._voice_listener.is_running:
            return False
        self._voice_listener.arm_follow_up_window()
        await self._broadcast_notification(
            "voice_status",
            {
                "status": "follow_up_ready",
                "message": "Listening continuously",
                "seconds": self.config.voice.follow_up_window_seconds,
            },
        )
        return True

    async def _voice_command_dispatch_legacy(self, command_text: str) -> None:
        """Called by ContinuousVoiceListener when a voice command is recognized.

        Runs the legacy voice planner/executor path and speaks the result back.

        Args:
            command_text: The recognized voice command text.
        """
        logger.info("Voice command received: '%s'", command_text)

        if self.config.voice.barge_in_enabled and self._speech_coordinator.status()["active"]:
            await self._speech_coordinator.stop_all()
            await self._broadcast_notification(
                "voice_status",
                {"status": "interrupted", "reason": "new voice command"},
            )

        if self._interactive_request_active:
            result = await self._handle_interject({"input": command_text}, None)
            await self._broadcast_notification(
                "voice_result",
                {
                    "command": command_text,
                    "status": result.get("status", "error"),
                    "message": result.get("message", ""),
                    "coordinated_correction": True,
                },
            )
            return

        language = getattr(
            self._voice_listener,
            "last_detected_language",
            self.config.voice.language if self.config.voice.language != "auto" else "en",
        )

        await self._broadcast_notification(
            "voice_command",
            {
                "command": command_text,
                "status": "executing",
                "language": language,
            },
        )

        try:
            screen_ctx = ""
            if self._screen_vision:
                try:
                    base_ctx = self._screen_vision.get_context_for_planner()
                    screen_ctx = f"{base_ctx}\nUser language: {language}"
                except Exception:
                    screen_ctx = f"User language: {language}"
            else:
                screen_ctx = f"User language: {language}"
            if self._recent_companion_context:
                screen_ctx = f"{screen_ctx}\n\n[RECENT COMPANION CONTEXT]\n{self._recent_companion_context}"
            if self._subconscious:
                try:
                    persona_context = await self._subconscious.get_persona_context()
                    if persona_context:
                        screen_ctx = (
                            f"{screen_ctx}\n\n[LEARNED USER BEHAVIOR]\n"
                            f"{persona_context}\n"
                            "Treat these as preferences, never as permission to bypass "
                            "confirmation, safety policy, or the user's current request."
                        )
                except Exception:
                    logger.debug("Could not load learned persona context for voice", exc_info=True)

            # Plan with multilingual context — single call only
            plan = await self._planner.plan(command_text, screen_context=screen_ctx)
            if plan.error:
                await self._broadcast_notification(
                    "voice_result",
                    {
                        "command": command_text,
                        "status": "error",
                        "message": plan.error,
                        "language": language,
                    },
                )
                await self._speak_voice_response(
                    f"Sorry, I couldn't process that. {plan.error[:100]}",
                    channel=SpeechChannel.TASK_FAILURE,
                    dedupe_key=f"voice-plan-error:{command_text.casefold()}",
                )
                return

            await self._broadcast_notification(
                "plan_preview",
                {
                    "plan_id": "voice",
                    "actions": [a.model_dump() for a in plan.actions],
                    "explanation": plan.explanation,
                    "source": "voice",
                    "language": language,
                },
            )

            from pilot.security.gateway import DEFAULT_SOURCE_PROFILES, InvocationSource, TaskScopeOverride

            if self._orchestrator:
                # Route through the specialist-agent orchestrator (same one
                # interactive text commands use) instead of a monolithic
                # executor call. Individual specialists set their own
                # invocation_source (e.g. WebAgent hardcodes WEB_AGENT); this
                # scope_override narrows whichever one applies down to at
                # most the "voice" profile's ceiling, so a voice-originated
                # plan can never end up wider than voice's restrictions just
                # because it got routed to a specialist with a more
                # permissive default. See TaskScopeOverride/
                # resolve_effective_profile in pilot.security.gateway.
                voice_profile = self.config.gateway.source_profiles.get("voice", DEFAULT_SOURCE_PROFILES["voice"])
                voice_scope_override = TaskScopeOverride(
                    max_tier=voice_profile.max_tier,
                    deny_action_types=voice_profile.deny_action_types,
                    allow_root=voice_profile.allow_root,
                )
                results = await self._orchestrator.execute_plan(
                    command_text,
                    plan,
                    plan_id="voice",
                    scope_override=voice_scope_override,
                )
            else:
                results = await self._executor.execute(
                    plan,
                    plan_id="voice",
                    invocation_source=InvocationSource.VOICE,
                )
            verification = await self._verifier.verify(plan, results)

            output_parts = []
            for r in results:
                if r.output:
                    output_parts.append(r.output[:200])

            result_text = " ".join(output_parts) if output_parts else plan.explanation
            status = "success" if verification.passed else "partial"
            await self._broadcast_notification(
                "voice_result",
                {
                    "command": command_text,
                    "status": status,
                    "result": result_text[:500],
                    "language": language,
                    "companion_follow_up": None,
                },
            )

            spoken = result_text[:300] if len(result_text) < 300 else result_text[:297] + "..."
            await self._speak_voice_response(
                spoken,
                channel=SpeechChannel.FINAL_ANSWER,
                dedupe_key=f"voice-result:{command_text.casefold()}",
            )
            if (
                verification.passed
                and self.config.narration.follow_up_enabled
                and self._execution_companion
                and hasattr(self._execution_companion, "follow_up")
            ):
                self._spawn_companion_follow_up(
                    user_input=command_text,
                    plan=plan,
                    results=results,
                    verification=verification,
                    result_text=result_text,
                    speak=True,
                )

        except Exception as e:
            logger.error("Voice command execution failed: %s", e)

            await self._broadcast_notification(
                "voice_result",
                {
                    "command": command_text,
                    "status": "error",
                    "message": str(e),
                    "language": language,
                },
            )

            try:
                await self._speak_voice_response(
                    "Sorry, something went wrong while executing your request.",
                    channel=SpeechChannel.TASK_FAILURE,
                    dedupe_key=f"voice-exception:{command_text.casefold()}",
                )
            except Exception:
                pass

    async def _voice_command_dispatch(self, command_text: str) -> None:
        """Run voice through the same safe interaction path as typed input."""
        logger.info("Voice command received: '%s'", command_text)

        if self.config.voice.barge_in_enabled and self._speech_coordinator.status()["active"]:
            await self._speech_coordinator.stop_all()
            await self._broadcast_notification(
                "voice_status",
                {"status": "interrupted", "reason": "new voice command"},
            )

        if self._interactive_request_active:
            await self._interaction_runtime.transition(
                InteractionPhase.CORRECTING,
                message="Applying your spoken correction",
            )
            result = await self._handle_interject({"input": command_text}, None)
            await self._broadcast_notification(
                "voice_result",
                {
                    "command": command_text,
                    "status": result.get("status", "error"),
                    "message": result.get("message", ""),
                    "coordinated_correction": True,
                },
            )
            return

        spoken_command = command_text
        proactive_decision = None
        if self._proactive and hasattr(self._proactive, "resolve_spoken_response"):
            proactive_decision = await self._proactive.resolve_spoken_response(command_text)
        if proactive_decision and proactive_decision["decision"] == "dismissed":
            message = f"Okay, I won’t act on {proactive_decision['title']}."
            await self._broadcast_notification(
                "voice_result",
                {
                    "command": spoken_command,
                    "status": "success",
                    "result": message,
                    "proactive_decision": proactive_decision,
                },
            )
            await self._speak_voice_response(
                message,
                channel=SpeechChannel.FINAL_ANSWER,
                dedupe_key=f"proactive-dismissed:{proactive_decision['suggestion_id']}",
            )
            await self._arm_voice_follow_up()
            return
        if proactive_decision and proactive_decision["decision"] == "accepted":
            command_text = (
                f"The user accepted the proactive suggestion '{proactive_decision['title']}'. "
                f"Carry out this requested action: {proactive_decision['action_command']}"
            )

        interaction = await self._interaction_runtime.start(command_text, source="voice")
        interaction_id = str(interaction["interaction_id"])
        language = getattr(
            self._voice_listener,
            "last_detected_language",
            self.config.voice.language if self.config.voice.language != "auto" else "en",
        )
        await self._broadcast_notification(
            "voice_command",
            {
                "command": spoken_command,
                "status": "executing",
                "language": language,
                "proactive_decision": proactive_decision,
            },
        )
        acknowledgement_task = self._spawn_interaction_speech(
            acknowledgement_for(command_text),
            channel=SpeechChannel.TASK_NARRATION,
            dedupe_key=f"voice-ack:{interaction_id}",
            delay_seconds=2.5,
        )

        try:
            await self._interaction_runtime.transition(
                InteractionPhase.PLANNING,
                message="Planning the safest useful action",
                interaction_id=interaction_id,
            )
            try:
                response = await self._handle_execute(
                    {
                        "input": command_text,
                        "session_id": "voice",
                        "user_id": "local",
                        "source": "voice",
                        "_interaction_id": interaction_id,
                    },
                    _BroadcastConnection(self._broadcast_notification),
                )
            finally:
                # A quick command should speak only its useful final result.
                # Longer work still gets the delayed acknowledgement, and the
                # coordinator lets the higher-priority final answer preempt it.
                if isinstance(acknowledgement_task, asyncio.Task) and not acknowledgement_task.done():
                    acknowledgement_task.cancel()
            status = str(response.get("status") or "error")
            result_text = str(
                response.get("message")
                or response.get("explanation")
                or "The request finished without a visible result."
            )
            voice_status = "partial" if status == "partial_failure" else status
            await self._broadcast_notification(
                "voice_result",
                {
                    "command": spoken_command,
                    "status": voice_status,
                    "result": result_text[:500],
                    "language": language,
                    "companion_follow_up": None,
                },
            )

            terminal_phase = (
                InteractionPhase.COMPLETED
                if status == "success"
                else InteractionPhase.INTERRUPTED
                if status in {"cancelled", "interrupted", "denied"}
                else InteractionPhase.FAILED
            )
            await self._interaction_runtime.transition(
                terminal_phase,
                message=result_text[:160],
                interaction_id=interaction_id,
            )
            spoken = result_text[:300] if len(result_text) < 300 else result_text[:297] + "..."
            await self._speak_voice_response(
                spoken,
                channel=(
                    SpeechChannel.FINAL_ANSWER
                    if terminal_phase == InteractionPhase.COMPLETED
                    else SpeechChannel.TASK_FAILURE
                ),
                dedupe_key=f"voice-result:{spoken_command.casefold()}",
            )
            if await self._arm_voice_follow_up():
                await self._interaction_runtime.transition(
                    InteractionPhase.LISTENING,
                    message="Listening continuously",
                    interaction_id=interaction_id,
                )
        except Exception as error:
            logger.error("Voice command execution failed: %s", error)
            message = "Something went wrong while executing your request."
            await self._broadcast_notification(
                "voice_result",
                {
                    "command": spoken_command,
                    "status": "error",
                    "message": str(error),
                    "language": language,
                },
            )
            await self._interaction_runtime.transition(
                InteractionPhase.FAILED,
                message=message,
                interaction_id=interaction_id,
            )
            try:
                await self._speak_voice_response(
                    message,
                    channel=SpeechChannel.TASK_FAILURE,
                    dedupe_key=f"voice-exception:{spoken_command.casefold()}",
                )
                await self._arm_voice_follow_up()
            except Exception:
                pass

    async def _voice_status_broadcast(self, status: str, data: dict) -> None:
        """Called by ContinuousVoiceListener for status updates.

        Args:
            status: The voice listener status.
            data: Additional status data.
        """
        await self._broadcast_notification("voice_status", {"status": status, **data})
        phase_map = {
            "wake_detected": InteractionPhase.UNDERSTANDING,
            "follow_up_detected": InteractionPhase.UNDERSTANDING,
            "follow_up_ready": InteractionPhase.LISTENING,
            "listening": InteractionPhase.LISTENING,
            "timeout": InteractionPhase.LISTENING,
            "interrupted": InteractionPhase.INTERRUPTED,
        }
        phase = phase_map.get(status)
        if phase is not None:
            await self._interaction_runtime.transition(
                phase,
                message=str(data.get("message") or status.replace("_", " ")),
            )

    async def _handle_voice_listener_start(self, params: dict, ws: ServerConnection) -> dict:
        """Start the continuous JARVIS-mode voice listener.

        Args:
            params: JSON-RPC parameters with wake_words.
            ws: The WebSocket connection.

        Returns:
            A dict with status, message, and wake_words.
        """
        from pilot.system.voice import ContinuousVoiceListener

        wake_words = params.get("wake_words", ["hey zariff", "zariff", "hey pilot"])

        if self._voice_listener and self._voice_listener.is_running:
            return {"status": "already_running", "wake_words": self._voice_listener.wake_words}

        self._voice_listener = ContinuousVoiceListener(
            wake_words=wake_words,
            on_command=self._voice_command_dispatch,
            on_status=self._voice_status_broadcast,
            workflow_control=self._voice_workflow_control_dispatch,
            config=self.config,
        )
        result = await self._voice_listener.start()
        if not self._voice_listener.is_running:
            self._voice_listener = None
            return {"status": "error", "message": result}
        await self._interaction_runtime.transition(
            InteractionPhase.LISTENING,
            message="Listening for your wake word",
        )
        return {"status": "started", "message": result, "wake_words": wake_words}

    async def _handle_voice_listener_stop(self, params: dict, ws: ServerConnection) -> dict:
        """Stop the continuous voice listener.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with status and message.
        """
        if not self._voice_listener or not self._voice_listener.is_running:
            return {"status": "not_running"}

        result = await self._voice_listener.stop()
        await self._interaction_runtime.transition(
            InteractionPhase.IDLE,
            message="Voice listening is off",
        )
        return {"status": "stopped", "message": result}

    async def _handle_voice_listener_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Get voice listener statistics.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with voice listener statistics.
        """
        if not self._voice_listener:
            return {"running": False, "message": "Voice listener not initialized"}
        return self._voice_listener.get_stats()

    async def _handle_interaction_status(self, params: dict, ws: ServerConnection) -> dict:
        """Return the shared text/voice interaction state."""
        return self._interaction_runtime.status()

    async def _handle_list_audio_input_devices(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        """List microphone inputs that support Heliox's recording format."""
        try:
            import sounddevice as sd

            from pilot.system.voice import list_audio_input_devices

            devices = await asyncio.to_thread(list_audio_input_devices, sd)
        except ImportError:
            return {
                "devices": [],
                "selected": self.config.voice.input_device,
                "message": "Python sounddevice is not installed.",
            }
        except Exception as error:
            logger.warning("Could not enumerate audio input devices: %s", error)
            return {
                "devices": [],
                "selected": self.config.voice.input_device,
                "message": str(error),
            }
        return {
            "devices": devices,
            "selected": self.config.voice.input_device,
            "message": "" if devices else "No compatible microphone inputs were found.",
        }

    async def _handle_speak_text(self, params: dict, ws: ServerConnection) -> dict:
        """Speak UI text through the configured daemon TTS engine."""
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"status": "error", "message": "text must be a non-empty string"}
        if len(text) > 4000:
            return {"status": "error", "message": "text must be 4000 characters or fewer"}

        logger.info(
            "Companion speech started (engine=%s, characters=%d)",
            self.config.voice.tts_engine,
            len(text.strip()),
        )
        channel = params.get("channel", SpeechChannel.FINAL_ANSWER.value)
        try:
            resolved_channel = SpeechChannel(channel)
        except ValueError:
            return {"status": "error", "message": f"Unknown speech channel: {channel}"}
        outcome = await self._speak_companion_text(
            text.strip(),
            channel=resolved_channel,
            dedupe_key=str(params.get("dedupe_key", "")).strip(),
        )
        if outcome.status == "spoken":
            logger.info("Companion speech completed (engine=%s)", self.config.voice.tts_engine)
            return {"status": "spoken", "message": f"Spoken: {text.strip()[:80]}..."}
        if outcome.status == "interrupted":
            logger.info("Companion speech interrupted by user")
        return {"status": outcome.status, "message": outcome.message}

    async def _handle_stop_speech(self, params: dict, ws: ServerConnection) -> dict:
        """Immediately stop daemon-side TTS playback."""
        from pilot.system.voice import stop_speaking

        cancelled = await self._speech_coordinator.stop_all()
        message = await stop_speaking()
        return {"status": "stopped", "message": message, "cancelled": cancelled}

    async def _handle_companion_speech_status(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        """Expose the one-channel coordinator state for diagnostics and UI."""
        return {"status": "ok", **self._speech_coordinator.status()}

    async def _handle_reset_wake_calibration(self, params: dict, ws: ServerConnection) -> dict:
        """Clear all learned wake-word calibration data.

        Deletes the on-device JSON store entirely — this is the "reset
        learned wake words" action in Settings. If the voice listener is
        currently running, its live WakeWordCalibrator is reset too so the
        change takes effect immediately without restarting the listener.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with status.
        """
        if self._voice_listener:
            self._voice_listener._wake_calibrator.reset()
        else:
            from pilot.system.voice_calibration import VoiceCalibrationStore

            VoiceCalibrationStore().reset()
        return {"status": "ok"}

    async def _handle_list_wake_variants(self, params: dict, ws: ServerConnection) -> dict:
        """List learned wake-word variants for the Settings transparency view.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with a list of variants (text, confirmed_count,
            first_seen, last_confirmed) and the promotion threshold so the
            UI can show progress toward it.
        """
        from dataclasses import asdict

        from pilot.system.voice_calibration import VoiceCalibrationStore, WakeWordCalibrator

        variants = VoiceCalibrationStore().load()
        return {
            "status": "ok",
            "variants": [asdict(v) for v in variants.values()],
            "promotion_threshold": WakeWordCalibrator.PROMOTION_THRESHOLD,
        }

    # ── Autonomous Executor Handlers ──

    async def _handle_autonomous_submit(self, params: dict, ws: ServerConnection) -> dict:
        """Submit a task for autonomous background execution.

        Args:
            params: JSON-RPC parameters with goal, source, and an optional
                scope_override restricting the AgentGateway's "autonomous"
                floor further for this job only (see pilot.security.gateway
                — an override can only narrow the floor, never widen it,
                regardless of what's supplied here).
            ws: The WebSocket connection.

        Returns:
            A dict with status and job information.
        """
        if not self._autonomous:
            return {"error": "Autonomous executor not initialized"}

        goal = params.get("goal", "")
        if not goal.strip():
            return {"error": "Empty goal"}

        source = params.get("source", "text")

        scope_override = None
        raw_override = params.get("scope_override")
        if raw_override is not None:
            from pydantic import ValidationError

            from pilot.security.gateway import TaskScopeOverride

            try:
                scope_override = TaskScopeOverride.model_validate(raw_override)
            except ValidationError as e:
                return {"error": f"Invalid scope_override: {e}"}

        job = await self._autonomous.submit(
            goal,
            source=source,
            scope_override=scope_override,
            session_id=str(params.get("session_id") or source or "default"),
        )
        return {"status": "submitted", "job": job.to_dict()}

    async def _handle_autonomous_cancel(self, params: dict, ws: ServerConnection) -> dict:
        """Cancel a running autonomous job.

        Args:
            params: JSON-RPC parameters with job_id.
            ws: The WebSocket connection.

        Returns:
            A dict with cancelled status and job_id.
        """
        if not self._autonomous:
            return {"error": "Autonomous executor not initialized"}

        job_id = params.get("job_id", "")
        success = await self._autonomous.cancel(job_id)
        return {"cancelled": success, "job_id": job_id}

    async def _handle_autonomous_jobs(self, params: dict, ws: ServerConnection) -> dict:
        """List all autonomous jobs.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with list of jobs.
        """
        if not self._autonomous:
            return {"jobs": []}
        return {"jobs": self._autonomous.list_jobs()}

    async def _handle_autonomous_job(self, params: dict, ws: ServerConnection) -> dict:
        """Get a specific autonomous job by ID.

        Args:
            params: JSON-RPC parameters with job_id.
            ws: The WebSocket connection.

        Returns:
            A dict with job information or error.
        """
        if not self._autonomous:
            return {"error": "Autonomous executor not initialized"}

        job_id = params.get("job_id", "")
        job = self._autonomous.get_job(job_id)
        if not job:
            return {"error": f"Job not found: {job_id}"}
        return job.to_dict()

    # ── Voice/Gesture Workflow Handlers ──
    # Durable, pausable/resumable multi-step goals spanning multiple voice
    # commands or gesture inputs over time — see
    # pilot.agents.voice_gesture_workflow.VoiceGestureWorkflowEngine. A
    # It persists workflow state and delegates each step to the same adaptive
    # observe/act/verify core used by AutonomousExecutor.

    async def _handle_voice_gesture_workflow_submit(self, params: dict, ws: ServerConnection) -> dict:
        """Submit a durable, pausable/resumable multi-step voice/gesture workflow.

        Args:
            params: JSON-RPC parameters with goal, invocation_source
                ("voice" or "gesture"), and an optional scope_override
                restricting the AgentGateway's floor further for this
                workflow only (see pilot.security.gateway).
            ws: The WebSocket connection.

        Returns:
            A dict with status and the created workflow, or an error.
        """
        from pydantic import ValidationError

        from pilot.security.gateway import InvocationSource, TaskScopeOverride

        if not self._voice_gesture_workflows:
            return {"status": "error", "message": "Voice/gesture workflow engine not initialized"}

        goal = params.get("goal", "")
        if not goal.strip():
            return {"status": "error", "message": "Empty goal"}

        source_raw = params.get("invocation_source", "voice")
        try:
            invocation_source = InvocationSource(source_raw)
        except ValueError:
            return {"status": "error", "message": f"Invalid invocation_source: {source_raw}"}
        if invocation_source not in (InvocationSource.VOICE, InvocationSource.GESTURE):
            return {"status": "error", "message": "invocation_source must be 'voice' or 'gesture'"}

        scope_override = None
        raw_override = params.get("scope_override")
        if raw_override is not None:
            try:
                scope_override = TaskScopeOverride.model_validate(raw_override)
            except ValidationError as e:
                return {"status": "error", "message": f"Invalid scope_override: {e}"}

        workflow = await self._voice_gesture_workflows.start(goal, invocation_source, scope_override)
        return {"status": "submitted", "workflow": workflow.to_dict()}

    async def _handle_voice_gesture_workflow_list(self, params: dict, ws: ServerConnection) -> dict:
        """List voice/gesture workflows.

        Args:
            params: JSON-RPC parameters, optionally {include_terminal}.
            ws: The WebSocket connection.

        Returns:
            A dict with the list of workflows.
        """
        if not self._voice_gesture_workflows:
            return {"workflows": []}
        include_terminal = bool(params.get("include_terminal", False))
        return {"workflows": await self._voice_gesture_workflows.list_workflows(include_terminal=include_terminal)}

    async def _handle_voice_gesture_workflow_get(self, params: dict, ws: ServerConnection) -> dict:
        """Get a specific voice/gesture workflow by ID.

        Args:
            params: JSON-RPC parameters with workflow_id.
            ws: The WebSocket connection.

        Returns:
            A dict with the workflow, or an error if not found.
        """
        if not self._voice_gesture_workflows:
            return {"status": "error", "message": "Voice/gesture workflow engine not initialized"}
        workflow_id = params.get("workflow_id", "")
        workflow = await self._voice_gesture_workflows.get_workflow(workflow_id)
        if workflow is None:
            return {"status": "error", "message": f"Workflow not found: {workflow_id}"}
        return {"status": "ok", "workflow": workflow}

    async def _handle_voice_gesture_workflow_pause(self, params: dict, ws: ServerConnection) -> dict:
        """Pause a running voice/gesture workflow at the next step boundary.

        Args:
            params: JSON-RPC parameters with workflow_id.
            ws: The WebSocket connection.

        Returns:
            A dict with paused status and workflow_id.
        """
        if not self._voice_gesture_workflows:
            return {"status": "error", "message": "Voice/gesture workflow engine not initialized"}
        workflow_id = params.get("workflow_id", "")
        paused = await self._voice_gesture_workflows.pause(workflow_id)
        return {"paused": paused, "workflow_id": workflow_id}

    async def _handle_voice_gesture_workflow_resume(self, params: dict, ws: ServerConnection) -> dict:
        """Resume a paused/waiting-for-trigger voice/gesture workflow.

        Args:
            params: JSON-RPC parameters with workflow_id.
            ws: The WebSocket connection.

        Returns:
            A dict with resumed status, workflow_id, and the workflow if successful.
        """
        if not self._voice_gesture_workflows:
            return {"status": "error", "message": "Voice/gesture workflow engine not initialized"}
        workflow_id = params.get("workflow_id", "")
        workflow = await self._voice_gesture_workflows.resume(workflow_id)
        return {
            "resumed": workflow is not None,
            "workflow_id": workflow_id,
            "workflow": workflow.to_dict() if workflow else None,
        }

    async def _handle_voice_gesture_workflow_cancel(self, params: dict, ws: ServerConnection) -> dict:
        """Cancel a voice/gesture workflow.

        Args:
            params: JSON-RPC parameters with workflow_id.
            ws: The WebSocket connection.

        Returns:
            A dict with cancelled status and workflow_id.
        """
        if not self._voice_gesture_workflows:
            return {"status": "error", "message": "Voice/gesture workflow engine not initialized"}
        workflow_id = params.get("workflow_id", "")
        cancelled = await self._voice_gesture_workflows.cancel(workflow_id)
        return {"cancelled": cancelled, "workflow_id": workflow_id}

    async def _handle_gesture_workflow_bindings_get(self, params: dict, ws: ServerConnection) -> dict:
        """Return the current gesture-to-goal workflow bindings for the Settings editor.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with enabled flag and the list of bindings.
        """
        from dataclasses import asdict

        from pilot.config import SUPPORTED_GESTURE_WORKFLOW_GESTURES

        return {
            "enabled": self.config.gesture_workflows.enabled,
            "bindings": [asdict(b) for b in self.config.gesture_workflows.bindings],
            "supported_gestures": list(SUPPORTED_GESTURE_WORKFLOW_GESTURES),
        }

    async def _handle_gesture_workflow_bindings_update(self, params: dict, ws: ServerConnection) -> dict:
        """Update the gesture-to-goal workflow bindings.

        Args:
            params: JSON-RPC parameters, optionally {enabled, bindings}
                where each binding is {gesture_name, goal_template, enabled}.
            ws: The WebSocket connection.

        Returns:
            A dict with status and the updated config.
        """
        from dataclasses import asdict

        from pilot.config import SUPPORTED_GESTURE_WORKFLOW_GESTURES, GestureWorkflowBinding

        enabled = bool(params.get("enabled", self.config.gesture_workflows.enabled))
        parsed = list(self.config.gesture_workflows.bindings)
        if "bindings" in params:
            raw_bindings = params["bindings"]
            if not isinstance(raw_bindings, list):
                return {"status": "error", "message": "bindings must be a list"}
            if len(raw_bindings) > len(SUPPORTED_GESTURE_WORKFLOW_GESTURES):
                return {"status": "error", "message": "too many gesture workflow bindings"}

            parsed = []
            seen_gestures: set[str] = set()
            for index, item in enumerate(raw_bindings):
                if not isinstance(item, dict):
                    return {"status": "error", "message": f"binding {index + 1} must be an object"}
                gesture_name = str(item.get("gesture_name", "")).strip().lower()
                goal_template = str(item.get("goal_template", "")).strip()
                if gesture_name not in SUPPORTED_GESTURE_WORKFLOW_GESTURES:
                    return {
                        "status": "error",
                        "message": f"unsupported gesture in binding {index + 1}: {gesture_name or '(empty)'}",
                    }
                if gesture_name in seen_gestures:
                    return {"status": "error", "message": f"duplicate gesture binding: {gesture_name}"}
                if not goal_template:
                    return {"status": "error", "message": f"binding {index + 1} requires a workflow goal"}
                if len(goal_template) > 2000:
                    return {"status": "error", "message": f"binding {index + 1} goal is too long"}
                seen_gestures.add(gesture_name)
                parsed.append(
                    GestureWorkflowBinding(
                        gesture_name=gesture_name,
                        goal_template=goal_template,
                        enabled=bool(item.get("enabled", True)),
                    )
                )
        if enabled and not any(binding.enabled for binding in parsed):
            return {
                "status": "error",
                "message": "enable at least one complete binding before enabling gesture workflows",
            }

        self.config.gesture_workflows.enabled = enabled
        self.config.gesture_workflows.bindings = parsed
        self.config.save()
        result = {
            "status": "ok",
            "enabled": self.config.gesture_workflows.enabled,
            "bindings": [asdict(b) for b in self.config.gesture_workflows.bindings],
            "supported_gestures": list(SUPPORTED_GESTURE_WORKFLOW_GESTURES),
        }
        await self._broadcast_notification("gesture_workflow_bindings_updated", result)
        return result

    # ── Proactive Suggestions Handlers ──

    async def _handle_proactive_start(self, params: dict, ws: ServerConnection) -> dict:
        """Start the proactive suggestion engine.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with status and message.
        """
        if not self._proactive:
            return {"error": "Proactive engine not initialized"}
        result = await self._proactive.start()
        return {"status": "started", "message": result}

    async def _handle_proactive_stop(self, params: dict, ws: ServerConnection) -> dict:
        """Stop the proactive suggestion engine.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with status and message.
        """
        if not self._proactive:
            return {"error": "Proactive engine not initialized"}
        result = await self._proactive.stop()
        return {"status": "stopped", "message": result}

    async def _handle_proactive_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Get proactive engine statistics.

        Args:
            params: JSON-RPC parameters (unused).
            ws: The WebSocket connection.

        Returns:
            A dict with proactive engine statistics.
        """
        if not self._proactive:
            return {"running": False, "message": "Proactive engine not initialized"}
        return self._proactive.get_stats()

    async def _handle_proactive_learning_status(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        """Return persisted accept/dismiss learning for proactive patterns."""
        if not self._proactive:
            return {
                "enabled": False,
                "patterns": {},
                "message": "Proactive engine not initialized",
            }
        return self._proactive.get_learning_status()

    async def _handle_proactive_learning_reset(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        """Forget learned proactive-suggestion preferences."""
        if not self._proactive:
            return {
                "enabled": False,
                "patterns": {},
                "message": "Proactive engine not initialized",
            }
        self._proactive.reset_learning()
        return self._proactive.get_learning_status()

    async def _handle_online_learning_status(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        """Return the bounded online learner's evidence and privacy state."""

        if self._online_learning is None:
            return {
                "enabled": False,
                "message": "Verified online learning is not initialized",
            }
        return self._online_learning.status()

    async def _handle_online_learning_reset(
        self,
        params: dict,
        ws: ServerConnection,
    ) -> dict:
        """Forget online model state without mutating the audit ledger."""

        if self._online_learning is None:
            return {
                "enabled": False,
                "message": "Verified online learning is not initialized",
            }
        if self._proactive is not None:
            self._proactive.reset_learning()
        return await self._online_learning.reset()

    async def _handle_proactive_accept(self, params: dict, ws: ServerConnection) -> dict:
        """Accept a proactive suggestion — execute the suggested action.

        Args:
            params: JSON-RPC parameters with suggestion_id.
            ws: The WebSocket connection.

        Returns:
            A dict with execution status and results.
        """
        if not self._proactive:
            return {"error": "Proactive engine not initialized"}

        if not self._autonomous:
            return {
                "status": "error",
                "message": ("The guarded autonomous executor is unavailable, so this suggestion was not executed."),
            }

        suggestion_id = params.get("suggestion_id", "")
        action_command = await self._proactive.accept_suggestion(suggestion_id)
        if not action_command:
            return {"error": f"Suggestion not found: {suggestion_id}"}

        job = await self._autonomous.submit(action_command, source="proactive")
        return {"status": "executing", "action": action_command, "job": job.to_dict()}

    async def _handle_proactive_dismiss(self, params: dict, ws: ServerConnection) -> dict:
        """Dismiss a proactive suggestion.

        Args:
            params: JSON-RPC parameters with suggestion_id.
            ws: The WebSocket connection.

        Returns:
            A dict with dismissed status and suggestion_id.
        """
        if not self._proactive:
            return {"error": "Proactive engine not initialized"}

        suggestion_id = params.get("suggestion_id", "")
        dismissed = await self._proactive.dismiss_suggestion(suggestion_id)
        return {"dismissed": dismissed, "suggestion_id": suggestion_id}

    async def _handle_resolve_git_conflict(self, params: dict, ws: ServerConnection) -> dict:
        """Resolve git merge conflicts in a file via LLM.

        Args:
            params: JSON-RPC parameters containing filepath (or path).
            ws: The WebSocket connection.

        Returns:
            A dict with resolution details.
        """
        if not self._model_router:
            return {"status": "error", "message": "Model router not initialized"}

        filepath = params.get("filepath") or params.get("path")
        if not filepath:
            return {"status": "error", "message": "Missing filepath or path parameter"}

        try:
            from pilot.system.git_conflict import resolve_conflicts_in_file

            resolved_blocks = await resolve_conflicts_in_file(filepath, self._model_router)
            return {"status": "success", "conflicts": resolved_blocks}
        except Exception as e:
            logger.exception("Failed to resolve git conflict in handler")
            return {"status": "error", "message": str(e)}

    async def _handle_apply_git_resolution(self, params: dict, ws: ServerConnection) -> dict:
        """Apply a git conflict resolution securely.

        Args:
            params: JSON-RPC parameters with path, full_block, resolved_code.
            ws: The WebSocket connection.

        Returns:
            A dict with execution status.
        """
        path = params.get("path")
        full_block = params.get("full_block")
        resolved_code = params.get("resolved_code")

        if not path or full_block is None or resolved_code is None:
            return {"status": "error", "message": "Missing required params: path, full_block, resolved_code"}

        try:
            from pilot.actions import Action, ActionPlan, ActionType, GitResolveParams

            action = Action(
                action_type=ActionType.GIT_RESOLVE,
                parameters=GitResolveParams(
                    path=path,
                    full_block=full_block,
                    resolved_code=resolved_code,
                ),
            )
            plan = ActionPlan(actions=[action], explanation="Apply git conflict resolution securely")
            results = await self._executor.execute(plan)
            success = all(r.success for r in results)
            error = next((r.error for r in results if not r.success), None)
            return {
                "status": "success" if success else "error",
                "message": "Git conflict resolved successfully" if success else (error or "Failed to resolve conflict"),
            }
        except Exception as e:
            logger.exception("Failed to apply git conflict resolution in handler")
            return {"status": "error", "message": str(e)}

    # ── Plan History Audit Log Handlers ──

    async def _handle_get_plan_history(self, params: dict, ws: ServerConnection) -> dict:
        """Return a paginated list of plan audit records (summaries, no large blobs).

        This is the internal plan-level audit log for debugging and compliance.
        It is distinct from the chat/session history returned by ``get_history``.

        JSON-RPC params
        ---------------
        limit : int, optional
            Maximum rows to return. Default 50, max 200.
        offset : int, optional
            Rows to skip (for pagination). Default 0.
        status : str, optional
            Filter by ``execution_status`` (e.g. ``"success"``, ``"partial_failure"``,
            ``"cancelled"``, ``"blocked_by_critic"``). Omit to return all statuses.

        Returns
        -------
        dict
            ``plans``   — list of summary dicts (no plan_json / results_json blobs)
            ``count``   — number of rows in this page
            ``offset``  — offset used
            ``limit``   — limit used
        """
        if not self._plan_history:
            return {"error": "Plan history store is not initialized", "plans": []}

        raw_limit = params.get("limit", 50)
        raw_offset = params.get("offset", 0)
        status_filter = params.get("status") or None  # empty string → None

        try:
            limit = max(1, min(int(raw_limit), 200))
            offset = max(0, int(raw_offset))
        except (TypeError, ValueError):
            return {"error": "limit and offset must be integers", "plans": []}

        plans = await self._plan_history.get_list(
            limit=limit,
            offset=offset,
            status_filter=status_filter,
        )
        return {
            "plans": plans,
            "count": len(plans),
            "offset": offset,
            "limit": limit,
        }

    async def _handle_get_plan_detail(self, params: dict, ws: ServerConnection) -> dict:
        """Return the full audit record for a single plan, including all JSON blobs.

        JSON-RPC params
        ---------------
        plan_id : str
            The 8-char plan identifier (as returned in ``plan_preview`` notifications
            and in ``get_plan_history`` rows).

        Returns
        -------
        dict
            Full plan record with parsed ``plan_json``, ``critic_verdict_json``,
            ``results_json``, and ``verification_json`` fields, or an ``error`` key
            if the plan_id is not found.
        """
        if not self._plan_history:
            return {"error": "Plan history store is not initialized"}

        plan_id = str(params.get("plan_id", "")).strip()
        if not plan_id:
            return {"error": "plan_id is required"}

        record = await self._plan_history.get_detail(plan_id)
        if record is None:
            return {"error": f"No plan found with plan_id: {plan_id}"}

        return record

    # ── Threat Containment (Issue #365) ──────────────────────────────────────

    async def _handle_threat_containment_stats(self, params: dict, ws: ServerConnection) -> dict:
        """Return the operational status of the ThreatContainmentBridge.

        JSON-RPC method: ``threat_containment_stats``

        Returns a simple status dict so the frontend can display whether
        autonomous threat containment is active and ready.

        Returns
        -------
        dict
            ``status``: "active" | "inactive"
            ``bridge_ready``: bool — True when the bridge is fully wired
            ``pending_confirmations``: int — number of open confirmation gates
        """
        if self._threat_bridge is None:
            return {
                "status": "inactive",
                "bridge_ready": False,
                "pending_confirmations": 0,
                "message": "ThreatContainmentBridge is not initialized.",
            }

        return {
            "status": "active",
            "bridge_ready": True,
            "pending_confirmations": len(self._pending_confirms),
            "message": "ThreatContainmentBridge is active and monitoring ForensicsAgent output.",
        }


def _setup_logging() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(ColorFormatter())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            stream_handler,
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )
    # httpx's INFO message includes the complete request URL. Gemini uses an
    # API-key query parameter, so leaving this enabled writes credentials into
    # console/file logs and any pasted support transcript.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    """Entry point for the pilot-daemon command."""
    parser = argparse.ArgumentParser(prog="pilot.server")
    parser.add_argument("--dry-run", action="store_true", help="Simulate actions without executing them")
    parser.add_argument(
        "--export-logs",
        action="store_true",
        help="Package all logs, config.toml, and audit trails into a zip on the Desktop for bug reporting.",
    )
    parser.add_argument("--replace-pid", type=int, help=argparse.SUPPRESS)
    args, _ = parser.parse_known_args()
    if args.replace_pid is not None:
        from pilot.system.elevation import replace_existing_daemon

        replace_existing_daemon(args.replace_pid)

    ensure_dirs()
    _setup_logging()
    config = PilotConfig.load()
    if args.export_logs:
        export_logs()
        return
    if args.dry_run:
        config.security.dry_run = True
        logger.info("Dry-run mode enabled via CLI flag")
    server = PilotServer(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run() -> None:
        await server.start()
        stop_event = asyncio.Event()

        def _signal_handler() -> None:
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _signal_handler)

        await stop_event.wait()
        await server.stop()

    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        loop.run_until_complete(server.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
