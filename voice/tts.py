"""Local text-to-speech via Windows SAPI (pyttsx3)."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("zariff.tts")

_engine = None
_lock = threading.Lock()
_speaking = False


def _get_engine():
    global _engine
    with _lock:
        if _engine is None:
            import pyttsx3

            _engine = pyttsx3.init()
        return _engine


def speak(text: str, rate: int = 175, volume: float = 1.0, blocking: bool = True) -> None:
    global _speaking
    if not text.strip():
        return
    engine = _get_engine()
    engine.setProperty("rate", rate)
    engine.setProperty("volume", max(0.0, min(1.0, volume)))
    _speaking = True
    try:
        engine.say(text)
        if blocking:
            engine.runAndWait()
        else:
            engine.startLoop(False)
            engine.iterate()
            engine.endLoop()
    except Exception as e:
        logger.exception("TTS failed")
        raise RuntimeError(f"Text-to-speech failed: {e}") from e
    finally:
        _speaking = False


def is_speaking() -> bool:
    return _speaking


def list_voices() -> list[str]:
    try:
        engine = _get_engine()
        return [v.name for v in engine.getProperty("voices")]
    except Exception:
        return []


def check_tts() -> tuple[bool, str]:
    try:
        _get_engine()
        voices = list_voices()
        return True, f"TTS ready ({len(voices)} voice(s))"
    except Exception as e:
        return False, str(e)
