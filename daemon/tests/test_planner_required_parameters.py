from unittest.mock import MagicMock

from pilot.agents.planner import Planner


def _planner() -> Planner:
    return Planner(MagicMock(), MagicMock())


def test_file_copy_without_destination_is_rejected_before_execution():
    plan = _planner()._parse_response(
        """
        {
          "explanation": "Copy a file.",
          "actions": [
            {
              "action_type": "file_copy",
              "target": "C:\\\\source.txt",
              "parameters": {"path": "C:\\\\source.txt"}
            }
          ]
        }
        """,
        "Copy C:\\source.txt to C:\\destination.txt.",
    )

    assert plan.actions == []
    assert "parameters.destination" in str(plan.error)


def test_file_copy_with_destination_is_preserved():
    plan = _planner()._parse_response(
        """
        {
          "explanation": "Copy a file.",
          "actions": [
            {
              "action_type": "file_copy",
              "target": "C:\\\\source.txt",
              "parameters": {
                "path": "C:\\\\source.txt",
                "destination": "C:\\\\destination.txt"
              }
            }
          ]
        }
        """,
        "Copy C:\\source.txt to C:\\destination.txt.",
    )

    assert plan.error is None
    assert plan.actions[0].parameters.destination == "C:\\destination.txt"
