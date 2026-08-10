"""Isolated child-process runner for native Python plugins.

The daemon never imports plugin Python. This process receives a single JSON
request, installs fail-closed capability guards, executes one tool, emits one
JSON response, and exits.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import inspect
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


class CapabilityDenied(PermissionError):
    """Raised when plugin code attempts an undeclared capability."""


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    return any(resolved == root or root in resolved.parents for root in roots)


def _install_guards(request: dict[str, Any]) -> None:
    capabilities = request["capabilities"]
    plugin_dir = Path(request["plugin_dir"]).resolve()
    runtime_roots = tuple(
        Path(item).resolve() for item in {sys.base_prefix, sys.prefix, *sys.path} if item and Path(item).exists()
    )
    read_roots = (
        plugin_dir,
        *runtime_roots,
        *(Path(item).expanduser().resolve(strict=False) for item in capabilities["filesystem"]["read"]),
    )
    write_roots = tuple(Path(item).expanduser().resolve(strict=False) for item in capabilities["filesystem"]["write"])
    allowed_domains = set(capabilities["network_domains"])
    allowed_addresses: set[str] = set()
    allowed_processes = {item.lower() for item in capabilities["processes"]}

    original_open = builtins.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, int):
            return original_open(file, mode, *args, **kwargs)
        path = Path(os.fspath(file))
        mutating = any(flag in mode for flag in ("w", "a", "x", "+"))
        roots = write_roots if mutating else read_roots
        if not _within(path, roots):
            operation = "write" if mutating else "read"
            raise CapabilityDenied(f"filesystem {operation} denied: {path}")
        return original_open(file, mode, *args, **kwargs)

    builtins.open = guarded_open

    original_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        hostname = str(host).lower().rstrip(".")
        if hostname not in allowed_domains:
            raise CapabilityDenied(f"network domain denied: {hostname}")
        results = original_getaddrinfo(host, *args, **kwargs)
        allowed_addresses.update(str(result[4][0]).lower() for result in results)
        return results

    socket.getaddrinfo = guarded_getaddrinfo
    original_connect = socket.socket.connect

    def guarded_connect(sock: socket.socket, address: Any) -> Any:
        hostname = str(address[0]).lower().rstrip(".") if isinstance(address, tuple) else str(address).lower()
        if hostname not in allowed_domains and hostname not in allowed_addresses:
            raise CapabilityDenied(f"network connection denied: {hostname}")
        return original_connect(sock, address)

    socket.socket.connect = guarded_connect

    original_popen = subprocess.Popen

    def guarded_popen(args: Any, *popen_args: Any, **popen_kwargs: Any) -> Any:
        command = args[0] if isinstance(args, (list, tuple)) and args else args
        executable = Path(str(command)).name.lower()
        if executable not in allowed_processes:
            raise CapabilityDenied(f"process denied: {executable}")
        return original_popen(args, *popen_args, **popen_kwargs)

    subprocess.Popen = guarded_popen

    original_import = builtins.__import__
    clipboard_modules = {"pyperclip", "tkinter", "win32clipboard"}
    camera_modules = {"cv2", "mediapipe"}
    microphone_modules = {"pyaudio", "sounddevice", "speech_recognition"}

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        root = name.split(".", 1)[0]
        clipboard = capabilities["clipboard"]
        media = capabilities["media"]
        if root in clipboard_modules and not (clipboard["read"] or clipboard["write"]):
            raise CapabilityDenied(f"clipboard module denied: {root}")
        if root in camera_modules and not media["camera"]:
            raise CapabilityDenied(f"camera module denied: {root}")
        if root in microphone_modules and not media["microphone"]:
            raise CapabilityDenied(f"microphone module denied: {root}")
        return original_import(name, *args, **kwargs)

    builtins.__import__ = guarded_import


def _invoke(request: dict[str, Any]) -> Any:
    script_path = Path(request["script_path"]).resolve()
    spec = importlib.util.spec_from_file_location("heliox_native_plugin", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load plugin module: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tool_name = request["tool_name"]
    params = request["params"]
    if hasattr(module, "handle_tool"):
        result = module.handle_tool(tool_name, params)
    elif isinstance(getattr(module, "TOOL_HANDLERS", None), dict) and tool_name in module.TOOL_HANDLERS:
        result = module.TOOL_HANDLERS[tool_name](**params)
    elif hasattr(module, tool_name):
        result = getattr(module, tool_name)(**params)
    else:
        raise RuntimeError(f"Plugin does not implement tool {tool_name!r}")
    return result


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        loop = asyncio.new_event_loop()
        _install_guards(request)
        result = _invoke(request)
        if inspect.isawaitable(result):
            result = loop.run_until_complete(result)
        loop.close()
        payload = result if isinstance(result, dict) else {"result": result}
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
