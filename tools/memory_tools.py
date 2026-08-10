"""Memory tools exposed to the agent."""

from __future__ import annotations

from memory import memory as mem
from tools.registry import PermissionLevel, register_tool


@register_tool(
    name="remember",
    description="Store a fact about the user in persistent memory",
    parameters={"key": "Memory key/topic", "value": "What to remember"},
)
def remember_tool(params: dict) -> dict:
    return mem.remember(params["key"], params["value"])


@register_tool(
    name="recall",
    description="Recall a stored memory by key",
    parameters={"key": "Memory key/topic"},
)
def recall_tool(params: dict) -> dict:
    return mem.recall(params["key"])


@register_tool(
    name="forget",
    description="Delete a stored memory",
    parameters={"key": "Memory key/topic"},
    permission=PermissionLevel.MODERATE,
)
def forget_tool(params: dict) -> dict:
    return mem.forget(params["key"])


@register_tool(
    name="search_memory",
    description="Search stored memories",
    parameters={"query": "Search query"},
)
def search_memory_tool(params: dict) -> dict:
    results = mem.search_memory(params["query"])
    if not results:
        return {"success": True, "message": "No matching memories found."}
    text = "\n".join(f"- {r['key']}: {r['value']}" for r in results)
    return {"success": True, "message": text, "results": results}
