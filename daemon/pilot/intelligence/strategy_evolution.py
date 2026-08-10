"""Human-gated reflective strategy evolution for Heliox.

GEPA's useful product pattern is preserved here: full diagnostic feedback
drives textual candidates, candidates compete on multiple evaluation
dimensions, and no candidate becomes production behavior without isolated
evaluation, shadow evidence, a consented canary, and explicit promotion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from pilot.intelligence.experience import (
    ExperienceEventType,
    ExperienceLedger,
    PrivacyClass,
    redact_for_persistence,
)

if TYPE_CHECKING:
    from pilot.models.router import ModelRouter
    from pilot.testing.evaluation import EvaluationReport

logger = logging.getLogger("pilot.intelligence.strategy_evolution")

STRATEGY_SCHEMA_VERSION = 1
MINIMUM_SHADOW_SAMPLES = 10
MINIMUM_CANARY_SAMPLES = 5
MAX_STRATEGY_LENGTH = 12_000


class StrategyArtifactType(StrEnum):
    PLANNER_INSTRUCTION = "planner_instruction"
    TOOL_DESCRIPTION = "tool_description"
    RECOVERY_STRATEGY = "recovery_strategy"
    CONTEXT_POLICY = "context_policy"
    SUGGESTION_WORDING = "suggestion_wording"
    DECOMPOSITION_POLICY = "decomposition_policy"


class StrategyStage(StrEnum):
    CANDIDATE = "candidate"
    REPLAY_PASSED = "replay_passed"
    SHADOW = "shadow"
    CANARY = "canary"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    candidate_id: str
    artifact_type: StrategyArtifactType
    component: str
    content: str
    rationale: str
    parent_candidate_id: str
    source_trace_ids: tuple[str, ...]
    stage: StrategyStage
    content_sha256: str
    created_at: str
    updated_at: str
    isolated_evaluation: dict[str, Any]
    shadow_evaluation: dict[str, Any]
    canary_evaluation: dict[str, Any]

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        value = asdict(self)
        value["artifact_type"] = self.artifact_type.value
        value["stage"] = self.stage.value
        if not include_content:
            value.pop("content", None)
        return value


@dataclass(frozen=True, slots=True)
class StrategyAssignment:
    component: str
    candidate_id: str
    previous_candidate_id: str
    version: int
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _EvaluationSummary:
    scenario_id: str
    passed: bool
    overall_score: float
    dimension_scores: dict[str, float]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS strategy_candidates (
    candidate_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    artifact_type TEXT NOT NULL,
    component TEXT NOT NULL,
    content TEXT NOT NULL,
    rationale TEXT NOT NULL,
    parent_candidate_id TEXT NOT NULL,
    source_trace_ids_json TEXT NOT NULL,
    stage TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    isolated_evaluation_json TEXT NOT NULL DEFAULT '{}',
    shadow_evaluation_json TEXT NOT NULL DEFAULT '{}',
    canary_evaluation_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_strategy_candidates_stage
    ON strategy_candidates(stage, updated_at);
CREATE INDEX IF NOT EXISTS idx_strategy_candidates_component
    ON strategy_candidates(component, stage);

CREATE TABLE IF NOT EXISTS strategy_assignments (
    component TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    previous_candidate_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES strategy_candidates(candidate_id)
);
"""

_SELECT_CANDIDATE = """
candidate_id, artifact_type, component, content, rationale,
parent_candidate_id, source_trace_ids_json, stage, content_sha256,
created_at, updated_at, isolated_evaluation_json,
shadow_evaluation_json, canary_evaluation_json
"""

REFLECTION_PROMPT = """\
You are improving one bounded Zariff strategy artifact using GEPA-style
reflective text evolution. Diagnose the supplied evaluation feedback and
propose one concise replacement. Do not add permissions, bypass approval,
weaken safety rules, claim unavailable tools, or include secrets.

Artifact type: {artifact_type}
Component: {component}
Current strategy:
{base_content}

Sanitized diagnostic feedback:
{diagnostics}

Return strict JSON:
{{
  "content": "the complete proposed replacement",
  "rationale": "specific failure diagnosis and why this change should help"
}}
"""


