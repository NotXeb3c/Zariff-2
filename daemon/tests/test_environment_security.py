"""Regression coverage for persistent environment variable safety."""

from __future__ import annotations

import os

import pytest

from pilot.system.environment import _append_env_to_profile, env_set


@pytest.mark.asyncio
async def test_profile_value_is_shell_quoted(tmp_path):
    profile = tmp_path / ".bashrc"
    malicious = '$(touch /tmp/heliox-pwned); "quoted"'

    await _append_env_to_profile(str(profile), "HELIOX_SAFE", malicious)

    line = profile.read_text(encoding="utf-8")
    assert line == "export HELIOX_SAFE='$(touch /tmp/heliox-pwned); \"quoted\"'  # pilot-env:HELIOX_SAFE\n"


@pytest.mark.asyncio
async def test_profile_update_replaces_only_owned_entry(tmp_path):
    profile = tmp_path / ".zshrc"
    profile.write_text(
        "export KEEP=yes\nexport HELIOX_SAFE=old  # pilot-env:HELIOX_SAFE\n",
        encoding="utf-8",
    )

    await _append_env_to_profile(str(profile), "HELIOX_SAFE", "new value")

    content = profile.read_text(encoding="utf-8")
    assert content.count("# pilot-env:HELIOX_SAFE") == 1
    assert "export KEEP=yes" in content
    assert "export HELIOX_SAFE='new value'" in content


@pytest.mark.asyncio
async def test_invalid_environment_name_is_rejected_before_process_mutation():
    name = "BAD;touch_pwned"
    os.environ.pop(name, None)

    with pytest.raises(ValueError, match="Environment variable names"):
        await env_set(name, "value")

    assert name not in os.environ
