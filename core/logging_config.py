"""Application logging setup."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from config.settings import LOGS_DIR, ensure_dirs

SENSITIVE_PATTERNS = [
    re.compile(r"(password|api_key|secret|token)\s*[=:]\s*\S+", re.I),
]


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pat in SENSITIVE_PATTERNS:
                record.msg = pat.sub(r"\1=***REDACTED***", record.msg)
        return True


def setup_logging(level: int = logging.INFO) -> None:
    ensure_dirs()
    log_file = LOGS_DIR / "zariff.log"
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger("zariff")
    root.setLevel(level)
    if not root.handlers:
        fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(RedactingFilter())
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.addFilter(RedactingFilter())
        root.addHandler(fh)
        root.addHandler(ch)
