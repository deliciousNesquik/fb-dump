import io

import pytest

from fb_dump import writer
from fb_dump.layout import MANIFEST, preset
from fb_dump.model import Artifact

MAN = preset("numbered").to_toml()


def _grouped(*pairs):
    return writer.group([Artifact(p, s) for p, s in pairs])


def test_group_keeps_order_and_drops_empty():
    g = writer.group([Artifact("b.sql", "B1"), Artifact("a.sql", "A"), Artifact("b.sql", "  "), Artifact("b.sql", "B2", psql=True)])
    assert list(g) == ["b.sql", "a.sql"]
    assert g["b.sql"] == [("B1", False), ("B2", True)]


def test_write_stdout_headers():
    buf = io.StringIO()
    assert writer.write_stdout(_grouped(("DATABASE.sql", "SET SQL DIALECT 3"), ("07_TABLES/T.sql", "CREATE TABLE T (A INT)")), buf) == 2
    assert buf.getvalue().split("\n")[:3] == ["-- ===== DATABASE.sql =====", "SET SQL DIALECT 3;", ""]
    assert "-- ===== 07_TABLES/T.sql =====" in buf.getvalue()


def test_replace_tree_creates_and_replaces(tmp_path):
    out = tmp_path / "schema"
    assert writer.replace_tree(_grouped(("DATABASE.sql", "SET SQL DIALECT 3"), ("07_TABLES/A.sql", "CREATE TABLE A (X INT)")), out, MAN) == 2
    assert (out / "07_TABLES/A.sql").read_text(encoding="utf-8") == "CREATE TABLE A (X INT);\n"
    assert (out / MANIFEST).read_text(encoding="utf-8") == MAN
    # second dump without A: A is gone, no leftovers next to the tree
    writer.replace_tree(_grouped(("DATABASE.sql", "SET SQL DIALECT 3"), ("07_TABLES/B.sql", "CREATE TABLE B (X INT)")), out, MAN)
    assert not (out / "07_TABLES/A.sql").exists() and (out / "07_TABLES/B.sql").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["schema"]


def test_replace_tree_refuses_foreign_directory_unless_forced(tmp_path):
    out = tmp_path / "home"
    out.mkdir()
    (out / "precious.txt").write_text("keep me")
    with pytest.raises(writer.WriterError):
        writer.replace_tree(_grouped(("DATABASE.sql", "X")), out, MAN)
    assert (out / "precious.txt").exists()
    writer.replace_tree(_grouped(("DATABASE.sql", "X")), out, MAN, force=True)
    assert not (out / "precious.txt").exists() and (out / MANIFEST).exists()


def test_replace_tree_recovers_previous_tree_after_crash(tmp_path):
    out = tmp_path / "schema"
    writer.replace_tree(_grouped(("DATABASE.sql", "OLD")), out, MAN)
    # simulate a crash between the two renames: tree parked under .schema.fb-dump-old, no `schema`
    out.rename(tmp_path / ".schema.fb-dump-old")
    (tmp_path / ".schema.fb-dump-new").mkdir()
    (tmp_path / ".schema.fb-dump-new" / "junk").write_text("x")
    writer.replace_tree(_grouped(("DATABASE.sql", "NEW")), out, MAN)
    assert (out / "DATABASE.sql").read_text(encoding="utf-8") == "NEW;\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["schema"]


def test_replace_tree_target_is_a_file(tmp_path):
    f = tmp_path / "schema"
    f.write_text("not a dir")
    with pytest.raises(writer.WriterError):
        writer.replace_tree(_grouped(("DATABASE.sql", "X")), f, MAN)


def test_update_tree_touches_only_given_files(tmp_path):
    out = tmp_path / "schema"
    writer.replace_tree(_grouped(("DATABASE.sql", "D"), ("07_TABLES/A.sql", "A1"), ("07_TABLES/B.sql", "B1")), out, MAN)
    assert writer.update_tree(_grouped(("07_TABLES/A.sql", "A2")), out, MAN) == 1
    assert (out / "07_TABLES/A.sql").read_text(encoding="utf-8") == "A2;\n"
    assert (out / "07_TABLES/B.sql").read_text(encoding="utf-8") == "B1;\n"
    assert (out / "DATABASE.sql").exists()


def test_update_tree_new_dir_gets_manifest_and_foreign_dir_is_refused(tmp_path):
    fresh = tmp_path / "fresh"
    writer.update_tree(_grouped(("x/A.sql", "A")), fresh, MAN)
    assert (fresh / MANIFEST).exists() and (fresh / "x/A.sql").exists()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "file").write_text("x")
    with pytest.raises(writer.WriterError):
        writer.update_tree(_grouped(("A.sql", "A")), foreign, MAN)
    writer.update_tree(_grouped(("A.sql", "A")), foreign, MAN, force=True)
    assert (foreign / "file").exists() and (foreign / "A.sql").exists()
