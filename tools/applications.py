"""Application launcher tools."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from config.settings import load_applications, merge_applications, save_applications
from tools.registry import PermissionLevel, register_tool


def _resolve_app(name: str) -> str | None:
    apps = merge_applications()
    key = name.strip()
    for k, v in apps.items():
        if k.lower() == key.lower() and v:
            return v
    return None


@register_tool(
    name="open_application",
    description="Open a registered application by name (Discord, Chrome, Steam, etc.)",
    parameters={"name": "Application name"},
)
def open_application(params: dict) -> dict:
    name = params["name"]
    path = _resolve_app(name)
    if not path:
        return {
            "success": False,
            "message": f"{name} isn't configured. Add it in Settings > Applications.",
        }
    if not Path(path).exists() and not path.endswith(".exe"):
        return {"success": False, "message": f"{name} isn't installed at the configured location."}
    try:
        if path.lower() in ("explorer.exe", "notepad.exe", "calc.exe"):
            subprocess.Popen([path], shell=False)
        else:
            os.startfile(path)  # type: ignore[attr-defined]
        return {"success": True, "message": f"Opened {name}."}
    except Exception as e:
        return {"success": False, "message": f"Could not open {name}: {e}"}


@register_tool(
    name="close_application",
    description="Close an application by process name",
    parameters={"name": "Process or app name"},
    permission=PermissionLevel.MODERATE,
    requires_confirmation=True,
)
def close_application(params: dict) -> dict:
    import psutil

    name = params["name"].lower().replace(".exe", "")
    closed = []
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if name in proc.info["name"].lower():
                proc.terminate()
                closed.append(proc.info["name"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if closed:
        return {"success": True, "message": f"Closed: {', '.join(closed)}"}
    return {"success": False, "message": f"No running process found for {params['name']}."}


@register_tool(
    name="open_folder",
    description="Open a folder in File Explorer",
    parameters={"path": "Folder path"},
)
def open_folder(params: dict) -> dict:
    path = Path(params["path"]).expanduser()
    if not path.exists():
        return {"success": False, "message": f"Folder not found: {path}"}
    os.startfile(str(path))  # type: ignore[attr-defined]
    return {"success": True, "message": f"Opened {path.name}."}


@register_tool(
    name="launch_game",
    description="Launch a registered game/application",
    parameters={"name": "Game name"},
)
def launch_game(params: dict) -> dict:
    return open_application(params)
