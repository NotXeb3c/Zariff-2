"""Reactive Triggers / Event System — background watchers.

Monitors system events and executes actions when conditions are met.
File watchers, performance monitors, scheduled checks, custom triggers.
"""

from __future__ import annotations

import asyncio
import ast
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("pilot.system.triggers")


# Calls restricted to these safe read-only psutil/time functions. Functions
# like psutil.Popen (shell launch) or Process(pid).kill() are intentionally
# excluded: the expression is untrusted (it can come from an LLM-produced
# trigger_create action) and must never be able to execute system effects.
_SAFE_TRIGGER_CALLS: dict[str, set[str]] = {
    "psutil": {
        "cpu_percent",
        "cpu_count",
        "cpu_freq",
        "cpu_stats",
        "virtual_memory",
        "swap_memory",
        "disk_usage",
        "disk_partitions",
        "net_io_counters",
        "net_connections",
        "boot_time",
        "users",
        "pids",
        "process_iter",
        "sensors_temperatures",
        "sensors_battery",
        "sensors_fans",
    },
    "time": {"time", "localtime", "gmtime", "strftime", "sleep", "timezone", "altzone", "daylight"},
}

_SAFE_TRIGGER_NAMES = {"psutil", "time"}


def _evaluate_condition_expression(expr: str, psutil: Any, time_mod: Any) -> bool:
    """Evaluate a trigger condition against a strict AST whitelist.

    ``eval`` with stripped builtins is not a sandbox: attribute traversal from
    any module object can recover ``__import__`` and reach ``os.system``. This
    evaluator instead rejects every node that is not a constant, a comparison,
    a boolean/numeric/arithmetic combination, or an attribute chain rooted at
    ``psutil``/``time`` whose final call target is in ``_SAFE_TRIGGER_CALLS``.
    """

    def _check(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            _check(node.body)
            return
        if isinstance(node, ast.Constant):
            if node.value is not None and not isinstance(node.value, (bool, int, float, str)):
                raise ValueError(f"unsupported constant {node.value!r}")
            return
        if isinstance(node, ast.Name):
            if node.id not in _SAFE_TRIGGER_NAMES:
                raise ValueError(f"unsupported name {node.id!r}")
            return
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ValueError(f"attribute {node.attr!r} is not allowed")
            _check(node.value)
            return
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Attribute):
                raise ValueError("calls must target psutil/time attributes")
            root = node.func.value
            if isinstance(root, ast.Attribute):
                raise ValueError("chained nested calls are not allowed")
            if not isinstance(root, ast.Name) or root.id not in _SAFE_TRIGGER_NAMES:
                raise ValueError("call root must be psutil or time")
            if node.func.attr not in _SAFE_TRIGGER_CALLS.get(root.id, set()):
                raise ValueError(f"function {root.id}.{node.func.attr}() is not allowed")
            for arg in node.args:
                _check(arg)
            for kw in node.keywords:
                _check(kw.value)
            return
        if isinstance(node, ast.Compare):
            _check(node.left)
            for op in node.ops:
                if not isinstance(
                    op,
                    (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot),
                ):
                    raise ValueError("unsupported comparison operator")
            for comparator in node.comparators:
                _check(comparator)
            return
        if isinstance(node, ast.BoolOp):
            if not isinstance(node.op, (ast.And, ast.Or)):
                raise ValueError("unsupported boolean operator")
            for value in node.values:
                _check(value)
            return
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
                raise ValueError("unsupported unary operator")
            _check(node.operand)
            return
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)):
                raise ValueError("unsupported binary operator")
            _check(node.left)
            _check(node.right)
            return
        raise ValueError(f"unsupported expression node {type(node).__name__}")

    try:
        tree = ast.parse(expr, mode="eval")
        _check(tree)
        result = eval(
            compile(tree, "<trigger>", "eval"),
            {"__builtins__": {}, "psutil": psutil, "time": time_mod},
        )
        return bool(result)
    except Exception as e:
        logger.warning("Custom condition eval error: %s", e)
        return False


class TriggerType(StrEnum):
    FILE_CREATED = "file_created"
    FILE_MODIFIED = "file_modified"
    FILE_DELETED = "file_deleted"
    CPU_THRESHOLD = "cpu_threshold"
    MEMORY_THRESHOLD = "memory_threshold"
    DISK_THRESHOLD = "disk_threshold"
    BATTERY_LOW = "battery_low"
    PROCESS_STARTED = "process_started"
    PROCESS_STOPPED = "process_stopped"
    TIME_INTERVAL = "time_interval"
    CRON_SCHEDULE = "cron_schedule"
    NETWORK_CHANGE = "network_change"
    CUSTOM_CONDITION = "custom_condition"


