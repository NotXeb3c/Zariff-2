"""Screenshot tool."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from config.settings import DATA_DIR
from tools.registry import register_tool


@register_tool(
    name="take_screenshot",
    description="Capture a screenshot and save to the data folder",
    parameters={},
)
def take_screenshot(params: dict) -> dict:
    try:
        import mss
        from PIL import Image

        out_dir = DATA_DIR / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"screenshot_{ts}.png"
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            img.save(str(path))
        return {"success": True, "message": f"Screenshot saved to {path}."}
    except Exception as e:
        return {"success": False, "message": f"Screenshot failed: {e}"}
