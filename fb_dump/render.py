"""Turn a file's statements into runnable ``isql`` text.

firebird-lib returns statements without terminators and without ``SET TERM``;
both are added here. Consecutive PSQL statements share one ``SET TERM`` block;
everything else ends with ``;``. A statement starting with ``--`` is a comment and
is emitted verbatim.
"""

from __future__ import annotations

from typing import Iterable

Statement = tuple[str, bool]  # (sql, psql)

# Candidates for the PSQL terminator, in preference order. A body may legally
# contain '^' inside a string literal or a comment, which would end the statement
# early, so the terminator is chosen per document from characters the bodies lack.
_TERMINATORS = "^~@#!%"


def _strip_terminator(text: str) -> str:
    """Drop the terminator firebird-lib sometimes leaves at the end of a body."""
    text = text.rstrip()
    return text[:-1].rstrip() if text[-1:] in _TERMINATORS else text


def _terminator(statements: list[Statement]) -> str:
    bodies = "".join(_strip_terminator(sql) for sql, psql in statements if psql)
    return next((c for c in _TERMINATORS if c not in bodies), _TERMINATORS[0])


def _ends_in_line_comment(text: str) -> bool:
    """True if a trailing ``--`` comment would swallow a terminator on the same line."""
    last = text.rsplit("\n", 1)[-1]
    return "--" in last and last.count("'", 0, last.index("--")) % 2 == 0


def render(statements: Iterable[Statement]) -> str:
    items = list(statements)
    term = _terminator(items)
    out: list[str] = []
    in_psql = False
    for sql, psql in items:
        text = sql.strip()
        if not text:
            continue
        if text.startswith("--"):
            if in_psql:
                out += [f"SET TERM ; {term}", ""]
                in_psql = False
            out += [text, ""]
            continue
        if psql and not in_psql:
            out += [f"SET TERM {term} ;", ""]
            in_psql = True
        elif not psql and in_psql:
            out += [f"SET TERM ; {term}", ""]
            in_psql = False
        if psql:
            # The terminator sits on its own line: a body ending in a line comment
            # would otherwise swallow it.
            out += [_strip_terminator(text), term, ""]
        else:
            body = text.rstrip(";").rstrip()
            out += [body, ";", ""] if _ends_in_line_comment(body) else [body + ";", ""]
    if in_psql:
        out += [f"SET TERM ; {term}", ""]
    return "\n".join(out)