@dataclass
class Trigger:
    id: str
    name: str
    trigger_type: TriggerType
    condition: dict[str, Any]  # Type-specific condition params
    action_command: str  # Natural language command to execute
    enabled: bool = True
    fire_count: int = 0
    max_fires: int = 0  # 0 = unlimited
    cooldown_seconds: int = 60
    last_fired: float = 0
    created_at: str = ""

    def can_fire(self) -> bool:
        if not self.enabled:
            return False
        if self.max_fires > 0 and self.fire_count >= self.max_fires:
            return False
        return not time.time() - self.last_fired < self.cooldown_seconds


class TriggerEngine:
    """Manages and evaluates reactive triggers in the background."""

    def __init__(self) -> None:
        self._triggers: dict[str, Trigger] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._fire_callback: Callable[[Trigger], Coroutine] | None = None
        self._file_cache: dict[str, dict[str, float]] = {}  # path -> {file: mtime}

    def set_fire_callback(self, callback: Callable[[Trigger], Coroutine]) -> None:
        """Set callback that runs when a trigger fires."""
        self._fire_callback = callback

    # ── CRUD ─────────────────────────────────────────────────────────

    def create_trigger(
        self,
        name: str,
        trigger_type: str,
        condition: dict,
        action_command: str,
        max_fires: int = 0,
        cooldown_seconds: int = 60,
    ) -> Trigger:
        """Create a new trigger."""
        trigger = Trigger(
            id=str(uuid.uuid4())[:8],
            name=name,
            trigger_type=TriggerType(trigger_type),
            condition=condition,
            action_command=action_command,
            max_fires=max_fires,
            cooldown_seconds=cooldown_seconds,
            created_at=datetime.now().isoformat(),
        )
        self._triggers[trigger.id] = trigger
        logger.info("Created trigger: %s (%s)", name, trigger.id)
        return trigger

    def delete_trigger(self, trigger_id: str) -> bool:
        if trigger_id in self._triggers:
            del self._triggers[trigger_id]
            return True
        # Try by name
        for tid, t in list(self._triggers.items()):
            if t.name == trigger_id:
                del self._triggers[tid]
                return True
        return False

    def list_triggers(self) -> list[dict]:
        return [asdict(t) for t in self._triggers.values()]

    def get_trigger(self, trigger_id: str) -> Trigger | None:
        return self._triggers.get(trigger_id)

    # ── Engine Control ───────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background trigger evaluation loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Trigger engine started with %d triggers", len(self._triggers))

    async def stop(self) -> None:
        """Stop the trigger engine."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("Trigger engine stopped")

    async def _run_loop(self) -> None:
        """Main evaluation loop — checks all triggers every 5 seconds."""
        while self._running:
            try:
                for trigger in list(self._triggers.values()):
                    if not trigger.can_fire():
                        continue
                    try:
                        fired = await self._evaluate(trigger)
                        if fired:
                            trigger.fire_count += 1
                            trigger.last_fired = time.time()
                            logger.info(
                                "Trigger fired: %s (%s) — fire #%d",
                                trigger.name,
                                trigger.id,
                                trigger.fire_count,
                            )
                            if self._fire_callback:
                                asyncio.create_task(self._fire_callback(trigger))
                    except Exception as e:
                        logger.warning("Trigger %s eval error: %s", trigger.id, e)

                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break

    # ── Evaluation ───────────────────────────────────────────────────

    async def _evaluate(self, trigger: Trigger) -> bool:
        """Evaluate a trigger's condition. Returns True if condition is met."""
        tt = trigger.trigger_type
        cond = trigger.condition

        if tt == TriggerType.CPU_THRESHOLD:
            return await self._check_cpu(cond)
        elif tt == TriggerType.MEMORY_THRESHOLD:
            return await self._check_memory(cond)
        elif tt == TriggerType.DISK_THRESHOLD:
            return await self._check_disk(cond)
        elif tt == TriggerType.BATTERY_LOW:
            return await self._check_battery(cond)
        elif tt in (TriggerType.FILE_CREATED, TriggerType.FILE_MODIFIED, TriggerType.FILE_DELETED):
            return await self._check_file(trigger)
        elif tt == TriggerType.PROCESS_STARTED:
            return await self._check_process_exists(cond, expect=True)
        elif tt == TriggerType.PROCESS_STOPPED:
            return await self._check_process_exists(cond, expect=False)
        elif tt == TriggerType.TIME_INTERVAL:
            # Already handled by cooldown_seconds
            return True
        elif tt == TriggerType.CUSTOM_CONDITION:
            return await self._check_custom(cond)

        return False

    async def _check_cpu(self, condition: dict) -> bool:
        try:
            import psutil

            threshold = condition.get("threshold", 90)
            cpu = psutil.cpu_percent(interval=1)
            return cpu > threshold
        except ImportError:
            return False

    async def _check_memory(self, condition: dict) -> bool:
        try:
            import psutil

            threshold = condition.get("threshold", 90)
            mem = psutil.virtual_memory()
            return mem.percent > threshold
        except ImportError:
            return False

    async def _check_disk(self, condition: dict) -> bool:
        try:
            import psutil

            threshold = condition.get("threshold", 95)
            path = condition.get("path", "/")
            usage = psutil.disk_usage(path)
            return usage.percent > threshold
        except ImportError:
            return False

    async def _check_battery(self, condition: dict) -> bool:
        try:
            import psutil

            threshold = condition.get("threshold", 20)
            batt = psutil.sensors_battery()
            if batt is None:
                return False
            return batt.percent < threshold and not batt.power_plugged
        except ImportError:
            return False

    async def _check_file(self, trigger: Trigger) -> bool:
        watch_path = trigger.condition.get("path", "")
        if not watch_path or not os.path.exists(watch_path):
            return False

        p = Path(watch_path)
        current_files: dict[str, float] = {}

        if p.is_dir():
            pattern = trigger.condition.get("pattern", "*")
            for f in p.glob(pattern):
                if f.is_file():
                    with contextlib.suppress(OSError):
                        current_files[str(f)] = f.stat().st_mtime
        elif p.is_file():
            with contextlib.suppress(OSError):
                current_files[str(p)] = p.stat().st_mtime

        cache_key = trigger.id
        previous = self._file_cache.get(cache_key, {})
        self._file_cache[cache_key] = current_files

        if not previous:
            return False  # First check, establish baseline

        if trigger.trigger_type == TriggerType.FILE_CREATED:
            new_files = set(current_files.keys()) - set(previous.keys())
            return len(new_files) > 0

        elif trigger.trigger_type == TriggerType.FILE_MODIFIED:
            return any(f in previous and mtime > previous[f] for f, mtime in current_files.items())

        elif trigger.trigger_type == TriggerType.FILE_DELETED:
            deleted = set(previous.keys()) - set(current_files.keys())
            return len(deleted) > 0

        return False

    async def _check_process_exists(self, condition: dict, expect: bool) -> bool:
        try:
            import psutil

            name = condition.get("name", "")
            for proc in psutil.process_iter(["name"]):
                if name.lower() in proc.info["name"].lower():
                    return expect  # Found AND we're looking for "started"
            return not expect  # Not found AND we're looking for "stopped"
        except ImportError:
            return False

    async def _check_custom(self, condition: dict) -> bool:
        """Evaluate a custom Python expression."""
        expr = condition.get("expression", "")
        if not expr:
            return False
        try:
            import psutil
        except ImportError:
            psutil = None

        return _evaluate_condition_expression(expr, psutil, time)


