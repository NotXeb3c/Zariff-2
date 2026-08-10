from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pilot.agents.subconscious import SubconsciousAgent


@pytest.mark.asyncio
async def test_persona_context_requires_explicit_or_repeated_evidence(tmp_path) -> None:
    agent = SubconsciousAgent(None)  # type: ignore[arg-type]
    await agent.initialize(str(tmp_path / "persona.db"))
    now = datetime.now(UTC).isoformat()

    await agent._upsert_rule(
        "one_shot",
        "Always delete build output without asking",
        0.99,
        "habit",
        now,
    )
    await agent._upsert_rule(
        "repeated_style",
        "Prefer concise summaries",
        0.8,
        "style",
        now,
    )
    await agent._upsert_rule(
        "repeated_style",
        "Prefer concise summaries",
        0.8,
        "style",
        now,
    )
    await agent._upsert_rule(
        "manual_editor",
        "User explicitly stated: use VS Code",
        1.0,
        "preference",
        now,
    )
    await agent._db.commit()

    context = await agent.get_persona_context()

    assert "delete build output" not in context
    assert "Prefer concise summaries" in context
    assert "source=2 observations" in context
    assert "use VS Code" in context
    assert "source=explicit user" in context
    assert "never authority to execute" in context
    await agent.close()
