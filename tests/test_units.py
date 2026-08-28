"""Function-level tests for branches the scenario tests do not reach."""

from __future__ import annotations

import subprocess
import sys

import pytest
from fakes import FObj, FPriv, FSchema

from fb_dump import categories, cli, db, layout, log, writer
from fb_dump.layout import preset
from fb_dump.model import Artifact, Context
from fb_dump.render import render


# ---------------------------------------------------------------- __main__ / log

def test_python_m_entry_point():
    proc = subprocess.run([sys.executable, "-m", "fb_dump", "--print-layout"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0 and 'file = "{name}.sql"' in proc.stdout


def test_log_levels(capsys):
    log.set_level(log.QUIET)
    log.error("e"); log.warning("w"); log.info("i"); log.debug("d")
    assert capsys.readouterr().err == "[ERROR] e\n"
    log.set_level(log.VERBOSE)
    log.warning("w"); log.debug("d")
    assert capsys.readouterr().err == "[WARNING] w\n[DEBUG] d\n"
    log.set_level(log.NORMAL)
    log.debug("d"); log.info("i")
    assert capsys.readouterr().err == "[INFO] i\n"


# ---------------------------------------------------------------- categories: remaining kinds

def _ctx(**kw):
    return Context(FSchema(**kw), preset("plain"), dialect=3)


def _sql(ctx, key, obj):
    return [(a.path, a.sql, a.psql) for a in categories.CATEGORY_BY_KEY[key].emit(ctx, obj)]


def test_collation_exception_domain_function():
    ctx = _ctx(privileges=[FPriv("U", "G", "E", subject_type=7), FPriv("U", "X", "F", subject_type=15)])
    assert _sql(ctx, "collation", FObj("C", description="c")) == [
        ("COLLATIONS/C.sql", "CREATE OBJ C", False), ("COLLATIONS/C.sql", "COMMENT ON C IS 'c'", False)]
    assert _sql(ctx, "exception", FObj("E")) == [
        ("EXCEPTIONS/E.sql", "CREATE OR ALTER OBJ E\nRETURNS INTEGER\nAS\nBEGIN END", False),
        ("EXCEPTIONS/E.sql", "GRANT USAGE ON EXCEPTION E TO USER U", False)]
    assert _sql(ctx, "domain", FObj("D", description="d")) == [
        ("DOMAINS/D.sql", "CREATE OBJ D", False), ("DOMAINS/D.sql", "COMMENT ON D IS 'd'", False)]
    assert _sql(ctx, "function", FObj("F")) == [
        ("FUNCTIONS/F.sql", "CREATE OR ALTER OBJ F\nRETURNS INTEGER\nAS\nBEGIN END", True),
        ("FUNCTIONS/F.sql", "GRANT EXECUTE ON FUNCTION F TO USER U", False)]


def test_preamble_without_charset_and_with_unknown_ddl_grant(capsys):
    class _NoCharset(FSchema):
        @property
        def default_character_set(self):
            raise RuntimeError("not loaded")

        @default_character_set.setter
        def default_character_set(self, value):
            pass

    ctx = Context(_NoCharset(privileges=[FPriv("U", "S", "X", subject_type=22)]), preset("plain"), dialect=3)
    arts = categories.database_preamble(ctx)
    assert [a.sql for a in arts] == ["SET SQL DIALECT 3"]
    assert "not dumped" in capsys.readouterr().err


def test_grants_helper_returns_nothing_without_privileges():
    ctx = _ctx()
    assert _sql(ctx, "table", FObj("T")) == [("TABLES/T.sql", "CREATE OBJ T", False)]


# ---------------------------------------------------------------- cli internals

def test_collector_section_failure_and_collision(capsys):
    col = cli.Collector()
    col.add_section("database", lambda: (_ for _ in ()).throw(RuntimeError("no privileges")))
    assert col.failures == ["database"]
    col.add_section("database", lambda: [Artifact("DATABASE.sql", "X")])
    col.add_section("other", lambda: [Artifact("database.sql", "Y")])   # collides case-insensitively
    assert col.failures == ["database", "other"]
    assert [a.sql for a in col.artifacts] == ["X"]
    assert "collides" in capsys.readouterr().err


def test_run_full_logs_unmapped_privileges(capsys):
    log.set_level(log.VERBOSE)
    try:
        ctx = _ctx(tables=[FObj("T")], privileges=[FPriv("U", "S", "W", subject_type=9)])
        assert cli.run_full(ctx, None, allow_partial=False, force=False) == 0
    finally:
        log.set_level(log.NORMAL)
    assert "unknown subject types were ignored" in capsys.readouterr().err


def _fake_driver(monkeypatch, schema, close_raises=False, with_fallback=False):
    class _Schema:
        closed = False

        def __getattr__(self, name):
            return getattr(schema, name)

        def close_fallback(self):
            _Schema.closed = True

    class _Con:
        def close(self):
            if close_raises:
                raise RuntimeError("already closed")

    monkeypatch.setenv("FB_DATABASE", "fake")
    monkeypatch.setattr(cli.db, "connect", lambda settings, charset=None: _Con())
    monkeypatch.setattr(cli.db, "open_schema", lambda settings, con: _Schema() if with_fallback else schema)
    monkeypatch.setattr(cli.db, "dialect", lambda con: 3)
    return _Schema


def test_main_writer_error_is_infrastructure(monkeypatch, tmp_path):
    _fake_driver(monkeypatch, FSchema(tables=[FObj("T")]))
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "x").write_text("x")
    assert cli.main(["-o", str(foreign)]) == 1
    assert (foreign / "x").exists()


def test_main_unicode_error_hint(monkeypatch, capsys):
    class _Bad(FSchema):
        @property
        def tables(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        @tables.setter
        def tables(self, value):
            pass

    _fake_driver(monkeypatch, _Bad())
    assert cli.main(["--list"]) == 1
    assert "--charset" in capsys.readouterr().err


def test_main_closes_fallback_and_tolerates_close_errors(monkeypatch):
    schema_cls = _fake_driver(monkeypatch, FSchema(tables=[FObj("T")]), close_raises=True, with_fallback=True)
    assert cli.main(["--list", "-v"]) == 0
    assert schema_cls.closed is True


def test_main_layout_error_is_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("FB_DATABASE", "fake")
    bad = tmp_path / "bad.toml"
    bad.write_text('file = "{owner}.sql"\n', encoding="utf-8")
    assert cli.main(["--layout", str(bad), "--list"]) == 2


# ---------------------------------------------------------------- db

def test_connect_passes_only_given_credentials(monkeypatch):
    calls = {}

    def fake_connect(database, **kw):
        calls["database"], calls["kw"] = database, kw
        return "CON"

    monkeypatch.setattr(db, "_connect", fake_connect)
    from fb_dump.config import Settings
    s = Settings(database="host:db", user=None, password=None, role=None, charset="UTF8", fallback_charset="WIN1251", isolation="read-committed")
    assert db.connect(s) == "CON" and calls["kw"] == {"charset": "UTF8"}
    db.connect(s, charset="WIN1251")
    assert calls["kw"] == {"charset": "WIN1251"}
    s2 = Settings(database="db", user="U", password="P", role="R", charset="UTF8", fallback_charset=None, isolation="read-committed")
    db.connect(s2)
    assert calls["kw"] == {"charset": "UTF8", "user": "U", "password": "P", "role": "R"}


def test_dialect_defaults_to_3():
    class _Info:
        sql_dialect = 1

    class _Con:
        info = _Info()

    assert db.dialect(_Con()) == 1
    assert db.dialect(object()) == 3


def test_nowait_tpb_forces_lock_timeout_zero(monkeypatch):
    seen = {}

    def fake_tpb(isolation, lock_timeout, access_mode):
        seen.update(isolation=isolation, lock_timeout=lock_timeout, access_mode=access_mode)
        return b"tpb"

    monkeypatch.setattr(db, "_driver_tpb", fake_tpb)
    db.set_isolation(None)                     # no --isolation: pass the library's own choice through
    assert db._nowait_tpb("ISO", lock_timeout=-1, access_mode="READ") == b"tpb"
    assert seen == {"isolation": "ISO", "lock_timeout": 0, "access_mode": "READ"}
    import firebird.lib.schema as fbs
    assert fbs.tpb is db._nowait_tpb      # the schema module resolves our wrapper


def test_close_fallback_swallows_close_errors():
    class _Con:
        schema = object()

        def close(self):
            raise RuntimeError("gone")

    rs = db.ResilientSchema(object(), lambda: _Con(), "WIN1251")
    rs._fallback()
    rs.close_fallback()                    # must not raise
    assert rs._fallback_con is None


# ---------------------------------------------------------------- layout / render / writer leftovers

def test_manifest_with_broken_toml_raises(tmp_path):
    (tmp_path / layout.MANIFEST).write_text("= broken", encoding="utf-8")
    with pytest.raises(layout.LayoutError):
        layout.load_manifest(tmp_path)


def test_layout_edge_validation():
    with pytest.raises(layout.LayoutError):
        layout.from_dict({"files": {"table": ""}}, source="t")
    with pytest.raises(layout.LayoutError):
        layout.from_dict({"file": "{name"}, source="t")          # unbalanced brace
    with pytest.raises(layout.LayoutError):
        layout.from_dict({"dirs": {"table": "C:\\x"}}, source="t")
    with pytest.raises(layout.LayoutError):
        layout.preset("nope")
    lay = layout.from_dict({"files": {"view": "{type}-{name}.sql"}}, source="t")
    assert lay.path_for("view", "V") == "10_VIEWS/view-V.sql"
    assert "[files]" in lay.to_toml()


def test_render_comment_closes_open_psql_block():
    out = render([("CREATE OR ALTER PROCEDURE P AS BEGIN END", True), ("-- note", False)])
    assert out.split("\n") == ["SET TERM ^ ;", "", "CREATE OR ALTER PROCEDURE P AS BEGIN END", "^", "", "SET TERM ; ^", "", "-- note", ""]


def test_writer_remove_handles_files_and_symlinks(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    link = tmp_path / "l"
    link.symlink_to(f)
    writer._remove(link)
    writer._remove(f)
    writer._remove(tmp_path / "missing")
    assert not f.exists() and not link.is_symlink()


def test_replace_tree_rolls_back_when_swap_fails(tmp_path, monkeypatch):
    out = tmp_path / "schema"
    man = preset("numbered").to_toml()
    writer.replace_tree(writer.group([Artifact("DATABASE.sql", "OLD")]), out, man)

    real_rename = writer.Path.rename

    def flaky_rename(self, target):
        if self.name.endswith(".fb-dump-new"):
            raise OSError("disk on fire")
        return real_rename(self, target)

    monkeypatch.setattr(writer.Path, "rename", flaky_rename)
    with pytest.raises(OSError):
        writer.replace_tree(writer.group([Artifact("DATABASE.sql", "NEW")]), out, man)
    assert (out / "DATABASE.sql").read_text(encoding="utf-8") == "OLD;\n"   # previous tree restored
