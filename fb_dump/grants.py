"""GRANT statements, grouped per subject object.

firebird-lib's ``get_grants()`` only knows table privileges, ``EXECUTE ON PROCEDURE``
and role membership; ``EXECUTE`` on functions/packages and ``USAGE`` on
sequences/exceptions (Firebird 3+) come out wrong or fail. Rendering is done here.

``RDB$USER_PRIVILEGES`` facts relied upon:

* tables *and views* have ``RDB$OBJECT_TYPE = 0`` (one relation namespace);
* Firebird 3+ DDL privileges (``GRANT CREATE TABLE …``) use object types 22–33,
  ``ALTER/DROP DATABASE`` type 21 — they belong to the database, not to an object;
* the owner's own privileges on an object are stored like any other grant; they
  are implicit and recreated with the object, so they are not dumped.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Callable, Iterable

# RDB$OBJECT_TYPE of a privilege subject -> namespace shared with the categories.
_SUBJECT_NS: dict[int, str] = {
    0: "relation", 1: "relation", 5: "procedure", 15: "function", 18: "package",
    7: "exception", 14: "generator", 13: "role",
}
_DATABASE_TYPE = 21
_DDL_NOUN: dict[int, str] = {
    22: "TABLE", 23: "VIEW", 24: "PROCEDURE", 25: "FUNCTION", 26: "PACKAGE",
    27: "SEQUENCE", 28: "DOMAIN", 29: "EXCEPTION", 30: "ROLE",
    31: "CHARACTER SET", 32: "COLLATION", 33: "FILTER",
}
_DDL_VERB = {"C": "CREATE", "L": "ALTER ANY", "O": "DROP ANY"}
_DATABASE_VERB = {"C": "CREATE DATABASE", "L": "ALTER DATABASE", "O": "DROP DATABASE"}

# RDB$USER_TYPE of a grantee -> keyword before the name.
_USER_TYPE = 8
_GRANTEE_KW: dict[int, str] = {
    _USER_TYPE: "", 13: "ROLE ", 5: "PROCEDURE ", 2: "TRIGGER ", 1: "VIEW ",
    15: "FUNCTION ", 18: "PACKAGE ",
}
_GRANTEE_ORDER = {_USER_TYPE: 0, 13: 1, 1: 2, 2: 3, 5: 4, 15: 5, 18: 6}

_ON_KW = {
    "relation": "", "procedure": "PROCEDURE ", "function": "FUNCTION ",
    "package": "PACKAGE ", "exception": "EXCEPTION ", "generator": "SEQUENCE ",
}
_TABLE_PRIVS = ("S", "I", "U", "D", "R")
_PRIV_NAME = {"S": "SELECT", "I": "INSERT", "U": "UPDATE", "D": "DELETE", "R": "REFERENCES"}

_PLAIN_IDENT = re.compile(r"^[A-Z][A-Z0-9_$]*$")

IsKeyword = Callable[[str], bool] | None


def quote_ident(name: str, is_keyword: IsKeyword = None) -> str:
    """Quote an identifier the way Firebird needs it (dialect 3)."""
    if _PLAIN_IDENT.match(name) and not (is_keyword is not None and is_keyword(name)):
        return name
    return '"' + name.replace('"', '""') + '"'


def _code(priv: Any) -> str:
    value = priv.privilege
    return str(getattr(value, "value", value))


def _has_grant(priv: Any) -> bool:
    return bool(priv.has_grant())


def _grantee(priv: Any, is_keyword: IsKeyword) -> str:
    utype = int(priv.user_type)
    name = priv.user_name
    if utype == _USER_TYPE and name == "PUBLIC":
        return "PUBLIC"
    return f"{_GRANTEE_KW.get(utype, '')}{quote_ident(name, is_keyword)}"


class GrantIndex:
    """Privileges bucketed by (namespace, subject name); database-level ones apart."""

    def __init__(self, privileges: Iterable[Any]) -> None:
        self._objects: dict[tuple[str, str], list[Any]] = defaultdict(list)
        self.database: list[Any] = []
        self.unmapped: list[Any] = []
        for p in privileges:
            st = int(p.subject_type)
            if st == _DATABASE_TYPE or st in _DDL_NOUN:
                self.database.append(p)
            elif (ns := _SUBJECT_NS.get(st)) is not None:
                self._objects[(ns, p.subject_name)].append(p)
            else:
                self.unmapped.append(p)

    def for_object(self, namespace: str, name: str) -> list[Any]:
        return self._objects.get((namespace, name), [])


def render_grants(privileges: Iterable[Any], namespace: str, subject: str,
                  owner: str | None = None, is_keyword: IsKeyword = None) -> list[str]:
    """GRANT statements for one subject. ``subject`` is the already-quoted object name."""
    groups: dict[tuple[int, str, bool, str], list[Any]] = {}
    for p in privileges:
        utype = int(p.user_type)
        if owner is not None and utype == _USER_TYPE and p.user_name == owner:
            continue  # implicit owner rights
        marker = (p.field_name or "") if namespace == "role" else ""
        groups.setdefault((utype, p.user_name, _has_grant(p), marker), []).append(p)

    out: list[str] = []
    for key in sorted(groups, key=lambda k: (_GRANTEE_ORDER.get(k[0], 99), k[1], k[2], k[3])):
        utype, uname, with_grant, marker = key
        privs = groups[key]
        grantee = _grantee(privs[0], is_keyword)
        if namespace == "role":
            if not any(_code(p) == "M" for p in privs):
                continue
            default = "DEFAULT " if marker == "D" else ""
            option = " WITH ADMIN OPTION" if with_grant else ""
            out.append(f"GRANT {default}{subject} TO {grantee}{option}")
            continue
        option = " WITH GRANT OPTION" if with_grant else ""
        if namespace == "relation":
            parts: list[str] = []
            for code in _TABLE_PRIVS:
                items = [p for p in privs if _code(p) == code]
                if not items:
                    continue
                if all(p.field_name for p in items):
                    cols = sorted({quote_ident(p.field_name, is_keyword) for p in items})
                    parts.append(f"{_PRIV_NAME[code]} ({', '.join(cols)})")
                else:
                    parts.append(_PRIV_NAME[code])
            if parts:
                out.append(f"GRANT {', '.join(parts)} ON {subject} TO {grantee}{option}")
        elif namespace in ("procedure", "function", "package"):
            if any(_code(p) == "X" for p in privs):
                out.append(f"GRANT EXECUTE ON {_ON_KW[namespace]}{subject} TO {grantee}{option}")
        elif namespace in ("exception", "generator"):
            if any(_code(p) == "G" for p in privs):
                out.append(f"GRANT USAGE ON {_ON_KW[namespace]}{subject} TO {grantee}{option}")
    return out


def render_database_grants(privileges: Iterable[Any], is_keyword: IsKeyword = None) -> tuple[list[str], int]:
    """Database-level (DDL) GRANTs. Returns (statements, number of privileges not understood)."""
    out: list[str] = []
    skipped = 0
    for p in sorted(privileges, key=lambda p: (int(p.subject_type), _code(p), int(p.user_type), p.user_name)):
        st, code = int(p.subject_type), _code(p)
        if st == _DATABASE_TYPE:
            what = _DATABASE_VERB.get(code)
        else:
            verb, noun = _DDL_VERB.get(code), _DDL_NOUN.get(st)
            what = f"{verb} {noun}" if verb and noun else None
        if what is None:
            skipped += 1
            continue
        option = " WITH GRANT OPTION" if _has_grant(p) else ""
        out.append(f"GRANT {what} TO {_grantee(p, is_keyword)}{option}")
    return out, skipped
