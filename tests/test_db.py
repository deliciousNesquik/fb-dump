"""ResilientSchema without a real database."""

from __future__ import annotations

import pytest

from fb_dump.config import Settings
from fb_dump.db import ResilientSchema, open_schema


class _Primary:
    other = "delegated"

    @property
    def procedures(self):
        raise RuntimeError("Cannot transliterate character between character sets")

    @property
    def tables(self):
        return ["T1", "T2"]


class _Fallback:
    @property
    def procedures(self):
        return ["P1", "P2"]


class _Con:
    def __init__(self, schema):
        self.schema = schema
        self.closed = False

    def close(self):
        self.closed = True


def _make():
    cons: list[_Con] = []

    def factory():
        c = _Con(_Fallback())
        cons.append(c)
        return c

    return ResilientSchema(_Primary(), factory, "WIN1251"), cons


def test_failing_collection_read_via_fallback_once():
    rs, cons = _make()
    assert list(rs.procedures) == ["P1", "P2"]
    list(rs.procedures)
    assert len(cons) == 1


def test_ok_collection_stays_on_primary_and_other_attrs_delegate():
    rs, cons = _make()
    assert list(rs.tables) == ["T1", "T2"]
    assert rs.other == "delegated"
    assert cons == []


def test_close_fallback():
    rs, cons = _make()
    list(rs.procedures)
    rs.close_fallback()
    assert cons[0].closed is True
    rs.close_fallback()  # idempotent


def test_open_schema_without_fallback_is_plain():
    con = _Con(_Primary())
    s = Settings(database="x", user=None, password=None, role=None, charset="UTF8", fallback_charset=None)
    assert open_schema(s, con) is con.schema
    s2 = Settings(database="x", user=None, password=None, role=None, charset="UTF8", fallback_charset="WIN1251")
    assert isinstance(open_schema(s2, con), ResilientSchema)


def test_fallback_failure_reports_original_error():
    class _BadFallback:
        @property
        def procedures(self):
            raise ValueError("fallback broke too")

    rs = ResilientSchema(_Primary(), lambda: _Con(_BadFallback()), "WIN1251")
    with pytest.raises(RuntimeError, match="transliterate"):
        rs.procedures