class StrategyEvolutionLab:
    """Persistent candidate archive and guarded promotion state machine."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        model_router: ModelRouter | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._model = model_router
        self._db: aiosqlite.Connection | None = None
        self._ledger: ExperienceLedger | None = None

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()

    def set_experience_ledger(self, ledger: ExperienceLedger) -> None:
        self._ledger = ledger

    async def propose(
        self,
        *,
        artifact_type: StrategyArtifactType | str,
        component: str,
        content: str,
        rationale: str,
        parent_candidate_id: str = "",
        source_trace_ids: Sequence[str] = (),
        source: str = "admin",
    ) -> StrategyCandidate:
        """Store an inert candidate; never alter an active assignment."""

        db = self._require_db()
        artifact = StrategyArtifactType(artifact_type)
        normalized_component = self._validate_component(component)
        safe_content = self._validate_content(content)
        safe_rationale = str(redact_for_persistence(rationale)).strip()[:4000]
        if parent_candidate_id and await self.get_candidate(parent_candidate_id) is None:
            raise ValueError("parent candidate does not exist")
        candidate_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        digest = hashlib.sha256(safe_content.encode("utf-8")).hexdigest()
        trace_ids = tuple(dict.fromkeys(str(item).strip() for item in source_trace_ids if str(item).strip()))
        await db.execute(
            """
            INSERT INTO strategy_candidates (
                candidate_id, schema_version, artifact_type, component,
                content, rationale, parent_candidate_id,
                source_trace_ids_json, stage, content_sha256,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                STRATEGY_SCHEMA_VERSION,
                artifact.value,
                normalized_component,
                safe_content,
                safe_rationale,
                parent_candidate_id,
                json.dumps(trace_ids),
                StrategyStage.CANDIDATE.value,
                digest,
                now,
                now,
            ),
        )
        await db.commit()
        candidate = await self._require_candidate(candidate_id)
        if self._ledger is not None:
            await self._ledger.append(
                ExperienceEventType.STRATEGY_CANDIDATE,
                idempotency_key=f"strategy:{candidate_id}:candidate",
                source=source,
                payload={
                    "candidate_id": candidate_id,
                    "artifact_type": artifact.value,
                    "component": normalized_component,
                    "stage": StrategyStage.CANDIDATE.value,
                    "content_sha256": digest,
                    "source_trace_ids": trace_ids,
                },
                provenance={"component": "StrategyEvolutionLab.propose"},
                privacy_class=PrivacyClass.SENSITIVE,
            )
        return candidate

    async def reflect_candidate(
        self,
        *,
        artifact_type: StrategyArtifactType | str,
        component: str,
        base_content: str,
        diagnostics: Sequence[str],
        source_trace_ids: Sequence[str] = (),
        parent_candidate_id: str = "",
    ) -> StrategyCandidate:
        """Ask the configured model for one inert, trace-informed mutation."""

        if self._model is None:
            raise RuntimeError("A reflection model is not configured")
        artifact = StrategyArtifactType(artifact_type)
        sanitized_diagnostics = [
            str(redact_for_persistence(item)).strip()[:1000] for item in diagnostics[:20] if str(item).strip()
        ]
        prompt = REFLECTION_PROMPT.format(
            artifact_type=artifact.value,
            component=self._validate_component(component),
            base_content=self._validate_content(base_content),
            diagnostics="\n".join(f"- {item}" for item in sanitized_diagnostics) or "- No diagnostic detail",
        )
        raw = await self._model.generate(prompt)
        data = self._parse_json_object(raw)
        return await self.propose(
            artifact_type=artifact,
            component=component,
            content=str(data.get("content", "")),
            rationale=str(data.get("rationale", "")),
            parent_candidate_id=parent_candidate_id,
            source_trace_ids=source_trace_ids,
            source="gepa_reflection",
        )

    async def record_isolated_evaluation(
        self,
        candidate_id: str,
        *,
        baseline_reports: Sequence[EvaluationReport],
        candidate_reports: Sequence[EvaluationReport],
    ) -> StrategyCandidate:
        """Compare replay results and advance only a non-regressing candidate."""

        candidate = await self._require_candidate(candidate_id)
        self._require_stage(candidate, StrategyStage.CANDIDATE)
        baseline = {report.scenario_id: report for report in baseline_reports}
        evaluated = {report.scenario_id: report for report in candidate_reports}
        if not baseline or baseline.keys() != evaluated.keys():
            raise ValueError("baseline and candidate evaluations must cover the same scenarios")

        regressions: list[str] = []
        capability_improvements = 0
        safety_passed = True
        for scenario_id, baseline_report in baseline.items():
            candidate_report = evaluated[scenario_id]
            if not candidate_report.passed:
                regressions.append(f"{scenario_id}: candidate failed")
            if candidate_report.dimension_scores.get("safety", 0.0) < 1.0:
                safety_passed = False
                regressions.append(f"{scenario_id}: safety score was not perfect")
            if candidate_report.overall_score + 0.02 < baseline_report.overall_score:
                regressions.append(
                    f"{scenario_id}: score regressed {baseline_report.overall_score:.3f}"
                    f" -> {candidate_report.overall_score:.3f}"
                )
            if candidate_report.overall_score > baseline_report.overall_score + 0.001:
                capability_improvements += 1

        payload = {
            "scenario_count": len(baseline),
            "baseline_score": round(
                sum(report.overall_score for report in baseline.values()) / len(baseline),
                4,
            ),
            "candidate_score": round(
                sum(report.overall_score for report in evaluated.values()) / len(evaluated),
                4,
            ),
            "capability_improvements": capability_improvements,
            "safety_passed": safety_passed,
            "regressions": regressions,
            "passed": not regressions and safety_passed,
        }
        next_stage = StrategyStage.REPLAY_PASSED if payload["passed"] else StrategyStage.REJECTED
        await self._update_candidate(
            candidate_id,
            stage=next_stage,
            isolated_evaluation=payload,
        )
        return await self._require_candidate(candidate_id)

    async def record_isolated_attestation(
        self,
        candidate_id: str,
        *,
        harness_run_id: str,
        baseline_results: Sequence[Mapping[str, Any]],
        candidate_results: Sequence[Mapping[str, Any]],
    ) -> StrategyCandidate:
        """Accept normalized output from the isolated replay harness."""

        run_id = harness_run_id.strip()
        if not run_id or len(run_id) > 256:
            raise ValueError("a bounded harness_run_id is required")
        if len(baseline_results) < 3 or len(candidate_results) < 3:
            raise ValueError("isolated evaluation requires at least three scenarios")
        baseline = tuple(self._evaluation_summary(item) for item in baseline_results)
        evaluated = tuple(self._evaluation_summary(item) for item in candidate_results)
        candidate = await self.record_isolated_evaluation(
            candidate_id,
            baseline_reports=baseline,
            candidate_reports=evaluated,
        )
        payload = {**candidate.isolated_evaluation, "harness_run_id": run_id}
        await self._update_candidate(candidate_id, isolated_evaluation=payload)
        return await self._require_candidate(candidate_id)

    async def start_shadow(self, candidate_id: str) -> StrategyCandidate:
        candidate = await self._require_candidate(candidate_id)
        self._require_stage(candidate, StrategyStage.REPLAY_PASSED)
        await self._update_candidate(candidate_id, stage=StrategyStage.SHADOW)
        return await self._require_candidate(candidate_id)

    async def record_shadow_evaluation(
        self,
        candidate_id: str,
        *,
        sample_count: int,
        baseline_score: float,
        candidate_score: float,
        safety_incidents: int = 0,
    ) -> StrategyCandidate:
        candidate = await self._require_candidate(candidate_id)
        self._require_stage(candidate, StrategyStage.SHADOW)
        payload = {
            "sample_count": max(0, int(sample_count)),
            "baseline_score": self._bounded_score(baseline_score),
            "candidate_score": self._bounded_score(candidate_score),
            "safety_incidents": max(0, int(safety_incidents)),
        }
        payload["eligible_for_canary"] = bool(
            payload["sample_count"] >= MINIMUM_SHADOW_SAMPLES
            and payload["candidate_score"] >= payload["baseline_score"]
            and payload["safety_incidents"] == 0
        )
        await self._update_candidate(candidate_id, shadow_evaluation=payload)
        return await self._require_candidate(candidate_id)

    async def start_canary(
        self,
        candidate_id: str,
        *,
        actor: str,
        consent_confirmed: bool,
    ) -> StrategyCandidate:
        candidate = await self._require_candidate(candidate_id)
        self._require_stage(candidate, StrategyStage.SHADOW)
        if not consent_confirmed or not actor.strip():
            raise PermissionError("Canary requires an identified actor and explicit consent")
        if not bool(candidate.shadow_evaluation.get("eligible_for_canary", False)):
            raise ValueError("Shadow evidence is not eligible for canary")
        await self._update_candidate(
            candidate_id,
            stage=StrategyStage.CANARY,
            canary_evaluation={
                "actor": actor.strip()[:128],
                "consent_confirmed": True,
                "sample_count": 0,
                "safety_incidents": 0,
            },
        )
        return await self._require_candidate(candidate_id)

    async def record_canary_evaluation(
        self,
        candidate_id: str,
        *,
        sample_count: int,
        baseline_score: float,
        candidate_score: float,
        safety_incidents: int = 0,
    ) -> StrategyCandidate:
        candidate = await self._require_candidate(candidate_id)
        self._require_stage(candidate, StrategyStage.CANARY)
        payload = {
            **candidate.canary_evaluation,
            "sample_count": max(0, int(sample_count)),
            "baseline_score": self._bounded_score(baseline_score),
            "candidate_score": self._bounded_score(candidate_score),
            "safety_incidents": max(0, int(safety_incidents)),
        }
        payload["eligible_for_promotion"] = bool(
            payload["sample_count"] >= MINIMUM_CANARY_SAMPLES
            and payload["candidate_score"] >= payload["baseline_score"]
            and payload["safety_incidents"] == 0
        )
        await self._update_candidate(candidate_id, canary_evaluation=payload)
        return await self._require_candidate(candidate_id)

    async def promote(
        self,
        candidate_id: str,
        *,
        actor: str,
        confirmation: str,
    ) -> StrategyAssignment:
        """Explicitly assign one fully evaluated candidate to production."""

        db = self._require_db()
        candidate = await self._require_candidate(candidate_id)
        self._require_stage(candidate, StrategyStage.CANARY)
        if confirmation != candidate_id or not actor.strip():
            raise PermissionError("Promotion requires the actor to confirm the exact candidate id")
        if not bool(candidate.canary_evaluation.get("eligible_for_promotion", False)):
            raise ValueError("Canary evidence is not eligible for promotion")

        current = await self.get_assignment(candidate.component)
        previous = current.candidate_id if current is not None else ""
        version = (current.version + 1) if current is not None else 1
        now = datetime.now(UTC).isoformat()
        if previous and previous != candidate_id:
            await db.execute(
                "UPDATE strategy_candidates SET stage = ?, updated_at = ? WHERE candidate_id = ?",
                (StrategyStage.ROLLED_BACK.value, now, previous),
            )
        await db.execute(
            """
            INSERT INTO strategy_assignments (
                component, candidate_id, previous_candidate_id, version, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(component) DO UPDATE SET
                candidate_id = excluded.candidate_id,
                previous_candidate_id = excluded.previous_candidate_id,
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (candidate.component, candidate_id, previous, version, now),
        )
        await db.execute(
            "UPDATE strategy_candidates SET stage = ?, updated_at = ? WHERE candidate_id = ?",
            (StrategyStage.PROMOTED.value, now, candidate_id),
        )
        await db.commit()
        return StrategyAssignment(candidate.component, candidate_id, previous, version, now)

    async def rollback(
        self,
        component: str,
        *,
        actor: str,
        confirmation: str,
    ) -> StrategyAssignment | None:
        """Restore the previous explicit assignment and mark the current one rolled back."""

        db = self._require_db()
        normalized_component = self._validate_component(component)
        current = await self.get_assignment(normalized_component)
        if current is None:
            return None
        if confirmation != current.candidate_id or not actor.strip():
            raise PermissionError("Rollback requires the actor to confirm the active candidate id")
        previous = current.previous_candidate_id
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "UPDATE strategy_candidates SET stage = ?, updated_at = ? WHERE candidate_id = ?",
            (StrategyStage.ROLLED_BACK.value, now, current.candidate_id),
        )
        if previous:
            version = current.version + 1
            await db.execute(
                """
                UPDATE strategy_assignments
                SET candidate_id = ?, previous_candidate_id = '',
                    version = ?, updated_at = ?
                WHERE component = ?
                """,
                (previous, version, now, normalized_component),
            )
            await db.execute(
                "UPDATE strategy_candidates SET stage = ?, updated_at = ? WHERE candidate_id = ?",
                (StrategyStage.PROMOTED.value, now, previous),
            )
            result: StrategyAssignment | None = StrategyAssignment(
                normalized_component,
                previous,
                "",
                version,
                now,
            )
        else:
            await db.execute(
                "DELETE FROM strategy_assignments WHERE component = ?",
                (normalized_component,),
            )
            result = None
        await db.commit()
        return result

    async def get_active_text(self, component: str) -> str:
        assignment = await self.get_assignment(component)
        if assignment is None:
            return ""
        candidate = await self.get_candidate(assignment.candidate_id)
        if candidate is None or candidate.stage != StrategyStage.PROMOTED:
            return ""
        return candidate.content

    async def get_assignment(self, component: str) -> StrategyAssignment | None:
        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT component, candidate_id, previous_candidate_id, version, updated_at
            FROM strategy_assignments WHERE component = ?
            """,
            (self._validate_component(component),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return StrategyAssignment(str(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4]))

    async def get_candidate(self, candidate_id: str) -> StrategyCandidate | None:
        db = self._require_db()
        cursor = await db.execute(
            f"SELECT {_SELECT_CANDIDATE} FROM strategy_candidates WHERE candidate_id = ?",
            (candidate_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._candidate_from_row(row)

    async def list_candidates(
        self,
        *,
        stage: StrategyStage | str | None = None,
        limit: int = 100,
    ) -> list[StrategyCandidate]:
        db = self._require_db()
        if stage is None:
            cursor = await db.execute(
                f"SELECT {_SELECT_CANDIDATE} FROM strategy_candidates ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            )
        else:
            cursor = await db.execute(
                f"""
                SELECT {_SELECT_CANDIDATE} FROM strategy_candidates
                WHERE stage = ? ORDER BY updated_at DESC LIMIT ?
                """,
                (StrategyStage(stage).value, max(1, min(limit, 500))),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._candidate_from_row(row) for row in rows]

    async def status(self) -> dict[str, Any]:
        candidates = await self.list_candidates(limit=500)
        assignments = {
            candidate.component: assignment.to_dict()
            for candidate in candidates
            if (assignment := await self.get_assignment(candidate.component)) is not None
        }
        counts = {stage.value: 0 for stage in StrategyStage}
        for candidate in candidates:
            counts[candidate.stage.value] += 1
        return {
            "enabled": True,
            "schema_version": STRATEGY_SCHEMA_VERSION,
            "algorithm": "gepa-style-reflective-pareto",
            "candidate_counts": counts,
            "pareto_front": self._pareto_front(candidates),
            "assignments": assignments,
            "promotion": {
                "automatic": False,
                "shadow_samples_required": MINIMUM_SHADOW_SAMPLES,
                "canary_samples_required": MINIMUM_CANARY_SAMPLES,
                "exact_id_confirmation_required": True,
            },
        }

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _update_candidate(
        self,
        candidate_id: str,
        *,
        stage: StrategyStage | None = None,
        isolated_evaluation: dict[str, Any] | None = None,
        shadow_evaluation: dict[str, Any] | None = None,
        canary_evaluation: dict[str, Any] | None = None,
    ) -> None:
        db = self._require_db()
        candidate = await self._require_candidate(candidate_id)
        await db.execute(
            """
            UPDATE strategy_candidates
            SET stage = ?, updated_at = ?, isolated_evaluation_json = ?,
                shadow_evaluation_json = ?, canary_evaluation_json = ?
            WHERE candidate_id = ?
            """,
            (
                (stage or candidate.stage).value,
                datetime.now(UTC).isoformat(),
                json.dumps(isolated_evaluation if isolated_evaluation is not None else candidate.isolated_evaluation),
                json.dumps(shadow_evaluation if shadow_evaluation is not None else candidate.shadow_evaluation),
                json.dumps(canary_evaluation if canary_evaluation is not None else candidate.canary_evaluation),
                candidate_id,
            ),
        )
        await db.commit()

    async def _require_candidate(self, candidate_id: str) -> StrategyCandidate:
        candidate = await self.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(f"Unknown strategy candidate: {candidate_id}")
        return candidate

    @staticmethod
    def _require_stage(candidate: StrategyCandidate, expected: StrategyStage) -> None:
        if candidate.stage != expected:
            raise ValueError(
                f"Candidate {candidate.candidate_id} is {candidate.stage.value}; expected {expected.value}"
            )

    @staticmethod
    def _candidate_from_row(row: Sequence[Any]) -> StrategyCandidate:
        return StrategyCandidate(
            candidate_id=str(row[0]),
            artifact_type=StrategyArtifactType(row[1]),
            component=str(row[2]),
            content=str(row[3]),
            rationale=str(row[4]),
            parent_candidate_id=str(row[5]),
            source_trace_ids=tuple(json.loads(row[6])),
            stage=StrategyStage(row[7]),
            content_sha256=str(row[8]),
            created_at=str(row[9]),
            updated_at=str(row[10]),
            isolated_evaluation=json.loads(row[11]),
            shadow_evaluation=json.loads(row[12]),
            canary_evaluation=json.loads(row[13]),
        )

    @staticmethod
    def _validate_component(component: str) -> str:
        normalized = component.strip().lower()
        if not normalized or len(normalized) > 128:
            raise ValueError("component must be between 1 and 128 characters")
        if not all(char.isalnum() or char in "._-" for char in normalized):
            raise ValueError("component contains unsupported characters")
        return normalized

    @staticmethod
    def _validate_content(content: str) -> str:
        safe = str(redact_for_persistence(content)).strip()
        if not safe:
            raise ValueError("strategy content cannot be empty")
        if len(safe) > MAX_STRATEGY_LENGTH:
            raise ValueError(f"strategy content exceeds {MAX_STRATEGY_LENGTH} characters")
        return safe

    @staticmethod
    def _bounded_score(score: float) -> float:
        value = float(score)
        if not 0.0 <= value <= 1.0:
            raise ValueError("evaluation scores must be between 0 and 1")
        return round(value, 4)

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].lstrip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("reflection response must be a JSON object")
        return data

    @classmethod
    def _evaluation_summary(cls, raw: Mapping[str, Any]) -> _EvaluationSummary:
        scenario_id = str(raw.get("scenario_id", "")).strip()
        if not scenario_id:
            raise ValueError("every evaluation result requires a scenario_id")
        dimensions_raw = raw.get("dimension_scores", {})
        if not isinstance(dimensions_raw, Mapping):
            raise ValueError("dimension_scores must be an object")
        dimensions = {str(name): cls._bounded_score(float(value)) for name, value in dimensions_raw.items()}
        if "safety" not in dimensions:
            raise ValueError("every evaluation result requires a safety score")
        return _EvaluationSummary(
            scenario_id=scenario_id,
            passed=bool(raw.get("passed", False)),
            overall_score=cls._bounded_score(float(raw.get("overall_score", 0.0))),
            dimension_scores=dimensions,
        )

    @staticmethod
    def _pareto_front(candidates: Sequence[StrategyCandidate]) -> list[str]:
        scored: list[tuple[StrategyCandidate, tuple[float, float, float, float, float]]] = []
        for candidate in candidates:
            metrics = candidate.isolated_evaluation
            if not metrics.get("passed"):
                continue
            vector = (
                float(metrics.get("candidate_score", 0.0)),
                1.0 if metrics.get("safety_passed") else 0.0,
                float(candidate.shadow_evaluation.get("candidate_score", 0.0)),
                float(candidate.canary_evaluation.get("candidate_score", 0.0)),
                -float(len(candidate.content)) / MAX_STRATEGY_LENGTH,
            )
            scored.append((candidate, vector))

        front: list[str] = []
        for candidate, vector in scored:
            dominated = any(
                all(other_value >= value for other_value, value in zip(other, vector, strict=True))
                and any(other_value > value for other_value, value in zip(other, vector, strict=True))
                for other_candidate, other in scored
                if other_candidate.candidate_id != candidate.candidate_id
            )
            if not dominated:
                front.append(candidate.candidate_id)
        return front

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("StrategyEvolutionLab is not initialized")
        return self._db
