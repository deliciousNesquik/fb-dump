"""Shared data types.

``Artifact`` is the unit of output: one SQL statement addressed to one file. It
decouples "which object produces which SQL" (categories) from "how files are
written" (writer). ``Context`` is what every extractor receives: the bound
schema, the connection dialect, the output layout and a lazily built privilege
index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .grants import GrantIndex
from .layout import Layout


@dataclass(frozen=True)
class Artifact:
    path: str            # tree-relative path, e.g. "07_TABLES/ACCOUNT.sql"
    sql: str             # one statement without terminator; lines starting with "--" are emitted verbatim
    psql: bool = False   # True -> wrapped in SET TERM ^ ; ... ^


class Context:
    def __init__(self, schema: Any, layout: Layout, dialect: int = 3) -> None:
        self.schema = schema
        self.layout = layout
        self.dialect = dialect
        self._grants: GrantIndex | None = None

    @property
    def grants(self) -> GrantIndex:
        """Privileges indexed by subject; built on first use (one catalog read)."""
        if self._grants is None:
            self._grants = GrantIndex(self.schema.privileges)
        return self._grants

    def is_keyword(self, ident: str) -> bool:
        fn = getattr(self.schema, "is_keyword", None)
        return bool(fn(ident)) if callable(fn) else False
