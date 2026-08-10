"""User Manual Supervision — auditable risk-pattern rule table.

Small, explicit, hardcoded regex table — never a learned model — mirroring
risk_safety.py's "the actual block/allow (here: warn/don't-warn) decision
stays fully auditable" philosophy. Matched against content derived from the
user's own independent activity (an OCR screen snippet, or a transient
keystroke buffer — see pilot.system.input_hook), never against anything
Heliox itself is executing.

This module is the privacy contract boundary for that content: callers may
pass text in, but must never log, persist, or return the text itself —
match_risk_pattern() returns only the matched pattern's NAME.
"""

from __future__ import annotations

import re

# Each entry is named so a match is traceable to a specific, auditable rule
# rather than a bare boolean — mirrors risk_safety.py's DISK_USAGE_RISK_THRESHOLD/
# FORK_BOMB_DELTA_THRESHOLD naming convention. Flat content-only matching,
# no per-application context — see SECURITY.md's Known scope limits.
RISK_PATTERNS: dict[str, re.Pattern[str]] = {
    "destructive_shell_command": re.compile(
        r"\brm\s+-rf\s+/|\bformat\s+[a-z]:|\bdd\s+if=.*of=/dev/",
        re.IGNORECASE,
    ),
    "credential_exposure": re.compile(
        r"\bpassword\s*[:=]\s*\S|\bapi[_-]?key\s*[:=]\s*\S",
        re.IGNORECASE,
    ),
    "destructive_sql": re.compile(
        r"\bDROP\s+(TABLE|DATABASE)\b|\bDELETE\s+FROM\s+\w+\s*;?\s*$",
        re.IGNORECASE,
    ),
    "unauthorized_admin_command": re.compile(
        r"\bnet\s+user\s+.*\s+/add\b|\bicacls\s+.*\s+/grant\b",
        re.IGNORECASE,
    ),
}


def match_risk_pattern(text: str) -> str | None:
    """Return the NAME of the first matching risk pattern, or None.

    Callers must never log or persist `text` itself, only the returned
    name — this function is the privacy contract boundary described above.
    """
    if not text:
        return None
    for name, pattern in RISK_PATTERNS.items():
        if pattern.search(text):
            return name
    return None
