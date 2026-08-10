"""Speech-to-text using faster-whisper or Windows Speech API fallback."""

from __future__ import annotations

import logging
import tempfile
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger("zariff.stt")

_whisper_model = None


def _record_audio(duration: float = 5.0, sample_rate: int = 16000, device_index: int | None = None) -> Path:
    import sounddevice as sd

    frames = int(duration * sample_rate)
    audio = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32", device=device_index)
    sd.wait()
    path = Path(tempfile.gettempdir()) / "zariff_recording.wav"
    audio_int = (audio * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int.tobytes())
    return path


def _transcribe_whisper(path: Path, model_name: str = "base") -> str:
    global _whisper_model
    try:
        from faster_whisper import WhisperModel

        if _whisper_model is None:
            _whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _ = _whisper_model.transcribe(str(path), beam_size=5)
        return " ".join(s.text.strip() for s in segments).strip()
    except ImportError:
        logger.warning("faster-whisper not installed; using Windows SAPI fallback")
        return _transcribe_sapi()
    except Exception as e:
        logger.exception("Whisper transcription failed")
        raise RuntimeError(f"Speech recognition failed: {e}") from e


def _transcribe_sapi() -> str:
    try:
        import speech_recognition as sr

        r = sr.Recognizer()
        with sr.Microphone() as source:
            r.adjust_for_ambient_noise(source, duration=0.5)
            audio = r.listen(source, timeout=8, phrase_time_limit=12)
        return r.recognize_sphinx(audio)  # fully offline
    except ImportError:
        raise RuntimeError(
            "No STT engine available. Install: pip install faster-whisper  OR  pip install SpeechRecognition pocketsphinx"
        )
    except Exception as e:
        raise RuntimeError(f"Microphone capture failed: {e}") from e


def listen(duration: float = 5.0, model_name: str = "base", device_index: int | None = None) -> str:
    path = _record_audio(duration, device_index=device_index)
    text = _transcribe_whisper(path, model_name)
    path.unlink(missing_ok=True)
    return text


def listen_from_mic(model_name: str = "base", device_index: int | None = None) -> str:
    try:
        path = _record_audio(6.0, device_index=device_index)
        return _transcribe_whisper(path, model_name)
    except Exception:
        return _transcribe_sapi()


def check_microphone() -> tuple[bool, str]:
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        inputs = [d for d in devices if d.get("max_input_channels", 0) > 0]
        if inputs:
            return True, f"Found {len(inputs)} microphone(s)"
        return False, "No microphone detected"
    except Exception as e:
        return False, str(e)
