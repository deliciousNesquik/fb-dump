"""Connection settings: command-line flags first, environment second.

``ISC_USER`` / ``ISC_PASSWORD`` are the standard Firebird client variables —
``fbclient`` itself honours them — so fb-dump only passes them through. The
password is never accepted on the command line (it would leak via ``ps``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

ENV_DATABASE = "FB_DATABASE"
ENV_CHARSET = "FB_CHARSET"
ENV_USER = "ISC_USER"
ENV_PASSWORD = "ISC_PASSWORD"
ENV_ROLE = "FB_ROLE"
ENV_ISOLATION = "FB_ISOLATION"
DEFAULT_CHARSET = "UTF8"

# Transaction isolation for reading the catalog. All are read-only and NO WAIT; they
# differ in what they guarantee and what they cost (see db.py and the README). Levels
# that would block writers (SNAPSHOT TABLE STABILITY) or fail on any record being
# modified (read committed without record version) are deliberately not offered.
ISOLATIONS = ("read-committed", "read-consistency", "snapshot")
DEFAULT_ISOLATION = "read-committed"


class ConfigError(Exception):
    """Missing or contradictory connection settings."""


@dataclass(frozen=True)
class Settings:
    database: str
    user: str | None
    password: str | None
    role: str | None
    charset: str
    fallback_charset: str | None
    isolation: str


def _norm_charset(value: str) -> str:
    return value.strip().upper().replace("-", "")


def resolve(env: Mapping[str, str], *, database: str | None = None, user: str | None = None,
            role: str | None = None, charset: str | None = None,
            fallback_charset: str | None = None, isolation: str | None = None) -> Settings:
    db = database or env.get(ENV_DATABASE) or ""
    if not db.strip():
        raise ConfigError(f"no database given: use --database or set {ENV_DATABASE}")
    cs = _norm_charset(charset or env.get(ENV_CHARSET) or DEFAULT_CHARSET)
    fb = _norm_charset(fallback_charset) if fallback_charset else None
    if fb is not None and fb == cs:
        raise ConfigError("--fallback-charset must differ from --charset")
    iso = (isolation or env.get(ENV_ISOLATION) or DEFAULT_ISOLATION).strip().lower()
    if iso not in ISOLATIONS:
        raise ConfigError(f"unknown isolation {iso!r}; choose from {', '.join(ISOLATIONS)}")
    return Settings(
        database=db.strip(),
        user=(user or env.get(ENV_USER) or None),
        password=(env.get(ENV_PASSWORD) or None),
        role=(role or env.get(ENV_ROLE) or None),
        charset=cs,
        fallback_charset=fb,
        isolation=iso,
    )
