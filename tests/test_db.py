"""ResilientSchema without a real database."""

from __future__ import annotations

import pytest

from fb_dump import db
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
    s = Settings(database="x", user=None, password=None, role=None, charset="UTF8", fallback_charset=None, isolation="read-committed")
    assert open_schema(s, con) is con.schema
    s2 = Settings(database="x", user=None, password=None, role=None, charset="UTF8", fallback_charset="WIN1251", isolation="read-committed")
    assert isinstance(open_schema(s2, con), ResilientSchema)


def test_fallback_failure_reports_original_error():
    class _BadFallback:
        @property
        def procedures(self):
            raise ValueError("fallback broke too")

    rs = ResilientSchema(_Primary(), lambda: _Con(_BadFallback()), "WIN1251")
    with pytest.raises(RuntimeError, match="transliterate"):
        rs.procedures


class TestIsolation:
    """The transaction firebird-lib opens for the catalog: level configurable, NO WAIT always."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        # set_isolation writes process-global state (see db.py); never leak it into other tests
        yield
        db.set_isolation(None)

    def _tpb(self, monkeypatch):
        seen: dict = {}

        def fake_tpb(isolation, lock_timeout, access_mode):
            seen.update(isolation=isolation, lock_timeout=lock_timeout, access_mode=access_mode)
            return b"tpb"

        monkeypatch.setattr(db, "_driver_tpb", fake_tpb)
        return seen

    def test_default_keeps_what_the_library_asked_for(self, monkeypatch):
        from firebird.driver import Isolation
        seen = self._tpb(monkeypatch)
        db.set_isolation("read-committed")
        db._nowait_tpb(Isolation.SERIALIZABLE, lock_timeout=-1, access_mode="READ")
        assert seen["isolation"] is Isolation.READ_COMMITTED_RECORD_VERSION
        assert seen["lock_timeout"] == 0                      # NO WAIT, whatever was requested

    def test_snapshot_overrides_the_library(self, monkeypatch):
        from firebird.driver import Isolation
        seen = self._tpb(monkeypatch)
        db.set_isolation("snapshot")
        db._nowait_tpb(Isolation.READ_COMMITTED_RECORD_VERSION, lock_timeout=-1, access_mode="READ")
        assert seen["isolation"] is Isolation.SNAPSHOT         # concurrency: consistent view, blocks nobody
        assert seen["lock_timeout"] == 0
        assert seen["access_mode"] == "READ"

    def test_none_restores_the_library_choice(self, monkeypatch):
        from firebird.driver import Isolation
        seen = self._tpb(monkeypatch)
        db.set_isolation(None)
        db._nowait_tpb(Isolation.READ_COMMITTED_NO_RECORD_VERSION, lock_timeout=-1, access_mode="READ")
        assert seen["isolation"] is Isolation.READ_COMMITTED_NO_RECORD_VERSION

    def test_unknown_level_rejected(self):
        with pytest.raises(ValueError, match="unknown isolation"):
            db.set_isolation("table-stability")

    def test_open_schema_applies_the_setting(self, monkeypatch):
        applied: list[str | None] = []
        monkeypatch.setattr(db, "set_isolation", lambda name: applied.append(name))
        con = _Con(_Primary())
        s = Settings(database="x", user=None, password=None, role=None, charset="UTF8",
                     fallback_charset=None, isolation="snapshot")
        db.open_schema(s, con)
        assert applied == ["snapshot"]


class TestIsolationSupport:
    """read-consistency exists only on Firebird 4+; say so instead of failing inside the driver."""

    def _con(self, engine):
        return type("C", (), {"info": type("I", (), {"engine_version": engine})(), "schema": _Primary()})()

    def _settings(self, isolation):
        return Settings(database="x", user=None, password=None, role=None, charset="UTF8",
                        fallback_charset=None, isolation=isolation)

    def test_read_consistency_rejected_on_old_servers(self):
        with pytest.raises(RuntimeError, match="needs Firebird 4"):
            open_schema(self._settings("read-consistency"), self._con(3.0))

    def test_read_consistency_accepted_on_firebird_4_and_5(self):
        for engine in (4.0, 5.0):
            assert open_schema(self._settings("read-consistency"), self._con(engine)) is not None
        db.set_isolation(None)

    def test_other_levels_do_not_check_the_version(self):
        for level in ("read-committed", "snapshot"):
            assert open_schema(self._settings(level), self._con(2.5)) is not None
        db.set_isolation(None)

    def test_levels_offered_match_the_config(self):
        from fb_dump.config import ISOLATIONS
        assert set(ISOLATIONS) == set(db._ISOLATIONS)
        from firebird.driver import Isolation
        # never offer levels that block writers or fail under NO WAIT
        assert Isolation.SERIALIZABLE not in db._ISOLATIONS.values()
        assert Isolation.READ_COMMITTED_NO_RECORD_VERSION not in db._ISOLATIONS.values()
