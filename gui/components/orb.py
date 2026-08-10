"""Animated Zariff core orb."""

from __future__ import annotations

import math

from PySide6.QtCore import QTimer, Qt, QRectF
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from core.events import AgentState


class ZariffOrb(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(180, 180)
        self._state = AgentState.IDLE
        self._phase = 0.0
        self._pulse = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def set_state(self, state: AgentState) -> None:
        self._state = state
        self.update()

    def _tick(self) -> None:
        self._phase += 0.08
        speed = {
            AgentState.IDLE: 0.02,
            AgentState.LISTENING: 0.12,
            AgentState.THINKING: 0.15,
            AgentState.SPEAKING: 0.18,
            AgentState.EXECUTING: 0.14,
            AgentState.ERROR: 0.06,
            AgentState.SEARCHING: 0.1,
        }.get(self._state, 0.05)
        self._pulse = (self._pulse + speed) % (2 * math.pi)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        base_r = min(w, h) * 0.28
        pulse = 1.0 + 0.08 * math.sin(self._pulse)

        if self._state == AgentState.ERROR:
            ring_color = QColor("#888888")
        else:
            ring_color = QColor("#ffffff")

        # Outer rings
        for i in range(3):
            r = base_r * (1.4 + i * 0.22) * pulse
            alpha = 80 - i * 20
            pen = QPen(QColor(255, 255, 255, alpha), 1)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # Core
        core_r = base_r * pulse
        if self._state == AgentState.THINKING:
            p.save()
            p.translate(cx, cy)
            p.rotate(self._phase * 30)
            p.translate(-cx, -cy)
        grad_steps = 12
        for i in range(grad_steps):
            t = i / grad_steps
            gray = int(40 + 180 * (1 - t))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(gray, gray, gray)))
            rr = core_r * (1 - t * 0.5)
            p.drawEllipse(QRectF(cx - rr, cy - rr, rr * 2, rr * 2))
        if self._state == AgentState.THINKING:
            p.restore()

        # Inner highlight
        p.setPen(QPen(ring_color, 2))
        p.setBrush(QBrush(QColor(20, 20, 20)))
        p.drawEllipse(QRectF(cx - core_r, cy - core_r, core_r * 2, core_r * 2))

        # Listening waveform bars
        if self._state == AgentState.LISTENING:
            for i in range(-4, 5):
                bar_h = 8 + 20 * abs(math.sin(self._phase * 2 + i * 0.5))
                x = cx + i * 12
                p.fillRect(int(x - 2), int(cy + core_r + 16), 4, int(bar_h), QColor("#cccccc"))

        p.end()
