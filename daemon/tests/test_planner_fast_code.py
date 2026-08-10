from pilot.actions import ActionType
from pilot.agents.planner import Planner


def test_explicit_fenced_python_uses_local_fast_path():
    plan = Planner._try_fast_path(
        "Run this Python code exactly and report stdout:\n"
        "```python\n"
        "import hashlib\n"
        "print(hashlib.sha256(b'heliox').hexdigest())\n"
        "```"
    )

    assert plan is not None
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.CODE_EXECUTE
    assert "hashlib.sha256" in plan.actions[0].parameters.code
    assert plan.actions[0].parameters.language == "python"


def test_natural_language_code_request_still_requires_model_planning():
    assert Planner._try_fast_path("Write some Python to analyze my files") is None
