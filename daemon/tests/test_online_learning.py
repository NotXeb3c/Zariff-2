"""Tests for evidence-gated River adaptation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pilot.intelligence.experience import ExperienceEventType, ExperienceLedger
from pilot.intelligence.online_learning import MINIMUM_PROMOTION_LABELS, VerifiedOnlineLearner


@pytest.fixture
def occurred_at() -> str:
    return datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_suggestion_model_promotes_only_after_repeated_explicit_feedback(
    tmp_path,
    occurred_at,
):
    learner = VerifiedOnlineLearner(tmp_path / "learning-state.json")
    ledger = ExperienceLedger(tmp_path / "experience.db")
    await ledger.initialize()
    await learner.initialize(ledger)
    ledger.subscribe(learner.consume)

    initial = learner.score_suggestion(
        pattern_id="terminal_error",
        app_name="powershell",
        priority="high",
        occurred_at=occurred_at,
    )
    assert initial.state == "candidate"
    assert initial.probability == 0.5

    for index in range(MINIMUM_PROMOTION_LABELS):
        suggestion_id = f"suggestion-{index}"
        await ledger.append(
            ExperienceEventType.SUGGESTION_SHOWN,
            idempotency_key=f"{suggestion_id}:shown",
            occurred_at=occurred_at,
            payload={
                "suggestion_id": suggestion_id,
                "pattern_id": "terminal_error",
                "context_app": "powershell",
                "priority": "high",
            },
        )
        await ledger.append(
            ExperienceEventType.SUGGESTION_FEEDBACK,
            idempotency_key=f"{suggestion_id}:feedback",
            occurred_at=occurred_at,
            payload={
                "suggestion_id": suggestion_id,
                "pattern_id": "terminal_error",
                "context_app": "powershell",
                "priority": "high",
                "decision": "accepted",
            },
        )
    await ledger.drain_subscribers()

    learned = learner.score_suggestion(
        pattern_id="terminal_error",
        app_name="powershell",
        priority="high",
        occurred_at=occurred_at,
    )
    assert learned.labels == MINIMUM_PROMOTION_LABELS
    assert learned.state == "promoted"
    assert learned.probability > 0.58
    assert learned.to_dict()["authority"] == "ranking_only"
    await ledger.close()


@pytest.mark.asyncio
async def test_ignored_suggestions_are_negative_labels(tmp_path, occurred_at):
    learner = VerifiedOnlineLearner(tmp_path / "learning-state.json")
    ledger = ExperienceLedger(tmp_path / "experience.db")
    await ledger.initialize()
    ledger.subscribe(learner.consume)

    for index in range(MINIMUM_PROMOTION_LABELS):
        await ledger.append(
            ExperienceEventType.SUGGESTION_FEEDBACK,
            idempotency_key=f"ignored-{index}",
            occurred_at=occurred_at,
            payload={
                "suggestion_id": f"ignored-{index}",
                "pattern_id": "browser_research",
                "context_app": "chrome",
                "priority": "low",
                "decision": "ignored",
            },
        )
    await ledger.drain_subscribers()

    learned = learner.score_suggestion(
        pattern_id="browser_research",
        app_name="chrome",
        priority="low",
        occurred_at=occurred_at,
    )
    assert learned.state == "promoted"
    assert learned.probability < 0.42
    assert learner.status()["suggestions"]["negative"] == MINIMUM_PROMOTION_LABELS
    await ledger.close()


@pytest.mark.asyncio
async def test_transition_learning_requires_real_callback_observation(tmp_path, occurred_at):
    learner = VerifiedOnlineLearner(tmp_path / "learning-state.json")
    ledger = ExperienceLedger(tmp_path / "experience.db")
    await ledger.initialize()
    ledger.subscribe(learner.consume)

    for index in range(MINIMUM_PROMOTION_LABELS):
        action_id = f"action-{index}"
        await ledger.append(
            ExperienceEventType.CANDIDATE_ACTION,
            idempotency_key=f"{action_id}:candidate",
            action_id=action_id,
            plan_id=f"plan-{index}",
            source="interactive",
            occurred_at=occurred_at,
            payload={"action": {"action_type": "browser_navigate"}},
        )
        await ledger.append(
            ExperienceEventType.ACTION_COMPLETED,
            idempotency_key=f"{action_id}:complete",
            action_id=action_id,
            plan_id=f"plan-{index}",
            source="interactive",
            occurred_at=occurred_at,
            payload={
                "success": True,
                "callback_observed": index != 0,
                "output_excerpt": "loaded",
            },
        )
    await ledger.append(
        ExperienceEventType.ACTION_COMPLETED,
        idempotency_key="dry-run:complete",
        action_id="action-1",
        source="interactive",
        occurred_at=occurred_at,
        payload={
            "success": True,
            "callback_observed": True,
            "output_excerpt": "(dry run) would navigate",
        },
    )
    await ledger.drain_subscribers()

    assert learner.status()["transitions"]["labels"] == MINIMUM_PROMOTION_LABELS - 1
    assert (
        learner.score_transition(
            action_type="browser_navigate",
            source="interactive",
            occurred_at=occurred_at,
        ).state
        == "candidate"
    )
    await ledger.close()


@pytest.mark.asyncio
async def test_rebuild_and_reset_checkpoint_are_deterministic(tmp_path, occurred_at):
    state_path = tmp_path / "learning-state.json"
    ledger = ExperienceLedger(tmp_path / "experience.db")
    await ledger.initialize()
    for index in range(3):
        await ledger.append(
            ExperienceEventType.SUGGESTION_FEEDBACK,
            idempotency_key=f"feedback-{index}",
            occurred_at=occurred_at,
            payload={
                "suggestion_id": f"suggestion-{index}",
                "pattern_id": "terminal_error",
                "decision": "accepted",
            },
        )

    learner = VerifiedOnlineLearner(state_path)
    await learner.initialize(ledger)
    assert learner.status()["suggestions"]["labels"] == 3
    reset = await learner.reset()
    assert reset["suggestions"]["labels"] == 0

    rebuilt = VerifiedOnlineLearner(state_path)
    await rebuilt.initialize(ledger)
    assert rebuilt.status()["suggestions"]["labels"] == 0
    assert rebuilt.status()["reset_before_sequence"] == 3
    await ledger.close()


@pytest.mark.asyncio
async def test_observations_only_build_coarse_decayed_routines(tmp_path, occurred_at):
    learner = VerifiedOnlineLearner(tmp_path / "learning-state.json")
    ledger = ExperienceLedger(tmp_path / "experience.db")
    await ledger.initialize()
    ledger.subscribe(learner.consume)
    await ledger.append(
        ExperienceEventType.OBSERVATION,
        idempotency_key="screen-observation",
        occurred_at=occurred_at,
        payload={
            "active_app": "Visual Studio Code",
            "window_title": "secret project title is not a feature",
            "raw_media_excluded": True,
        },
    )
    await ledger.drain_subscribers()

    status = learner.status()
    assert status["suggestions"]["labels"] == 0
    assert status["transitions"]["labels"] == 0
    assert status["routine_patterns"][0]["pattern"].startswith("app:visual_studio_code:hour:")
    assert "window_title" not in str(status)
    assert "project title" not in str(status)
    assert status["privacy"]["secret_browsing"] is False
    await ledger.close()
