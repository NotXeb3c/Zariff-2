"""Tests for speak()'s supersede-in-progress-speech behavior: calling
speak() again while a previous call is still playing cancels the old one
immediately instead of overlapping with or queuing behind it -- the
daemon-side counterpart to tts.ts's speechSynthesis.cancel()-before-every-
speak on the frontend. Mirrors test_voice_barge_in.py's patch.object(voice,
...) style; the actual cancellation-kills-the-OS-subprocess behavior is
already covered by that file's test_run_command_kills_subprocess_on_cancellation
regression test and is untouched by this change.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from pilot.system import voice


@pytest.mark.asyncio
async def test_second_call_cancels_first_in_flight_call():
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()

    async def _slow_impl(text, *args, **kwargs):
        first_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            first_cancelled.set()
            raise
        return "first"

    with patch.object(voice, "_speak_impl", new=_slow_impl):
        first_task = asyncio.create_task(voice.speak("first text"))
        await first_started.wait()

        with patch.object(voice, "_speak_impl", new=AsyncMock(return_value="second")):
            result = await voice.speak("second text")

        assert result == "second"
        assert first_cancelled.is_set()

        with pytest.raises(asyncio.CancelledError):
            await first_task


@pytest.mark.asyncio
async def test_sequential_non_overlapping_calls_do_not_cancel_each_other():
    with patch.object(voice, "_speak_impl", new=AsyncMock(return_value="ok")) as mock_impl:
        first = await voice.speak("first")
        second = await voice.speak("second")

    assert first == "ok"
    assert second == "ok"
    assert mock_impl.await_count == 2
    assert voice._current_speech_task is None


@pytest.mark.asyncio
async def test_current_speech_task_cleared_after_completion():
    with patch.object(voice, "_speak_impl", new=AsyncMock(return_value="ok")):
        await voice.speak("hello")

    assert voice._current_speech_task is None


@pytest.mark.asyncio
async def test_current_speech_task_cleared_after_being_superseded():
    first_started = asyncio.Event()

    async def _slow_impl(text, *args, **kwargs):
        first_started.set()
        await asyncio.sleep(10)
        return "first"

    with patch.object(voice, "_speak_impl", new=_slow_impl):
        first_task = asyncio.create_task(voice.speak("first"))
        await first_started.wait()

        with patch.object(voice, "_speak_impl", new=AsyncMock(return_value="second")):
            await voice.speak("second")

        assert voice._current_speech_task is None

        with pytest.raises(asyncio.CancelledError):
            await first_task


@pytest.mark.asyncio
async def test_no_prior_speech_supersede_is_a_safe_noop():
    assert voice._current_speech_task is None
    with patch.object(voice, "_speak_impl", new=AsyncMock(return_value="ok")):
        result = await voice.speak("hello")
    assert result == "ok"


@pytest.mark.asyncio
async def test_new_plain_speak_call_supersedes_an_in_flight_speak_interruptible():
    """Composition check: speak_interruptible() internally calls speak(),
    so a second, unrelated speak() call elsewhere in the daemon must still
    supersede it -- there is only one voice output device."""
    first_started = asyncio.Event()

    async def _slow_impl(text, *args, **kwargs):
        first_started.set()
        await asyncio.sleep(10)
        return "first"

    class _NeverInterruptingRecorder:
        is_active = True

        async def wait_for_speech_start(self, timeout=None) -> bool:
            await asyncio.sleep(30)
            return True

    with patch.object(voice, "_speak_impl", new=_slow_impl):
        interruptible_task = asyncio.create_task(
            voice.speak_interruptible("first", recorder=_NeverInterruptingRecorder())
        )
        await first_started.wait()

        with patch.object(voice, "_speak_impl", new=AsyncMock(return_value="second")):
            result = await voice.speak("second")

        assert result == "second"

        # The superseded speak_interruptible() call resolves on its own,
        # via its internal speak() task completing (with the cancellation),
        # which satisfies asyncio.wait(..., FIRST_COMPLETED) -- no extra
        # cleanup needed by the caller. It reports False (not interrupted
        # by user speech specifically), which is correct: that return
        # value encodes barge-in, a different concept from being
        # superseded by another speak() call.
        interrupted = await asyncio.wait_for(interruptible_task, timeout=1.0)
        assert interrupted is False
