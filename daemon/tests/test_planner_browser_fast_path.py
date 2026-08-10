from pilot.actions import ActionType
from pilot.agents.planner import Planner


def test_explicit_semantic_browser_report_uses_local_fast_path():
    plan = Planner._try_fast_path(
        "Navigate to https://example.com, click the link that means More information "
        "even if the visible wording differs, then report the final page title and "
        "first visible paragraph. Do not execute code or JavaScript."
    )

    assert plan is not None
    assert [action.action_type for action in plan.actions] == [
        ActionType.BROWSER_NAVIGATE,
        ActionType.BROWSER_CLICK_TEXT,
        ActionType.BROWSER_PAGE_INFO,
        ActionType.BROWSER_EXTRACT,
    ]
    assert plan.actions[0].parameters.url == "https://example.com"
    assert plan.actions[1].parameters.text == "More information"
    assert plan.actions[3].parameters.selector == "p"
    assert plan.actions[3].parameters.multiple is False


def test_underspecified_web_research_still_uses_the_model():
    assert Planner._try_fast_path("Research the latest browser automation tools and compare them") is None
