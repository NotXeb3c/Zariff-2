#!/usr/bin/env python3
"""Verify that a built daemon wheel contains end-user release capabilities."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

PACKAGE_PREFIX = "pilot/plugins/marketplace_catalog"
REQUIRED_FILES = {
    "pilot/security/risk_gate_weights.npz",
    f"{PACKAGE_PREFIX}/registry.json",
}
REQUIRED_REQUIREMENTS = ("kokoro", "pocket-tts", "sounddevice", "openai-whisper", "playwright")


def validate_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
        missing = sorted(REQUIRED_FILES - names)
        if missing:
            raise ValueError(f"wheel is missing required release files: {', '.join(missing)}")

        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("wheel must contain exactly one distribution METADATA file")
        metadata = wheel.read(metadata_names[0]).decode("utf-8")
        for requirement in REQUIRED_REQUIREMENTS:
            if f"Requires-Dist: {requirement}" not in metadata:
                raise ValueError(f"wheel is missing the {requirement!r} end-user dependency")

        registry = json.loads(wheel.read(f"{PACKAGE_PREFIX}/registry.json"))
        for plugin in registry.get("plugins", []):
            package_path = Path(str(plugin["package"]["path"]))
            if not package_path.parts or package_path.parts[0] != "plugins":
                raise ValueError(f"invalid bundled plugin package path: {package_path}")
            bundled_root = Path(PACKAGE_PREFIX).joinpath(*package_path.parts[1:])
            for file_entry in plugin["package"]["files"]:
                expected = (bundled_root / str(file_entry["path"])).as_posix()
                if expected not in names:
                    raise ValueError(f"wheel is missing bundled marketplace file: {expected}")

        if wheel.getinfo("pilot/security/risk_gate_weights.npz").file_size == 0:
            raise ValueError("bundled learned-risk world-model weights are empty")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    try:
        validate_wheel(args.wheel.resolve())
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        print(f"daemon artifact check failed: {exc}", file=sys.stderr)
        return 1
    print(f"daemon artifact verified: {args.wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
