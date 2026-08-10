"""Optional wake word detection via transcript prefix matching."""

from __future__ import annotations

from config.settings import get_settings


def strip_wake_word(text: str) -> tuple[bool, str]:
    settings = get_settings()
    if not settings.wake_word_enabled:
        return False, text
    wake = settings.wake_word.lower()
    t = text.lower().strip()
    if t.startswith(wake):
        remainder = text[len(wake) :].strip(" ,.!")
        return True, remainder
    return False, text


def contains_wake_word(text: str) -> bool:
    settings = get_settings()
    if not settings.wake_word_enabled:
        return True
    return settings.wake_word.lower() in text.lower()
