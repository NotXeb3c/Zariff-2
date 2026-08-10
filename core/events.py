"""Global event bus for Zariff."""

from __future__ import annotations

from enum import Enum, auto
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal


class AgentState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    EXECUTING = auto()
    ERROR = auto()
    SEARCHING = auto()


class EventBus(QObject):
    state_changed = Signal(object)
    message_added = Signal(str, str)
    tool_started = Signal(str)
    tool_finished = Signal(str, bool, str)
    confirmation_requested = Signal(str, str, object)
    stats_updated = Signal(dict)
    log_line = Signal(str)
    web_search_status = Signal(str)
    error_occurred = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._state = AgentState.IDLE

    @property
    def state(self) -> AgentState:
        return self._state

    def set_state(self, state: AgentState) -> None:
        self._state = state
        self.state_changed.emit(state)


bus = EventBus()
