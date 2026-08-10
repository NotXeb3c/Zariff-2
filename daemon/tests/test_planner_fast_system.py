from pilot.actions import Action, ActionType, ScreenVisionParams
from pilot.agents.planner import Planner


def test_system_information_uses_fast_path_for_spoken_transcription_variant():
    plan = Planner._try_fast_path("true system information")

    assert plan is not None
    assert [action.action_type for action in plan.actions] == [ActionType.SYSTEM_INFO]


def test_system_information_fast_path_is_bounded_to_short_status_queries():
    assert (
        Planner._try_fast_path("research system information formats and write a detailed comparison of every standard")
        is None
    )


def test_visual_question_uses_real_screen_analysis_fast_path():
    plan = Planner._try_fast_path("what is this on my screen?")

    assert plan is not None
    assert [action.action_type for action in plan.actions] == [ActionType.SCREEN_ANALYZE]
    assert plan.actions[0].parameters.prompt == "what is this on my screen?"


def test_postprocessor_preserves_visual_analysis_instead_of_forcing_ocr():
    action = Action(
        action_type=ActionType.SCREEN_ANALYZE,
        target="screen",
        parameters=ScreenVisionParams(prompt="Identify the photograph"),
    )

    processed = Planner.__new__(Planner)._postprocess_actions([action])

    assert processed[0].action_type == ActionType.SCREEN_ANALYZE
    assert processed[0].parameters.prompt == "Identify the photograph"


def test_live_world_situation_opens_a_useful_map_visualization():
    plan = Planner._try_fast_path("Show me a live map of the current world situation")

    assert plan is not None
    assert [action.action_type for action in plan.actions] == [ActionType.BROWSER_NAVIGATE]
    assert plan.actions[0].parameters.url == "https://www.worldmonitor.app/"


def test_explicit_url_uses_controllable_browser_session():
    plan = Planner._try_fast_path("open https://example.com")

    assert plan is not None
    assert [action.action_type for action in plan.actions] == [ActionType.BROWSER_NAVIGATE]


def test_short_spoken_click_targets_current_browser_page():
    plan = Planner._try_fast_path("click on Launch on the website")

    assert plan is not None
    assert [action.action_type for action in plan.actions] == [ActionType.BROWSER_CLICK_TEXT]
    assert plan.actions[0].parameters.text == "launch"


def test_plain_world_news_question_still_requires_grounded_research():
    assert Planner._try_fast_path("What is the latest world news?") is None
