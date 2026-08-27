"""Turn a file's statements into runnable ``isql`` text.

firebird-lib returns statements without terminators and without ``SET TERM``;
both are added here. Consecutive PSQL statements share one ``SET TERM ^ ;`` block;
everything else ends with ``;``. A statement starting with ``--`` is a comment and
is emitted verbatim.
"""

from __future__ import annotations

from typing import Iterable

Statement = tuple[str, bool]  # (sql, psql)


def render(statements: Iterable[Statement]) -> str:
    out: list[str] = []
    in_psql = False
    for sql, psql in statements:
        text = sql.strip()
        if not text:
            continue
        if text.startswith("--"):
            if in_psql:
                out += ["SET TERM ; ^", ""]
                in_psql = False
            out += [text, ""]
            continue
        if psql and not in_psql:
            out += ["SET TERM ^ ;", ""]
            in_psql = True
        elif not psql and in_psql:
            out += ["SET TERM ; ^", ""]
            in_psql = False
        if psql:
            out += [text.rstrip("^").rstrip(), "^", ""]
        else:
            out += [text.rstrip(";").rstrip() + ";", ""]
    if in_psql:
        out += ["SET TERM ; ^", ""]
    return "\n".join(out)
