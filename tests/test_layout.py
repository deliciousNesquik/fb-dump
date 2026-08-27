import tomllib

import pytest

from fb_dump import layout
from fb_dump.layout import CATEGORY_KEYS, Layout, LayoutError, from_dict, preset, safe_component


def test_numbered_preset_paths():
    lay = preset("numbered")
    assert lay.path_for("role", "R1") == "01_ROLES/R1.sql"
    assert lay.path_for("table", "ACCOUNT") == "07_TABLES/ACCOUNT.sql"
    assert lay.path_for("trigger", "T") == "13_TRIGGERS/T.sql"
    assert lay.database == "DATABASE.sql"


def test_plain_and_flat_presets():
    assert preset("plain").path_for("procedure", "P") == "PROCEDURES/P.sql"
    flat = preset("flat")
    assert flat.path_for("table", "ACCOUNT") == "ACCOUNT.table.sql"
    assert flat.path_for("index", "ACCOUNT") == "ACCOUNT.index.sql"


def test_custom_layout_russian_and_nested():
    lay = from_dict({
        "base": "plain",
        "dirs": {"table": "Таблицы", "index": "Таблицы/Индексы", "role": ""},
        "files": {"index": "{name}.index.sql"},
        "database": "БАЗА.sql",
    }, source="t")
    assert lay.path_for("table", "ACCOUNT") == "Таблицы/ACCOUNT.sql"
    assert lay.path_for("index", "IX1") == "Таблицы/Индексы/IX1.index.sql"
    assert lay.path_for("role", "R") == "R.sql"
    assert lay.path_for("view", "V") == "VIEWS/V.sql"     # untouched categories keep the base
    assert lay.database == "БАЗА.sql"


def test_dir_normalisation():
    lay = from_dict({"dirs": {"table": "a//b/", "view": ".\\c"}}, source="t")
    assert lay.dirs["table"] == "a/b"
    assert lay.dirs["view"] == "c"


@pytest.mark.parametrize("data", [
    {"dirs": {"table": "../x"}},
    {"dirs": {"table": "/abs"}},
    {"dirs": {"tables": "X"}},          # unknown category
    {"dirz": {}},                       # unknown key
    {"file": "{type}.sql"},             # no {name}
    {"file": "{name}.{owner}.sql"},     # unknown placeholder
    {"file": "sub/{name}.sql"},         # separator in template
    {"base": "nope"},
    {"database": "dir/DB.sql"},
    {"database": ""},
    {"dirs": "not a table"},
    {"dirs": {"table": 5}},
])
def test_invalid_layouts_raise(data):
    with pytest.raises(LayoutError):
        from_dict(data, source="t")


def test_toml_round_trip_with_unicode():
    lay = from_dict({"base": "plain", "dirs": {"table": "Таблицы"}, "files": {"table": "{name}.таблица.sql"}}, source="t")
    again = from_dict(tomllib.loads(lay.to_toml()), source="rt")
    assert again == lay
    assert set(tomllib.loads(lay.to_toml())["dirs"]) == set(CATEGORY_KEYS)


@pytest.mark.parametrize("raw,expected", [
    ("ACCOUNT", "ACCOUNT"), ("A/B", "A_B"), ("A\\B", "A_B"), ("A:B*?", "A_B__"),
    ("CON", "CON_"), ("com1", "com1_"), ("x.", "x"), ("  ", "_"), ("Счёт", "Счёт"),
])
def test_safe_component(raw, expected):
    assert safe_component(raw) == expected


def test_load_preset_file_and_manifest(tmp_path):
    assert layout.load("flat") == preset("flat")
    f = tmp_path / "lay.toml"
    f.write_text('base = "plain"\n[dirs]\ntable = "T"\n', encoding="utf-8")
    assert layout.load(str(f)).path_for("table", "X") == "T/X.sql"
    with pytest.raises(LayoutError):
        layout.load("does-not-exist")
    f.write_text("this is not toml = = =", encoding="utf-8")
    with pytest.raises(LayoutError):
        layout.load(str(f))

    tree = tmp_path / "tree"
    assert layout.load_manifest(tree) is None
    tree.mkdir()
    (tree / layout.MANIFEST).write_text(preset("flat").to_toml(), encoding="utf-8")
    assert layout.load_manifest(tree) == preset("flat")


def test_layout_equality_is_structural():
    assert preset("numbered") == from_dict({}, source="t")
    assert preset("numbered") != preset("plain")
