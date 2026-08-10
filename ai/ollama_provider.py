"""Ollama AI provider."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ai.provider import AIProvider, AIResponse, ChatMessage
from config.settings import get_settings

logger = logging.getLogger("zariff.ai")


class OllamaProvider(AIProvider):
    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.ollama_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = httpx.Timeout(120.0, connect=10.0)

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def is_available(self) -> tuple[bool, str]:
        try:
            with self._client() as client:
                r = client.get("/api/tags")
                if r.status_code != 200:
                    return False, f"Ollama returned status {r.status_code}"
                models = [m.get("name", "") for m in r.json().get("models", [])]
                if not any(self.model.split(":")[0] in m for m in models):
                    return False, (
                        f"Model '{self.model}' not found. "
                        f"Run: ollama pull {self.model}"
                    )
                return True, "Ollama is running"
        except httpx.ConnectError:
            return False, "Ollama is not running. Install from https://ollama.com and run 'ollama serve'"
        except Exception as e:
            logger.exception("Ollama availability check failed")
            return False, str(e)

    def list_models(self) -> list[str]:
        try:
            with self._client() as client:
                r = client.get("/api/tags")
                r.raise_for_status()
                return [m.get("name", "") for m in r.json().get("models", [])]
        except Exception:
            return []

    def chat(
        self,
        messages: list[ChatMessage],
        system_prompt: str,
        temperature: float | None = None,
    ) -> AIResponse:
        settings = get_settings()
        temp = temperature if temperature is not None else settings.ollama_temperature
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "options": {
                "temperature": temp,
                "num_ctx": settings.ollama_context,
            },
            "messages": [{"role": "system", "content": system_prompt}]
            + [{"role": m.role, "content": m.content} for m in messages],
        }
        with self._client() as client:
            r = client.post("/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        content = data.get("message", {}).get("content", "")
        return AIResponse(content=content.strip(), raw=data)
