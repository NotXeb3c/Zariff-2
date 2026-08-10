"""First-run setup wizard."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from ai.ollama_provider import OllamaProvider
from config.settings import ensure_dirs
from memory.database import init_database
from voice import stt, tts


class SetupWizard(QDialog):
    def __init__(self, checks: list[tuple[str, bool, str]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ZARIFF — Setup")
        self.setMinimumSize(520, 420)
        layout = QVBoxLayout(self)
        title = QLabel("Z A R I F F   S E T U P")
        title.setObjectName("titleLabel")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        body = QTextEdit()
        body.setReadOnly(True)
        lines = []
        all_ok = True
        for name, ok, msg in checks:
            status = "OK" if ok else "MISSING"
            lines.append(f"[{status}] {name}: {msg}")
            if not ok:
                all_ok = False
        if not all_ok:
            lines.extend([
                "",
                "To fix missing items:",
                "1. Install Python 3.12+ from python.org",
                "2. Install Ollama from https://ollama.com",
                "3. Run: ollama pull qwen2.5:7b",
                "4. Run install.bat in the Zariff folder",
                "5. Optional STT: pip install faster-whisper",
            ])
        body.setPlainText("\n".join(lines))
        layout.addWidget(body)
        btn = QPushButton("Continue" if all_ok else "Continue Anyway")
        btn.clicked.connect(self.accept)
        layout.addWidget(btn)


def run_startup_checks() -> list[tuple[str, bool, str]]:
    ensure_dirs()
    init_database()
    checks: list[tuple[str, bool, str]] = []

    try:
        import PySide6  # noqa: F401
        checks.append(("Python dependencies", True, "Core packages loaded"))
    except ImportError as e:
        checks.append(("Python dependencies", False, str(e)))

    provider = OllamaProvider()
    ok, msg = provider.is_available()
    checks.append(("Ollama + model", ok, msg))

    mic_ok, mic_msg = stt.check_microphone()
    checks.append(("Microphone", mic_ok, mic_msg))

    tts_ok, tts_msg = tts.check_tts()
    checks.append(("Text-to-speech", tts_ok, tts_msg))

    checks.append(("Database", True, "SQLite initialized"))
    return checks
