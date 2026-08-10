"""Environment variable management — get, set, list.

Cross-platform: handles both session and persistent environment variables.
"""

from __future__ import annotations

import logging
import os
import re
import shlex

from pilot.system.platform_detect import CURRENT_PLATFORM, Platform, run_command

logger = logging.getLogger("pilot.system.environment")

_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_env_name(name: str) -> None:
    if not _ENV_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Environment variable names must start with a letter or underscore and contain "
            "only letters, numbers, and underscores."
        )


async def env_get(name: str) -> str:
    """Get an environment variable value."""
    _validate_env_name(name)
    value = os.environ.get(name)
    if value is None:
        return f"Environment variable '{name}' is not set"
    return f"{name}={value}"


async def env_set(name: str, value: str, persistent: bool = False) -> str:
    """Set an environment variable.

    If persistent=True, write to the appropriate shell profile or system config.
    """
    _validate_env_name(name)

    # Set for current process
    os.environ[name] = value

    if persistent:
        if CURRENT_PLATFORM == Platform.WINDOWS:
            # Use setx for persistent user-level env var
            code, out, err = await run_command(["setx", name, value])
            if code != 0:
                raise RuntimeError(f"setx failed: {err.strip()}")
            return f"Set {name}={value} (persistent, user scope)"

        elif CURRENT_PLATFORM == Platform.MACOS:
            # Write to ~/.zshrc (default macOS shell)
            profile = os.path.expanduser("~/.zshrc")
            await _append_env_to_profile(profile, name, value)
            return f"Set {name}={value} (persistent, added to ~/.zshrc)"

        else:  # Linux
            # Write to ~/.bashrc
            profile = os.path.expanduser("~/.bashrc")
            await _append_env_to_profile(profile, name, value)
            return f"Set {name}={value} (persistent, added to ~/.bashrc)"

    return f"Set {name}={value} (session only)"


async def _append_env_to_profile(profile_path: str, name: str, value: str) -> None:
    """Append or update an env var in a shell profile file."""
    _validate_env_name(name)
    marker = f"# pilot-env:{name}"

    try:
        with open(profile_path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    # Remove old entry
    new_lines = [l for l in lines if marker not in l]
    # POSIX shell quoting keeps command substitutions, newlines, quotes, and
    # metacharacters literal when the profile is sourced.
    new_lines.append(f"export {name}={shlex.quote(value)}  {marker}\n")

    with open(profile_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


async def env_list(filter_prefix: str | None = None) -> str:
    """List environment variables, optionally filtered by prefix."""
    entries: list[str] = []
    for k, v in sorted(os.environ.items()):
        if filter_prefix and not k.upper().startswith(filter_prefix.upper()):
            continue
        # Truncate long values for readability
        display_v = v if len(v) <= 200 else v[:200] + "..."
        entries.append(f"{k}={display_v}")

    return "\n".join(entries[:100]) if entries else "No matching environment variables"
