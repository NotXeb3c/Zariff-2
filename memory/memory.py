"""Persistent memory for Zariff."""

from __future__ import annotations

import json
import logging
from typing import Any

from config.settings import get_settings
from memory.database import get_connection, init_database

logger = logging.getLogger("zariff.memory")


def _enabled() -> bool:
    return get_settings().memory_enabled


def remember(key: str, value: str) -> dict[str, Any]:
    if not _enabled():
        return {"success": False, "message": "Memory is disabled in settings."}
    init_database()
    key = key.strip().lower()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM memories WHERE key = ?", (key,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE memories SET value = ?, updated_at = datetime('now') WHERE key = ?",
                (value, key),
            )
        else:
            conn.execute(
                "INSERT INTO memories (key, value) VALUES (?, ?)", (key, value)
            )
        conn.commit()
    return {"success": True, "message": f"Remembered: {key} = {value}"}


def recall(key: str) -> dict[str, Any]:
    if not _enabled():
        return {"success": False, "message": "Memory is disabled."}
    init_database()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM memories WHERE key = ?", (key.strip().lower(),)
        ).fetchone()
    if row:
        return {"success": True, "key": key, "value": row["value"]}
    return {"success": False, "message": f"No memory found for '{key}'."}


def forget(key: str) -> dict[str, Any]:
    if not _enabled():
        return {"success": False, "message": "Memory is disabled."}
    init_database()
    with get_connection() as conn:
        conn.execute("DELETE FROM memories WHERE key = ?", (key.strip().lower(),))
        conn.commit()
    return {"success": True, "message": f"Forgot memory about '{key}'."}


def search_memory(query: str) -> list[dict[str, str]]:
    if not _enabled():
        return []
    init_database()
    q = f"%{query.strip().lower()}%"
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT key, value FROM memories WHERE key LIKE ? OR value LIKE ? ORDER BY updated_at DESC",
            (q, q),
        ).fetchall()
    return [{"key": r["key"], "value": r["value"]} for r in rows]


def list_all_memories() -> list[dict[str, str]]:
    init_database()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT key, value, created_at, updated_at FROM memories ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def clear_all_memories() -> None:
    init_database()
    with get_connection() as conn:
        conn.execute("DELETE FROM memories")
        conn.commit()


def get_context_memories(limit: int = 10) -> str:
    if not _enabled():
        return ""
    init_database()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT key, value FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    if not rows:
        return ""
    lines = [f"- {r['key']}: {r['value']}" for r in rows]
    return "\n".join(lines)


def log_tool_call(name: str, parameters: dict, success: bool, result: str) -> None:
    init_database()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO tool_log (tool_name, parameters, success, result) VALUES (?, ?, ?, ?)",
            (name, json.dumps(parameters), int(success), result[:2000]),
        )
        conn.commit()
