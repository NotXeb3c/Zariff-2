"""Changelog & Feature Announcements — notifies users of new features."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("pilot.changelog")

VERSION = "0.10.1"

CHANGELOG = {
    "0.10.1": {
        "title": "Reliable Interactive Sessions",
        "date": "2026-07-30",
        "features": [
            {
                "name": "Persistent Private Sessions",
                "description": (
                    "Start isolated chats, reopen prior sessions, and retain adaptive memory "
                    "without leaking one conversation's transcript into another."
                ),
                "jarvis_announce": "Your sessions are now durable, private, and easy to reopen.",
            },
            {
                "name": "Faster Semantic Browser Control",
                "description": (
                    "Common browser tasks use bounded planning, resolve controls by meaning, and "
                    "serialize shared-page actions to avoid slow or conflicting execution."
                ),
                "jarvis_announce": "I can now resolve and use browser controls more quickly and reliably.",
            },
            {
                "name": "Coordinated Companion Services",
                "description": (
                    "Narration, learned-risk interruption, voice, and verified follow-up suggestions "
                    "remain available together while approvals pass through every action gate."
                ),
                "jarvis_announce": "My voice, safety model, and follow-up guidance now stay coordinated.",
            },
            {
                "name": "Safer, Truthful Task Results",
                "description": (
                    "Cancelled work, cloud-provider failures, destructive file changes, and exact "
                    "findings now produce bounded, redacted, recoverable results."
                ),
                "jarvis_announce": "Task outcomes are now clearer, safer, and easier to recover.",
            },
        ],
        "summary": "Persistent sessions, faster browser control, and coordinated companion reliability",
    },
    "0.10.0": {
        "title": "Interactive Companion and Reliability",
        "date": "2026-07-29",
        "features": [
            {
                "name": "Interactive Companion",
                "description": (
                    "Opt-in narration, risk interruption, spoken follow-up, and suggestions now "
                    "share one coordinated conversation loop."
                ),
                "jarvis_announce": "I can now narrate, interrupt, and follow up without voices overlapping.",
            },
            {
                "name": "Learned Risk World Model",
                "description": (
                    "On-device learned predictions now advise the deterministic safety gate and can "
                    "add caution or pause risky actions."
                ),
                "jarvis_announce": "My learned risk model can now warn or pause before an unsafe action.",
            },
            {
                "name": "Unified Camera Intelligence",
                "description": (
                    "Gaze, hand gestures, and cursor control can run together from one camera stream "
                    "without suppressing enabled inputs."
                ),
                "jarvis_announce": "Gaze and gesture controls can now work together from one camera.",
            },
            {
                "name": "Moderated Plugin Marketplace",
                "description": (
                    "Reviewed packages are verified by manifest and SHA-256 before installation, "
                    "and merged catalog entries appear without a desktop release."
                ),
                "jarvis_announce": "Approved marketplace plugins are now verified before installation.",
            },
            {
                "name": "Reliable Approvals and Results",
                "description": (
                    "Approval responses remain live on the active connection, while cancelled, "
                    "blocked, and failed actions now finish with truthful terminal results."
                ),
                "jarvis_announce": "Approvals and failures now stay synchronized with the task.",
            },
            {
                "name": "Durable Voice and Gesture Workflows",
                "description": (
                    "Multi-step workflows can be submitted, paused, resumed, and inspected while "
                    "autonomous healing remains separately permission-gated."
                ),
                "jarvis_announce": "Long-running voice and gesture workflows can now pause and resume safely.",
            },
        ],
        "summary": "Companion intelligence, learned safety, multimodal control, and reliable execution",
    },
    "0.9.0": {
        "title": "JARVIS Autonomy",
        "date": "2026-07-22",
        "features": [
            {
                "name": "Autonomous Background Tasks",
                "description": "Run permission-gated multi-step tasks while continuing to use the app.",
            },
            {
                "name": "Live Execution Narrator",
                "description": "Optionally hear short progress descriptions and risk interruptions.",
            },
            {
                "name": "Local Voice, Gesture, and Gaze Inputs",
                "description": "Use on-device multimodal controls without sending camera frames.",
            },
        ],
        "summary": "Background autonomy and local multimodal control",
    },
}


def get_state_dir() -> Path:
    from pilot.config import STATE_DIR

    return STATE_DIR


def get_last_version() -> str | None:
    state_dir = get_state_dir()
    version_file = state_dir / "last_version.txt"
    if version_file.exists():
        try:
            return version_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return None


def set_last_version(version: str) -> None:
    state_dir = get_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    version_file = state_dir / "last_version.txt"
    version_file.write_text(version, encoding="utf-8")


def check_for_updates() -> list[dict[str, Any]]:
    """Check if there are new features since last run."""
    last_version = get_last_version()
    current_version = VERSION

    if last_version is None:
        return get_full_changelog()

    if last_version == current_version:
        return []

    new_features = []
    for ver in CHANGELOG:
        if _compare_versions(ver, last_version) > 0:
            new_features.extend(CHANGELOG[ver]["features"])

    return new_features


def get_full_changelog() -> list[dict[str, Any]]:
    features = []
    for ver in CHANGELOG:
        ver_features = CHANGELOG[ver]["features"]
        # Handle both dict features and string features
        if ver_features and isinstance(ver_features[0], dict):
            for feat in ver_features:
                feat_copy = dict(feat)  # Copy to avoid mutation
                feat_copy["version"] = ver
                feat_copy["date"] = CHANGELOG[ver]["date"]
                features.append(feat_copy)
        else:
            # String feature - convert to dict
            for feat_name in ver_features:
                features.append(
                    {
                        "name": feat_name,
                        "version": ver,
                        "date": CHANGELOG[ver]["date"],
                    }
                )
    return features


def _compare_versions(v1: str, v2: str) -> int:
    """Compare semantic versions. Returns 1 if v1 > v2, -1 if v1 < v2, 0 if equal."""
    parts1 = [int(x) for x in v1.split(".")]
    parts2 = [int(x) for x in v2.split(".")]
    for p1, p2 in zip(parts1, parts2):
        if p1 > p2:
            return 1
        elif p1 < p2:
            return -1
    return 0


def get_welcome_message() -> dict[str, Any]:
    """Get the welcome message for new users."""
    return {
        "title": "Welcome to Zariff",
        "version": VERSION,
        "tagline": "Your biologically-inspired AI assistant",
        "features": get_full_changelog()[:3],
    }


def announce_new_features() -> str:
    """Generate JARVIS announcement for new features."""
    new_features = check_for_updates()

    if not new_features:
        return ""

    lines = [
        "Welcome to Zariff version " + VERSION + ".",
    ]

    for feat in new_features[:3]:
        if "jarvis_announce" in feat:
            lines.append(feat["jarvis_announce"])

    lines.append("Say 'What can you do?' to learn more.")

    return " ".join(lines)


def mark_version_seen() -> None:
    """Mark that user has seen current version."""
    set_last_version(VERSION)


def get_cognitive_status() -> dict[str, Any]:
    """Get brief cognitive feature status for HUD."""
    new_features = check_for_updates()
    return {
        "new_features_available": len(new_features) > 0,
        "new_feature_count": len(new_features),
        "version": VERSION,
        "cognitive_engine_available": True,
    }
