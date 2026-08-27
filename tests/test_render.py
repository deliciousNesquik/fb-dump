from fb_dump.render import render


def test_plain_statements_get_semicolons():
    assert render([("CREATE TABLE T (A INT)", False), ("ALTER TABLE T ADD X INT;", False)]) == (
        "CREATE TABLE T (A INT);\n\nALTER TABLE T ADD X INT;\n"
    )


def test_psql_block_and_mixed_terminators():
    out = render([
        ("CREATE OR ALTER PROCEDURE P AS BEGIN END", True),
        ("COMMENT ON PROCEDURE P IS 'x'", False),
        ("GRANT EXECUTE ON PROCEDURE P TO U", False),
    ])
    assert out.split("\n") == [
        "SET TERM ^ ;", "",
        "CREATE OR ALTER PROCEDURE P AS BEGIN END", "^", "",
        "SET TERM ; ^", "",
        "COMMENT ON PROCEDURE P IS 'x';", "",
        "GRANT EXECUTE ON PROCEDURE P TO U;", "",
    ]


def test_consecutive_psql_share_one_block_and_trailing_block_is_closed():
    out = render([("CREATE OR ALTER PACKAGE K AS BEGIN END", True), ("RECREATE PACKAGE BODY K AS BEGIN END^", True)])
    assert out.count("SET TERM ^ ;") == 1
    assert out.rstrip().endswith("SET TERM ; ^")
    assert "END^\n" not in out          # stray terminator stripped


def test_comment_lines_verbatim_and_empty_skipped():
    out = render([("SET SQL DIALECT 3", False), ("-- Default character set: UTF8", False), ("   ", False)])
    assert out == "SET SQL DIALECT 3;\n\n-- Default character set: UTF8\n"
