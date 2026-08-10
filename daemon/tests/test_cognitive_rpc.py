from unittest.mock import AsyncMock, MagicMock

import pytest

from pilot.cognitive.cognitive_engine import CognitiveSnapshot
from pilot.config import PilotConfig
from pilot.server import PilotServer


@pytest.mark.asyncio
async def test_cognitive_state_records_bounded_browser_dynamics():
    server = PilotServer(PilotConfig())
    engine = MagicMock()
    engine.predict_cognitive_state = AsyncMock(
        return_value=CognitiveSnapshot(
            attention_score=0.62,
            stress_level=0.2,
            cognitive_load=0.45,
            confidence=0.5,
            raw_activations={"signal_sources": 1},
        )
    )
    server._cognitive_engine = engine

    result = await server._handle_cognitive_state(
        {
            "input_dynamics": {
                "keystroke_rate_per_min": 75,
                "click_rate_per_min": 8,
                "pointer_move_rate_per_min": 140,
                "idle_seconds": 1.5,
            }
        },
        MagicMock(),
    )

    engine.record_input_dynamics.assert_called_once_with(
        keystroke_rate_per_min=75.0,
        click_rate_per_min=8.0,
        pointer_move_rate_per_min=140.0,
        idle_seconds=1.5,
    )
    assert result["attention_score"] == 0.62
    assert result["estimate_kind"] == "behavioral"
    assert result["signal_sources"] == 1


@pytest.mark.asyncio
async def test_cognitive_state_rejects_non_finite_metrics():
    server = PilotServer(PilotConfig())
    engine = MagicMock()
    server._cognitive_engine = engine

    result = await server._handle_cognitive_state(
        {"input_dynamics": {"keystroke_rate_per_min": float("nan")}},
        MagicMock(),
    )

    assert result == {"error": "keystroke_rate_per_min must be finite"}
    engine.record_input_dynamics.assert_not_called()


@pytest.mark.asyncio
async def test_cognitive_state_clamps_untrusted_ui_rates():
    server = PilotServer(PilotConfig())
    engine = MagicMock()
    engine.predict_cognitive_state = AsyncMock(return_value=CognitiveSnapshot())
    server._cognitive_engine = engine

    await server._handle_cognitive_state(
        {
            "input_dynamics": {
                "keystroke_rate_per_min": 99_999,
                "click_rate_per_min": 99_999,
                "pointer_move_rate_per_min": 99_999,
                "idle_seconds": 99_999,
            }
        },
        MagicMock(),
    )

    engine.record_input_dynamics.assert_called_once_with(
        keystroke_rate_per_min=1200.0,
        click_rate_per_min=600.0,
        pointer_move_rate_per_min=1200.0,
        idle_seconds=3600.0,
    )
