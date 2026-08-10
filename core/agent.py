"""Zariff agent orchestrator."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ai.ollama_provider import OllamaProvider
from ai.provider import AIProvider, ChatMessage
from config.settings import get_settings
from core.events import AgentState, bus
from core.prompts import build_system_prompt
from core.router import ToolRouter, parse_tool_calls, strip_tool_json
from memory.database import get_connection, init_database
from memory.memory import get_context_memories, search_memory
from tools.registry import registry

logger = logging.getLogger("zariff.agent")


class ConversationStore:
    def __init__(self) -> None:
        init_database()
        self.conversation_id: int | None = None

    def new_conversation(self, title: str = "New Conversation") -> int:
        with get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO conversations (title) VALUES (?)", (title,)
            )
            conn.commit()
            self.conversation_id = cur.lastrowid
            return self.conversation_id  # type: ignore

    def load(self, conversation_id: int) -> list[ChatMessage]:
        self.conversation_id = conversation_id
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        return [ChatMessage(role=r["role"], content=r["content"]) for r in rows]

    def save_message(self, role: str, content: str) -> None:
        if self.conversation_id is None:
            self.new_conversation()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
                (self.conversation_id, role, content),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = datetime('now') WHERE id = ?",
                (self.conversation_id,),
            )
            conn.commit()

    def list_conversations(self) -> list[dict[str, Any]]:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def rename(self, cid: int, title: str) -> None:
        with get_connection() as conn:
            conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, cid))
            conn.commit()

    def delete(self, cid: int) -> None:
        with get_connection() as conn:
            conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
            conn.commit()


class Agent:
    def __init__(self, provider: AIProvider | None = None) -> None:
        self.provider = provider or OllamaProvider()
        self.router = ToolRouter()
        self.store = ConversationStore()
        self.history: list[ChatMessage] = []
        self.store.new_conversation()

    def _build_system_prompt(self, user_text: str) -> str:
        memories = get_context_memories()
        query_mem = search_memory(user_text)
        if query_mem:
            extra = "\n".join(f"- {m['key']}: {m['value']}" for m in query_mem[:5])
            memories = (memories + "\n" + extra).strip()
        return build_system_prompt(registry.descriptions(), memories)

    def _maybe_direct_tool(self, text: str) -> list[dict] | None:
        """Fast path for common natural-language commands."""
        t = text.lower().strip()
        wake = get_settings().wake_word
        if wake and t.startswith(wake):
            t = t[len(wake) :].strip(" ,")

        open_match = re.match(r"^(?:open|launch|start)\s+(.+)$", t, re.I)
        if open_match:
            name = open_match.group(1).strip(".")
            if " and " in name:
                apps = [a.strip() for a in name.split(" and ")]
                return [{"name": "open_application", "parameters": {"name": a}} for a in apps]
            return [{"name": "open_application", "parameters": {"name": name}}]

        close_match = re.match(r"^close\s+(.+)$", t, re.I)
        if close_match:
            return [{"name": "close_application", "parameters": {"name": close_match.group(1).strip()}}]

        yt_match = re.match(r"^(?:search youtube for|youtube search)\s+(.+)$", t, re.I)
        if yt_match:
            return [{"name": "search_youtube", "parameters": {"query": yt_match.group(1).strip()}}]

        search_match = re.match(r"^(?:search(?: the web)? for|google)\s+(.+)$", t, re.I)
        if search_match:
            return [{"name": "search_web", "parameters": {"query": search_match.group(1).strip()}}]

        if "ram" in t or "cpu" in t or "system info" in t or "gpu" in t:
            return [{"name": "get_system_info", "parameters": {}}]

        if "screenshot" in t:
            return [{"name": "take_screenshot", "parameters": {}}]

        if t.startswith("remember that"):
            rest = text.split("remember that", 1)[-1].strip()
            if " is " in rest.lower():
                key, val = rest.split(" is ", 1)
                return [{"name": "remember", "parameters": {"key": key.strip(), "value": val.strip()}}]

        if "what do you remember" in t:
            return [{"name": "search_memory", "parameters": {"query": ""}}]

        vol_down = re.match(r"^(?:turn )?(?:the )?volume down", t)
        if vol_down:
            return [{"name": "control_volume", "parameters": {"action": "down"}}]
        vol_up = re.match(r"^(?:turn )?(?:the )?volume up", t)
        if vol_up:
            return [{"name": "control_volume", "parameters": {"action": "up"}}]

        return None

    def process(self, user_text: str) -> str:
        user_text = user_text.strip()
        if not user_text:
            return ""

        bus.set_state(AgentState.THINKING)
        self.store.save_message("user", user_text)
        self.history.append(ChatMessage(role="user", content=user_text))
        bus.message_added.emit("user", user_text)

        tool_calls = self._maybe_direct_tool(user_text)
        response_text = ""

        if not tool_calls:
            try:
                system = self._build_system_prompt(user_text)
                ai_resp = self.provider.chat(self.history[-20:], system)
                response_text = ai_resp.content
                tool_calls = parse_tool_calls(response_text)
                if tool_calls:
                    response_text = strip_tool_json(response_text)
            except Exception as e:
                logger.exception("AI request failed")
                bus.set_state(AgentState.ERROR)
                msg = f"I'm having trouble connecting to Ollama. {e}"
                bus.error_occurred.emit(msg)
                return msg

        if tool_calls:
            results = self.router.execute_batch(tool_calls)
            summaries = [r.get("message", "") for r in results if r.get("message")]
            if response_text:
                final = response_text
            elif len(summaries) == 1:
                final = summaries[0]
            else:
                final = " ".join(summaries)
        else:
            final = response_text or "Done."

        self.history.append(ChatMessage(role="assistant", content=final))
        self.store.save_message("assistant", final)
        bus.message_added.emit("assistant", final)
        bus.set_state(AgentState.IDLE)
        return final
