"""AI provider abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class AIResponse:
    content: str
    raw: dict[str, Any] = field(default_factory=dict)


class AIProvider(ABC):
    @abstractmethod
    def chat(
        self,
        messages: list[ChatMessage],
        system_prompt: str,
        temperature: float | None = None,
    ) -> AIResponse:
        ...

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        ...

    @abstractmethod
    def list_models(self) -> list[str]:
        ...
