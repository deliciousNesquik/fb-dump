"""Regression tests for the defects found in review: writer edge cases, layout
validation, library patches, CLI failure modes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fakes import FObj, FPriv, FSchema

from fb_dump import cli, db, layout, writer
from fb_dump.layout import MANIFEST, LayoutError, from_dict, preset
from fb_dump.model import Artifact, Context

MAN = preset("numbered").to_toml()
OWNED = preset("numbered").top_level_entries()


def _g(*pairs):
    return writer.group([Artifact(p, s) for p, s in pairs])


# ------------------------------------------------------------------ writer

def test_unowned_entries_block_replace_unless_forced(tmp_path):
    out = tmp_path / "schema"
    writer.replace_tree(_g(("DATABASE.sql", "D")), out, MAN, owned=OWNED)
    (out / ".git").mkdir()
    (out / "README.md").write_text("mine")
    (out / "stray.sql").write_text("ok")                     # .sql files in the root are tolerated
    with pytest.raises(writer.WriterError, match=r"\.git, README\.md"):
        writer.replace_tree(_g(("DATABASE.sql", "D")), out, MAN, owned=OWNED)
    assert (out / "README.md").exists()
    writer.replace_tree(_g(("DATABASE.sql", "D")), out, MAN, force=True, owned=OWNED)
    assert not (out / ".git").exists() and not (out / "stray.sql").exists()


def test_switching_layout_is_not_treated_as_foreign_entries(tmp_path):
    out = tmp_path / "schema"
    writer.replace_tree(_g(("07_TABLES/A.sql", "A")), out, MAN, owned=OWNED)
    plain = preset("plain")
    writer.replace_tree(_g(("TABLES/A.sql", "A")), out, plain.to_toml(), owned=plain.top_level_entries())
    assert (out / "TABLES/A.sql").exists() and not (out / "07_TABLES").exists()


def test_in_place_rebuild_when_cwd_is_inside_the_tree(tmp_path, monkeypatch):
    out = tmp_path / "schema"
    writer.replace_tree(_g(("DATABASE.sql", "OLD"), ("07_TABLES/A.sql", "A")), out, MAN)
    inode = out.stat().st_ino
    monkeypatch.chdir(out)
    writer.replace_tree(_g(("DATABASE.sql", "NEW"), ("07_TABLES/B.sql", "B")), Path("."), MAN, owned=OWNED)
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
    monkeypatch.setattr(cli.db, "open_schema", lambda settings, con: schema)
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
