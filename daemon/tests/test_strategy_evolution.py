"""Tests for the human-gated reflective strategy pipeline."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pilot.config import PilotConfig
from pilot.intelligence.experience import ExperienceEventType, ExperienceLedger
from pilot.intelligence.strategy_evolution import (
    MINIMUM_CANARY_SAMPLES,
    MINIMUM_SHADOW_SAMPLES,
    StrategyArtifactType,
    StrategyEvolutionLab,
    StrategyStage,
)
from pilot.server import PilotServer


def _report(
    scenario_id: str,
    score: float,
    *,
    passed: bool = True,
    safety: float = 1.0,
):
    return SimpleNamespace(
        scenario_id=scenario_id,
        overall_score=score,
        passed=passed,
        dimension_scores={"safety": safety},
    )


@pytest.fixture
async def lab(tmp_path):
    instance = StrategyEvolutionLab(tmp_path / "strategy.db")
    await instance.initialize()
    yield instance
    await instance.close()


@pytest.mark.asyncio
async def test_candidate_is_inert_and_redacted_until_promotion(lab):
    candidate = await lab.propose(
        artifact_type=StrategyArtifactType.PLANNER_INSTRUCTION,
        component="planner.primary",
        content="Use concise plans with token sk_secret_that_must_not_persist",
        rationale="Trace showed unnecessary actions.",
        source_trace_ids=("task-1",),
    )

    assert candidate.stage == StrategyStage.CANDIDATE
    assert "sk_secret" not in candidate.content
    assert await lab.get_assignment("planner.primary") is None
    assert await lab.get_active_text("planner.primary") == ""
    status = await lab.status()
    assert status["promotion"]["automatic"] is False
    assert status["candidate_counts"]["candidate"] == 1


@pytest.mark.asyncio
async def test_regression_or_safety_failure_rejects_candidate(lab):
    candidate = await lab.propose(
        artifact_type="recovery_strategy",
        component="executor.recovery",
        content="Retry only the failed reversible action.",
        rationale="Avoid replaying successful actions.",
    )
    evaluated = await lab.record_isolated_evaluation(
        candidate.candidate_id,
        baseline_reports=[_report("recovery", 0.9)],
        candidate_reports=[_report("recovery", 0.7, safety=0.0)],
    )

    assert evaluated.stage == StrategyStage.REJECTED
    assert evaluated.isolated_evaluation["passed"] is False
    assert evaluated.isolated_evaluation["regressions"]
    with pytest.raises(ValueError, match="expected replay_passed"):
        await lab.start_shadow(candidate.candidate_id)


@pytest.mark.asyncio
async def test_isolated_harness_attestation_requires_three_matching_scenarios(lab):
    candidate = await lab.propose(
        artifact_type="planner_instruction",
        component="planner.primary",
        content="Resolve ambiguity before selecting a mutating action.",
        rationale="Ambiguous targets caused a failed trace.",
    )

    def result(scenario, score):
        return {
            "scenario_id": scenario,
            "passed": True,
            "overall_score": score,
            "dimension_scores": {"safety": 1.0},
        }

    with pytest.raises(ValueError, match="at least three"):
        await lab.record_isolated_attestation(
            candidate.candidate_id,
            harness_run_id="run-too-small",
            baseline_results=[result("one", 0.8)],
            candidate_results=[result("one", 0.9)],
        )

    evaluated = await lab.record_isolated_attestation(
        candidate.candidate_id,
        harness_run_id="harness-run-123",
        baseline_results=[
            result("ambiguous-target", 0.8),
            result("approval-delay", 0.82),
            result("offline-fallback", 0.81),
        ],
        candidate_results=[
            result("ambiguous-target", 0.86),
            result("approval-delay", 0.83),
            result("offline-fallback", 0.82),
        ],
    )

    assert evaluated.stage == StrategyStage.REPLAY_PASSED
    assert evaluated.isolated_evaluation["harness_run_id"] == "harness-run-123"


@pytest.mark.asyncio
async def test_full_pipeline_requires_evidence_consent_and_exact_confirmation(lab):
    candidate = await lab.propose(
        artifact_type="context_policy",
        component="context.assembler",
        content="Prefer unresolved goals and verified recent outcomes.",
        rationale="The baseline over-selected stale facts.",
    )
    candidate = await lab.record_isolated_evaluation(
        candidate.candidate_id,
        baseline_reports=[_report("context-a", 0.8), _report("context-b", 0.82)],
        candidate_reports=[_report("context-a", 0.84), _report("context-b", 0.83)],
    )
    assert candidate.stage == StrategyStage.REPLAY_PASSED

    candidate = await lab.start_shadow(candidate.candidate_id)
    assert candidate.stage == StrategyStage.SHADOW
    candidate = await lab.record_shadow_evaluation(
        candidate.candidate_id,
        sample_count=MINIMUM_SHADOW_SAMPLES - 1,
        baseline_score=0.8,
        candidate_score=0.9,
    )
    assert candidate.shadow_evaluation["eligible_for_canary"] is False
    with pytest.raises(ValueError, match="not eligible"):
        await lab.start_canary(candidate.candidate_id, actor="local-admin", consent_confirmed=True)

    candidate = await lab.record_shadow_evaluation(
        candidate.candidate_id,
        sample_count=MINIMUM_SHADOW_SAMPLES,
        baseline_score=0.8,
        candidate_score=0.82,
    )
    with pytest.raises(PermissionError, match="explicit consent"):
        await lab.start_canary(candidate.candidate_id, actor="local-admin", consent_confirmed=False)
    candidate = await lab.start_canary(
        candidate.candidate_id,
        actor="local-admin",
        consent_confirmed=True,
    )

    candidate = await lab.record_canary_evaluation(
        candidate.candidate_id,
        sample_count=MINIMUM_CANARY_SAMPLES,
        baseline_score=0.81,
        candidate_score=0.83,
    )
    with pytest.raises(PermissionError, match="exact candidate id"):
        await lab.promote(candidate.candidate_id, actor="local-admin", confirmation="wrong-id")
    assignment = await lab.promote(
        candidate.candidate_id,
        actor="local-admin",
        confirmation=candidate.candidate_id,
    )

    assert assignment.candidate_id == candidate.candidate_id
    assert await lab.get_active_text("context.assembler") == candidate.content
    assert (await lab.get_candidate(candidate.candidate_id)).stage == StrategyStage.PROMOTED

    with pytest.raises(PermissionError, match="active candidate id"):
        await lab.rollback("context.assembler", actor="local-admin", confirmation="wrong-id")
    assert (
        await lab.rollback(
            "context.assembler",
            actor="local-admin",
            confirmation=candidate.candidate_id,
        )
        is None
    )
    assert await lab.get_active_text("context.assembler") == ""
    assert (await lab.get_candidate(candidate.candidate_id)).stage == StrategyStage.ROLLED_BACK


@pytest.mark.asyncio
async def test_reflective_candidate_uses_sanitized_diagnostics_and_ledger(tmp_path):
    class FakeModel:
        prompt = ""

        async def generate(self, prompt):
            self.prompt = prompt
            return '{"content":"Prefer semantic targets before coordinates.","rationale":"Coordinates drifted."}'

    model = FakeModel()
    ledger = ExperienceLedger(tmp_path / "experience.db")
    await ledger.initialize()
    lab = StrategyEvolutionLab(tmp_path / "strategy.db", model_router=model)
    await lab.initialize()
    lab.set_experience_ledger(ledger)

    candidate = await lab.reflect_candidate(
        artifact_type="tool_description",
        component="browser.click",
        base_content="Click by coordinates.",
        diagnostics=["Bearer very-secret-token caused failure", "Target moved"],
        source_trace_ids=("task-browser",),
    )

    assert candidate.stage == StrategyStage.CANDIDATE
    assert "very-secret-token" not in model.prompt
    events = await ledger.list_events(event_type=ExperienceEventType.STRATEGY_CANDIDATE)
    assert len(events) == 1
    assert events[0].payload["content_sha256"] == candidate.content_sha256
    assert "content" not in events[0].payload
    await lab.close()
    await ledger.close()


@pytest.mark.asyncio
async def test_pareto_front_keeps_quality_length_tradeoffs(lab):
    first = await lab.propose(
        artifact_type="suggestion_wording",
        component="companion.suggestion",
        content="Would you like a concise summary?",
        rationale="Short wording.",
    )
    second = await lab.propose(
        artifact_type="suggestion_wording",
        component="companion.suggestion",
        content="I can summarize this now if that would help.",
        rationale="Grounded wording.",
    )
    for candidate, score in ((first, 0.82), (second, 0.9)):
        await lab.record_isolated_evaluation(
            candidate.candidate_id,
            baseline_reports=[_report("suggestion", 0.8)],
            candidate_reports=[_report("suggestion", score)],
        )

    front = (await lab.status())["pareto_front"]
    assert second.candidate_id in front
    assert first.candidate_id in front


@pytest.mark.asyncio
async def test_strategy_rpc_creates_visible_but_inert_candidate(lab):
    server = PilotServer(PilotConfig())
    server._strategy_evolution = lab

    proposed = await server._handle_strategy_propose(
        {
            "artifact_type": "decomposition_policy",
            "component": "planner.decomposition",
            "content": "Split only when steps have independent verifiable outcomes.",
            "rationale": "A trace showed unnecessary decomposition.",
            "source_trace_ids": ["task-1"],
        },
        ws=None,
    )
    listed = await server._handle_strategy_candidates({}, ws=None)
    status = await server._handle_strategy_evolution_status({}, ws=None)

    assert proposed["status"] == "candidate"
    assert proposed["candidate"]["content"].startswith("Split only")
    assert "content" not in listed["candidates"][0]
    assert status["candidate_counts"]["candidate"] == 1
    assert status["assignments"] == {}
