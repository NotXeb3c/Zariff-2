"""Mouse & Keyboard Control — full desktop automation.

Cross-platform input simulation using pyautogui.
Click, type, drag, scroll, hotkeys — control ANY application.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("pilot.system.input_control")


def _ensure_pyautogui():
    """Lazy import pyautogui with safety settings."""
    import pyautogui

    pyautogui.FAILSAFE = True  # Move mouse to corner to abort
    pyautogui.PAUSE = 0.05  # Small pause between actions
    return pyautogui


# ── Mouse ────────────────────────────────────────────────────────────


async def mouse_click(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.1,
) -> str:
    """Click at screen coordinates."""
    pag = _ensure_pyautogui()

    def _do():
        pag.click(x=x, y=y, button=button, clicks=clicks, interval=interval)

    await asyncio.to_thread(_do)
    return f"Clicked ({button}, {clicks}x) at ({x}, {y})"


async def mouse_double_click(x: int, y: int) -> str:
    """Double-click at coordinates."""
    return await mouse_click(x, y, clicks=2)


async def mouse_right_click(x: int, y: int) -> str:
    """Right-click at coordinates."""
    return await mouse_click(x, y, button="right")


async def mouse_move(
    x: int,
    y: int,
    duration: float = 0.3,
    relative: bool = False,
) -> str:
    """Move mouse to coordinates (or relative offset)."""
    pag = _ensure_pyautogui()

    def _do():
        if relative:
            pag.moveRel(x, y, duration=duration)
        else:
            pag.moveTo(x, y, duration=duration)

    await asyncio.to_thread(_do)
    mode = "relative" if relative else "absolute"
    return f"Moved mouse to ({x}, {y}) [{mode}]"


async def mouse_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration: float = 0.5,
    button: str = "left",
) -> str:
    """Drag from one position to another."""
    pag = _ensure_pyautogui()

    def _do():
        pag.moveTo(start_x, start_y, duration=0.1)
        pag.drag(end_x - start_x, end_y - start_y, duration=duration, button=button)

    await asyncio.to_thread(_do)
    return f"Dragged from ({start_x},{start_y}) to ({end_x},{end_y})"


async def mouse_scroll(
    amount: int,
    x: int | None = None,
    y: int | None = None,
    horizontal: bool = False,
) -> str:
    """Scroll the mouse wheel. Positive = up, negative = down."""
    pag = _ensure_pyautogui()

    def _do():
        if x is not None and y is not None:
            pag.moveTo(x, y)
        if horizontal:
            pag.hscroll(amount)
        else:
            pag.scroll(amount)

    await asyncio.to_thread(_do)
    direction = "horizontal" if horizontal else "vertical"
    return f"Scrolled {direction} by {amount}"


async def mouse_position() -> str:
    """Get current mouse position."""
    pag = _ensure_pyautogui()
    pos = pag.position()
    return f"Mouse at ({pos.x}, {pos.y})"


# ── Keyboard ─────────────────────────────────────────────────────────


async def keyboard_type(
    text: str,
    interval: float = 0.03,
    *,
    target_window: str | None = None,
) -> str:
    """Enter exact text at the current cursor position.

    Key-by-key synthesis is affected by Caps Lock and the active keyboard
    layout. Prefer a clipboard paste for deterministic case and Unicode, then
    restore the user's prior clipboard value immediately afterward.
    """
    pag = _ensure_pyautogui()

    if target_window:
        try:
            from pilot.system.window_mgr import window_set_text

            await window_set_text(text, title=target_window)
            preview = text[:80] + "..." if len(text) > 80 else text
            return f"Typed exactly in {target_window}: {preview}"
        except Exception as error:
            logger.info(
                "Background text entry unavailable for %r; attempting verified foreground entry: %s",
                target_window,
                error,
            )
            from pilot.system.window_mgr import window_focus

            # Fail closed if Windows refuses to re-acquire the requested app;
            # never paste into whichever unrelated window happens to be active.
            await window_focus(title=target_window)

    try:
        import pyperclip

        def _paste_exact() -> None:
            previous = pyperclip.paste()
            try:
                pyperclip.copy(text)
                pag.hotkey("ctrl", "v")
                # Give the foreground application time to consume WM_PASTE
                # before restoring the clipboard that belongs to the user.
                time.sleep(0.05)
            finally:
                pyperclip.copy(previous)

        await asyncio.to_thread(_paste_exact)
        preview = text[:80] + "..." if len(text) > 80 else text
        return f"Typed exactly: {preview}"
    except (ImportError, RuntimeError):
        logger.debug("Exact clipboard typing unavailable; using key synthesis", exc_info=True)

    def _do() -> None:
        pag.typewrite(text, interval=interval) if text.isascii() else pag.write(text)

    await asyncio.to_thread(_do)
    preview = text[:80] + "..." if len(text) > 80 else text
    return f"Typed: {preview}"


async def keyboard_press(key: str, presses: int = 1) -> str:
    """Press a single key (enter, tab, escape, f1, etc.)."""
    pag = _ensure_pyautogui()
    await asyncio.to_thread(lambda: pag.press(key, presses=presses))
    return f"Pressed '{key}' x{presses}"


async def keyboard_hotkey(*keys: str) -> str:
    """Press a keyboard shortcut (e.g., ctrl+c, alt+tab, win+d).

    Examples: ('ctrl', 'c'), ('alt', 'tab'), ('win', 'd'), ('ctrl', 'shift', 'esc')
    """
    pag = _ensure_pyautogui()
    await asyncio.to_thread(lambda: pag.hotkey(*keys))
    return f"Pressed hotkey: {'+'.join(keys)}"


async def keyboard_hold(key: str, duration: float = 0.5) -> str:
    """Hold a key down for a duration."""
    _ensure_pyautogui()

    def _do():
        import pyautogui

        with pyautogui.hold(key):
            time.sleep(duration)

    await asyncio.to_thread(_do)
    return f"Held '{key}' for {duration}s"


# ── Screen Info ──────────────────────────────────────────────────────


async def screen_size() -> str:
    """Get screen resolution."""
    pag = _ensure_pyautogui()
    w, h = pag.size()
    return f"Screen resolution: {w}x{h}"


async def pixel_color(x: int, y: int) -> str:
    """Get the color of a pixel at coordinates."""
    pag = _ensure_pyautogui()
    r, g, b = pag.pixel(x, y)
    return f"Pixel at ({x},{y}): RGB({r},{g},{b}) / #{r:02x}{g:02x}{b:02x}"


async def locate_on_screen(image_path: str, confidence: float = 0.8) -> str:
    """Find an image on screen (template matching).

    Returns the center coordinates if found.
    """
    pag = _ensure_pyautogui()

    def _do():
        try:
            location = pag.locateOnScreen(image_path, confidence=confidence)
            if location:
                center = pag.center(location)
                return f"Found at ({center.x}, {center.y}), region: {location}"
            return "Image not found on screen"
        except Exception as e:
            return f"Image search failed: {e}"

    return await asyncio.to_thread(_do)
