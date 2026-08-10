"""Installed-application discovery and truthful application launching.

Windows' ``cmd /c start`` returns success after handing a string to the shell,
even when the shell later displays an "app not found" dialog.  This module
resolves a user's application label against registrations that Windows knows
about *before* launching it, and fails closed when the label is missing or
ambiguous.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class ApplicationResolutionError(RuntimeError):
    """Raised when an application label cannot be resolved safely."""


class ApplicationLaunchError(RuntimeError):
    """Raised when a resolved application cannot be started."""


@dataclass(frozen=True)
class ApplicationTarget:
    """A verified Windows application launch target."""

    display_name: str
    kind: Literal["executable", "start_app"]
    value: str
    source: str


@dataclass(frozen=True)
class _NamedTarget:
    label: str
    value: str


def _normalise_label(value: str) -> str:
    value = re.sub(r"\.(?:exe|com|cmd|bat|lnk)$", "", value.strip(), flags=re.IGNORECASE)
    return "".join(character for character in value.casefold() if character.isalnum())


def _label_tokens(value: str) -> set[str]:
    value = re.sub(r"\.(?:exe|com|cmd|bat|lnk)$", "", value.strip(), flags=re.IGNORECASE)
    return {token for token in re.findall(r"[a-z0-9]+", value.casefold()) if token}


def _match_rank(query: str, label: str) -> tuple[int, int]:
    """Return a conservative match rank; ``(0, 0)`` means no match."""

    query_normalised = _normalise_label(query)
    label_normalised = _normalise_label(label)
    if not query_normalised or not label_normalised:
        return (0, 0)
    if query_normalised == label_normalised:
        return (3, len(label_normalised))

    query_tokens = _label_tokens(query)
    label_tokens = _label_tokens(label)
    if query_tokens and label_tokens and (query_tokens.issubset(label_tokens) or label_tokens.issubset(query_tokens)):
        return (2, len(query_tokens & label_tokens))

    # Joined-word variants such as "open screen" -> "Openscreen" are safe
    # when the shorter side is still substantial.  Tiny partial labels would
    # make common application names dangerously ambiguous.
    shorter = min(len(query_normalised), len(label_normalised))
    if shorter >= 5 and (query_normalised in label_normalised or label_normalised in query_normalised):
        return (1, shorter)
    return (0, 0)


def _best_matches(query: str, candidates: list[_NamedTarget]) -> list[_NamedTarget]:
    ranked = [(candidate, _match_rank(query, candidate.label)) for candidate in candidates]
    ranked = [(candidate, rank) for candidate, rank in ranked if rank[0] > 0]
    if not ranked:
        return []
    best_rank = max(rank for _, rank in ranked)
    matches = [candidate for candidate, rank in ranked if rank == best_rank]

    # Duplicate registrations for the same target are not ambiguous.
    unique: dict[str, _NamedTarget] = {}
    for match in matches:
        unique.setdefault(match.value.casefold(), match)
    return list(unique.values())


def _default_start_menu_roots() -> list[Path]:
    roots: list[Path] = []
    app_data = os.environ.get("APPDATA")
    program_data = os.environ.get("PROGRAMDATA")
    if app_data:
        roots.append(Path(app_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if program_data:
        roots.append(Path(program_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return roots


def _find_start_menu_shortcuts() -> list[_NamedTarget]:
    shortcuts: list[_NamedTarget] = []
    for root in _default_start_menu_roots():
        if not root.is_dir():
            continue
        for shortcut in root.rglob("*.lnk"):
            shortcuts.append(_NamedTarget(label=shortcut.stem, value=str(shortcut)))
    return shortcuts


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def _resolve_shortcut_target(shortcut: Path) -> str:
    from pilot.system.platform_detect import run_powershell

    literal = _powershell_literal(str(shortcut))
    script = "$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut(" + literal + "); $shortcut.TargetPath"
    code, stdout, stderr = await run_powershell(script, timeout=10)
    target = stdout.strip()
    if code != 0 or not target:
        detail = stderr.strip() or "shortcut has no executable target"
        raise ApplicationResolutionError(f"Could not resolve Start-menu shortcut: {detail}")
    return target


async def _load_start_apps() -> list[_NamedTarget]:
    from pilot.system.platform_detect import run_powershell

    script = "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"
    code, stdout, stderr = await run_powershell(script, timeout=15)
    if code != 0 or not stdout.strip():
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    return [
        _NamedTarget(label=str(entry["Name"]), value=str(entry["AppID"]))
        for entry in payload
        if isinstance(entry, dict) and entry.get("Name") and entry.get("AppID")
    ]


def _ambiguous_error(name: str, matches: list[_NamedTarget]) -> ApplicationResolutionError:
    labels = ", ".join(sorted({match.label for match in matches}, key=str.casefold))
    return ApplicationResolutionError(f"Application {name!r} is ambiguous ({labels}). Use the exact application name.")


async def resolve_windows_application(name: str) -> ApplicationTarget:
    """Resolve an installed Windows application without guessing blindly."""

    requested = name.strip()
    if not requested:
        raise ApplicationResolutionError("Application name is empty.")

    explicit = Path(requested).expanduser()
    if explicit.is_file():
        return ApplicationTarget(explicit.stem, "executable", str(explicit.resolve()), "file path")

    shortcut_matches = _best_matches(requested, _find_start_menu_shortcuts())
    if shortcut_matches:
        resolved_shortcuts: dict[str, _NamedTarget] = {}
        for shortcut in shortcut_matches:
            target = Path(await _resolve_shortcut_target(Path(shortcut.value))).expanduser()
            if not target.is_file():
                raise ApplicationResolutionError(
                    f"{shortcut.label!r} is registered, but its executable is missing: {target}"
                )
            resolved = str(target.resolve())
            resolved_shortcuts.setdefault(resolved.casefold(), _NamedTarget(label=shortcut.label, value=resolved))
        unique_shortcuts = list(resolved_shortcuts.values())
        if len(unique_shortcuts) > 1:
            raise _ambiguous_error(requested, unique_shortcuts)
        shortcut = unique_shortcuts[0]
        return ApplicationTarget(shortcut.label, "executable", shortcut.value, "Start menu")

    # Command-line application shims are a valid registration source, but
    # only after user-facing Start-menu labels have had the first chance to
    # resolve an exact product name.
    path_candidates = [requested, requested.replace(" ", "-"), requested.replace(" ", "")]
    for candidate in dict.fromkeys(path_candidates):
        binary = shutil.which(candidate)
        if binary:
            return ApplicationTarget(requested, "executable", str(Path(binary).resolve()), "PATH")

    start_app_matches = _best_matches(requested, await _load_start_apps())
    if len(start_app_matches) > 1:
        raise _ambiguous_error(requested, start_app_matches)
    if start_app_matches:
        match = start_app_matches[0]
        return ApplicationTarget(match.label, "start_app", match.value, "Windows app registry")

    raise ApplicationResolutionError(
        f"Application {requested!r} was not found in the Start menu, PATH, or Windows app registry."
    )


async def _start_and_check(command: list[str], display_name: str) -> int:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ApplicationLaunchError(f"Could not launch {display_name}: {exc}") from exc

    try:
        return_code = await asyncio.wait_for(process.wait(), timeout=0.8)
    except TimeoutError:
        # The process remained alive past startup, which is the normal GUI-app
        # case.  asyncio will reap it later without blocking the command.
        return process.pid

    stderr = b""
    if process.stderr is not None:
        stderr = await process.stderr.read()
    if return_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ApplicationLaunchError(
            f"{display_name} exited during startup with code {return_code}" + (f": {detail}" if detail else ".")
        )
    return process.pid


async def launch_windows_application(name: str, args: list[str] | None = None) -> str:
    """Resolve and launch an installed Windows application truthfully."""

    target = await resolve_windows_application(name)
    arguments = list(args or [])
    if target.kind == "start_app":
        if arguments:
            raise ApplicationLaunchError(f"Arguments cannot be passed to Windows app {target.display_name!r}.")
        command = ["explorer.exe", rf"shell:AppsFolder\{target.value}"]
    else:
        suffix = Path(target.value).suffix.casefold()
        if suffix in {".cmd", ".bat"}:
            command = ["cmd.exe", "/d", "/c", target.value, *arguments]
        else:
            command = [target.value, *arguments]

    await _start_and_check(command, target.display_name)
    return f"Launched {target.display_name} (resolved via {target.source})."


async def launch_application(name: str, args: list[str] | None = None) -> str:
    """Resolve and launch an installed application on the current platform."""

    from pilot.system.platform_detect import CURRENT_PLATFORM, Platform, run_command

    requested = name.strip()
    if not requested:
        raise ApplicationResolutionError("Application name is empty.")

    arguments = list(args or [])
    if CURRENT_PLATFORM == Platform.WINDOWS:
        return await launch_windows_application(requested, arguments)

    if CURRENT_PLATFORM == Platform.MACOS:
        command = ["open", "-a", requested]
        if arguments:
            command.extend(["--args", *arguments])
        code, _, stderr = await run_command(command)
        if code != 0:
            detail = stderr.strip() or "application is not registered with Launch Services"
            raise ApplicationResolutionError(f"Application {requested!r} was not found: {detail}")
        return f"Launched {requested} (resolved via macOS Launch Services)."

    binary = shutil.which(requested)
    if binary:
        await _start_and_check([binary, *arguments], requested)
        return f"Launched {requested} (resolved via PATH)."

    desktop_launcher = shutil.which("gtk-launch")
    if not desktop_launcher:
        raise ApplicationResolutionError(
            f"Application {requested!r} was not found in PATH and gtk-launch is unavailable."
        )
    if arguments:
        raise ApplicationLaunchError(
            f"Arguments cannot be passed to desktop application {requested!r} through gtk-launch."
        )
    code, _, stderr = await run_command([desktop_launcher, requested])
    if code != 0:
        detail = stderr.strip() or "desktop application is not registered"
        raise ApplicationResolutionError(f"Application {requested!r} was not found: {detail}")
    return f"Launched {requested} (resolved via Linux desktop registry)."
