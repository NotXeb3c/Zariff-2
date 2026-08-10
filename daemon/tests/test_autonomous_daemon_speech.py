"""Tests for AutonomousExecutor._announce_completion's daemon_speech pairing.

The end-of-job announcement speaks directly on the daemon's own OS audio
(pilot.system.voice.speak()), not the frontend's speechSynthesis -- without
a paired notification, the user gets zero visual trace of it. daemon_speech
must carry the exact spoken text and must be display-only (the frontend's
session.ts appends a chat bubble via addSystemMessage, it must NOT also
call speakText() for this notification, or the phrase would be spoken
twice).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pilot.agents.autonomous import AutonomousExecutor, AutonomousJob, JobStatus


def _executor() -> AutonomousExecutor:
    return AutonomousExecutor(planner=None, executor=None, verifier=None, decomposer=None)


class _Broadcast:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, method: str, params: dict) -> None:
        self.calls.append((method, params))


@pytest.mark.asyncio
async def test_success_speaks_and_broadcasts_matching_text():
    ex = _executor()
    broadcast = _Broadcast()
    ex.set_broadcast(broadcast)
    job = AutonomousJob(goal="do a thing", status=JobStatus.SUCCESS, result_summary="All 3 steps completed.")

    with patch("pilot.system.voice.speak", new=AsyncMock(return_value="")) as mock_speak:
        await ex._announce_completion(job)

    spoken_text = mock_speak.call_args.args[0]
    assert len(broadcast.calls) == 1
    method, params = broadcast.calls[0]
    assert method == "daemon_speech"
    assert params["text"] == spoken_text
    assert params["source"] == "autonomous_job"


@pytest.mark.asyncio
async def test_partial_and_failed_statuses_broadcast_their_own_text():
    ex = _executor()
    broadcast = _Broadcast()
    ex.set_broadcast(broadcast)

    with patch("pilot.system.voice.speak", new=AsyncMock(return_value="")):
        await ex._announce_completion(
            AutonomousJob(goal="x", status=JobStatus.PARTIAL, result_summary="2/3 steps succeeded.")
        )
        await ex._announce_completion(AutonomousJob(goal="x", status=JobStatus.FAILED, result_summary="boom"))

    assert "partially complete" in broadcast.calls[0][1]["text"].lower()
    assert "failed" in broadcast.calls[1][1]["text"].lower()


@pytest.mark.asyncio
async def test_no_broadcast_configured_is_a_safe_noop():
    ex = _executor()  # set_broadcast() never called
    job = AutonomousJob(goal="x", status=JobStatus.SUCCESS, result_summary="done")

    with patch("pilot.system.voice.speak", new=AsyncMock(return_value="")) as mock_speak:
        await ex._announce_completion(job)  # must not raise

    mock_speak.assert_awaited_once()


@pytest.mark.asyncio
async def test_speak_failure_does_not_propagate():
    ex = _executor()
    broadcast = _Broadcast()
    ex.set_broadcast(broadcast)
    job = AutonomousJob(goal="x", status=JobStatus.SUCCESS, result_summary="done")

    with patch("pilot.system.voice.speak", new=AsyncMock(side_effect=RuntimeError("no tts engine"))):
        await ex._announce_completion(job)  # must not raise

    assert broadcast.calls == []  # never reached the broadcast after speak() raised


@pytest.mark.asyncio
async def test_completion_uses_shared_priority_speech_when_wired():
    ex = _executor()
    speech = AsyncMock()
    ex.set_speech(speech)
    job = AutonomousJob(
        job_id="job-1",
        goal="x",
        status=JobStatus.FAILED,
        result_summary="boom",
    )

    with patch("pilot.system.voice.speak", new=AsyncMock()) as direct_speak:
        await ex._announce_completion(job)

    direct_speak.assert_not_awaited()
    speech.assert_awaited_once_with(
        "Task failed. boom",
        "task_failure",
        "autonomous:job-1:complete",
    )
