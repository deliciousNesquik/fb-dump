"""Lightweight fakes of firebird-lib schema objects for offline tests (no database, no libfbclient)."""

from __future__ import annotations

import re
from typing import Any

_COLLECTIONS = (
    "roles", "collations", "functions", "generators", "exceptions", "domains", "tables",
    "indices", "views", "procedures", "packages", "triggers", "privileges",
)
_PLAIN = re.compile(r"^[A-Z][A-Z0-9_$]*$")


def quoted(name: str) -> str:
    return name if _PLAIN.match(name) else '"' + name.replace('"', '""') + '"'


class FPriv:
    """A row of RDB$USER_PRIVILEGES."""

    def __init__(self, user: str, priv: str, subject: str, *, subject_type: int = 0, user_type: int = 8,
                 field: str | None = None, grant: bool = False, grantor: str = "SYSDBA") -> None:
        self.user_name = user
        self.privilege = priv          # 'S','I','U','D','R','X','G','M','C','L','O'
        self.subject_name = subject
        self.subject_type = subject_type
        self.user_type = user_type
        self.field_name = field
        self.grantor_name = grantor
        self._grant = grant

    def has_grant(self) -> bool:
        return self._grant


class FConstraint:
    def __init__(self, name: str, kind: str) -> None:
        self.name = name
        self.kind = kind  # pkey | unique | check | fkey | not_null

    def is_pkey(self) -> bool: return self.kind == "pkey"
    def is_unique(self) -> bool: return self.kind == "unique"
    def is_check(self) -> bool: return self.kind == "check"
    def is_fkey(self) -> bool: return self.kind == "fkey"
    def is_not_null(self) -> bool: return self.kind == "not_null"

    def get_sql_for(self, action: str, **kw: Any) -> str:
        return f"ALTER TABLE ADD CONSTRAINT {self.name} ({self.kind})"


class FChild:
    """Column or parameter."""

    def __init__(self, name: str, description: str | None = None) -> None:
        self.name = name
        self.description = description

    def get_sql_for(self, action: str, **kw: Any) -> str:
        return f"COMMENT ON CHILD {self.name} IS '{self.description}'"


class FObj:
    """Universal schema object fake."""

    def __init__(self, name: str, *, sys: bool = False, external: bool = False, packaged: bool = False,
                 enforcer: bool = False, inactive: bool = False, owner: str = "SYSDBA",
                 description: str | None = None, body: str | None = None,
                 constraints: list[FConstraint] | None = None, columns: list[FChild] | None = None,
                 input_params: list[FChild] | None = None, output_params: list[FChild] | None = None,
                 fail: bool = False) -> None:
        self.name = name
        self.owner_name = owner
        self.description = description
        self.body = body
        self.constraints = constraints or []
        self.columns = columns or []
        self.input_params = input_params or []
        self.output_params = output_params or []
        self._sys, self._ext, self._pkg, self._enf, self._inactive, self._fail = sys, external, packaged, enforcer, inactive, fail

    def is_sys_object(self) -> bool: return self._sys
    def is_external(self) -> bool: return self._ext
    def is_packaged(self) -> bool: return self._pkg
    def is_enforcer(self) -> bool: return self._enf
    def is_inactive(self) -> bool: return self._inactive
    def get_quoted_name(self) -> str: return quoted(self.name)

    def get_sql_for(self, action: str, **kw: Any) -> str:
        if self._fail:
            raise RuntimeError("boom")
        if action == "create":
            if kw.get("body"):
                return f"CREATE PACKAGE BODY {self.name}"
            return f"CREATE OBJ {self.name}"
        if action == "create_or_alter":
            return f"CREATE OR ALTER OBJ {self.name}"
        if action == "recreate":
            return f"RECREATE PACKAGE BODY {self.name}" if kw.get("body") else f"RECREATE OBJ {self.name}"
        if action == "declare":
            return f"DECLARE EXTERNAL FUNCTION {self.name}"
        if action == "comment":
            return f"COMMENT ON {self.name} IS '{self.description}'"
        if action == "deactivate":
            return f"ALTER INDEX {self.name} INACTIVE"
        raise ValueError(action)


class FCharset:
    def __init__(self, name: str) -> None:
        self.name = name


class FSchema:
    def __init__(self, *, description: str | None = None, charset: str | None = "UTF8",
                 keywords: set[str] | None = None, **collections: list[Any]) -> None:
        for name in _COLLECTIONS:
            setattr(self, name, collections.get(name, []))
        self.description = description
        self.default_character_set = FCharset(charset) if charset else None
        self._keywords = keywords or set()

    def is_keyword(self, ident: str) -> bool:
        return ident in self._keywords
