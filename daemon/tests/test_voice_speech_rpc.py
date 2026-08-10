from unittest.mock import AsyncMock, MagicMock

import pytest

from pilot.config import PilotConfig
from pilot.server import PilotServer
from pilot.system.companion_speech import SpeechChannel, SpeechOutcome


@pytest.mark.asyncio
async def test_speak_text_uses_daemon_voice_engine(monkeypatch):
    speak = AsyncMock(return_value="Spoken: hello...")
    monkeypatch.setattr("pilot.system.voice.speak", speak)
    server = PilotServer(PilotConfig())

    result = await server._handle_speak_text({"text": "  hello  "}, MagicMock())

    speak.assert_awaited_once_with("hello")
    assert result == {"status": "spoken", "message": "Spoken: hello..."}
    await server._speech_coordinator.close()


@pytest.mark.asyncio
async def test_superseded_speech_still_returns_a_terminal_rpc_response(monkeypatch):
    server = PilotServer(PilotConfig())
    server._speak_companion_text = AsyncMock(
        return_value=SpeechOutcome(
            "superseded",
            SpeechChannel.FINAL_ANSWER,
            "Speech superseded",
        )
    )

    result = await server._handle_speak_text({"text": "old message"}, MagicMock())

    assert result == {"status": "superseded", "message": "Speech superseded"}


@pytest.mark.asyncio
async def test_barge_in_returns_interrupted_terminal_status():
    server = PilotServer(PilotConfig())
    server._speak_companion_text = AsyncMock(
        return_value=SpeechOutcome(
            "interrupted",
            SpeechChannel.FINAL_ANSWER,
            "Speech interrupted by user",
        )
    )

    result = await server._handle_speak_text({"text": "hello"}, MagicMock())

    assert result == {
        "status": "interrupted",
        "message": "Speech interrupted by user",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", " ", None, 42])
async def test_speak_text_rejects_invalid_text(text):
    server = PilotServer(PilotConfig())

    result = await server._handle_speak_text({"text": text}, MagicMock())

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_speak_text_rejects_oversized_text():
    server = PilotServer(PilotConfig())

    result = await server._handle_speak_text({"text": "x" * 4001}, MagicMock())

    assert result["status"] == "error"
    assert "4000" in result["message"]


@pytest.mark.asyncio
async def test_stop_speech_stops_daemon_playback(monkeypatch):
    stop_speaking = AsyncMock(return_value="Speech stopped")
    monkeypatch.setattr("pilot.system.voice.stop_speaking", stop_speaking)
    server = PilotServer(PilotConfig())

    result = await server._handle_stop_speech({}, MagicMock())

    stop_speaking.assert_awaited_once_with()
    assert result == {"status": "stopped", "message": "Speech stopped", "cancelled": 0}


@pytest.mark.asyncio
async def test_speak_text_passes_priority_channel_and_dedupe_key():
    server = PilotServer(PilotConfig())
    server._speak_companion_text = AsyncMock(
        return_value=SpeechOutcome(
            "spoken",
            SpeechChannel.APPROVAL_RISK,
            "Speech completed",
        )
    )

    result = await server._handle_speak_text(
        {
            "text": "Approval required",
            "channel": "approval_risk",
            "dedupe_key": "plan-1",
        },
        MagicMock(),
    )

    assert result["status"] == "spoken"
    server._speak_companion_text.assert_awaited_once_with(
        "Approval required",
        channel=SpeechChannel.APPROVAL_RISK,
        dedupe_key="plan-1",
    )


@pytest.mark.asyncio
async def test_speech_status_exposes_single_audio_authority():
    server = PilotServer(PilotConfig())

    result = await server._handle_companion_speech_status({}, MagicMock())

    assert result == {
        "status": "ok",
        "active": False,
        "active_channel": "",
        "queued": 0,
        "spoken": 0,
        "preemptions": 0,
        "duplicates_suppressed": 0,
    }
