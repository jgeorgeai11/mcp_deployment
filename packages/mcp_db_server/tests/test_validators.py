"""Unit tests for the shared SQL-identifier validator (mcp_db_server.validators)."""

import pytest
from mcp_db_server.validators import validate_sql_identifier


@pytest.mark.parametrize(
    "name",
    [
        "public",
        "cms_iom",
        "_private",
        "a",
        "_",
        "table1",
        "document_content",
        "a1_b2_c3",
    ],
)
def test_validate_sql_identifier_valid_returned_unchanged(name: str) -> None:
    assert validate_sql_identifier(name, "db_schema") == name


def test_validate_sql_identifier_rejects_trailing_newline() -> None:
    # Regression guard for fullmatch vs match: with re.match, the anchored
    # ``$`` would also match just before a trailing newline, so "public\n"
    # would pass validation and reach SQL.
    with pytest.raises(ValueError, match="Unsafe SQL identifier"):
        validate_sql_identifier("public\n", "db_schema")


@pytest.mark.parametrize(
    "name",
    [
        "Public",  # uppercase
        "PUBLIC",  # uppercase
        "1table",  # leading digit
        "my-table",  # dash
        "my table",  # space
        'my"table',  # double quote
        "my'table",  # single quote
        "table;drop",  # semicolon
        "",  # empty string
    ],
)
def test_validate_sql_identifier_invalid_raises_with_value_and_label(
    name: str,
) -> None:
    with pytest.raises(ValueError, match="Unsafe SQL identifier") as exc_info:
        validate_sql_identifier(name, "db_schema")
    # The message must name both the offending value and the label so the
    # failure is diagnosable from the log line alone
    assert repr(name) in str(exc_info.value)
    assert "db_schema" in str(exc_info.value)
