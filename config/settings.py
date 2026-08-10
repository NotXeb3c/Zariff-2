"""Zariff configuration management."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
CONFIG_DIR = PROJECT_ROOT / "config"
APPLICATIONS_FILE = CONFIG_DIR / "applications.json"
USER_SETTINGS_FILE = DATA_DIR / "user_settings.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ollama_url: str = Field(default="http://127.0.0.1:11434", alias="OLLAMA_URL")
    ollama_model: str = Field(default="qwen2.5:7b", alias="OLLAMA_MODEL")
    ollama_temperature: float = Field(default=0.7, alias="OLLAMA_TEMPERATURE")
    ollama_context: int = Field(default=8192, alias="OLLAMA_CONTEXT")

    whisper_model: str = Field(default="base", alias="WHISPER_MODEL")
    wake_word_enabled: bool = Field(default=False, alias="WAKE_WORD_ENABLED")
    wake_word: str = "hey zariff"

    web_search_enabled: bool = Field(default=True, alias="WEB_SEARCH_ENABLED")

    memory_enabled: bool = True
    tool_confirmations: bool = True
    shell_access: bool = False
    allowed_directories: list[str] = Field(default_factory=lambda: [
        str(Path.home() / "Downloads"),
        str(Path.home() / "Documents"),
        str(Path.home() / "Desktop"),
    ])

    theme: str = "retro_bw"
    accent_color: str = "#ffffff"
    animation_intensity: float = 1.0
    transparency: float = 0.95
    always_on_top: bool = False

    tts_rate: int = 175
    tts_volume: float = 1.0
    microphone_index: int | None = None


_settings: Settings | None = None


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        ensure_dirs()
        _settings = Settings()
        if USER_SETTINGS_FILE.exists():
            data = json.loads(USER_SETTINGS_FILE.read_text(encoding="utf-8"))
            _settings = Settings(**{**_settings.model_dump(), **data})
    return _settings


def save_settings(updates: dict[str, Any]) -> Settings:
    global _settings
    ensure_dirs()
    current = get_settings().model_dump()
    current.update(updates)
    USER_SETTINGS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
    _settings = Settings(**current)
    return _settings


def load_applications() -> dict[str, str]:
    if not APPLICATIONS_FILE.exists():
        return {}
    return json.loads(APPLICATIONS_FILE.read_text(encoding="utf-8"))


def save_applications(apps: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    APPLICATIONS_FILE.write_text(json.dumps(apps, indent=2), encoding="utf-8")


def detect_common_apps() -> dict[str, str]:
    """Best-effort detection of common Windows applications."""
    candidates = {
        "Discord": [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Discord" / "Update.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Discord" / "app-*" / "Discord.exe",
        ],
        "Steam": [
            Path("C:/Program Files (x86)/Steam/steam.exe"),
            Path("C:/Program Files/Steam/steam.exe"),
        ],
        "Chrome": [
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        ],
        "Firefox": [
            Path("C:/Program Files/Mozilla Firefox/firefox.exe"),
        ],
        "Minecraft": [
            Path(os.environ.get("APPDATA", "")) / ".minecraft" / "launcher" / "minecraft.exe",
        ],
    }
    found: dict[str, str] = {}
    for name, paths in candidates.items():
        for p in paths:
            if "*" in str(p):
                parent = p.parent
                if parent.exists():
                    matches = list(parent.parent.glob(p.name))
                    if matches:
                        found[name] = str(matches[0])
                        break
            elif p.exists():
                found[name] = str(p)
                break
    return found


def merge_applications() -> dict[str, str]:
    apps = load_applications()
    detected = detect_common_apps()
    for name, path in detected.items():
        if not apps.get(name):
            apps[name] = path
    save_applications(apps)
    return apps
