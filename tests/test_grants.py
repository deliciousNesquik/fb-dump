from fakes import FPriv

from fb_dump.grants import GrantIndex, quote_ident, render_database_grants, render_grants


def test_table_grants_grouped_and_ordered():
    privs = [
        FPriv("U1", "I", "T"), FPriv("U1", "S", "T"),
        FPriv("R1", "U", "T", user_type=13, field="B"), FPriv("R1", "U", "T", user_type=13, field="A"),
        FPriv("PUBLIC", "S", "T"),
        FPriv("SYSDBA", "S", "T"), FPriv("SYSDBA", "D", "T"),      # owner: implicit, dropped
    ]
    assert render_grants(privs, "relation", "T", owner="SYSDBA") == [
        "GRANT SELECT ON T TO PUBLIC",
        "GRANT SELECT, INSERT ON T TO USER U1",
        "GRANT UPDATE (A, B) ON T TO ROLE R1",
    ]


def test_owner_filter_is_per_object_and_granted_by_names_a_foreign_grantor():
    privs = [FPriv("SYSDBA", "S", "T")]
    assert render_grants(privs, "relation", "T", owner="OTHER") == ["GRANT SELECT ON T TO USER SYSDBA GRANTED BY SYSDBA"]
    assert render_grants(privs, "relation", "T", owner="SYSDBA") == []
    assert render_grants([FPriv("U", "S", "T", grantor="APP")], "relation", "T", owner="APP") == ["GRANT SELECT ON T TO USER U"]
    assert render_grants([FPriv("U", "S", "T", grantor="dba")], "relation", "T", owner="APP") == ['GRANT SELECT ON T TO USER U GRANTED BY "dba"']
    # different grantors never merge into one statement
    assert render_grants([FPriv("U", "S", "T", grantor="A"), FPriv("U", "I", "T", grantor="B")], "relation", "T", owner="A") == [
        "GRANT SELECT ON T TO USER U", "GRANT INSERT ON T TO USER U GRANTED BY B"]


def test_column_and_table_level_update_collapse_to_table_level():
    privs = [FPriv("U", "U", "T", field="A"), FPriv("U", "U", "T")]
    assert render_grants(privs, "relation", "T") == ["GRANT UPDATE ON T TO USER U"]


def test_grant_option_splits_statements():
    privs = [FPriv("U", "S", "T"), FPriv("U", "I", "T", grant=True)]
    assert render_grants(privs, "relation", "T") == [
        "GRANT SELECT ON T TO USER U",
        "GRANT INSERT ON T TO USER U WITH GRANT OPTION",
    ]


def test_grantee_kinds_and_quoting(capsys):
    privs = [
        FPriv("P1", "S", "T", user_type=5), FPriv("TR", "S", "T", user_type=2), FPriv("V", "S", "T", user_type=1),
        FPriv("F", "S", "T", user_type=15), FPriv("PK", "S", "T", user_type=18), FPriv("bob", "S", "T"),
        FPriv("G", "S", "T", user_type=12), FPriv("X", "S", "T", user_type=99),
    ]
    assert render_grants(privs, "relation", "T") == [
        'GRANT SELECT ON T TO USER "bob"',
        "GRANT SELECT ON T TO GROUP G",
        "GRANT SELECT ON T TO VIEW V",
        "GRANT SELECT ON T TO TRIGGER TR",
        "GRANT SELECT ON T TO PROCEDURE P1",
        "GRANT SELECT ON T TO FUNCTION F",
        "GRANT SELECT ON T TO PACKAGE PK",
    ]
    assert "unknown type 99" in capsys.readouterr().err


