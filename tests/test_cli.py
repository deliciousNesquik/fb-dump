import io
from contextlib import redirect_stdout

import pytest
from fakes import FObj, FPriv, FSchema

from fb_dump import cli, layout
from fb_dump.layout import MANIFEST, preset
from fb_dump.model import Context


def _ctx(lay=None, **schema_kw) -> Context:
    return Context(FSchema(**schema_kw), lay or preset("numbered"), dialect=3)


def _schema_kw(**extra):
    base = dict(
        tables=[FObj("ACCOUNT")], procedures=[FObj("CALC")], generators=[FObj("GEN")], roles=[FObj("R")],
        privileges=[FPriv("U", "S", "ACCOUNT")],
    )
    base.update(extra)
    return base


# ---------------------------------------------------------------- argparse

@pytest.mark.parametrize("argv", [
    ["--list", "FOO"],
    ["--list", "-o", "x"],
    ["--type", "table"],
    ["--allow-partial", "ACCOUNT"],
    ["--allow-partial", "--list"],
    ["--force"],
    ["--type", "zzz", "ACCOUNT"],
    ["-q", "-v", "--list"],
])
def test_usage_errors_exit_2(argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2


def test_missing_database_is_usage_error(monkeypatch):
    monkeypatch.delenv("FB_DATABASE", raising=False)
    assert cli.main(["--list"]) == 2


def test_print_layout_needs_no_database(monkeypatch, capsys):
    monkeypatch.delenv("FB_DATABASE", raising=False)
    assert cli.main(["--print-layout", "--layout", "flat"]) == 0
    out = capsys.readouterr().out
    assert 'file = "{name}.{type}.sql"' in out and 'table = ""' in out
    assert cli.main(["--print-layout", "--layout", "nope"]) == 2


# ---------------------------------------------------------------- modes

def test_run_list_is_tab_separated(capsys):
    assert cli.run_list(_ctx(**_schema_kw()), None) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["role\tR", "generator\tGEN", "table\tACCOUNT", "procedure\tCALC"]
    cli.run_list(_ctx(**_schema_kw()), "procedure")
    assert capsys.readouterr().out.splitlines() == ["procedure\tCALC"]


def test_run_full_writes_complete_tree(tmp_path):
    out = tmp_path / "schema"
    code = cli.run_full(_ctx(**_schema_kw()), out, allow_partial=False, force=False)
    assert code == 0
    assert (out / "DATABASE.sql").read_text(encoding="utf-8").startswith("SET SQL DIALECT 3;")
    assert (out / "07_TABLES/ACCOUNT.sql").read_text(encoding="utf-8") == "CREATE OBJ ACCOUNT;\n\nGRANT SELECT ON ACCOUNT TO USER U;\n"
    assert (out / "11_PROCEDURES/CALC.sql").exists() and (out / "01_ROLES/R.sql").exists()
    assert (out / MANIFEST).exists()
    assert not (out / "13_TRIGGERS").exists()          # empty categories create no directories


def test_run_full_is_all_or_nothing(tmp_path):
    out = tmp_path / "schema"
    kw = _schema_kw(tables=[FObj("ACCOUNT"), FObj("BROKEN", fail=True)])
    assert cli.run_full(_ctx(**kw), out, allow_partial=False, force=False) == 3
    assert not out.exists()
    assert cli.run_full(_ctx(**kw), out, allow_partial=True, force=False) == 3
    assert (out / "07_TABLES/ACCOUNT.sql").exists() and not (out / "07_TABLES/BROKEN.sql").exists()


def test_run_full_to_stdout_is_an_ordered_script(capsys):
    """Without --out the dump is one script, sorted so it can be applied."""
    assert cli.run_full(_ctx(**_schema_kw()), None, allow_partial=False, force=False) == 0
    out = capsys.readouterr().out
    assert out.startswith("SET SQL DIALECT 3;")          # dialect first
    assert "-- ===== " not in out                        # not a per-file listing any more
    order = [out.index(x) for x in ("CREATE OBJ R",                  # role
                                    "CREATE OBJ GEN",                # generator
                                    "CREATE OBJ ACCOUNT",            # table
                                    "CREATE OR ALTER OBJ CALC",      # routine body
                                    "GRANT SELECT ON ACCOUNT")]      # grants last
    assert order == sorted(order), out


def test_run_full_respects_layout(tmp_path):
    lay = layout.from_dict({"base": "plain", "dirs": {"table": "Таблицы"}}, source="t")
    out = tmp_path / "s"
    cli.run_full(_ctx(lay, **_schema_kw()), out, allow_partial=False, force=False)
    assert (out / "Таблицы/ACCOUNT.sql").exists() and (out / "PROCEDURES/CALC.sql").exists()
    assert layout.load_manifest(out) == lay


def test_run_targeted_best_effort(tmp_path, capsys):
    out = tmp_path / "schema"
    assert cli.run_targeted(_ctx(**_schema_kw()), out, ["account", "NOPE"], None, force=False) == 3
    assert (out / "07_TABLES/ACCOUNT.sql").exists() and (out / MANIFEST).exists()
    assert cli.run_targeted(_ctx(**_schema_kw()), None, ["CALC"], "proc", force=False) == 0
    assert capsys.readouterr().out.startswith("-- ===== 11_PROCEDURES/CALC.sql =====")


def test_collector_detects_case_insensitive_collisions():
    ctx = _ctx(tables=[FObj("Account"), FObj("ACCOUNT")])
    col = cli.Collector()
    cat = next(c for c in cli.categories.CATEGORIES if c.key == "table")
    for obj in cat.objects(ctx.schema):
        col.add(ctx, cat, obj)
    assert col.failures == ["table ACCOUNT"]
    assert [a.path for a in col.artifacts] == ["07_TABLES/Account.sql"]


# ---------------------------------------------------------------- layout choice

def test_targeted_export_adopts_tree_layout(tmp_path):
    out = tmp_path / "s"
    flat = preset("flat")
    cli.run_full(_ctx(flat, **_schema_kw()), out, allow_partial=False, force=False)
    ns = cli._build_parser().parse_args(["ACCOUNT", "-o", str(out)])
    assert cli._choose_layout(ns, out) == flat
    ns = cli._build_parser().parse_args(["ACCOUNT", "-o", str(out), "--layout", "flat"])
    assert cli._choose_layout(ns, out) == flat
    ns = cli._build_parser().parse_args(["ACCOUNT", "-o", str(out), "--layout", "plain"])
    with pytest.raises(layout.LayoutError):
        cli._choose_layout(ns, out)
    ns = cli._build_parser().parse_args(["-o", str(out), "--layout", "plain"])   # full dump: rebuild is allowed
    assert cli._choose_layout(ns, out) == preset("plain")


# ---------------------------------------------------------------- end to end with a fake driver

def test_main_end_to_end(monkeypatch, tmp_path):
    class _Con:
        schema = FSchema(**_schema_kw())
        def close(self): pass

    monkeypatch.setenv("FB_DATABASE", "fake")
    monkeypatch.setattr(cli.db, "connect", lambda settings, charset=None: _Con())
    monkeypatch.setattr(cli.db, "open_schema", lambda settings, con: con.schema)
    monkeypatch.setattr(cli.db, "dialect", lambda con: 3)
    out = tmp_path / "tree"
    assert cli.main(["-o", str(out), "-q"]) == 0
    assert (out / "07_TABLES/ACCOUNT.sql").exists()
    assert cli.main(["ACCOUNT", "-o", str(out)]) == 0
    assert cli.main(["MISSING", "-o", str(out)]) == 3


def test_main_maps_infrastructure_errors_to_1(monkeypatch):
    monkeypatch.setenv("FB_DATABASE", "fake")

    def boom(settings, charset=None):
        raise ConnectionError("no server")

    monkeypatch.setattr(cli.db, "connect", boom)
    assert cli.main(["--list"]) == 1
