"""Natural local text-to-speech using the open-weight Kokoro model.

Kokoro is optional at import time. The voice dispatcher falls back to the
platform's native TTS if its package, weights, or audio device are unavailable.
Model inference and playback stay off the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

SAMPLE_RATE = 24000
_pipeline_cache: dict[str, Any] = {}
_pipeline_lock = threading.Lock()


def _get_pipeline(language: str = "a") -> Any:
    with _pipeline_lock:
        if language not in _pipeline_cache:
            from kokoro import KPipeline

            _pipeline_cache[language] = KPipeline(
                lang_code=language,
                repo_id="hexgrad/Kokoro-82M",
            )
        return _pipeline_cache[language]


def _to_numpy(audio: Any) -> Any:
    value = audio.detach().cpu() if hasattr(audio, "detach") else audio
    return value.numpy() if hasattr(value, "numpy") else value


def _generate(text: str, voice: str) -> tuple[Any, int]:
    import numpy as np

    pipeline = _get_pipeline()
    chunks = [
        _to_numpy(audio)
        for _graphemes, _phonemes, audio in pipeline(
            text,
            voice=voice,
            speed=1.0,
            split_pattern=r"\n+",
        )
    ]
    if not chunks:
        raise RuntimeError("Kokoro produced no audio")
    return np.concatenate(chunks), SAMPLE_RATE


async def warmup(_voice: str) -> None:
    """Load model weights before the first spoken intervention."""
    await asyncio.to_thread(_get_pipeline)


async def synthesize(text: str, voice: str) -> tuple[Any, int]:
    return await asyncio.to_thread(_generate, text, voice)


def _play_blocking(audio: Any, sample_rate: int) -> None:
    import sounddevice as sd

    sd.play(audio, sample_rate)
    sd.wait()


async def play(audio: Any, sample_rate: int) -> None:
    try:
        await asyncio.to_thread(_play_blocking, audio, sample_rate)
    except asyncio.CancelledError:
        import sounddevice as sd

        sd.stop()
        raise


async def synthesize_and_play(text: str, voice: str) -> None:
    audio, sample_rate = await synthesize(text, voice)
    await play(audio, sample_rate)


async def synthesize_to_file(text: str, voice: str, output_file: str) -> None:
    audio, sample_rate = await synthesize(text, voice)

    def _write() -> None:
        import soundfile

        soundfile.write(output_file, audio, sample_rate)

    await asyncio.to_thread(_write)
