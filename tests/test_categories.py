from fakes import FChild, FConstraint, FObj, FPriv, FSchema

from fb_dump import categories
from fb_dump.layout import preset
from fb_dump.model import Context


def _ctx(**schema_kw) -> Context:
    return Context(FSchema(**schema_kw), preset("numbered"), dialect=3)


def _sql(ctx, key, obj):
    return [a.sql for a in categories.CATEGORY_BY_KEY[key].emit(ctx, obj)]


def test_table_file_is_complete_definition():
    t = FObj("ACC", description="Accounts", owner="SYSDBA",
             constraints=[FConstraint("FK1", "fkey"), FConstraint("NN", "not_null"), FConstraint("PK1", "pkey"),
                          FConstraint("CK1", "check"), FConstraint("UQ1", "unique")],
             columns=[FChild("ID", "identifier"), FChild("NAME")])
    ctx = _ctx(privileges=[FPriv("U1", "S", "ACC"), FPriv("SYSDBA", "S", "ACC")])
    arts = categories.CATEGORY_BY_KEY["table"].emit(ctx, t)
    assert {a.path for a in arts} == {"07_TABLES/ACC.sql"}
    assert [a.sql for a in arts] == [
        "CREATE OBJ ACC",
        "ALTER TABLE ADD CONSTRAINT PK1 (pkey)",
        "ALTER TABLE ADD CONSTRAINT UQ1 (unique)",
        "ALTER TABLE ADD CONSTRAINT CK1 (check)",
        "ALTER TABLE ADD CONSTRAINT FK1 (fkey)",
        "COMMENT ON ACC IS 'Accounts'",
        "COMMENT ON CHILD ID IS 'identifier'",
        "GRANT SELECT ON ACC TO U1",
    ]
    assert not any(a.psql for a in arts)


def test_index_inactive_gets_deactivate():
    ctx = _ctx()
    assert _sql(ctx, "index", FObj("IX", inactive=True)) == ["CREATE OBJ IX", "ALTER INDEX IX INACTIVE"]
    assert _sql(ctx, "index", FObj("IX")) == ["CREATE OBJ IX"]
    assert categories.CATEGORY_BY_KEY["index"].emit(ctx, FObj("IX"))[0].path == "08_INDICES/IX.sql"


def test_procedure_single_file_with_params_and_grants():
    p = FObj("CALC", description="d", input_params=[FChild("A", "in a")], output_params=[FChild("R", "out r")])
    ctx = _ctx(privileges=[FPriv("U", "X", "CALC", subject_type=5)])
    arts = categories.CATEGORY_BY_KEY["procedure"].emit(ctx, p)
    assert [a.sql for a in arts] == [
        "CREATE OR ALTER OBJ CALC",
        "COMMENT ON CALC IS 'd'",
        "COMMENT ON CHILD A IS 'in a'",
        "COMMENT ON CHILD R IS 'out r'",
        "GRANT EXECUTE ON PROCEDURE CALC TO U",
    ]
    assert [a.psql for a in arts] == [True, False, False, False, False]


def test_package_header_and_body():
    ctx = _ctx()
    assert _sql(ctx, "package", FObj("K", body="x")) == ["CREATE OR ALTER OBJ K", "RECREATE PACKAGE BODY K"]
    assert _sql(ctx, "package", FObj("K")) == ["CREATE OR ALTER OBJ K"]


def test_generator_has_no_runtime_value():
    ctx = _ctx(privileges=[FPriv("U", "G", "GEN", subject_type=14)])
    assert _sql(ctx, "generator", FObj("GEN")) == ["CREATE OBJ GEN", "GRANT USAGE ON SEQUENCE GEN TO U"]


def test_role_file_has_memberships():
    ctx = _ctx(privileges=[FPriv("U1", "M", "R", subject_type=13), FPriv("SYSDBA", "M", "R", subject_type=13)])
    assert _sql(ctx, "role", FObj("R")) == ["CREATE OBJ R", "GRANT R TO U1"]


def test_view_and_udf_and_trigger():
    ctx = _ctx(privileges=[FPriv("U", "S", "V", subject_type=0), FPriv("U", "X", "F", subject_type=15)])
    assert _sql(ctx, "view", FObj("V", columns=[FChild("C", "col")])) == [
        "CREATE OR ALTER OBJ V", "COMMENT ON CHILD C IS 'col'", "GRANT SELECT ON V TO U"]
    assert _sql(ctx, "external_function", FObj("F", external=True)) == [
        "DECLARE EXTERNAL FUNCTION F", "GRANT EXECUTE ON FUNCTION F TO U"]
    arts = categories.CATEGORY_BY_KEY["trigger"].emit(ctx, FObj("TR"))
    assert arts[0].psql and arts[0].path == "13_TRIGGERS/TR.sql"


def test_quoted_object_names_are_quoted_in_grants_and_safe_in_paths():
    ctx = _ctx(privileges=[FPriv("U", "S", "my/table")])
    arts = categories.CATEGORY_BY_KEY["table"].emit(ctx, FObj("my/table"))
    assert arts[0].path == "07_TABLES/my_table.sql"
    assert arts[-1].sql == 'GRANT SELECT ON "my/table" TO U'


def test_collections_filter_system_and_split_functions():
    s = FSchema(
        functions=[FObj("UDF", external=True), FObj("F"), FObj("PKG_F", packaged=True), FObj("SYS_F", sys=True)],
        procedures=[FObj("P"), FObj("PKG_P", packaged=True)],
        indices=[FObj("IX"), FObj("RDB$PRIMARY1", enforcer=True), FObj("SYSIX", sys=True)],
        tables=[FObj("T"), FObj("RDB$RELATIONS", sys=True)],
    )
    names = lambda key: sorted(o.name for o in categories.CATEGORY_BY_KEY[key].objects(s))
    assert names("external_function") == ["UDF"]
    assert names("function") == ["F"]
    assert names("procedure") == ["P"]
    assert names("index") == ["IX"]
    assert names("table") == ["T"]


def test_database_preamble():
    ctx = Context(FSchema(description="Main DB", charset="WIN1251",
                          privileges=[FPriv("U", "C", "X", subject_type=22)]), preset("numbered"), dialect=1)
    arts = categories.database_preamble(ctx)
    assert {a.path for a in arts} == {"DATABASE.sql"}
    assert [a.sql for a in arts] == [
        "SET SQL DIALECT 1",
        "-- Default character set: WIN1251",
        "COMMENT ON DATABASE IS 'Main DB'",
        "GRANT CREATE TABLE TO U",
    ]


def test_registry_and_aliases():
    assert categories.CATEGORY_BY_ALIAS["udf"].key == "external_function"
    assert categories.CATEGORY_BY_ALIAS["proc"].key == "procedure"
    assert categories.CATEGORY_BY_ALIAS["sequence"].key == "generator"
    assert "grant" not in categories.TYPE_CHOICES and "comment" not in categories.TYPE_CHOICES
    assert categories.CATEGORY_ORDER["role"] == 0 and categories.CATEGORY_ORDER["trigger"] == 12
