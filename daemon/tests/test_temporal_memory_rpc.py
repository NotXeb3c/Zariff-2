from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pilot.config import PilotConfig
from pilot.server import PilotServer


@pytest.mark.asyncio
async def test_temporal_memory_status_returns_reviewable_state() -> None:
    server = PilotServer(PilotConfig())
    server._memory = AsyncMock()
    server._memory.temporal_status.return_value = {
        "available": True,
        "facts": [{"fact_id": "fact-1", "status": "candidate"}],
        "counts": {"facts": {"candidate": 1}, "episodes": 3, "working_items": 1},
    }

    result = await server._handle_temporal_memory_status({"limit": 25}, ws=None)

    assert result["status"] == "ok"
    assert result["facts"][0]["fact_id"] == "fact-1"
    server._memory.temporal_status.assert_awaited_once_with(limit=25)


@pytest.mark.asyncio
async def test_temporal_memory_retract_requires_fact_id() -> None:
    server = PilotServer(PilotConfig())
    server._memory = AsyncMock()

    result = await server._handle_temporal_memory_retract({}, ws=None)

    assert result == {"status": "error", "message": "fact_id is required"}


@pytest.mark.asyncio
async def test_temporal_memory_retract_reports_result() -> None:
    server = PilotServer(PilotConfig())
    server._memory = AsyncMock()
    server._memory.retract_fact.return_value = SimpleNamespace(
        fact_id="fact-1",
        status=SimpleNamespace(value="retracted"),
    )

    result = await server._handle_temporal_memory_retract(
        {"fact_id": "fact-1", "reason": "wrong preference"},
        ws=None,
    )

    assert result == {
        "status": "ok",
        "fact_id": "fact-1",
        "fact_status": "retracted",
    }
