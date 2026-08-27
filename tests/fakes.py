"""Lightweight fakes of firebird-lib schema objects for offline tests (no database, no libfbclient)."""

from __future__ import annotations

import re
from typing import Any

_COLLECTIONS = (
    "roles", "collations", "functions", "generators", "exceptions", "domains", "tables",
    "indices", "views", "procedures", "packages", "triggers", "privileges", "character_sets",
    "constraints",
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


class FIndex:
    def __init__(self, segments: list[str]) -> None:
        self.segment_names = segments


class FConstraint:
    def __init__(self, name: str, kind: str, segments: list[str] | None = None,
                 partner_segments: list[str] | None = None) -> None:
        self.name = name
        self.kind = kind  # pkey | unique | check | fkey | not_null
        self.index = FIndex(segments) if segments is not None else None
        self.partner_constraint = FConstraint(f"{name}_P", "pkey", partner_segments) if partner_segments else None

    def is_pkey(self) -> bool: return self.kind == "pkey"
    def is_unique(self) -> bool: return self.kind == "unique"
    def is_check(self) -> bool: return self.kind == "check"
    def is_fkey(self) -> bool: return self.kind == "fkey"
    def is_not_null(self) -> bool: return self.kind == "not_null"

    def get_sql_for(self, action: str, **kw: Any) -> str:
        sql = f"ALTER TABLE ADD CONSTRAINT {self.name} ({self.kind})"
        if self.index is not None:
            sql += f" ({','.join(self.index.segment_names)})"
        if self.partner_constraint is not None and self.partner_constraint.index is not None:
            sql += f" REFERENCES P ({','.join(self.partner_constraint.index.segment_names)})"
        return sql


class FGenerator:
    def __init__(self, increment: int | None = None, initial: int | None = None) -> None:
        self.increment = increment
        self.inital_value = initial


class FChild:
    """Column or parameter."""

    def __init__(self, name: str, description: str | None = None, *, identity: int | None = None,
                 generator: FGenerator | None = None) -> None:
        self.name = name
        self.description = description
        self._attributes = {"RDB$IDENTITY_TYPE": identity}
        self.generator = generator

    def is_identity(self) -> bool: return self._attributes["RDB$IDENTITY_TYPE"] is not None
    def get_quoted_name(self) -> str: return quoted(self.name)

    def get_sql_for(self, action: str, **kw: Any) -> str:
        return f"COMMENT ON CHILD {self.name} IS '{self.description}'"


class FObj:
    """Universal schema object fake."""

    def __init__(self, name: str, *, sys: bool = False, external: bool = False, packaged: bool = False,
                 enforcer: bool = False, inactive: bool = False, owner: str = "SYSDBA",
                 description: str | None = None, body: str | None = None,
                 constraints: list[FConstraint] | None = None, columns: list[FChild] | None = None,
                 input_params: list[FChild] | None = None, output_params: list[FChild] | None = None,
                 fail: bool = False, attributes: dict[str, Any] | None = None, actions: set[str] | None = None,
                 segments: list[str] | None = None, expression: bool = False, increment: int | None = None,
                 initial: int | None = None, deterministic: int | None = None, ddl: bool = False,
                 relation: "FObj | None" = None, active: bool = True, position: int = 0,
                 source: str | None = "BEGIN END", type_string: str = "BEFORE INSERT") -> None:
        self.name = name
        self.owner_name = owner
        self.description = description
        self.body = body
        self.constraints = constraints or []
        self.columns = columns or []
        self.input_params = input_params or []
        self.output_params = output_params or []
        self._attributes = attributes or {}
        self._actions = actions
        self.segment_names = segments or []
        self.increment = increment
        self.inital_value = initial
        self.deterministic_flag = deterministic
        self.relation = relation
        self.active = active
        self.sequence = position
        self.source = source
        self._type_string = type_string
        self._sys, self._ext, self._pkg, self._enf, self._inactive, self._fail = sys, external, packaged, enforcer, inactive, fail
        self._expr, self._ddl = expression, ddl

    def is_sys_object(self) -> bool: return self._sys
    def is_external(self) -> bool: return self._ext
    def is_packaged(self) -> bool: return self._pkg
    def is_enforcer(self) -> bool: return self._enf
    def is_inactive(self) -> bool: return self._inactive
    def is_expression(self) -> bool: return self._expr
    def is_ddl_trigger(self) -> bool: return self._ddl
    def get_quoted_name(self) -> str: return quoted(self.name)
    def get_type_as_string(self) -> str: return self._type_string

    def get_sql_for(self, action: str, **kw: Any) -> str:
        if self._fail:
            raise RuntimeError("boom")
        if self._actions is not None and action not in self._actions:
            raise ValueError(f"Unsupported action '{action}'")
        if action == "create":
            if kw.get("body"):
                return f"CREATE PACKAGE BODY {self.name}"
            if kw.get("no_code"):
                return f"CREATE OBJ {self.name} (A INTEGER)\nRETURNS INTEGER\nAS\nBEGIN\nEND"
            extra = "".join(f" {k}={v}" for k, v in sorted(kw.items()) if k in ("value", "increment"))
            if self.segment_names and not self._expr:
                return f"CREATE OBJ {self.name} ({','.join(self.segment_names)})"
            return f"CREATE OBJ {self.name}{extra}"
        if action == "create_or_alter":
            return f"CREATE OR ALTER OBJ {self.name}\nRETURNS INTEGER\nAS\n{self.source}"
        if action == "recreate":
            return f"RECREATE PACKAGE BODY {self.name}" if kw.get("body") else f"RECREATE OBJ {self.name}"
        if action == "declare":
            return f"DECLARE EXTERNAL FUNCTION {self.name}"
        if action == "comment":
            return f"COMMENT ON {self.name} IS '{self.description}'"
        if action == "deactivate":
            return f"ALTER INDEX {self.name} INACTIVE"
        if action == "alter":
            return f"ALTER CHARACTER SET {self.name} SET DEFAULT COLLATION {kw['collation'].name}"
        raise ValueError(action)


class FCharset:
    def __init__(self, name: str, default_collation: str | None = None, description: str | None = None) -> None:
        self.name = name
        self.default_collation = FCharset(default_collation) if default_collation else FCharset.__new__(FCharset)
        if not default_collation:
            self.default_collation.name = name
        self.description = description

    def get_sql_for(self, action: str, **kw: Any) -> str:
        if action == "alter":
            return f"ALTER CHARACTER SET {self.name} SET DEFAULT COLLATION {kw['collation'].name}"
        return f"COMMENT ON CHARACTER SET {self.name} IS '{self.description}'"


class FSchema:
    def __init__(self, *, description: str | None = None, charset: str | None = "UTF8",
                 keywords: set[str] | None = None, owner: str | None = "SYSDBA",
                 **collections: list[Any]) -> None:
        for name in _COLLECTIONS:
            setattr(self, name, collections.get(name, []))
        self.description = description
        self.owner_name = owner
        self.default_character_set = FCharset(charset) if charset else None
        self._keywords = keywords or set()

    def is_keyword(self, ident: str) -> bool:
        return ident in self._keywords
