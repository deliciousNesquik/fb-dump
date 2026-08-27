from fakes import FObj, FSchema

from fb_dump.selection import resolve


def _schema():
    return FSchema(
        tables=[FObj("ACCOUNT"), FObj("Account"), FObj("RDB$X", sys=True)],
        procedures=[FObj("ACCOUNT"), FObj("CALC")],
        indices=[FObj("IX_ACC"), FObj("RDB$PRIMARY1", enforcer=True)],
    )


def test_case_insensitive_and_multi_category():
    r = resolve(_schema(), ["account"])
    assert [(c.key, o.name) for c, o in r.matches] == [("table", "ACCOUNT"), ("table", "Account"), ("procedure", "ACCOUNT")]
    assert r.missing == []


def test_type_restricts():
    r = resolve(_schema(), ["ACCOUNT"], "proc")
    assert [(c.key, o.name) for c, o in r.matches] == [("procedure", "ACCOUNT")]


def test_missing_and_filtered_objects():
    r = resolve(_schema(), ["NOPE", "RDB$X", "RDB$PRIMARY1", "calc"])
    assert r.missing == ["NOPE", "RDB$X", "RDB$PRIMARY1"]
    assert [(c.key, o.name) for c, o in r.matches] == [("procedure", "CALC")]


def test_duplicates_collapse_and_order_is_category_then_name():
    r = resolve(_schema(), ["CALC", "IX_ACC", "calc"])
    assert [(c.key, o.name) for c, o in r.matches] == [("index", "IX_ACC"), ("procedure", "CALC")]
