"""System information and control tools."""

from __future__ import annotations

import datetime
import platform
import subprocess

import psutil

from tools.registry import PermissionLevel, register_tool


@register_tool(
    name="get_system_info",
    description="Get CPU, RAM, disk, and process information",
    parameters={},
)
def get_system_info(params: dict) -> dict:
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    top = sorted(
        psutil.process_iter(["name", "memory_info"]),
        key=lambda p: p.info["memory_info"].rss if p.info.get("memory_info") else 0,
        reverse=True,
    )[:5]
    top_ram = []
    for p in top:
        try:
            mb = p.info["memory_info"].rss / (1024 * 1024)
            top_ram.append(f"{p.info['name']}: {mb:.0f} MB")
        except Exception:
            pass
    gpu_info = _get_gpu_info()
    msg = (
        f"CPU: {cpu}%\n"
        f"RAM: {mem.percent}% ({mem.used // (1024**3)}GB / {mem.total // (1024**3)}GB)\n"
        f"Disk: {disk.percent}% used\n"
        f"OS: {platform.system()} {platform.release()}\n"
        f"Top RAM: {', '.join(top_ram)}\n"
        f"GPU: {gpu_info}"
    )
    return {"success": True, "message": msg}


def _get_gpu_info() -> str:
    try:
        import wmi  # type: ignore

        c = wmi.WMI()
        gpus = c.Win32_VideoController()
        if gpus:
            return gpus[0].Name or "Unknown"
    except Exception:
        pass
    return "Not detected (install wmi or check drivers)"


@register_tool(
    name="get_time",
    description="Get the current time",
    parameters={},
)
def get_time(params: dict) -> dict:
    now = datetime.datetime.now().strftime("%I:%M %p")
    return {"success": True, "message": now}


@register_tool(
    name="get_date",
    description="Get today's date",
    parameters={},
)
def get_date(params: dict) -> dict:
    today = datetime.datetime.now().strftime("%A, %B %d, %Y")
    return {"success": True, "message": today}


@register_tool(
    name="control_volume",
    description="Adjust system volume up, down, mute, or set level",
    parameters={"action": "up|down|mute|unmute|set", "level": "0-100 for set (optional)"},
)
def control_volume(params: dict) -> dict:
    action = params.get("action", "up").lower()
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        if action == "mute":
            volume.SetMute(1, None)
            return {"success": True, "message": "Volume muted."}
        if action == "unmute":
            volume.SetMute(0, None)
            return {"success": True, "message": "Volume unmuted."}
        current = volume.GetMasterVolumeLevelScalar()
        if action == "up":
            volume.SetMasterVolumeLevelScalar(min(1.0, current + 0.1), None)
            return {"success": True, "message": "Volume up."}
        if action == "down":
            volume.SetMasterVolumeLevelScalar(max(0.0, current - 0.1), None)
            return {"success": True, "message": "Volume down."}
        if action == "set" and "level" in params:
            level = max(0, min(100, int(params["level"]))) / 100.0
            volume.SetMasterVolumeLevelScalar(level, None)
            return {"success": True, "message": f"Volume set to {int(level * 100)}%."}
    except Exception as e:
        return {"success": False, "message": f"Volume control failed: {e}"}
    return {"success": False, "message": "Unknown volume action."}


@register_tool(
    name="lock_computer",
    description="Lock the Windows session",
    parameters={},
    permission=PermissionLevel.MODERATE,
    requires_confirmation=True,
)
def lock_computer(params: dict) -> dict:
    subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False)
    return {"success": True, "message": "Computer locked."}
