"""Regression tests for the static expertise that produced 1.2.1.

Each test pins a decision that was wrong (or unproven) before: the apply order,
tolerance for catalog codes the library cannot decode, what the writer may
delete, and the assumptions fb-dump makes about firebird-lib and its driver.
"""

from __future__ import annotations

import pathlib

import pytest
from fakes import UNKNOWN, FEnum, FObj, FPriv, FSchema

from fb_dump import categories, cli, db, grants, layout, selection, writer
from fb_dump.model import Artifact, Context, Phase
from fb_dump.render import render


# ------------------------------------------------------------------ apply order

def test_phase_order_is_a_frozen_contract():
    """The script output is only as good as this order; changing it is a decision,
    not a refactor, so the whole sequence is written down here."""
    assert [p.name for p in Phase] == [
        "DIALECT", "ROLE", "COLLATION", "CHARACTER_SET", "UDF", "GENERATOR", "EXCEPTION",
        "DOMAIN", "TABLE", "TABLE_ALTER", "KEY", "ROUTINE_STUB", "CHECK", "FOREIGN_KEY",
        "INDEX", "INDEX_STATE", "VIEW", "ROUTINE", "TRIGGER", "TRIGGER_DDL", "COMMENT", "GRANT",
    ]
    assert list(Phase) == sorted(Phase)                      # values follow the names


def test_routine_stubs_precede_everything_that_may_call_a_routine():
    # A CHECK constraint, a computed column's index and a view can all invoke a
    # function; the stubs must already be there when they are created.
    assert Phase.KEY < Phase.ROUTINE_STUB
    for later in (Phase.CHECK, Phase.FOREIGN_KEY, Phase.INDEX, Phase.VIEW, Phase.ROUTINE):
        assert Phase.ROUTINE_STUB < later


def test_script_mode_keeps_objects_whose_file_names_would_collide():
    """A script has no file names, so the case-insensitive collision check — which
    drops one of the two objects — must not run there."""
    col = cli.Collector(check_paths=False)
    ctx = Context(FSchema(), layout.preset("numbered"), dialect=3)
    cat = categories.CATEGORY_BY_ALIAS["table"]
    for name in ("Account", "ACCOUNT"):
        col.add(ctx, cat, FObj(name))
    assert [a.sql for a in col.artifacts] == ["CREATE OBJ Account", "CREATE OBJ ACCOUNT"]
    assert col.failures == []

    tree = cli.Collector()                                    # …but a tree still refuses
    for name in ("Account", "ACCOUNT"):
        tree.add(ctx, cat, FObj(name))
    assert len(tree.artifacts) == 1 and tree.failures == ["table ACCOUNT"]


# ------------------------------------------------------------------ grants

def test_enum_typed_catalog_codes_are_read_as_numbers():
    index = grants.GrantIndex([FPriv("U", FEnum("S"), "T", subject_type=FEnum(0), user_type=FEnum(8))])
    assert grants.render_grants(index.for_object("relation", "T"), "relation", "T") == \
        ["GRANT SELECT ON T TO USER U"]


def test_a_code_the_library_cannot_decode_falls_back_to_the_raw_column():
    priv = FPriv("U", "S", "T", subject_type=UNKNOWN, user_type=UNKNOWN,
                 raw={"RDB$OBJECT_TYPE": 0, "RDB$USER_TYPE": 8})
    index = grants.GrantIndex([priv])
    assert grants.render_grants(index.for_object("relation", "T"), "relation", "T") == \
        ["GRANT SELECT ON T TO USER U"]


def test_an_undecodable_subject_is_parked_not_lost(caplog):
    priv = FPriv("U", "S", "T", subject_type=UNKNOWN)         # no raw column either
    index = grants.GrantIndex([priv])
    assert index.for_object("relation", "T") == [] and index.unmapped == [priv]


def test_an_undecodable_grantee_is_warned_about_and_skipped(capsys):
    priv = FPriv("U", "S", "T", user_type=UNKNOWN)
    assert grants.render_grants([priv], "relation", "T") == []
    assert "unreadable grantee type" in capsys.readouterr().err