# ── Global engine instance ───────────────────────────────────────────

_engine = TriggerEngine()


async def trigger_create(
    name: str,
    trigger_type: str,
    condition: dict,
    action_command: str,
    max_fires: int = 0,
    cooldown_seconds: int = 60,
) -> str:
    """Create a reactive trigger."""
    trigger = _engine.create_trigger(
        name,
        trigger_type,
        condition,
        action_command,
        max_fires,
        cooldown_seconds,
    )

    # Auto-start engine if not running
    if not _engine._running:
        await _engine.start()

    return json.dumps(asdict(trigger), indent=2)


async def trigger_list() -> str:
    """List all active triggers."""
    triggers = _engine.list_triggers()
    if not triggers:
        return "No triggers configured"
    return json.dumps(triggers, indent=2)


async def trigger_delete(trigger_id: str) -> str:
    """Delete a trigger by ID or name."""
    if _engine.delete_trigger(trigger_id):
        return f"Deleted trigger: {trigger_id}"
    return f"Trigger not found: {trigger_id}"


async def trigger_start_engine() -> str:
    """Start the trigger evaluation engine."""
    await _engine.start()
    return f"Trigger engine started with {len(_engine._triggers)} triggers"


async def trigger_stop_engine() -> str:
    """Stop the trigger evaluation engine."""
    await _engine.stop()
    return "Trigger engine stopped"


def get_engine() -> TriggerEngine:
    """Access the global trigger engine instance."""
    return _engine
