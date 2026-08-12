from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pilot.agents.planner import Planner


class _ConversationalModel:
    def __init__(self):
        self.calls = []

    async def generate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        user_text = args[0][-1]["content"]
        return f"Model-generated reply for: {user_text}"


class _MemoryThatMustNotRun:
    async def get_context(self, user_input):
        raise AssertionError("simple conversation must not load action-planning context")


@pytest.mark.asyncio
async def test_greeting_uses_model_for_wording_but_returns_zero_actions():
    model = _ConversationalModel()
    planner = Planner(model, _MemoryThatMustNotRun())

    plan = await planner.plan("Hello Zariff")

    assert plan.error is None
    assert plan.actions == []
    assert plan.explanation == "Model-generated reply for: Hello Zariff"
    assert len(model.calls) == 1
    assert model.calls[0][1]["json_mode"] is False


def test_compound_greeting_with_action_is_not_treated_as_conversation_only():
    assert Planner._is_conversation_only("Hello Zariff, open GitHub") is False
    assert Planner._try_fast_path("Hello Zariff, open GitHub") is None


def test_parser_accepts_helpful_zero_action_model_response():
    planner = Planner(MagicMock(), MagicMock())

    plan = planner._parse_response(
        '{"explanation":"I can answer that without changing your computer.","actions":[]}',
        "What can you tell me?",
    )

    assert plan.error is None
    assert plan.actions == []
    assert plan.explanation == "I can answer that without changing your computer."


def test_parser_rejects_empty_zero_action_model_response():
    planner = Planner(MagicMock(), MagicMock())

    plan = planner._parse_response('{"explanation":"","actions":[]}', "Hello")

    assert plan.error is not None
    assert "EMPTY RESPONSE" in plan.error
