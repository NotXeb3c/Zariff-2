"""Filesystem tools with path restrictions."""

from __future__ import annotations

import shutil
from pathlib import Path

from config.settings import get_settings
from tools.registry import PermissionLevel, register_tool


def _is_allowed(path: Path) -> bool:
    settings = get_settings()
    resolved = path.resolve()
    home = Path.home().resolve()
    if resolved.is_relative_to(home):
        return True
    allowed = [Path(p).expanduser().resolve() for p in settings.allowed_directories]
    return any(resolved.is_relative_to(a) for a in allowed if a.exists())


def _guard(path_str: str) -> tuple[Path | None, dict | None]:
    path = Path(path_str).expanduser()
    if not _is_allowed(path):
        return None, {
            "success": False,
            "message": f"Access denied: {path} is outside allowed directories.",
        }
    return path, None


@register_tool(
    name="list_files",
    description="List files in a directory",
    parameters={"path": "Directory path"},
)
def list_files(params: dict) -> dict:
    path, err = _guard(params["path"])
    if err:
        return err
    assert path is not None
    if not path.is_dir():
        return {"success": False, "message": f"Not a directory: {path}"}
    items = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    names = [f"{'[DIR]' if p.is_dir() else '[FILE]'} {p.name}" for p in items[:100]]
    return {"success": True, "message": "\n".join(names) or "(empty)", "files": [p.name for p in items[:100]]}


@register_tool(
    name="read_text_file",
    description="Read a text file (max 50KB)",
    parameters={"path": "File path"},
)
def read_text_file(params: dict) -> dict:
    path, err = _guard(params["path"])
    if err:
        return err
    assert path is not None
    if not path.is_file():
        return {"success": False, "message": f"File not found: {path}"}
    if path.stat().st_size > 50_000:
        return {"success": False, "message": "File too large to read (max 50KB)."}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return {"success": True, "message": content[:4000], "content": content[:4000]}
    except Exception as e:
        return {"success": False, "message": str(e)}


@register_tool(
    name="create_text_file",
    description="Create or overwrite a text file",
    parameters={"path": "File path", "content": "File content"},
    permission=PermissionLevel.MODERATE,
    requires_confirmation=True,
)
def create_text_file(params: dict) -> dict:
    path, err = _guard(params["path"])
    if err:
        return err
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(params["content"], encoding="utf-8")
    return {"success": True, "message": f"Created {path.name}."}


@register_tool(
    name="copy_file",
    description="Copy a file to a destination",
    parameters={"source": "Source path", "destination": "Destination path"},
    permission=PermissionLevel.MODERATE,
    requires_confirmation=True,
)
def copy_file(params: dict) -> dict:
    src, err = _guard(params["source"])
    if err:
        return err
    dst, err2 = _guard(params["destination"])
    if err2:
        return err2
    assert src is not None and dst is not None
    if not src.is_file():
        return {"success": False, "message": "Source is not a file."}
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"success": True, "message": f"Copied to {dst}."}


@register_tool(
    name="move_file",
    description="Move a file or folder",
    parameters={"source": "Source path", "destination": "Destination path"},
    permission=PermissionLevel.DANGEROUS,
    requires_confirmation=True,
)
def move_file(params: dict) -> dict:
    src, err = _guard(params["source"])
    if err:
        return err
    dst, err2 = _guard(params["destination"])
    if err2:
        return err2
    assert src is not None and dst is not None
    if not src.exists():
        return {"success": False, "message": "Source not found."}
    shutil.move(str(src), str(dst))
    return {"success": True, "message": f"Moved to {dst}."}
