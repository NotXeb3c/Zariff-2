"""Sidebar navigation."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QPushButton, QVBoxLayout, QLabel

NAV_ITEMS = [
    ("home", "◉ HOME"),
    ("conversations", "💬 CONVERSATIONS"),
    ("memory", "🧠 MEMORY"),
    ("tools", "🛠 TOOLS"),
    ("system", "📊 SYSTEM"),
    ("settings", "⚙ SETTINGS"),
]


class Sidebar(QFrame):
    navigated = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(200)
        layout = QVBoxLayout(self)
        logo = QLabel("ZARIFF")
        logo.setStyleSheet("font-size: 18px; letter-spacing: 6px; padding: 20px; color: #ffffff;")
        layout.addWidget(logo)
        self._buttons: dict[str, QPushButton] = {}
        for key, label in NAV_ITEMS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, k=key: self._on_nav(k))
            layout.addWidget(btn)
            self._buttons[key] = btn
        layout.addStretch()
        self._buttons["home"].setChecked(True)

    def _on_nav(self, key: str) -> None:
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
        self.navigated.emit(key)

    def set_active(self, key: str) -> None:
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
