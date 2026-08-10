import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pilot.actions import Action, ActionPlan, ActionType, BrowserParams, PowerParams
from pilot.agents.destructive_critic import (
    CriticVerdict,
    DestructiveCriticAgent,
    constrain_verdict_to_plan_authority,
)


@pytest.mark.asyncio
async def test_tier4_plan_gets_blocked():
    model_router = AsyncMock()

    model_router.generate.return_value = json.dumps(
        {
            "verdict": "BLOCK",
            "risk_score": 0.95,
            "issues": ["Dangerous destructive action"],
            "safe_actions": [],
            "flagged_actions": ["delete_root"],
            "recommendation": "Do not execute",
        }
    )

    critic = DestructiveCriticAgent(model_router)

    fake_plan = MagicMock()
    fake_plan.actions = []
    fake_plan.max_tier.name = "ROOT_CRITICAL"
    fake_plan.explanation = "Delete system files"

    verdict = await critic.review(
        "Delete everything",
        fake_plan,
    )

    assert verdict.verdict == "BLOCK"
    assert verdict.is_blocked is True


@pytest.mark.asyncio
async def test_tier3_plan_gets_warned():
    model_router = AsyncMock()

    model_router.generate.return_value = json.dumps(
        {
            "verdict": "WARN",
            "risk_score": 0.60,
            "issues": ["Potential risk detected"],
            "safe_actions": [],
            "flagged_actions": ["delete_files"],
            "recommendation": "Proceed carefully",
        }
    )

    critic = DestructiveCriticAgent(model_router)

    fake_plan = MagicMock()
    fake_plan.actions = []
    fake_plan.max_tier.name = "DESTRUCTIVE"
    fake_plan.explanation = "Remove user files"

    verdict = await critic.review(
        "Delete selected files",
        fake_plan,
    )

    assert verdict.verdict == "WARN"
    assert verdict.has_warnings is True


@pytest.mark.asyncio
async def test_safe_plan_approved():
    model_router = AsyncMock()

    model_router.generate.return_value = json.dumps(
        {
            "verdict": "APPROVE",
            "risk_score": 0.10,
            "issues": [],
            "safe_actions": ["read_file"],
            "flagged_actions": [],
            "recommendation": "Safe to continue",
        }
    )

    critic = DestructiveCriticAgent(model_router)

    fake_plan = MagicMock()
    fake_plan.actions = []
    fake_plan.max_tier.name = "SAFE"
    fake_plan.explanation = "Read a file"

    verdict = await critic.review(
        "Open a text file",
        fake_plan,
    )

    assert verdict.verdict == "APPROVE"
    assert verdict.is_blocked is False
    _, kwargs = model_router.generate.await_args
    assert kwargs["system"]
    assert kwargs["json_mode"] is True
    assert "system_prompt" not in kwargs


@pytest.mark.asyncio
async def test_critic_error_falls_back_to_warn():
    model_router = AsyncMock()

    model_router.generate.side_effect = Exception("LLM unavailable")

    critic = DestructiveCriticAgent(model_router)

    fake_plan = MagicMock()
    fake_plan.actions = []
    fake_plan.max_tier.name = "ROOT_CRITICAL"
    fake_plan.explanation = "Delete files"

    verdict = await critic.review(
        "Delete files",
        fake_plan,
    )

    assert verdict.verdict == "WARN"
    assert verdict.has_warnings is True


def test_low_authority_plan_cannot_be_hard_blocked_for_invented_privileges():
    plan = ActionPlan(
        actions=[
            Action(
                action_type=ActionType.BROWSER_NAVIGATE,
                target="https://example.com",
                parameters=BrowserParams(url="https://example.com"),
            )
        ],
        explanation="Open a public webpage.",
    )
    verdict = CriticVerdict(
        verdict="BLOCK",
        risk_score=0.95,
        issues=["Browser navigation allegedly requires root."],
        recommendation="Do not proceed.",
    )

    constrained = constrain_verdict_to_plan_authority(plan, verdict)

    assert constrained.verdict == "WARN"
    assert constrained.risk_score == 0.74
    assert "normal approval" in constrained.recommendation


def test_root_critical_plan_retains_hard_block():
    plan = ActionPlan(
        actions=[
            Action(
                action_type=ActionType.POWER_SHUTDOWN,
                target="system",
                parameters=PowerParams(),
                requires_root=True,
            )
        ],
        explanation="Shut down the system.",
    )
    verdict = CriticVerdict(
        verdict="BLOCK",
        risk_score=1.0,
        recommendation="Unsafe power operation.",
    )

    assert constrain_verdict_to_plan_authority(plan, verdict) is verdict
