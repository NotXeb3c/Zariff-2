#!/usr/bin/env python3
"""Fail when Heliox release metadata is inconsistent."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path

SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _python_assignments(path: Path, names: set[str]) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in names:
                values[target.id] = ast.literal_eval(node.value)
    return values


def collect_versions(root: Path) -> dict[str, str]:
    """Return every first-party version source that must stay synchronized."""
    daemon_project = _toml(root / "daemon" / "pyproject.toml")
    daemon_init = _python_assignments(root / "daemon" / "pilot" / "__init__.py", {"__version__"})
    changelog = _python_assignments(
        root / "daemon" / "pilot" / "changelog.py",
        {"VERSION", "CHANGELOG"},
    )
    tauri_package = _json(root / "tauri-app" / "package.json")
    ui_package = _json(root / "tauri-app" / "ui" / "package.json")
    ui_lock = _json(root / "tauri-app" / "ui" / "package-lock.json")
    cargo_manifest = _toml(root / "tauri-app" / "src-tauri" / "Cargo.toml")
    cargo_lock = _toml(root / "tauri-app" / "src-tauri" / "Cargo.lock")
    tauri_config = _json(root / "tauri-app" / "src-tauri" / "tauri.conf.json")

    heliox_lock = next(
        package for package in cargo_lock["package"] if package.get("name") == "heliox-os"
    )
    current_changelog = changelog.get("CHANGELOG")
    changelog_version = str(changelog.get("VERSION", ""))
    if not isinstance(current_changelog, dict) or changelog_version not in current_changelog:
        raise ValueError(f"CHANGELOG has no entry for current version {changelog_version!r}")

    return {
        "daemon/pyproject.toml": str(daemon_project["project"]["version"]),
        "daemon/pilot/__init__.py": str(daemon_init.get("__version__", "")),
        "daemon/pilot/changelog.py": changelog_version,
        "tauri-app/package.json": str(tauri_package["version"]),
        "tauri-app/ui/package.json": str(ui_package["version"]),
        "tauri-app/ui/package-lock.json": str(ui_lock["version"]),
        "tauri-app/ui/package-lock.json packages root": str(ui_lock["packages"][""]["version"]),
        "tauri-app/src-tauri/Cargo.toml": str(cargo_manifest["package"]["version"]),
        "tauri-app/src-tauri/Cargo.lock": str(heliox_lock["version"]),
        "tauri-app/src-tauri/tauri.conf.json": str(tauri_config["version"]),
    }


def validate_release(root: Path, tag: str = "") -> str:
    versions = collect_versions(root)
    unique = set(versions.values())
    if len(unique) != 1:
        details = "\n".join(f"  {path}: {version}" for path, version in versions.items())
        raise ValueError(f"Release versions are inconsistent:\n{details}")

    version = unique.pop()
    if not SEMVER_PATTERN.fullmatch(version):
        raise ValueError(f"Release version is not valid semantic versioning: {version!r}")

    if tag and tag != f"v{version}":
        raise ValueError(f"Git tag {tag!r} does not match synchronized version v{version}")

    return version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    try:
        version = validate_release(args.root.resolve(), args.tag)
    except (KeyError, OSError, StopIteration, SyntaxError, ValueError) as exc:
        print(f"release preflight failed: {exc}", file=sys.stderr)
        return 1

    print(f"release version {version} verified across {len(collect_versions(args.root.resolve()))} sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
