"""Regression tests for the defects found in review: writer edge cases, layout
validation, library patches, CLI failure modes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fakes import FChild, FConstraint, FGenerator, FObj, FPriv, FSchema

from fb_dump import cli, db, layout, writer
from fb_dump.layout import MANIFEST, LayoutError, from_dict, preset
from fb_dump.model import Artifact, Context

MAN = preset("numbered").to_toml()
OWNED = preset("numbered")


def _g(*pairs):
    return writer.group([Artifact(p, s) for p, s in pairs])


# ------------------------------------------------------------------ writer

def test_unowned_entries_block_replace_unless_forced(tmp_path):
    out = tmp_path / "schema"
    writer.replace_tree(_g(("DATABASE.sql", "D")), out, MAN, layout=OWNED)
    (out / ".git").mkdir()
    (out / "README.md").write_text("mine")
    (out / "stray.sql").write_text("mine")                   # not a file this layout writes
    with pytest.raises(writer.WriterError, match=r"\.git, README\.md, stray\.sql"):
        writer.replace_tree(_g(("DATABASE.sql", "D")), out, MAN, layout=OWNED)
    assert (out / "README.md").exists()
    writer.replace_tree(_g(("DATABASE.sql", "D")), out, MAN, force=True, layout=OWNED)
    assert not (out / ".git").exists() and not (out / "stray.sql").exists()


def test_switching_layout_is_not_treated_as_foreign_entries(tmp_path):
    out = tmp_path / "schema"
    writer.replace_tree(_g(("07_TABLES/A.sql", "A")), out, MAN, layout=OWNED)
    plain = preset("plain")
    writer.replace_tree(_g(("TABLES/A.sql", "A")), out, plain.to_toml(), layout=plain)
    assert (out / "TABLES/A.sql").exists() and not (out / "07_TABLES").exists()


def test_in_place_rebuild_when_cwd_is_inside_the_tree(tmp_path, monkeypatch):
    out = tmp_path / "schema"
    writer.replace_tree(_g(("DATABASE.sql", "OLD"), ("07_TABLES/A.sql", "A")), out, MAN)
    inode = out.stat().st_ino
    monkeypatch.chdir(out)
    writer.replace_tree(_g(("DATABASE.sql", "NEW"), ("07_TABLES/B.sql", "B")), Path("."), MAN, layout=OWNED)
    assert out.stat().st_ino == inode                          # the directory itself survived
    assert Path.cwd() == out                                   # …and so did the caller's cwd
    assert (out / "DATABASE.sql").read_text() == "NEW;\n"
    assert not (out / "07_TABLES/A.sql").exists() and (out / "07_TABLES/B.sql").exists()
    assert sorted(p.name for p in out.iterdir()) == [MANIFEST, "07_TABLES", "DATABASE.sql"]


def test_in_place_rebuild_when_parent_is_not_writable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    parent = tmp_path / "ro"
    out = parent / "schema"
    writer.replace_tree(_g(("DATABASE.sql", "OLD")), out, MAN)
    parent.chmod(0o555)
    try:
        writer.replace_tree(_g(("DATABASE.sql", "NEW")), out, MAN)
        assert (out / "DATABASE.sql").read_text() == "NEW;\n"
        assert sorted(p.name for p in parent.iterdir()) == ["schema"]
    finally:
        parent.chmod(0o755)


def test_rename_failure_falls_back_to_in_place(tmp_path, monkeypatch):
    out = tmp_path / "schema"
    writer.replace_tree(_g(("DATABASE.sql", "OLD"), ("07_TABLES/A.sql", "A")), out, MAN)
    real_rename = writer.Path.rename

    def locked(self, target):
        if self == out:
            raise PermissionError("[WinError 32] in use")
        return real_rename(self, target)

    monkeypatch.setattr(writer.Path, "rename", locked)
    assert writer.replace_tree(_g(("DATABASE.sql", "NEW")), out, MAN) == 1
    assert (out / "DATABASE.sql").read_text() == "NEW;\n" and not (out / "07_TABLES").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["schema"]


def test_staging_is_removed_when_writing_fails(tmp_path, monkeypatch):
    out = tmp_path / "schema"

    def enospc(grouped, root):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(writer, "_write_files", enospc)
    with pytest.raises(OSError):
        writer.replace_tree(_g(("DATABASE.sql", "X")), out, MAN)
    assert list(tmp_path.iterdir()) == []


def test_stale_old_directory_without_manifest_is_not_restored(tmp_path):
    out = tmp_path / "schema"
    stale = tmp_path / ".schema.fb-dump-old"
    stale.mkdir()
    (stale / "leftover.txt").write_text("x")
    writer.replace_tree(_g(("DATABASE.sql", "X")), out, MAN)
    assert not stale.exists() and (out / "DATABASE.sql").exists()


def test_forced_update_does_not_mark_a_foreign_directory(tmp_path, capsys):
    foreign = tmp_path / "work"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("x")
    writer.update_tree(_g(("07_TABLES/A.sql", "A")), foreign, MAN, force=True)
    assert (foreign / "07_TABLES/A.sql").exists() and not (foreign / MANIFEST).exists()
    assert "stays unmarked" in capsys.readouterr().err
    with pytest.raises(writer.WriterError):                     # so a later full dump still needs --force
        writer.replace_tree(_g(("DATABASE.sql", "X")), foreign, MAN)


# ------------------------------------------------------------------ layout

@pytest.mark.parametrize("data", [
    {"dirs": {"table": "./C:/x"}},
    {"dirs": {"table": "a/C:b"}},
    {"dirs": {"table": 'a*b'}},
    {"dirs": {"table": "a. "}},
    {"file": "C:{name}.sql"},
    {"file": "{type}:{name}.sql"},
    {"file": "{name!r}.sql"},
    {"file": "{name:>10}.sql"},
    {"file": "{name}\x01.sql"},
    {"database": "C:DB.sql"},
    {"database": "DB?.sql"},
])
def test_layout_rejects_paths_that_escape_or_break(data):
    with pytest.raises(LayoutError):
        from_dict(data, source="t")


def test_layout_file_with_bom_loads(tmp_path):
    f = tmp_path / "lay.toml"
    f.write_bytes(b"\xef\xbb\xbf" + b'base = "flat"\n')
    assert layout.load(str(f)) == preset("flat")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / MANIFEST).write_bytes(b"\xef\xbb\xbf" + preset("plain").to_toml().encode())
    assert layout.load_manifest(tree) == preset("plain")


def test_top_level_entries():
    assert preset("numbered").top_level_entries() == {"DATABASE.sql", *{f"{i:02d}_{d}" for i, d in enumerate(
        ["ROLES", "COLLATIONS", "EXTERNAL_FUNCTIONS", "GENERATORS", "EXCEPTIONS", "DOMAINS", "TABLES", "INDICES",
         "FUNCTIONS", "VIEWS", "PROCEDURES", "PACKAGES", "TRIGGERS"], 1)}}
    assert from_dict({"dirs": {"table": "Схема/Таблицы", "view": ""}}, source="t").top_level_entries() >= {"Схема", "DATABASE.sql"}


# ------------------------------------------------------------------ db patches

class _Dom:
    def __init__(self, field_type, precision=None, sub_type=0, scale=0):
        self.field_type, self.precision, self.sub_type, self.scale = field_type, precision, sub_type, scale


def test_domain_datatype_knows_firebird4_types():
    from firebird.lib.schema import COLUMN_TYPES, FieldSubType, FieldType
    assert db._datatype(_Dom(FieldType.INT128)) == "INT128"
    assert db._datatype(_Dom(FieldType.INT128, precision=20, sub_type=FieldSubType.NUMERIC, scale=-2)) == "NUMERIC(20, 2)"
    assert db._datatype(_Dom(FieldType.INT128, precision=38, sub_type=FieldSubType.DECIMAL, scale=0)) == "DECIMAL(38, 0)"
    assert db._datatype(_Dom(FieldType.DEC16)) == "DECFLOAT(16)"
    assert db._datatype(_Dom(FieldType.DEC34)) == "DECFLOAT(34)"
    assert db._datatype(_Dom(FieldType.TIME_TZ)) == "TIME WITH TIME ZONE"
    assert db._datatype(_Dom(FieldType.TIMESTAMP_TZ_EX)) == "TIMESTAMP WITH TIME ZONE"
    assert COLUMN_TYPES[FieldType.INT128] == "INT128"


def test_null_system_flag_means_user_object():
    class _Item:
        def __init__(self, flag):
            self._attributes = {"RDB$SYSTEM_FLAG": flag}

    assert db._is_sys_object(_Item(None)) is False
    assert db._is_sys_object(_Item(0)) is False
    assert db._is_sys_object(_Item(1)) is True
    assert db._is_sys_object(_Item(3)) is True
    from firebird.lib.schema import SchemaItem
    assert SchemaItem.is_sys_object is db._is_sys_object


# ------------------------------------------------------------------ cli

def _fake_driver(monkeypatch, schema):
    class _Con:
        def close(self):
            pass

    monkeypatch.setenv("FB_DATABASE", "fake")
    monkeypatch.setattr(cli.db, "connect", lambda settings, charset=None: _Con())
    monkeypatch.setattr(cli.db, "open_schema",
                        lambda settings, con: (cli.db.set_isolation(settings.isolation), schema)[1])
    monkeypatch.setattr(cli.db, "dialect", lambda con: 3)


def test_collection_failure_is_exit_1_not_a_pile_of_skips(monkeypatch, capsys):
    class _Bad(FSchema):
        @property
        def constraints(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

        @constraints.setter
        def constraints(self, value):
            pass

    _fake_driver(monkeypatch, _Bad(tables=[FObj("A"), FObj("B")]))
    assert cli.main([]) == 1
    err = capsys.readouterr().err
    assert "Skipping" not in err and "--charset" in err


def test_targeted_export_with_no_matches_writes_nothing(monkeypatch, tmp_path):
    _fake_driver(monkeypatch, FSchema(tables=[FObj("A")]))
    out = tmp_path / "new"
    assert cli.main(["NOPE", "-o", str(out)]) == 3
    assert not out.exists()


def test_type_accepts_category_keys(monkeypatch, capsys):
    _fake_driver(monkeypatch, FSchema(functions=[FObj("UDF", external=True), FObj("F")]))
    assert cli.main(["--list", "--type", "external_function"]) == 0
    assert capsys.readouterr().out == "external_function\tUDF\n"


def test_system_role_privileges_are_reported(monkeypatch, capsys):
    _fake_driver(monkeypatch, FSchema(tables=[FObj("A")], privileges=[FPriv("U", "S", "RDB$DATABASE")]))
    assert cli.main([]) == 0
    assert "not in the dump (system objects)" in capsys.readouterr().err


def test_target_is_checked_before_connecting(monkeypatch, tmp_path, capsys):
    """A doomed --out must be reported without reading the schema (that takes minutes)."""
    calls: list[str] = []

    def boom(settings, charset=None):
        calls.append("connect")
        raise AssertionError("must not connect")

    monkeypatch.setenv("FB_DATABASE", "fake")
    monkeypatch.setattr(cli.db, "connect", boom)
    foreign = tmp_path / "work"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("keep")

    assert cli.main(["-o", str(foreign)]) == 1
    assert calls == []
    assert "was not written by fb-dump" in capsys.readouterr().err
    assert (foreign / "notes.txt").exists()

    # a tree of ours with a foreign entry is refused just as early…
    tree = tmp_path / "schema"
    writer.replace_tree(_g(("DATABASE.sql", "D")), tree, MAN, layout=OWNED)
    (tree / ".git").mkdir()
    assert cli.main(["-o", str(tree)]) == 1
    assert calls == []
    assert "a full dump would delete" in capsys.readouterr().err
    # …but a targeted export into it is allowed (it deletes nothing)
    monkeypatch.setattr(cli.db, "open_schema", lambda settings, con: FSchema(tables=[FObj("A")]))
    monkeypatch.setattr(cli.db, "dialect", lambda con: 3)
    monkeypatch.setattr(cli.db, "connect", lambda settings, charset=None: type("C", (), {"close": lambda self: None})())
    assert cli.main(["A", "-o", str(tree)]) == 0
    assert (tree / ".git").exists()


def test_isolation_reaches_the_driver_end_to_end(monkeypatch, capsys):
    """--isolation must be applied before the schema binds its read transaction."""
    applied: list[str] = []
    monkeypatch.setattr(cli.db, "set_isolation", lambda name: applied.append(name))
    _fake_driver(monkeypatch, FSchema(tables=[FObj("A")]))
    assert cli.main(["--list", "--isolation", "snapshot", "-v"]) == 0
    assert applied == ["snapshot"]
    assert "isolation=snapshot" in capsys.readouterr().err

    assert cli.main(["--list"]) == 0
    assert applied[-1] == "read-committed"


def test_unknown_isolation_is_a_usage_error():
    with pytest.raises(SystemExit) as e:
        cli.main(["--list", "--isolation", "serializable"])
    assert e.value.code == 2


# ------------------------------------------------- Change A: ordered script

def test_phase_order_covers_every_emission_site():
    """Every artifact a category can emit must carry a deliberate phase."""
    from fb_dump.model import Phase
    ctx = Context(FSchema(privileges=[FPriv("U", "S", "T"), FPriv("U", "S", "V"),
                                      FPriv("U", "M", "R", subject_type=13)]),
                  preset("numbered"), dialect=3)
    seen: dict[str, set[Phase]] = {}
    objects = {
        "role": FObj("R", description="d"), "collation": FObj("C", description="d"),
        "external_function": FObj("F", external=True, description="d"),
        "generator": FObj("G", increment=5, description="d"), "exception": FObj("E", description="d"),
        "domain": FObj("D", description="d"),
        "table": FObj("T", description="d", attributes={"RDB$RELATION_TYPE": 4, "RDB$SQL_SECURITY": True},
                      columns=[FChild("ID", "c", identity=0, generator=FGenerator(increment=2))],
                      constraints=[FConstraint("PK", "pkey", ["A"]), FConstraint("U1", "unique", ["B"]),
                                   FConstraint("CK", "check"), FConstraint("FK", "fkey", ["C"], ["D"])]),
        "index": FObj("IX", inactive=True, description="d"), "function": FObj("FN", description="d"),
        "view": FObj("V", description="d"), "procedure": FObj("P", description="d"),
        "package": FObj("K", body="x", description="d"), "trigger": FObj("TR", description="d"),
    }
    for key, obj in objects.items():
        seen[key] = {a.phase for a in cli.categories.CATEGORY_BY_KEY[key].emit(ctx, obj, stub=True)}

    assert seen["role"] == {Phase.ROLE, Phase.COMMENT, Phase.GRANT}
    assert seen["domain"] == {Phase.DOMAIN, Phase.COMMENT}
    assert seen["table"] >= {Phase.TABLE, Phase.TABLE_ALTER, Phase.KEY, Phase.CHECK, Phase.FOREIGN_KEY}
    assert seen["index"] == {Phase.INDEX, Phase.INDEX_STATE, Phase.COMMENT}
    assert Phase.ROUTINE_STUB in seen["function"] and Phase.ROUTINE in seen["function"]
    assert Phase.ROUTINE_STUB in seen["procedure"] and Phase.ROUTINE in seen["procedure"]
    assert seen["package"] >= {Phase.ROUTINE_STUB, Phase.ROUTINE}     # header is the stub
    assert seen["view"] == {Phase.VIEW, Phase.COMMENT, Phase.GRANT}
    assert seen["trigger"] == {Phase.TRIGGER, Phase.COMMENT}


def test_ddl_and_database_triggers_come_after_plain_ones():
    from fb_dump.model import Phase
    ctx = Context(FSchema(), preset("numbered"), dialect=3)
    cat = cli.categories.CATEGORY_BY_KEY["trigger"]
    dml = cat.emit(ctx, FObj("T"))[0]
    ddl = cat.emit(ctx, FObj("D", ddl=True, attributes={"RDB$TRIGGER_TYPE": 16384 | (1 << 3)}))[0]
    assert dml.phase is Phase.TRIGGER and ddl.phase is Phase.TRIGGER_DDL
    assert ddl.phase > dml.phase


def test_stub_only_appears_when_asked_and_never_for_udr():
    from fb_dump.model import Phase
    ctx = Context(FSchema(), preset("numbered"), dialect=3)
    cat = cli.categories.CATEGORY_BY_KEY["procedure"]
    assert [a.phase for a in cat.emit(ctx, FObj("P"))] == [Phase.ROUTINE]          # tree: no stub
    stubbed = cat.emit(ctx, FObj("P"), stub=True)
    assert [a.phase for a in stubbed] == [Phase.ROUTINE_STUB, Phase.ROUTINE]
    assert stubbed[0].sql.startswith("CREATE OR ALTER") and "BEGIN\nEND" in stubbed[0].sql
    # A UDR routine needs no stub — its definition is already header-only — but it
    # must land in the stub phase, because views and routines after it may call it.
    udr = FObj("U", attributes={"RDB$ENGINE_NAME": "UDR", "RDB$ENTRYPOINT": "x!y"}, source=None)
    assert [a.phase for a in cat.emit(ctx, udr, stub=True)] == [Phase.ROUTINE_STUB]


def test_write_script_sorts_by_phase_and_is_stable(capsys):
    from fb_dump.model import Phase
    arts = [Artifact("a.sql", "GRANT A", phase=Phase.GRANT),
            Artifact("b.sql", "CREATE TABLE B", phase=Phase.TABLE),
            Artifact("c.sql", "GRANT B", phase=Phase.GRANT),
            Artifact("d.sql", "SET SQL DIALECT 3", phase=Phase.DIALECT),
            Artifact("e.sql", "   ", phase=Phase.TABLE)]
    assert writer.write_script(arts) == 4                       # blank dropped
    out = capsys.readouterr().out
    assert [l for l in out.split("\n") if l.strip()] == [
        "SET SQL DIALECT 3;", "CREATE TABLE B;", "GRANT A;", "GRANT B;"]   # GRANT A before GRANT B


def test_tree_output_untouched_by_phases(tmp_path):
    """Phases must not change what lands in a file, only the script order."""
    ctx = Context(FSchema(tables=[FObj("T", description="d",
                                       constraints=[FConstraint("FK", "fkey"), FConstraint("PK", "pkey")])],
                          privileges=[FPriv("U", "S", "T")]), preset("numbered"), dialect=3)
    out = tmp_path / "tree"
    cli.run_full(ctx, out, allow_partial=False, force=False)
    assert (out / "07_TABLES/T.sql").read_text(encoding="utf-8") == (
        "CREATE OBJ T;\n\nALTER TABLE ADD CONSTRAINT PK (pkey);\n\n"
        "ALTER TABLE ADD CONSTRAINT FK (fkey);\n\nCOMMENT ON T IS 'd';\n\nGRANT SELECT ON T TO USER U;\n")
