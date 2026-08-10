"""User Manual Supervision — keyboard/mouse hook, the feature's privacy boundary.

Installs a global (OS-level) keyboard + mouse listener via `pynput` so
`UserSupervisionEngine` can watch the user's own independent activity for
risk-pattern matches and activity-rate signals. This module is where the
privacy contract lives, not just a design note elsewhere:

  - Raw keystrokes are buffered ONLY in-memory, in a small bounded deque,
    purely to be joined into a short-lived local string and pattern-matched
    by `pilot.security.risk_patterns.match_risk_pattern()`. That string is
    NEVER logged, returned, or persisted -- only the matched pattern's NAME
    (or None) ever leaves `snapshot()`.
  - Mouse clicks are counted, never located -- click coordinates are never
    read, buffered, or stored anywhere in this module.
  - `start()`/`stop()` install/remove the OS-level hook; this module never
    calls either on its own -- the caller (server.py, gated by
    `config.supervision.keyboard_mouse_hook_enabled`) owns that decision.

See SECURITY.md's "User Manual Supervision" section for the full privacy
contract and known scope limits (UAC/elevation blind spot, AV/EDR risk,
silent hook death, Windows-only verified).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

from pilot.security.risk_patterns import match_risk_pattern

logger = logging.getLogger("pilot.system.input_hook")


@dataclass
class InputActivitySnapshot:
    """A point-in-time read of activity since the last snapshot() call."""

    keystroke_rate_per_min: float
    click_rate_per_min: float
    matched_pattern: str | None
    hook_healthy: bool


class InputSupervisionHook:
    """Wraps pynput's keyboard/mouse listeners behind the privacy boundary
    described in this module's docstring. Safe to construct even when
    `pynput` isn't installed -- the import only happens inside start()."""

    def __init__(self, keystroke_buffer_max_chars: int = 256) -> None:
        self._lock = threading.Lock()
        self._char_buffer: deque[str] = deque(maxlen=keystroke_buffer_max_chars)
        self._keystroke_count = 0
        self._click_count = 0
        self._last_snapshot_time = time.monotonic()

        self._keyboard_listener: object | None = None
        self._mouse_listener: object | None = None
        self._import_failed = False

    def start(self) -> None:
        """Install the OS-level keyboard + mouse hook. No-op if already
        running. Only ever called when the user has opted into
        `keyboard_mouse_hook_enabled` -- see this module's docstring."""
        if self._keyboard_listener is not None or self._import_failed:
            return

        try:
            from pynput import keyboard, mouse
        except ImportError:
            logger.warning(
                "pynput not installed -- keyboard/mouse hook cannot start. "
                "Install with: pip install pilot-daemon[supervision]"
            )
            self._import_failed = True
            return

        with self._lock:
            self._last_snapshot_time = time.monotonic()

        self._keyboard_listener = keyboard.Listener(on_press=self._on_key_press)
        self._mouse_listener = mouse.Listener(on_click=self._on_click)
        self._keyboard_listener.start()
        self._mouse_listener.start()
        logger.info("User Manual Supervision keyboard/mouse hook installed")

    def stop(self) -> None:
        """Uninstall the hook. Safe to call even if never started."""
        if self._keyboard_listener is not None:
            self._keyboard_listener.stop()
            self._keyboard_listener = None
        if self._mouse_listener is not None:
            self._mouse_listener.stop()
            self._mouse_listener = None
        with self._lock:
            self._char_buffer.clear()
            self._keystroke_count = 0
            self._click_count = 0
        logger.info("User Manual Supervision keyboard/mouse hook removed")

    def _on_key_press(self, key: object) -> None:
        """OS hook callback -- must stay O(1) and fast, or Windows will
        silently remove the hook. Does exactly one thing: buffer a
        character, nothing else."""
        char = getattr(key, "char", None)
        if char is None:
            # Special keys (Key.space/Key.enter/...) -- keep just enough to
            # preserve word boundaries for pattern matching; anything else
            # (arrows, modifiers, function keys) is dropped.
            name = getattr(key, "name", "")
            if name in ("space", "enter", "tab"):
                char = " "
            else:
                return
        with self._lock:
            self._char_buffer.append(char)
            self._keystroke_count += 1

    def _on_click(self, x: object, y: object, button: object, pressed: bool) -> None:
        """OS hook callback. Deliberately ignores x/y -- click coordinates
        are never read into this module at all."""
        if not pressed:
            return
        with self._lock:
            self._click_count += 1

    def snapshot(self) -> InputActivitySnapshot:
        """Blocking -- call via asyncio.to_thread. Computes rates, matches
        the buffered text against the risk-pattern table, then immediately
        discards the raw text regardless of match result."""
        with self._lock:
            now = time.monotonic()
            elapsed_s = max(now - self._last_snapshot_time, 1e-6)
            keystroke_rate = (self._keystroke_count / elapsed_s) * 60.0
            click_rate = (self._click_count / elapsed_s) * 60.0

            text = "".join(self._char_buffer)
            matched = match_risk_pattern(text)

            self._char_buffer.clear()
            self._keystroke_count = 0
            self._click_count = 0
            self._last_snapshot_time = now

        return InputActivitySnapshot(
            keystroke_rate_per_min=keystroke_rate,
            click_rate_per_min=click_rate,
            matched_pattern=matched,
            hook_healthy=self.is_running(),
        )

    def is_running(self) -> bool:
        """Best-effort liveness check. Windows silently removes a hook whose
        callback is too slow, with no in-process exception -- this can only
        detect that the listener thread itself died, not a silent OS-level
        unhook. See SECURITY.md's Known scope limits."""
        if self._keyboard_listener is None or self._mouse_listener is None:
            return False
        keyboard_alive = getattr(self._keyboard_listener, "is_alive", lambda: False)()
        mouse_alive = getattr(self._mouse_listener, "is_alive", lambda: False)()
        return bool(keyboard_alive and mouse_alive)