def test_database_grants_of_the_same_shape_keep_a_stable_order():
    """Two DDL grants differing only in grantor must not swap places between runs."""
    privs = [FPriv("U", "C", "D", subject_type=22, grantor="BOB"),
             FPriv("U", "C", "D", subject_type=22, grantor="ALICE")]
    first, _ = grants.render_database_grants(privs, owner="SYSDBA")
    second, _ = grants.render_database_grants(list(reversed(privs)), owner="SYSDBA")
    assert first == second
    assert [s.split("GRANTED BY ")[1] for s in first] == ["ALICE", "BOB"]


# ------------------------------------------------------------------ selection

def test_a_name_repeated_on_the_command_line_is_not_reported_missing():
    schema = FSchema(tables=[FObj("T")])
    res = selection.resolve(schema, ["T", "t"])
    assert res.missing == [] and [c.key for c, _ in res.matches] == ["table"]


# ------------------------------------------------------------------ library and driver contract

def test_metadata_blobs_are_materialised_not_streamed():
    """A PSQL source above the threshold arrives as a BlobReader and get_sql_for
    then fails with "can only concatenate str"; the patch must survive an import."""
    from firebird.driver import driver_config
    assert driver_config.stream_blob_threshold.value >= 64 * 1024 * 1024


def test_the_catalog_transaction_is_read_only_by_default(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(db, "_driver_tpb", lambda isolation, lock_timeout, access_mode:
                        seen.update(isolation=isolation, lock_timeout=lock_timeout, access_mode=access_mode))
    from firebird.driver import Isolation, TraAccessMode
    db._nowait_tpb(Isolation.SNAPSHOT)                        # a caller that passes nothing else
    assert seen["access_mode"] is TraAccessMode.READ          # never WRITE, whatever the driver defaults to
    assert seen["lock_timeout"] == 0


def test_the_library_still_offers_the_actions_we_build_on():
    """fb-dump calls get_sql_for with these actions; firebird-lib removing one
    would only show up on a live database, so it is asserted offline."""
    import inspect

    from firebird.lib.schema import (CharacterSet, Collation, DatabaseException, Domain, Function,
                                     Index, Package, Procedure, Role, Sequence, Table, Trigger, View)
    # The list is filled per instance (a system object supports less), so the
    # contract is read from where it is written.
    expected = {
        Table: ("create", "comment"), View: ("create_or_alter", "comment"),
        Procedure: ("create_or_alter", "comment"), Trigger: ("create_or_alter", "comment"),
        Package: ("create_or_alter", "recreate", "comment"), Sequence: ("create", "comment"),
        Index: ("create", "deactivate", "comment"), Role: ("create", "comment"),
        CharacterSet: ("alter", "comment"), Domain: ("create", "comment"),
        DatabaseException: ("create_or_alter", "comment"), Collation: ("create", "comment"),
    }
    for cls, actions in expected.items():
        src = inspect.getsource(cls.__init__)
        assert all(f"'{a}'" in src for a in actions), f"{cls.__name__}: {actions}"

    udf, psql = inspect.getsource(Function.__init__).split("elif", 1)
    assert "'comment'" in udf and "'declare'" in udf              # an external function (UDF)
    assert "'create_or_alter'" in psql and "'comment'" not in psql  # PSQL: COMMENT ON FUNCTION is ours


# ------------------------------------------------------------------ writer

def _g(*pairs):
    return writer.group([Artifact(p, s) for p, s in pairs])


def test_a_failed_dump_leaves_the_existing_tree_intact(tmp_path, monkeypatch):
    out = tmp_path / "schema"
    writer.replace_tree(_g(("DATABASE.sql", "OLD")), out, layout.preset("numbered").to_toml())
    monkeypatch.setattr(writer, "_write_files", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        writer.replace_tree(_g(("DATABASE.sql", "NEW")), out, layout.preset("numbered").to_toml())
    assert (out / "DATABASE.sql").read_text() == "OLD;\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["schema"]      # no staging left behind


def test_in_place_fallback_keeps_the_new_tree_when_the_target_cannot_be_renamed(tmp_path, monkeypatch):
    """Windows: the target directory is held open, so it cannot be moved aside. The
    rebuild then happens inside it — and must not be undone by the cleanup that
    guards the normal path, which would leave the caller with no tree at all."""
    out = tmp_path / "schema"
    man = layout.preset("numbered").to_toml()
    writer.replace_tree(_g(("DATABASE.sql", "OLD"), ("07_TABLES/A.sql", "A")), out, man)
    real_rename = pathlib.Path.rename

    def stubborn(self, target):
        if self == out:
            raise OSError(5, "used by another process")
        return real_rename(self, target)

    monkeypatch.setattr(pathlib.Path, "rename", stubborn)
    assert writer.replace_tree(_g(("DATABASE.sql", "NEW"), ("07_TABLES/B.sql", "B")), out, man) == 2
    assert (out / "DATABASE.sql").read_text() == "NEW;\n"
    assert (out / "07_TABLES/B.sql").exists() and not (out / "07_TABLES/A.sql").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["schema"]


def test_a_stray_sql_file_in_the_root_is_a_stranger(tmp_path):
    out = tmp_path / "schema"
    man = layout.preset("numbered").to_toml()
    writer.replace_tree(_g(("07_TABLES/A.sql", "A")), out, man)
    (out / "notes.sql").write_text("mine")
    with pytest.raises(writer.WriterError, match="notes.sql"):
        writer.replace_tree(_g(("07_TABLES/A.sql", "A")), out, man, layout=layout.preset("numbered"))


def test_a_flat_tree_owns_the_files_in_its_root(tmp_path):
    flat = layout.preset("flat")
    out = tmp_path / "schema"
    writer.replace_tree(_g(("A.table.sql", "A")), out, flat.to_toml(), layout=flat)
    writer.replace_tree(_g(("B.table.sql", "B")), out, flat.to_toml(), layout=flat)
    assert not (out / "A.table.sql").exists() and (out / "B.table.sql").exists()


def test_owns_root_file_follows_the_layout_not_the_extension():
    assert layout.preset("flat").owns_root_file("ANY.table.sql") is True
    assert layout.preset("numbered").owns_root_file("notes.sql") is False
    assert layout.preset("numbered").owns_root_file("DATABASE.sql") is True
    # A template without an extension cannot tell files apart: everything in the
    # root belongs to the layout.
    loose = layout.from_dict({"base": "flat", "file": "{name}"}, "test")
    assert loose.owns_root_file("whatever") is True


# ------------------------------------------------------------------ render

def test_the_psql_terminator_avoids_characters_the_bodies_contain():
    body = "CREATE OR ALTER PROCEDURE P AS BEGIN v = 'a^b'; END"
    out = render([(body, True)])
    assert "SET TERM ~ ;" in out and out.rstrip().endswith("SET TERM ; ~")
    assert "\n~\n" in out                                     # the statement ends with the new one


def test_a_body_the_library_already_terminated_is_not_terminated_twice():
    out = render([("CREATE OR ALTER PROCEDURE P AS BEGIN END^", True)])
    assert out.count("^") == 3                                # SET TERM ^ ; / ^ / SET TERM ; ^
    assert "END^" not in out


def test_a_statement_ending_in_a_comment_keeps_its_terminator():
    out = render([("GRANT SELECT ON T TO U -- granted by hand", False)])
    assert out.endswith("-- granted by hand\n;\n")
    # …while a `--` inside a literal is not a comment at all.
    assert render([("INSERT INTO T VALUES ('-- x')", False)]) == "INSERT INTO T VALUES ('-- x');\n"


# ------------------------------------------------------------------ layout and cli contracts

def test_a_manifest_from_a_newer_fb_dump_is_refused():
    """The manifest drives a targeted export; guessing at an unknown format would
    put files in the wrong place."""
    with pytest.raises(layout.LayoutError, match="version"):
        layout.from_dict({"base": "plain", "version": 2}, "test")
    assert "version = 1" in layout.preset("numbered").to_toml()


def test_all_or_nothing_covers_the_script_as_well_as_the_tree(capsys):
    ctx = Context(FSchema(tables=[FObj("GOOD"), FObj("BAD", fail=True)]), layout.preset("numbered"), dialect=3)
    assert cli.run_full(ctx, None, allow_partial=False, force=False) == cli.EXIT_PARTIAL
    out, err = capsys.readouterr()
    assert out == "" and "nothing written" in err
    assert cli.run_full(ctx, None, allow_partial=True, force=False) == cli.EXIT_PARTIAL
    assert "CREATE OBJ GOOD;" in capsys.readouterr().out
