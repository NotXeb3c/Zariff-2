"""Tool router with confirmation and validation."""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable

from config.settings import get_settings
from core.events import AgentState, bus
from memory.memory import log_tool_call
from tools.registry import ToolRegistry, registry

logger = logging.getLogger("zariff.router")

_confirmation_lock = threading.Lock()
_pending_confirmations: dict[str, threading.Event] = {}
_confirmation_results: dict[str, bool] = {}


def parse_tool_calls(text: str) -> list[dict[str, Any]] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r'\{\s*"tools"\s*:', text, re.DOTALL)
    if not match:
        match = re.search(r'\{\s*"tool"\s*:', text, re.DOTALL)
    if not match:
        return None
    start = text.find("{", match.start())
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start : i + 1])
                    if "tools" in data:
                        return data["tools"]
                    if "tool" in data:
                        return [{"name": data["tool"], "parameters": data.get("parameters", {})}]
                    if "name" in data:
                        return [data]
                except json.JSONDecodeError:
                    return None
                break
    return None


def strip_tool_json(text: str) -> str:
    cleaned = re.sub(r'\{[\s\S]*?"tools"[\s\S]*?\}', "", text).strip()
    cleaned = re.sub(r'\{[\s\S]*?"tool"[\s\S]*?\}', "", cleaned).strip()
    return cleaned or text


class ToolRouter:
    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self.registry = tool_registry or registry

    def request_confirmation(self, tool_name: str, description: str) -> bool:
        settings = get_settings()
        if not settings.tool_confirmations:
            return True
        event = threading.Event()
        token = f"{tool_name}_{threading.get_ident()}"
        with _confirmation_lock:
            _pending_confirmations[token] = event
        result_holder: list[bool] = [False]

        def on_result(confirmed: bool) -> None:
            result_holder[0] = confirmed
            event.set()

        bus.confirmation_requested.emit(tool_name, description, on_result)
        event.wait(timeout=120)
        with _confirmation_lock:
            _pending_confirmations.pop(token, None)
        return result_holder[0]

    def execute(
        self,
        name: str,
        parameters: dict[str, Any],
        confirm_callback: Callable[[str, str], bool] | None = None,
    ) -> dict[str, Any]:
        tool = self.registry.get(name)
        if not tool:
            return {"success": False, "message": f"Unknown tool: {name}"}

        err = self.registry.validate_parameters(tool, parameters)
        if err:
            return {"success": False, "message": err}

        if tool.requires_confirmation:
            confirm = confirm_callback or self.request_confirmation
            desc = f"Execute {name} with {parameters}?"
            if not confirm(name, desc):
                return {"success": False, "message": "Action cancelled by user."}

        bus.set_state(AgentState.EXECUTING)
        bus.tool_started.emit(name)
        logger.info("Tool call: %s %s", name, parameters)
        try:
            result = tool.handler(parameters)
            success = bool(result.get("success", True))
            msg = result.get("message", str(result))
            log_tool_call(name, parameters, success, msg)
            bus.tool_finished.emit(name, success, msg)
            return result
        except Exception as e:
            logger.exception("Tool %s failed", name)
            log_tool_call(name, parameters, False, str(e))
            bus.tool_finished.emit(name, False, str(e))
            return {"success": False, "message": str(e)}
        finally:
            bus.set_state(AgentState.IDLE)

    def execute_batch(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for call in calls:
            name = call.get("name") or call.get("tool")
            params = call.get("parameters") or call.get("params") or {}
            if name:
                results.append(self.execute(name, params))
        return results