def test_execute_and_usage_subjects():
    assert render_grants([FPriv("U", "X", "P", subject_type=5)], "procedure", "P") == ["GRANT EXECUTE ON PROCEDURE P TO USER U"]
    assert render_grants([FPriv("U", "X", "F", subject_type=15)], "function", "F") == ["GRANT EXECUTE ON FUNCTION F TO USER U"]
    assert render_grants([FPriv("U", "X", "K", subject_type=18)], "package", "K") == ["GRANT EXECUTE ON PACKAGE K TO USER U"]
    assert render_grants([FPriv("U", "G", "G1", subject_type=14)], "generator", "G1") == ["GRANT USAGE ON SEQUENCE G1 TO USER U"]
    assert render_grants([FPriv("U", "G", "E1", subject_type=7)], "exception", "E1") == ["GRANT USAGE ON EXCEPTION E1 TO USER U"]
    # a privilege code that makes no sense for the namespace is ignored, not rendered wrongly
    assert render_grants([FPriv("U", "S", "P", subject_type=5)], "procedure", "P") == []


def test_role_membership_default_and_admin():
    privs = [
        FPriv("U1", "M", "R", subject_type=13),
        FPriv("U2", "M", "R", subject_type=13, grant=True),
        FPriv("U3", "M", "R", subject_type=13, field="D"),
        FPriv("R2", "M", "R", subject_type=13, user_type=13),
        FPriv("SYSDBA", "M", "R", subject_type=13, grant=True),            # owner's implicit membership
        FPriv("SYSDBA", "M", "R", subject_type=13, grant=True, field="D"), # …but DEFAULT is configuration
    ]
    assert render_grants(privs, "role", "R", owner="SYSDBA") == [
        "GRANT DEFAULT R TO USER SYSDBA WITH ADMIN OPTION",
        "GRANT R TO USER U1",
        "GRANT R TO USER U2 WITH ADMIN OPTION",
        "GRANT DEFAULT R TO USER U3",
        "GRANT R TO ROLE R2",
    ]


def test_grant_index_namespaces_and_consumption():
    idx = GrantIndex([
        FPriv("U", "S", "T", subject_type=0), FPriv("U", "S", "V", subject_type=1),
        FPriv("U", "X", "P", subject_type=5), FPriv("U", "C", "SQL$TABLES", subject_type=22),
        FPriv("U", "L", "DB", subject_type=21), FPriv("U", "S", "WEIRD", subject_type=9),
        FPriv("U", "S", "RDB$DATABASE", subject_type=0),
    ])
    assert [p.subject_name for p in idx.for_object("relation", "T")] == ["T"]
    assert [p.subject_name for p in idx.for_object("relation", "V")] == ["V"]     # views share the relation namespace
    assert [p.subject_name for p in idx.for_object("procedure", "P")] == ["P"]
    assert idx.for_object("relation", "NOPE") == []
    assert [p.subject_type for p in idx.database] == [22, 21]
    assert [p.subject_name for p in idx.unmapped] == ["WEIRD"]
    assert idx.unconsumed() == [("relation", "RDB$DATABASE")]


def test_database_level_grants():
    stmts, skipped = render_database_grants([
        FPriv("U", "L", "X", subject_type=22), FPriv("U", "C", "X", subject_type=22),
        FPriv("U", "O", "X", subject_type=24), FPriv("U", "L", "X", subject_type=21),
        FPriv("U", "C", "X", subject_type=31, grant=True), FPriv("U", "S", "X", subject_type=22),
        FPriv("U", "C", "X", subject_type=23, grantor="ADMIN"), FPriv("X", "C", "X", subject_type=22, user_type=77),
    ], owner="SYSDBA")
    assert stmts == [
        "GRANT ALTER DATABASE TO USER U",
        "GRANT CREATE TABLE TO USER U",
        "GRANT ALTER ANY TABLE TO USER U",
        "GRANT CREATE VIEW TO USER U GRANTED BY ADMIN",
        "GRANT DROP ANY PROCEDURE TO USER U",
        "GRANT CREATE CHARACTER SET TO USER U WITH GRANT OPTION",
    ]
    assert skipped == 2


def test_quote_ident():
    assert quote_ident("ACCOUNT") == "ACCOUNT"
    assert quote_ident("BAS$ID") == "BAS$ID"
    assert quote_ident("account") == '"account"'
    assert quote_ident('a"b') == '"a""b"'
    assert quote_ident("USER", is_keyword=lambda s: s == "USER") == '"USER"'
    assert quote_ident("Счёт") == '"Счёт"'
