"""Firebird 4/5 details firebird-lib 2.0 gets wrong or omits, and the guards around them."""

from __future__ import annotations

from fakes import FCharset, FChild, FConstraint, FGenerator, FObj, FPriv, FSchema

from fb_dump import categories
from fb_dump.layout import preset
from fb_dump.model import Context


def _ctx(**kw):
    return Context(FSchema(**kw), preset("plain"), dialect=3)


def _sql(ctx, key, obj):
    return [a.sql for a in categories.CATEGORY_BY_KEY[key].emit(ctx, obj)]


def test_psql_function_comment_does_not_use_missing_action():
    # firebird-lib registers 'comment' for external UDFs only
    f = FObj("F", description="doc's", actions={"create", "recreate", "alter", "create_or_alter", "drop"})
    assert _sql(_ctx(), "function", f) == [
        "CREATE OR ALTER OBJ F\nRETURNS INTEGER\nAS\nBEGIN END",
        "COMMENT ON FUNCTION F IS 'doc''s'",
    ]


def test_deterministic_function():
    assert _sql(_ctx(), "function", FObj("F", deterministic=1))[0] == "CREATE OR ALTER OBJ F\nRETURNS INTEGER DETERMINISTIC\nAS\nBEGIN END"
    assert _sql(_ctx(), "function", FObj("F", deterministic=0))[0] == "CREATE OR ALTER OBJ F\nRETURNS INTEGER\nAS\nBEGIN END"


def test_external_engine_routines():
    udr = {"RDB$ENGINE_NAME": "UDR ", "RDB$ENTRYPOINT": "my.Class!method"}
    assert _sql(_ctx(), "function", FObj("F", attributes=udr, source=None)) == [
        "CREATE OR ALTER OBJ F (A INTEGER)\nRETURNS INTEGER\nEXTERNAL NAME 'my.Class!method' ENGINE UDR"]
    assert _sql(_ctx(), "procedure", FObj("P", attributes=udr, source=None))[0].endswith("EXTERNAL NAME 'my.Class!method' ENGINE UDR")
    tr = FObj("T", attributes={**udr, "RDB$ENTRYPOINT": "it's"}, source=None, relation=FObj("TBL"), position=3, type_string="AFTER UPDATE")
    assert _sql(_ctx(), "trigger", tr) == [
        "CREATE OR ALTER TRIGGER T FOR TBL ACTIVE\nAFTER UPDATE POSITION 3\nEXTERNAL NAME 'it''s' ENGINE UDR"]


def test_ddl_trigger_events_decoded_from_bitmask():
    ddl = 16384
    cases = {
        ddl | (1 << 3): "BEFORE DROP TABLE",
        ddl | 1 | (1 << 4): "AFTER CREATE PROCEDURE",
        ddl | (1 << 1) | (1 << 2): "BEFORE CREATE TABLE OR ALTER TABLE",
        ddl | 1 | (1 << 10): "AFTER CREATE TRIGGER",
        ddl | (0x7FFFFFFFFFFFFFFF & ~(0x3 << 13) & ~1): "BEFORE ANY DDL STATEMENT",
    }
    for raw, expected in cases.items():
        tr = FObj("D", ddl=True, active=False, position=1, attributes={"RDB$TRIGGER_TYPE": raw}, source="BEGIN END")
        assert _sql(_ctx(), "trigger", tr) == [f"CREATE OR ALTER TRIGGER D INACTIVE\n{expected} POSITION 1\nBEGIN END"], expected
    assert "unknown DDL event 15" in categories._ddl_trigger_event(ddl | (1 << 15))


def test_dml_trigger_still_uses_library_ddl():
    assert _sql(_ctx(), "trigger", FObj("T")) == ["CREATE OR ALTER OBJ T\nRETURNS INTEGER\nAS\nBEGIN END"]


def test_partial_index_condition_and_segment_quoting():
    ctx = _ctx(keywords={"VALUE"})
    assert _sql(ctx, "index", FObj("IX", segments=["A", "b"], attributes={"RDB$CONDITION_SOURCE": "(A > 0)"})) == [
        'CREATE OBJ IX (A, "b")\nWHERE (A > 0)']
    assert _sql(ctx, "index", FObj("IX", segments=["VALUE"], attributes={"RDB$CONDITION_SOURCE": " where x"})) == [
        'CREATE OBJ IX ("VALUE")\nwhere x']
    assert _sql(ctx, "index", FObj("IX", segments=["A", "B"])) == ["CREATE OBJ IX (A,B)"]     # untouched when nothing needs quoting


def test_table_identity_gtt_security_and_constraint_quoting():
    t = FObj("T", attributes={"RDB$RELATION_TYPE": 4, "RDB$SQL_SECURITY": True},
             columns=[FChild("ID", identity=0, generator=FGenerator(increment=5)), FChild("N", identity=1, generator=FGenerator(increment=1)), FChild("X")],
             constraints=[FConstraint("PK", "pkey", ["Id"]), FConstraint("FK", "fkey", ["A"], ["ref id"]), FConstraint("CK", "check")])
    ctx = _ctx()
    assert _sql(ctx, "table", t) == [
        "CREATE OBJ T\nON COMMIT PRESERVE ROWS",
        "ALTER TABLE T ALTER COLUMN ID SET GENERATED ALWAYS",
        "ALTER TABLE T ALTER COLUMN ID SET INCREMENT BY 5",
        "ALTER TABLE T ALTER SQL SECURITY DEFINER",
        'ALTER TABLE ADD CONSTRAINT PK (pkey) ("Id")',
        "ALTER TABLE ADD CONSTRAINT CK (check)",
        'ALTER TABLE ADD CONSTRAINT FK (fkey) (A) REFERENCES P ("ref id")',
    ]
    t2 = FObj("T2", attributes={"RDB$RELATION_TYPE": 5, "RDB$SQL_SECURITY": False})
    assert _sql(ctx, "table", t2) == ["CREATE OBJ T2\nON COMMIT DELETE ROWS", "ALTER TABLE T2 ALTER SQL SECURITY INVOKER"]
    assert _sql(ctx, "table", FObj("T3", attributes={"RDB$RELATION_TYPE": 0})) == ["CREATE OBJ T3"]


def test_preamble_character_sets_and_system_role_memberships():
    schema = FSchema(
        character_sets=[FCharset("WIN1251", default_collation="PXW_CYRL", description="cyr"), FCharset("UTF8")],
        roles=[FObj("RDB$ADMIN", sys=True), FObj("APP")],
        privileges=[FPriv("JOE", "M", "RDB$ADMIN", subject_type=13), FPriv("SYSDBA", "M", "RDB$ADMIN", subject_type=13),
                    FPriv("JOE", "M", "APP", subject_type=13)],
    )
    arts = categories.database_preamble(Context(schema, preset("plain"), dialect=3))
    assert [a.sql for a in arts] == [
        "SET SQL DIALECT 3",
        "-- Default character set: UTF8",
        "ALTER CHARACTER SET WIN1251 SET DEFAULT COLLATION PXW_CYRL",
        "COMMENT ON CHARACTER SET WIN1251 IS 'cyr'",
        "GRANT RDB$ADMIN TO USER JOE",
    ]
