"""Unit tests for the mcp_db_server server.

These tests are fully mocked -- no live database, no model downloads, no live
server. They cover the FastMCP rewrite and the reranked search pipeline:

- corrected default embedding model + env override
- the dimension guard raises on a model/table mismatch
- the reranker reorders RRF candidates (mock CrossEncoder)
- RRF candidate generation still feeds the reranker
- bearer-token auth rejects missing/bad tokens and accepts a valid one,
  fails closed when unset, and leaves /health open
- role-derived list_databases + privilege-filtered list_schemas/list_tables
- run_sql single-statement and row-limit/truncation guards hold
- the _env_int tuning-knob floor clamps unsafe values and warns
- the MCP_LOG_LEVEL resolution falls back to INFO on an unknown name
- canonical validate_sql_identifier is reused

Tests are grouped into ``class Test...`` blocks to match the sibling suites
(e.g. ``file_ingestion/unit_tests/test_utils.py``). Shared arrange logic lives
in module-level fixtures: ``search_preamble`` (the matched-dimension search
monkeypatch block) and ``bearer_middleware`` (the auth ASGI harness).
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
import uvicorn
from mcp_db_server import server as mcp_db_server
from mcp_db_server.server import (
    BearerAuthMiddleware,
    describe_tables,
    get_embedding_model_name,
    get_rerank_model_name,
    list_databases,
    list_schemas,
    list_tables,
    parse_auth_tokens,
    reciprocal_rank_fusion,
    run_sql,
    search,
    validate_sql_identifier,
)

# ---------------------------------------------------------------------------
# Fakes for the SQLAlchemy connection/engine layer
# ---------------------------------------------------------------------------


class _FakeResult:
    """Minimal stand-in for a SQLAlchemy Result.

    Note: ``fetchmany`` mutates ``self._rows`` (removing the taken slice), so a
    subsequent ``fetchone()`` sees only the remainder. The run_sql truncation
    probe relies on exactly this ordering -- it fetchmany(max_rows) then
    fetchone() to detect a leftover row.
    """

    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self._columns = columns
        self._rows = list(rows)

    def keys(self) -> list[str]:
        return self._columns

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, size: int) -> list[tuple]:
        taken, self._rows = self._rows[:size], self._rows[size:]
        return taken


class _CapturingConnection:
    """Fake connection returning canned results in order, capturing SQL."""

    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = list(results)
        self.executed_sql: list[str] = []
        # (str(statement), params) for each execute, so tests can assert the
        # bound values alongside the SQL text where relevant.
        self.executed_params: list[tuple[str, object]] = []

    def __enter__(self) -> "_CapturingConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, params: object = None) -> _FakeResult:
        self.executed_sql.append(str(statement))
        self.executed_params.append((str(statement), params))
        return self._results.pop(0)


def _engine_yielding(*connections: _CapturingConnection) -> MagicMock:
    """Build a fake engine whose .connect() returns each connection in turn."""
    engine = MagicMock()
    engine.connect.side_effect = list(connections)
    return engine


def _stub_served_databases(
    monkeypatch: pytest.MonkeyPatch, names: set[str]
) -> None:
    """Stub the role-derived served-database set used by _validate_database."""
    monkeypatch.setattr(
        mcp_db_server, "get_served_databases", lambda: set(names)
    )


def _make_embedding_model(dim: int) -> MagicMock:
    """Build a fake SentenceTransformer with a fixed output dimension."""
    model = MagicMock()
    model.get_sentence_embedding_dimension.return_value = dim
    model.encode.return_value = MagicMock(tolist=lambda: [0.0] * dim)
    return model


def _cand(
    doc_id: str, fused_score: float, matched_legs: list[str] | None = None
) -> dict:
    """Build a minimal RRF candidate dict for search pre-cut tests.

    ``chunk_text`` mirrors ``doc_id`` so a candidate is identifiable by the
    ``(query, chunk_text)`` pair the reranker receives.
    """
    return {
        "doc_id": doc_id,
        "chunk_text": doc_id,
        "fused_score": fused_score,
        "source_table": "t",
        "matched_legs": matched_legs or ["dense"],
    }


def _capturing_reranker() -> tuple[MagicMock, list[list[str]]]:
    """A fake reranker recording the pairs it scores; ranks by list order.

    The returned ``captured`` list collects every ``(query, chunk_text)`` pair
    passed to ``predict``, so a test can assert exactly which candidates
    survived the pre-cut to reach the reranker.
    """
    captured: list[list[str]] = []
    reranker = MagicMock()

    def predict(pairs: list[list[str]]) -> list[float]:
        captured.extend(pairs)
        return [1.0 - 0.01 * i for i in range(len(pairs))]

    reranker.predict.side_effect = predict
    return reranker, captured


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def search_preamble(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arrange the matched-dimension (384/384) ``search`` monkeypatch block.

    Stubs the served set, the engine, a 384-dim embedding model, a single
    ``embedding`` vector column, and a 384-dim table vector so the dimension
    guard passes. Tests that need a different dimension (e.g. the mismatch and
    empty-table guard cases) deliberately do NOT use this fixture and set their
    own model/table dims, since varying that dim is the point of those tests.
    """
    _stub_served_databases(monkeypatch, {"policy_db"})
    monkeypatch.setattr(
        mcp_db_server, "get_database_engine", lambda db: MagicMock()
    )
    monkeypatch.setattr(
        mcp_db_server, "get_embedding_model", lambda: _make_embedding_model(384)
    )
    monkeypatch.setattr(
        mcp_db_server, "_get_vector_columns", lambda e, s, t: ["embedding"]
    )
    monkeypatch.setattr(
        mcp_db_server, "_get_vector_dimension", lambda e, s, t, c: 384
    )
    # Default served tables are hybrid (dense + sparse), matching the pipeline
    # invariant. The multi-table pre-cut tripwire checks this via
    # _get_tsvector_columns; tripwire tests override it to return [] (dense-only).
    monkeypatch.setattr(
        mcp_db_server, "_get_tsvector_columns", lambda e, s, t: ["chunk_tsv"]
    )


# ---------------------------------------------------------------------------
# Embedding / reranker model name resolution
# ---------------------------------------------------------------------------


class TestModelNameResolution:
    """Model-name resolution from defaults and env overrides."""

    def test_embedding_model_name_defaults_to_granite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The corrected default query model is the granite embedding model."""
        monkeypatch.delenv("MCP_EMBEDDING_MODEL", raising=False)
        assert (
            get_embedding_model_name()
            == "ibm-granite/granite-embedding-small-english-r2"
        )

    def test_embedding_model_name_reads_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MCP_EMBEDDING_MODEL overrides the default model name."""
        monkeypatch.setenv("MCP_EMBEDDING_MODEL", "some-org/custom-model")
        assert get_embedding_model_name() == "some-org/custom-model"

    def test_rerank_model_name_default_and_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reranker model name comes from the constant, overridable by env."""
        monkeypatch.delenv("MCP_RERANK_MODEL", raising=False)
        assert get_rerank_model_name() == mcp_db_server._DEFAULT_RERANK_MODEL

        monkeypatch.setenv("MCP_RERANK_MODEL", "some-org/custom-reranker")
        assert get_rerank_model_name() == "some-org/custom-reranker"

    def test_instructions_default_and_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Server instructions come from the default, overridable by env."""
        monkeypatch.delenv("MCP_INSTRUCTIONS", raising=False)
        assert (
            mcp_db_server.get_instructions()
            == mcp_db_server._DEFAULT_INSTRUCTIONS
        )

        monkeypatch.setenv("MCP_INSTRUCTIONS", "custom domain orientation")
        assert mcp_db_server.get_instructions() == "custom domain orientation"

    def test_trust_remote_code_default_false_and_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """trust_remote_code defaults to False; only truthy strings enable it."""
        monkeypatch.delenv("MCP_TRUST_REMOTE_CODE", raising=False)
        assert mcp_db_server._trust_remote_code() is False

        for truthy in ("true", "True", "1", "yes", "on", "ON"):
            monkeypatch.setenv("MCP_TRUST_REMOTE_CODE", truthy)
            assert mcp_db_server._trust_remote_code() is True

        for falsy in ("false", "0", "no", "off", "", "maybe"):
            monkeypatch.setenv("MCP_TRUST_REMOTE_CODE", falsy)
            assert mcp_db_server._trust_remote_code() is False

    def test_warm_models_enabled_default_and_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Warm-up defaults ON; only explicit falsy values disable it."""
        monkeypatch.delenv("MCP_WARM_MODELS", raising=False)
        assert mcp_db_server._warm_models_enabled() is True

        for falsy in ("false", "0", "no", "off", "False"):
            monkeypatch.setenv("MCP_WARM_MODELS", falsy)
            assert mcp_db_server._warm_models_enabled() is False

        # "" (explicitly empty) is default-on here, unlike the trust-remote-code
        # knob where "" is falsy -- warm-models defaults on, so empty means on.
        for truthy in ("true", "1", "yes", "on", "anything", ""):
            monkeypatch.setenv("MCP_WARM_MODELS", truthy)
            assert mcp_db_server._warm_models_enabled() is True

    def test_warm_models_loads_and_warms_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """warm_models loads both models and runs one warm-up inference each."""
        emb, rer = MagicMock(), MagicMock()
        monkeypatch.setattr(mcp_db_server, "get_embedding_model", lambda: emb)
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: rer)

        mcp_db_server.warm_models()

        emb.encode.assert_called_once()
        rer.predict.assert_called_once()


# ---------------------------------------------------------------------------
# Transport security (DNS-rebinding Host allowlist) from MCP_ALLOWED_HOSTS
# ---------------------------------------------------------------------------


class TestTransportSecurity:
    """_transport_security() maps MCP_ALLOWED_HOSTS to a TransportSecuritySettings.

    Unset preserves the SDK's localhost-only default (return None); ``*`` disables
    rebinding protection; a list enables protection scoped to those hosts/origins.
    """

    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset MCP_ALLOWED_HOSTS returns None so FastMCP keeps its default."""
        monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
        assert mcp_db_server._transport_security() is None

    def test_blank_or_whitespace_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank/whitespace value is treated as unset (return None)."""
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "   ")
        assert mcp_db_server._transport_security() is None

    def test_star_disables_rebinding_protection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'*' disables DNS-rebinding protection entirely (any Host accepted)."""
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*")
        ts = mcp_db_server._transport_security()
        assert ts is not None
        assert ts.enable_dns_rebinding_protection is False

    def test_list_enables_protection_with_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A comma list enables protection scoped to those (trimmed) hosts."""
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", " 10.0.0.5:* , localhost:8000 ")
        monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
        ts = mcp_db_server._transport_security()
        assert ts is not None
        assert ts.enable_dns_rebinding_protection is True
        assert ts.allowed_hosts == ["10.0.0.5:*", "localhost:8000"]
        assert ts.allowed_origins == []

    def test_origins_included_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MCP_ALLOWED_ORIGINS is parsed alongside an explicit host list."""
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "a:8000")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://x, https://y")
        ts = mcp_db_server._transport_security()
        assert ts is not None
        assert ts.allowed_hosts == ["a:8000"]
        assert ts.allowed_origins == ["https://x", "https://y"]


# ---------------------------------------------------------------------------
# Tuning-knob resolution + clamping (_env_int) and log-level resolution
# ---------------------------------------------------------------------------


class TestEnvInt:
    """The _env_int helper: parsing, non-int fallback, and minimum clamping."""

    def test_non_integer_warns_and_falls_back_to_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-integer value warns and returns the default."""
        monkeypatch.setenv("MCP_MAX_ROWS", "not-a-number")
        with caplog.at_level(logging.WARNING, logger="mcp_db_server.server"):
            value = mcp_db_server._env_int("MCP_MAX_ROWS", 500)

        assert value == 500
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "Ignoring non-integer" in m and "MCP_MAX_ROWS" in m
            for m in warnings
        )

    def test_value_below_minimum_warns_and_clamps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A value below the floor clamps to the floor and warns (MCP_DB_POOL_SIZE)."""
        monkeypatch.setenv("MCP_DB_POOL_SIZE", "0")
        with caplog.at_level(logging.WARNING, logger="mcp_db_server.server"):
            value = mcp_db_server._env_int("MCP_DB_POOL_SIZE", 5, minimum=1)

        assert value == 1
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "below the minimum" in m and "MCP_DB_POOL_SIZE" in m
            for m in warnings
        )

    def test_value_at_or_above_minimum_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A value at/above the floor is returned unchanged."""
        monkeypatch.setenv("MCP_DB_POOL_SIZE", "7")
        assert mcp_db_server._env_int("MCP_DB_POOL_SIZE", 5, minimum=1) == 7


class TestResolveLogLevel:
    """MCP_LOG_LEVEL resolution falls back to INFO on an unknown name."""

    def test_known_level_name_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A known level name resolves to its logging int."""
        monkeypatch.setenv("MCP_LOG_LEVEL", "debug")
        assert mcp_db_server._resolve_log_level() == logging.DEBUG

    def test_unknown_level_name_falls_back_to_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown/bogus level name falls back to INFO."""
        monkeypatch.setenv("MCP_LOG_LEVEL", "not-a-level")
        assert mcp_db_server._resolve_log_level() == logging.INFO

    def test_default_when_unset_is_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset MCP_LOG_LEVEL resolves to the INFO default."""
        monkeypatch.delenv("MCP_LOG_LEVEL", raising=False)
        assert mcp_db_server._resolve_log_level() == logging.INFO


# ---------------------------------------------------------------------------
# SQL identifier validation (reused canonical validator)
# ---------------------------------------------------------------------------


class TestValidateSqlIdentifier:
    """The reused canonical SQL-identifier validator."""

    @pytest.mark.parametrize(
        "identifier",
        ["pub_100_01", "my_schema", "_private", "a1b2c3", "qpp_cm"],
    )
    def test_accepts_valid(self, identifier: str) -> None:
        """Valid SQL identifiers pass and are returned unchanged."""
        assert validate_sql_identifier(identifier, "test") == identifier

    @pytest.mark.parametrize(
        "identifier",
        ["DROP TABLE", "schema; --", "123abc", "UPPERCASE", "has-dashes", ""],
    )
    def test_rejects_unsafe(self, identifier: str) -> None:
        """Unsafe SQL identifiers raise ValueError."""
        with pytest.raises(ValueError):
            validate_sql_identifier(identifier, "test")


# ---------------------------------------------------------------------------
# Role-derived list_databases + privilege-filtered list tools
# ---------------------------------------------------------------------------


class TestListTools:
    """Role-derived list_databases and privilege-filtered list tools."""

    def test_list_databases_uses_role_derived_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """list_databases issues the has_database_privilege CONNECT query."""
        conn = _CapturingConnection(
            [
                _FakeResult(
                    ["database_name", "description"],
                    [("policy_db", "Policy")],
                )
            ]
        )
        monkeypatch.setattr(
            mcp_db_server,
            "_get_bootstrap_engine",
            lambda: _engine_yielding(conn),
        )

        results = list_databases()

        assert results == [
            {"database_name": "policy_db", "description": "Policy"}
        ]
        sql = conn.executed_sql[0]
        assert "has_database_privilege(current_user" in sql
        assert "'CONNECT'" in sql
        assert "datistemplate = false" in sql

    def test_list_schemas_filters_by_usage_privilege(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """list_schemas adds the has_schema_privilege USAGE filter."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        conn = _CapturingConnection(
            [_FakeResult(["schema_name", "description"], [("qpp_cm", "QPP_CM")])]
        )
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )

        results = list_schemas(database="policy_db")

        assert results == [{"schema_name": "qpp_cm", "description": "QPP_CM"}]
        sql = conn.executed_sql[0]
        assert "has_schema_privilege(current_user" in sql
        assert "'USAGE'" in sql

    def test_list_tables_filters_by_select_privilege(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """list_tables adds the has_table_privilege SELECT filter."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        conn = _CapturingConnection(
            [
                _FakeResult(
                    ["table_name", "description", "approximate_row_count"],
                    [("measures", "Measures table", 42)],
                )
            ]
        )
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )

        results = list_tables(database="policy_db", schema="qpp_cm")

        assert results[0]["table_name"] == "measures"
        assert results[0]["approximate_row_count"] == 42
        sql = conn.executed_sql[0]
        assert "has_table_privilege(current_user" in sql
        assert "'SELECT'" in sql

    def test_tool_drives_real_served_databases_fresh_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tool re-resolves the served set fresh on every call (no cache).

        list_schemas is driven through the REAL get_served_databases /
        _fetch_databases (only the engine layer is faked). The bootstrap catalog
        returns a DIFFERENT served set on each resolution: {policy_db} on the
        first call, {policy_db, other_db} on the second. The second call
        validates ``other_db`` -- which is served only on the second resolution
        -- so it succeeds ONLY if the served set is queried fresh. A reintroduced
        process-lifetime cache would return the stale {policy_db} set and reject
        ``other_db`` with ValueError, failing this test.
        """
        # One bootstrap engine; each .connect() yields a fresh connection whose
        # canned result is the served set for THAT resolution.
        bootstrap_set_1 = _CapturingConnection(
            [
                _FakeResult(
                    ["database_name", "description"], [("policy_db", None)]
                )
            ]
        )
        bootstrap_set_2 = _CapturingConnection(
            [
                _FakeResult(
                    ["database_name", "description"],
                    [("policy_db", None), ("other_db", None)],
                )
            ]
        )
        bootstrap_engine = _engine_yielding(bootstrap_set_1, bootstrap_set_2)
        monkeypatch.setattr(
            mcp_db_server, "_get_bootstrap_engine", lambda: bootstrap_engine
        )
        # A fresh schema connection per call so the second call is not starved by
        # an exhausted single-use fake (which would mask the cache regression).
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(
                _CapturingConnection(
                    [
                        _FakeResult(
                            ["schema_name", "description"], [("qpp_cm", None)]
                        )
                    ]
                )
            ),
        )

        # First call: only policy_db is served.
        first = list_schemas(database="policy_db")
        assert first == [{"schema_name": "qpp_cm", "description": None}]

        # Second call: other_db became served. A fresh query observes the new
        # set; a process-lifetime cache would still hold {policy_db} and reject.
        second = list_schemas(database="other_db")
        assert second == [{"schema_name": "qpp_cm", "description": None}]

        # Structural lock: the bootstrap engine is connected once per served-set
        # resolution -- twice across two tool calls. A cache would connect once.
        assert bootstrap_engine.connect.call_count == 2

    def test_list_tables_rejects_unserved_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A database outside the role's served set raises ValueError."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        with pytest.raises(ValueError, match="not served"):
            list_tables(database="other_db", schema="qpp_cm")

    def test_list_tables_rejects_invalid_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unsafe schema identifier raises ValueError."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        with pytest.raises(ValueError, match="identifier"):
            list_tables(database="policy_db", schema="DROP TABLE")


# ---------------------------------------------------------------------------
# describe_tables
# ---------------------------------------------------------------------------


def _columns_result() -> _FakeResult:
    """Build a canned information_schema.columns result for one table.

    The two rows mirror the SQL CASE's already-resolved ``data_type`` (the
    USER-DEFINED ``embedding`` column arrives as ``vector``) and exercise both
    nullable values, so callers can assert the YES/NO -> bool coercion.
    """
    return _FakeResult(
        ["column_name", "data_type", "is_nullable"],
        [
            # USER-DEFINED column: SQL CASE resolves data_type to udt_name
            # (``vector``); is_nullable NO -> nullable False.
            ("embedding", "vector", "NO"),
            # Plain column with is_nullable YES -> nullable True.
            ("chunk_text", "text", "YES"),
        ],
    )


class TestDescribeTables:
    """describe_tables column mapping, default-all discovery, and guards."""

    def test_rejects_invalid_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unsafe table identifier in the list raises ValueError."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        monkeypatch.setattr(
            mcp_db_server, "get_database_engine", lambda db: MagicMock()
        )
        with pytest.raises(ValueError, match="identifier"):
            describe_tables(
                database="policy_db", schema="qpp_cm", tables=["DROP TABLE"]
            )

    def test_maps_columns_with_pk_and_references(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit single table -> {table: cols} with all five column fields.

        The USER-DEFINED -> udt_name resolution lives in the SQL CASE, so the
        fake supplies the already-resolved data_type per row; the test asserts
        the issued SQL carries that CASE, the Python-side ``row[2] == "YES"``
        coercion yields True/False, and the canned PK/FK helpers feed
        ``is_primary_key`` / ``references``.
        """
        _stub_served_databases(monkeypatch, {"policy_db"})
        conn = _CapturingConnection([_columns_result()])
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )
        # ``embedding`` is the PK; ``chunk_text`` is a FK into another table.
        monkeypatch.setattr(
            mcp_db_server,
            "_get_primary_key_columns",
            lambda e, s, t: ["embedding"],
        )
        monkeypatch.setattr(
            mcp_db_server,
            "_get_foreign_keys",
            lambda e, s, t: {"chunk_text": "qpp_cm.document.id"},
        )

        results = describe_tables(
            database="policy_db",
            schema="qpp_cm",
            tables=["measures_embedding"],
        )

        assert results == {
            "measures_embedding": [
                {
                    "column_name": "embedding",
                    "data_type": "vector",
                    "nullable": False,
                    "is_primary_key": True,
                    "references": None,
                },
                {
                    "column_name": "chunk_text",
                    "data_type": "text",
                    "nullable": True,
                    "is_primary_key": False,
                    "references": "qpp_cm.document.id",
                },
            ]
        }
        assert results["measures_embedding"][0]["nullable"] is False
        assert results["measures_embedding"][1]["nullable"] is True
        # The udt_name CASE lives in the SQL; lock it via the issued statement.
        sql = conn.executed_sql[0]
        assert "udt_name" in sql
        assert "USER-DEFINED" in sql

    def test_explicit_table_no_columns_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicitly requested empty table raises 'not found or no columns'."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        conn = _CapturingConnection(
            [_FakeResult(["column_name", "data_type", "is_nullable"], [])]
        )
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )

        with pytest.raises(ValueError, match="not found or has no columns"):
            describe_tables(
                database="policy_db", schema="qpp_cm", tables=["ghost_table"]
            )

    def test_default_all_describes_every_role_readable_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tables=None discovers all role-readable tables and describes each.

        ``_list_table_names`` is patched to the discovered set, and the engine
        yields a fresh connection per table (the listing query consumes none
        because it is patched). The result keys are exactly the discovered
        tables, each with column dicts.
        """
        _stub_served_databases(monkeypatch, {"policy_db"})
        # One connection per discovered table's columns query (listing patched).
        engine = _engine_yielding(
            _CapturingConnection([_columns_result()]),
            _CapturingConnection([_columns_result()]),
        )
        monkeypatch.setattr(
            mcp_db_server, "get_database_engine", lambda db: engine
        )
        monkeypatch.setattr(
            mcp_db_server,
            "_list_table_names",
            lambda e, s: ["document", "document_content"],
        )
        monkeypatch.setattr(
            mcp_db_server, "_get_primary_key_columns", lambda e, s, t: []
        )
        monkeypatch.setattr(
            mcp_db_server, "_get_foreign_keys", lambda e, s, t: {}
        )

        results = describe_tables(database="policy_db", schema="qpp_cm")

        assert set(results.keys()) == {"document", "document_content"}
        for columns in results.values():
            assert columns  # each discovered table carries its columns
            assert columns[0]["column_name"] == "embedding"

    def test_default_all_skips_discovered_table_with_no_columns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In default-all mode, a discovered empty table is skipped, not raised.

        This is the behavior that distinguishes default-all from explicit mode:
        ``document`` yields columns while ``ghost`` yields none, so the result
        keeps only ``document`` and no error is raised.
        """
        _stub_served_databases(monkeypatch, {"policy_db"})
        engine = _engine_yielding(
            _CapturingConnection([_columns_result()]),
            _CapturingConnection(
                [_FakeResult(["column_name", "data_type", "is_nullable"], [])]
            ),
        )
        monkeypatch.setattr(
            mcp_db_server, "get_database_engine", lambda db: engine
        )
        monkeypatch.setattr(
            mcp_db_server,
            "_list_table_names",
            lambda e, s: ["document", "ghost"],
        )
        monkeypatch.setattr(
            mcp_db_server, "_get_primary_key_columns", lambda e, s, t: []
        )
        monkeypatch.setattr(
            mcp_db_server, "_get_foreign_keys", lambda e, s, t: {}
        )

        results = describe_tables(database="policy_db", schema="qpp_cm")

        # ``ghost`` is skipped (no columns); no exception is raised.
        assert set(results.keys()) == {"document"}

    def test_composite_pk_and_composite_fk_split_per_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A multi-column PK marks every member; a composite FK splits per column.

        Drives the result purely through the canned PK/FK helpers: both columns
        belong to the PK, and both are FK members whose references resolve to
        distinct ``schema.table.column`` targets.
        """
        _stub_served_databases(monkeypatch, {"policy_db"})
        conn = _CapturingConnection(
            [
                _FakeResult(
                    ["column_name", "data_type", "is_nullable"],
                    [
                        ("doc_id", "integer", "NO"),
                        ("chunk_id", "integer", "NO"),
                    ],
                )
            ]
        )
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )
        # Composite PK: both columns are PK members.
        monkeypatch.setattr(
            mcp_db_server,
            "_get_primary_key_columns",
            lambda e, s, t: ["doc_id", "chunk_id"],
        )
        # Composite FK: each constrained column maps to its own referenced column.
        monkeypatch.setattr(
            mcp_db_server,
            "_get_foreign_keys",
            lambda e, s, t: {
                "doc_id": "qpp_cm.document.doc_id",
                "chunk_id": "qpp_cm.document.chunk_id",
            },
        )

        results = describe_tables(
            database="policy_db", schema="qpp_cm", tables=["chunk_link"]
        )

        columns = results["chunk_link"]
        assert all(col["is_primary_key"] for col in columns)
        assert columns[0]["references"] == "qpp_cm.document.doc_id"
        assert columns[1]["references"] == "qpp_cm.document.chunk_id"

    def test_list_table_names_filters_by_select_privilege(self) -> None:
        """_list_table_names issues the SELECT-privilege + relkind='r' filter.

        Default-all ``describe_tables`` discovers tables via this query, so the
        privilege filter and the ordinary-table gate must hold. Driven through a
        real ``_CapturingConnection`` (NOT monkeypatched) so the issued SQL is
        asserted, mirroring ``test_list_tables_filters_by_select_privilege``.
        """
        conn = _CapturingConnection(
            [_FakeResult(["table_name"], [("document",), ("measures",)])]
        )
        engine = _engine_yielding(conn)

        names = mcp_db_server._list_table_names(engine, "qpp_cm")

        assert names == ["document", "measures"]
        sql = conn.executed_sql[0]
        assert "has_table_privilege(current_user" in sql
        assert "'SELECT'" in sql
        assert "relkind = 'r'" in sql
        assert conn.executed_params[0][1] == {"schema": "qpp_cm"}

    def test_get_foreign_keys_splits_composite_with_schema_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_get_foreign_keys zips a composite FK per column with schema fallback.

        Patches the module-level ``inspect`` so the real splitting body runs:
        a composite constraint (``constrained_columns``/``referred_columns``
        positionally paired) is split into one reference per column, and a
        ``referred_schema`` of None falls back to the current schema.
        """
        fake_inspector = MagicMock()
        fake_inspector.get_foreign_keys.return_value = [
            {
                "constrained_columns": ["doc_id", "chunk_id"],
                "referred_columns": ["id", "cid"],
                "referred_table": "document",
                # None -> the helper falls back to the current schema (qpp_cm).
                "referred_schema": None,
            }
        ]
        monkeypatch.setattr(
            mcp_db_server, "inspect", lambda engine: fake_inspector
        )

        fk_map = mcp_db_server._get_foreign_keys(
            MagicMock(), "qpp_cm", "chunk_link"
        )

        assert fk_map == {
            "doc_id": "qpp_cm.document.id",
            "chunk_id": "qpp_cm.document.cid",
        }

    def test_get_foreign_keys_uses_explicit_referred_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cross-schema FK renders with the constraint's referred_schema."""
        fake_inspector = MagicMock()
        fake_inspector.get_foreign_keys.return_value = [
            {
                "constrained_columns": ["org_id"],
                "referred_columns": ["id"],
                "referred_table": "organisation",
                "referred_schema": "shared",
            }
        ]
        monkeypatch.setattr(
            mcp_db_server, "inspect", lambda engine: fake_inspector
        )

        fk_map = mcp_db_server._get_foreign_keys(MagicMock(), "qpp_cm", "person")

        assert fk_map == {"org_id": "shared.organisation.id"}

    def test_get_foreign_keys_last_constraint_wins_per_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A column in two FKs keeps the last constraint encountered."""
        fake_inspector = MagicMock()
        fake_inspector.get_foreign_keys.return_value = [
            {
                "constrained_columns": ["doc_id"],
                "referred_columns": ["id"],
                "referred_table": "document_a",
                "referred_schema": None,
            },
            {
                "constrained_columns": ["doc_id"],
                "referred_columns": ["id"],
                "referred_table": "document_b",
                "referred_schema": None,
            },
        ]
        monkeypatch.setattr(
            mcp_db_server, "inspect", lambda engine: fake_inspector
        )

        fk_map = mcp_db_server._get_foreign_keys(MagicMock(), "qpp_cm", "chunk")

        assert fk_map == {"doc_id": "qpp_cm.document_b.id"}

    def test_get_foreign_keys_skips_fk_with_no_referred_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed FK with a falsy referred_table is skipped, not rendered.

        Guards against emitting a ``schema.None.column`` literal from a partial
        reflection.
        """
        fake_inspector = MagicMock()
        fake_inspector.get_foreign_keys.return_value = [
            {
                "constrained_columns": ["doc_id"],
                "referred_columns": ["id"],
                "referred_table": None,
                "referred_schema": None,
            }
        ]
        monkeypatch.setattr(
            mcp_db_server, "inspect", lambda engine: fake_inspector
        )

        fk_map = mcp_db_server._get_foreign_keys(MagicMock(), "qpp_cm", "chunk")

        assert fk_map == {}


# ---------------------------------------------------------------------------
# run_sql safety guards
# ---------------------------------------------------------------------------


class TestRunSql:
    """run_sql single-statement, timeout, row-limit, and clamp guards."""

    @pytest.mark.parametrize(
        ("sql", "rejected"),
        [
            # A single statement with one trailing ';' is accepted (the guard
            # strips a single trailing ';' before checking for any remaining).
            ("select 1;", False),
            # A bare single statement is accepted.
            ("select 1", False),
            # Two statements separated by ';' are rejected.
            ("select 1; select 2", True),
        ],
    )
    def test_semicolon_boundary(
        self, monkeypatch: pytest.MonkeyPatch, sql: str, rejected: bool
    ) -> None:
        """The multi-statement guard accepts a single trailing ';' but rejects two."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        # Engine stubbed unconditionally; the reject case raises before use.
        conn = _CapturingConnection(
            [_FakeResult([], []), _FakeResult(["n"], [(1,)])]
        )
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )

        if rejected:
            with pytest.raises(ValueError, match="single SQL statements"):
                run_sql(database="policy_db", sql=sql)
        else:
            out = run_sql(database="policy_db", sql=sql)
            assert out["row_count"] == 1

    def test_rejects_unserved_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_sql against an unserved database raises ValueError."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        with pytest.raises(ValueError, match="not served"):
            run_sql(database="other_db", sql="SELECT 1")

    def test_sets_timeout_and_returns_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_sql sets the statement timeout and returns rows + metadata."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        # First execute = SET statement_timeout; second = the query.
        timeout_result = _FakeResult([], [])
        query_result = _FakeResult(
            ["measure_name"], [("Cost A",), ("Cost B",)]
        )
        conn = _CapturingConnection([timeout_result, query_result])
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )

        out = run_sql(
            database="policy_db", sql="SELECT measure_name FROM qpp_cm.measures"
        )

        assert "set local statement_timeout = '5s'" in conn.executed_sql[0]
        assert out["row_count"] == 2
        assert out["truncated"] is False
        assert out["rows"][0]["measure_name"] == "Cost A"

    def test_flags_truncation_at_max_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_sql caps at MCP_MAX_ROWS and flags truncation when more exist."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        monkeypatch.setenv("MCP_MAX_ROWS", "3")
        timeout_result = _FakeResult([], [])
        # Five rows available, limit of 3 -> truncated.
        query_result = _FakeResult(["n"], [(i,) for i in range(5)])
        conn = _CapturingConnection([timeout_result, query_result])
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )

        out = run_sql(database="policy_db", sql="SELECT n FROM t")

        assert out["row_count"] == 3
        assert out["truncated"] is True

    def test_statement_timeout_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MCP_STATEMENT_TIMEOUT_S changes the emitted set-local-timeout SQL."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        monkeypatch.setenv("MCP_STATEMENT_TIMEOUT_S", "12")
        timeout_result = _FakeResult([], [])
        query_result = _FakeResult(["n"], [(1,)])
        conn = _CapturingConnection([timeout_result, query_result])
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )

        run_sql(database="policy_db", sql="SELECT n FROM t")

        assert "set local statement_timeout = '12s'" in conn.executed_sql[0]

    def test_statement_timeout_non_int_falls_back_and_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-integer MCP_STATEMENT_TIMEOUT_S falls back and warns."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        monkeypatch.setenv("MCP_STATEMENT_TIMEOUT_S", "not-a-number")
        timeout_result = _FakeResult([], [])
        query_result = _FakeResult(["n"], [(1,)])
        conn = _CapturingConnection([timeout_result, query_result])
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )

        with caplog.at_level(logging.WARNING, logger="mcp_db_server.server"):
            run_sql(database="policy_db", sql="SELECT n FROM t")

        expected = f"'{mcp_db_server._DEFAULT_STATEMENT_TIMEOUT_S}s'"
        assert (
            f"set local statement_timeout = {expected}" in conn.executed_sql[0]
        )
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "Ignoring non-integer" in m and "MCP_STATEMENT_TIMEOUT_S" in m
            for m in warnings
        )

    def test_statement_timeout_zero_clamps_to_floor_and_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """MCP_STATEMENT_TIMEOUT_S=0 clamps to the 1s floor and warns.

        ``0`` means "no timeout" in PostgreSQL, silently disabling the guard, so
        the floor must keep it at ``'1s'`` and warn loudly.
        """
        _stub_served_databases(monkeypatch, {"policy_db"})
        monkeypatch.setenv("MCP_STATEMENT_TIMEOUT_S", "0")
        timeout_result = _FakeResult([], [])
        query_result = _FakeResult(["n"], [(1,)])
        conn = _CapturingConnection([timeout_result, query_result])
        monkeypatch.setattr(
            mcp_db_server,
            "get_database_engine",
            lambda db: _engine_yielding(conn),
        )

        with caplog.at_level(logging.WARNING, logger="mcp_db_server.server"):
            run_sql(database="policy_db", sql="SELECT n FROM t")

        assert "set local statement_timeout = '1s'" in conn.executed_sql[0]
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "below the minimum" in m and "MCP_STATEMENT_TIMEOUT_S" in m
            for m in warnings
        )


# ---------------------------------------------------------------------------
# Reciprocal rank fusion (pure)
# ---------------------------------------------------------------------------


class TestReciprocalRankFusion:
    """Pure RRF fusion math."""

    def test_row_in_both_legs_outranks_single_leg_rows(self) -> None:
        """A row in both legs outranks single-leg rows and records both legs."""
        shared, dense_only, sparse_only = {"id": 1}, {"id": 2}, {"id": 3}
        fused = reciprocal_rank_fusion(
            [[shared, dense_only], [shared, sparse_only]],
            key=lambda row: row["id"],
        )

        assert fused[0][0]["id"] == 1
        assert fused[0][2] == [0, 1]
        assert {fused[1][0]["id"], fused[2][0]["id"]} == {2, 3}

    def test_handles_empty_legs(self) -> None:
        """Empty ranked lists contribute nothing and do not error."""
        fused = reciprocal_rank_fusion(
            [[{"id": 1}, {"id": 2}], []], key=lambda row: row["id"]
        )
        assert [row["id"] for row, _, _ in fused] == [1, 2]
        assert reciprocal_rank_fusion([[], []]) == []


# ---------------------------------------------------------------------------
# _search_single_table -- RRF candidate generation (mocked engine)
# ---------------------------------------------------------------------------


def _patch_introspection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    vector_cols: list[str],
    tsvector_cols: list[str],
    all_cols: list[str],
    pk_cols: list[str],
) -> None:
    """Patch the table-introspection helpers used by _search_single_table."""
    monkeypatch.setattr(
        mcp_db_server, "_get_vector_columns", lambda e, s, t: vector_cols
    )
    monkeypatch.setattr(
        mcp_db_server, "_get_tsvector_columns", lambda e, s, t: tsvector_cols
    )
    monkeypatch.setattr(
        mcp_db_server, "_get_table_columns", lambda e, s, t: all_cols
    )
    monkeypatch.setattr(
        mcp_db_server, "_get_primary_key_columns", lambda e, s, t: pk_cols
    )


class TestSearchSingleTable:
    """Per-table hybrid candidate generation (dense + sparse, RRF)."""

    def test_hybrid_issues_both_legs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a tsvector column, both dense and sparse SQL are issued/fused."""
        display_cols = ["doc_id", "chunk_text"]
        _patch_introspection(
            monkeypatch,
            vector_cols=["embedding"],
            tsvector_cols=["chunk_tsv"],
            all_cols=["doc_id", "chunk_text", "embedding", "chunk_tsv"],
            pk_cols=["doc_id"],
        )
        dense = _FakeResult(display_cols, [(1, "alpha"), (2, "beta")])
        sparse = _FakeResult(display_cols, [(2, "beta"), (3, "gamma")])
        dense_conn = _CapturingConnection([dense])
        sparse_conn = _CapturingConnection([sparse])
        engine = _engine_yielding(dense_conn, sparse_conn)

        results = mcp_db_server._search_single_table(
            engine,
            schema="cms_iom",
            table="document_content_embedding",
            query="beta",
            query_embedding_str="[0.1,0.2]",
            pool_size=10,
            min_similarity=0.3,
        )

        # Row 2 is in both legs -> ranked first, both legs recorded.
        assert results[0]["doc_id"] == 2
        assert results[0]["matched_legs"] == ["dense", "sparse"]
        assert results[0]["source_table"] == "document_content_embedding"
        assert results[0]["fused_score"] > results[1]["fused_score"]

        # Each leg issues exactly one statement on its own connection.
        assert len(dense_conn.executed_sql) == 1
        assert len(sparse_conn.executed_sql) == 1

        # Dense leg: cosine operator + min_similarity predicate.
        dense_sql = dense_conn.executed_sql[0]
        assert "<=>" in dense_sql
        assert "min_similarity" in dense_sql
        assert "websearch_to_tsquery" not in dense_sql
        # Bound values reach the dense leg.
        _, dense_params = dense_conn.executed_params[0]
        assert dense_params["min_similarity"] == 0.3

        # Sparse leg: full-text query + rank function, bound query text.
        sparse_sql = sparse_conn.executed_sql[0]
        assert "websearch_to_tsquery" in sparse_sql
        assert "ts_rank_cd" in sparse_sql
        _, sparse_params = sparse_conn.executed_params[0]
        assert sparse_params["query"] == "beta"

    def test_dense_only_without_tsvector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a tsvector column, only the dense leg runs (fallback)."""
        display_cols = ["doc_id", "chunk_text"]
        _patch_introspection(
            monkeypatch,
            vector_cols=["embedding"],
            tsvector_cols=[],
            all_cols=["doc_id", "chunk_text", "embedding"],
            pk_cols=["doc_id"],
        )
        dense = _FakeResult(display_cols, [(1, "alpha"), (2, "beta")])
        dense_conn = _CapturingConnection([dense])
        engine = _engine_yielding(dense_conn)

        results = mcp_db_server._search_single_table(
            engine,
            schema="cms_iom",
            table="embedding_no_fts",
            query="alpha",
            query_embedding_str="[0.1,0.2]",
            pool_size=10,
            min_similarity=0.3,
        )

        for r in results:
            assert r["matched_legs"] == ["dense"]
            assert "fused_score" in r

        # Dense-only: exactly one statement, the dense leg, no full-text query.
        assert len(dense_conn.executed_sql) == 1
        dense_sql = dense_conn.executed_sql[0]
        assert "<=>" in dense_sql
        assert "min_similarity" in dense_sql
        assert "websearch_to_tsquery" not in dense_sql

    def test_no_pk_fuses_via_display_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no primary key, rows fuse via the full-display-row fallback key."""
        display_cols = ["doc_id", "chunk_text"]
        _patch_introspection(
            monkeypatch,
            vector_cols=["embedding"],
            tsvector_cols=["chunk_tsv"],
            all_cols=["doc_id", "chunk_text", "embedding", "chunk_tsv"],
            pk_cols=[],
        )
        # The shared row must match on the ENTIRE display tuple to fuse across
        # legs when the fallback key is the full display row.
        dense = _FakeResult(display_cols, [(1, "alpha"), (2, "beta")])
        sparse = _FakeResult(display_cols, [(2, "beta"), (3, "gamma")])
        engine = _engine_yielding(
            _CapturingConnection([dense]), _CapturingConnection([sparse])
        )

        results = mcp_db_server._search_single_table(
            engine,
            schema="cms_iom",
            table="no_pk_embedding",
            query="beta",
            query_embedding_str="[0.1,0.2]",
            pool_size=10,
            min_similarity=0.3,
        )

        # The identical (2, "beta") row fuses across both legs and ranks first.
        assert results[0]["doc_id"] == 2
        assert results[0]["matched_legs"] == ["dense", "sparse"]
        assert results[0]["fused_score"] > results[1]["fused_score"]


# ---------------------------------------------------------------------------
# search -- dimension guard + reranking pipeline (mocked)
# ---------------------------------------------------------------------------


class TestSearch:
    """The search pipeline: dimension guard, reranking, discovery, pooling."""

    def test_dimension_guard_raises_on_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model/table dimension mismatch raises a clear ValueError.

        Uses a deliberately mismatched 1024-dim model vs a 384-dim table, so it
        does NOT use the matched-dim ``search_preamble`` fixture.
        """
        _stub_served_databases(monkeypatch, {"policy_db"})
        monkeypatch.setattr(
            mcp_db_server, "get_database_engine", lambda db: MagicMock()
        )
        monkeypatch.setattr(
            mcp_db_server,
            "get_embedding_model",
            lambda: _make_embedding_model(1024),
        )
        monkeypatch.setattr(
            mcp_db_server, "_get_vector_columns", lambda e, s, t: ["embedding"]
        )
        # Table stores 384-dim vectors; model produces 1024-dim.
        monkeypatch.setattr(
            mcp_db_server, "_get_vector_dimension", lambda e, s, t, c: 384
        )

        with pytest.raises(ValueError, match="dimension mismatch"):
            search(
                database="policy_db",
                schema="qpp_cm",
                query="anything",
                tables=["measures_embedding"],
            )

    def test_dimension_guard_skips_empty_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty table (no determinable dim) does not trip the guard.

        Uses ``_get_vector_dimension -> None`` (empty table), so it does NOT use
        the matched-dim ``search_preamble`` fixture.
        """
        _stub_served_databases(monkeypatch, {"policy_db"})
        monkeypatch.setattr(
            mcp_db_server, "get_database_engine", lambda db: MagicMock()
        )
        monkeypatch.setattr(
            mcp_db_server,
            "get_embedding_model",
            lambda: _make_embedding_model(384),
        )
        monkeypatch.setattr(
            mcp_db_server, "_get_vector_columns", lambda e, s, t: ["embedding"]
        )
        # None -> empty table, dimension undeterminable.
        monkeypatch.setattr(
            mcp_db_server, "_get_vector_dimension", lambda e, s, t, c: None
        )
        monkeypatch.setattr(
            mcp_db_server, "_search_single_table", lambda *a, **k: []
        )
        # No get_reranker stub: search early-returns on empty candidates before
        # any reranking, so the reranker is never resolved here.

        # No candidates -> empty result, but crucially no dimension error.
        results = search(
            database="policy_db",
            schema="qpp_cm",
            query="anything",
            tables=["empty_embedding"],
        )
        assert results == []

    def test_reranker_reorders_rrf_candidates(
        self, monkeypatch: pytest.MonkeyPatch, search_preamble: None
    ) -> None:
        """The reranker reorders RRF candidates; output is by rerank_score."""
        # RRF candidates: row A ranks above row B by fused_score.
        candidates = [
            {
                "doc_id": "A",
                "chunk_text": "alpha text",
                "fused_score": 0.9,
                "source_table": "t",
                "matched_legs": ["dense"],
            },
            {
                "doc_id": "B",
                "chunk_text": "beta text",
                "fused_score": 0.1,
                "source_table": "t",
                "matched_legs": ["dense", "sparse"],
            },
        ]
        captured_pairs: list[list[str]] = []

        def fake_single_table(*args: object, **kwargs: object) -> list[dict]:
            # Return fresh copies so the test stays order-independent.
            return [dict(c) for c in candidates]

        monkeypatch.setattr(
            mcp_db_server, "_search_single_table", fake_single_table
        )

        fake_reranker = MagicMock()

        def fake_predict(pairs: list[list[str]]) -> list[float]:
            captured_pairs.extend(pairs)
            # Invert the RRF order: B scores higher than A.
            return [0.2 if p[1] == "alpha text" else 0.8 for p in pairs]

        fake_reranker.predict.side_effect = fake_predict
        monkeypatch.setattr(
            mcp_db_server, "get_reranker", lambda: fake_reranker
        )

        results = search(
            database="policy_db",
            schema="qpp_cm",
            query="find beta",
            top_k=2,
            tables=["t"],
        )

        # Reranker put B first despite A's higher fused_score.
        assert [r["doc_id"] for r in results] == ["B", "A"]
        assert results[0]["rerank_score"] == 0.8
        assert results[1]["rerank_score"] == 0.2
        # source_table / matched_legs preserved through reranking.
        assert results[0]["matched_legs"] == ["dense", "sparse"]
        # The reranker scored (query, chunk_text) pairs from the RRF candidates.
        assert ["find beta", "alpha text"] in captured_pairs
        assert ["find beta", "beta text"] in captured_pairs

    def test_no_embedding_tables_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A schema with no embedding tables raises ValueError."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        monkeypatch.setattr(
            mcp_db_server, "get_database_engine", lambda db: MagicMock()
        )
        monkeypatch.setattr(
            mcp_db_server,
            "get_embedding_model",
            lambda: _make_embedding_model(384),
        )
        monkeypatch.setattr(
            mcp_db_server, "_discover_embedding_tables", lambda e, s: []
        )

        with pytest.raises(ValueError, match="No embedding tables"):
            search(database="policy_db", schema="public", query="x")

    def test_multi_schema_searches_all_and_reranks_once(
        self, monkeypatch: pytest.MonkeyPatch, search_preamble: None
    ) -> None:
        """A schema list searches each schema and reranks the merged pool once."""
        monkeypatch.setattr(
            mcp_db_server, "_discover_embedding_tables", lambda e, s: [f"{s}_emb"]
        )

        def fake_single_table(
            engine: object, schema: str, table: str, *a: object, **k: object
        ) -> list[dict]:
            return [
                {
                    "doc_id": f"{schema}-1",
                    "chunk_text": f"text from {schema}",
                    "fused_score": 0.5,
                    "source_table": table,
                    "matched_legs": ["dense"],
                }
            ]

        monkeypatch.setattr(
            mcp_db_server, "_search_single_table", fake_single_table
        )

        predict_calls: list[list[list[str]]] = []
        fake_reranker = MagicMock()

        def fake_predict(pairs: list[list[str]]) -> list[float]:
            predict_calls.append(pairs)
            return [0.9 - 0.1 * i for i in range(len(pairs))]

        fake_reranker.predict.side_effect = fake_predict
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: fake_reranker)

        results = search(
            database="policy_db", schema=["cms_iom", "usc"], query="q", top_k=10
        )

        # Both schemas contributed; each result carries its source_schema.
        assert {r["source_schema"] for r in results} == {"cms_iom", "usc"}
        # Exactly ONE rerank pass over the merged 2-candidate pool.
        assert len(predict_calls) == 1
        assert len(predict_calls[0]) == 2

    def test_single_schema_string_still_works(
        self, monkeypatch: pytest.MonkeyPatch, search_preamble: None
    ) -> None:
        """schema as a plain string keeps single-schema behavior; source_schema set."""
        monkeypatch.setattr(
            mcp_db_server,
            "_search_single_table",
            lambda *a, **k: [
                {
                    "doc_id": "A",
                    "chunk_text": "t",
                    "fused_score": 0.5,
                    "source_table": "t",
                    "matched_legs": ["dense"],
                }
            ],
        )
        fake_reranker = MagicMock()
        fake_reranker.predict.return_value = [0.9]
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: fake_reranker)

        results = search(
            database="policy_db", schema="cms_iom", query="q", tables=["t"]
        )

        # source_schema set, and the string path still traverses the full
        # (encode -> candidates -> merged rerank) pipeline.
        assert results[0]["source_schema"] == "cms_iom"
        assert results[0]["rerank_score"] == 0.9

    def test_tables_with_multiple_schemas_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`tables` cannot be combined with multiple schemas."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        with pytest.raises(
            ValueError, match="cannot be combined with multiple schemas"
        ):
            search(
                database="policy_db",
                schema=["cms_iom", "usc"],
                query="q",
                tables=["t"],
            )

    def test_empty_schema_list_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty schema list raises before any engine work."""
        _stub_served_databases(monkeypatch, {"policy_db"})
        with pytest.raises(ValueError, match="At least one schema"):
            search(database="policy_db", schema=[], query="q")

    def test_multi_schema_dimension_mismatch_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dimension mismatch in a NON-first schema still trips the guard.

        Does NOT use search_preamble (which forces matching dims); the second
        schema's table stores 1024-dim vectors vs the 384-dim query model.
        """
        _stub_served_databases(monkeypatch, {"policy_db"})
        monkeypatch.setattr(
            mcp_db_server, "get_database_engine", lambda db: MagicMock()
        )
        monkeypatch.setattr(
            mcp_db_server,
            "get_embedding_model",
            lambda: _make_embedding_model(384),
        )
        monkeypatch.setattr(
            mcp_db_server, "_discover_embedding_tables", lambda e, s: [f"{s}_emb"]
        )
        monkeypatch.setattr(
            mcp_db_server, "_get_vector_columns", lambda e, s, t: ["embedding"]
        )
        # cms_iom (first) matches at 384; usc (second) mismatches at 1024.
        monkeypatch.setattr(
            mcp_db_server,
            "_get_vector_dimension",
            lambda e, s, t, c: 384 if s == "cms_iom" else 1024,
        )

        with pytest.raises(ValueError, match="dimension mismatch for usc"):
            search(database="policy_db", schema=["cms_iom", "usc"], query="q")

    def test_explicit_table_without_embedding_column_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicitly requested table with no vector column raises clearly.

        Auto-discovery only returns vector tables, so this only happens when the
        caller names a non-embedding table in `tables`; it must fail loudly
        rather than silently contribute no candidates.
        """
        _stub_served_databases(monkeypatch, {"policy_db"})
        monkeypatch.setattr(
            mcp_db_server, "get_database_engine", lambda db: MagicMock()
        )
        monkeypatch.setattr(
            mcp_db_server, "get_embedding_model", lambda: _make_embedding_model(384)
        )
        monkeypatch.setattr(
            mcp_db_server, "_get_vector_columns", lambda e, s, t: []
        )

        with pytest.raises(ValueError, match="not searchable"):
            search(
                database="policy_db",
                schema="cms_iom",
                query="x",
                tables=["document"],
            )

    def test_warns_on_empty_chunk_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        search_preamble: None,
    ) -> None:
        """A candidate missing chunk_text warns and yields a [query, ''] pair.

        The reranker must still receive an empty-string pair for that candidate
        (no crash), and it ranks low because the fake reranker scores empty text
        0.0.
        """
        # One candidate has no chunk_text key at all; the other is non-empty.
        candidates = [
            {
                "doc_id": "missing",
                "fused_score": 0.9,
                "source_table": "t",
                "matched_legs": ["dense"],
            },
            {
                "doc_id": "present",
                "chunk_text": "real text",
                "fused_score": 0.1,
                "source_table": "t",
                "matched_legs": ["dense"],
            },
        ]
        monkeypatch.setattr(
            mcp_db_server,
            "_search_single_table",
            lambda *a, **k: [dict(c) for c in candidates],
        )

        captured_pairs: list[list[str]] = []
        fake_reranker = MagicMock()

        def fake_predict(pairs: list[list[str]]) -> list[float]:
            captured_pairs.extend(pairs)
            # Empty text ranks low; non-empty text ranks high.
            return [0.0 if p[1] == "" else 0.9 for p in pairs]

        fake_reranker.predict.side_effect = fake_predict
        monkeypatch.setattr(
            mcp_db_server, "get_reranker", lambda: fake_reranker
        )

        with caplog.at_level(logging.WARNING, logger="mcp_db_server.server"):
            results = search(
                database="policy_db",
                schema="qpp_cm",
                query="find text",
                top_k=2,
                tables=["t"],
            )

        # The warning fired and reports the count (1 of 2).
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert any(
            "1 of 2 rerank candidates" in m and "chunk_text" in m
            for m in warnings
        )
        # The reranker received a [query, ""] pair for the missing-text candidate.
        assert ["find text", ""] in captured_pairs
        # The empty-text candidate ranked low (last), without crashing.
        assert [r["doc_id"] for r in results] == ["present", "missing"]

    def test_auto_discovers_tables_when_none(
        self, monkeypatch: pytest.MonkeyPatch, search_preamble: None
    ) -> None:
        """tables=None searches the tables from _discover_embedding_tables."""
        # Discovery returns a non-empty list that should flow into the search.
        monkeypatch.setattr(
            mcp_db_server,
            "_discover_embedding_tables",
            lambda e, s: ["alpha_embedding", "beta_embedding"],
        )

        searched_tables: list[str] = []

        def spy_single_table(
            engine: object,
            schema: str,
            table: str,
            *args: object,
            **kwargs: object,
        ) -> list[dict]:
            searched_tables.append(table)
            return [
                {
                    "doc_id": f"{table}-1",
                    "chunk_text": "text",
                    "fused_score": 0.5,
                    "source_table": table,
                    "matched_legs": ["dense"],
                }
            ]

        monkeypatch.setattr(
            mcp_db_server, "_search_single_table", spy_single_table
        )

        fake_reranker = MagicMock()
        fake_reranker.predict.side_effect = lambda pairs: [0.5] * len(pairs)
        monkeypatch.setattr(
            mcp_db_server, "get_reranker", lambda: fake_reranker
        )

        results = search(
            database="policy_db", schema="qpp_cm", query="anything", tables=None
        )

        # Both discovered tables were searched.
        assert searched_tables == ["alpha_embedding", "beta_embedding"]
        assert {r["source_table"] for r in results} == {
            "alpha_embedding",
            "beta_embedding",
        }

    def test_rerank_pool_env_override_changes_pool_size(
        self, monkeypatch: pytest.MonkeyPatch, search_preamble: None
    ) -> None:
        """MCP_RERANK_POOL changes the pool_size passed to _search_single_table.

        With top_k below the override, pool_size = max(top_k, MCP_RERANK_POOL)
        resolves to the override, so the captured per-table pool equals it.
        """
        monkeypatch.setenv("MCP_RERANK_POOL", "37")

        captured_pool: list[int] = []

        def spy_single_table(
            engine: object,
            schema: str,
            table: str,
            query: str,
            query_embedding_str: str,
            pool_size: int,
            min_similarity: float,
        ) -> list[dict]:
            captured_pool.append(pool_size)
            return []

        monkeypatch.setattr(
            mcp_db_server, "_search_single_table", spy_single_table
        )

        search(
            database="policy_db",
            schema="qpp_cm",
            query="anything",
            top_k=5,
            tables=["t"],
        )

        # pool_size = max(top_k=5, MCP_RERANK_POOL=37) = 37.
        assert captured_pool == [37]


# ---------------------------------------------------------------------------
# _rerank_global_pool -- global (cross-table) rerank-pool cap resolution
# ---------------------------------------------------------------------------


class TestRerankGlobalPool:
    """MCP_RERANK_GLOBAL_POOL resolution, default, and clamping."""

    def test_defaults_to_rerank_pool_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset -> falls back to _rerank_pool() (the per-table pool default)."""
        monkeypatch.delenv("MCP_RERANK_GLOBAL_POOL", raising=False)
        monkeypatch.delenv("MCP_RERANK_POOL", raising=False)
        assert (
            mcp_db_server._rerank_global_pool()
            == mcp_db_server._DEFAULT_RERANK_POOL
        )

    def test_tracks_rerank_pool_override_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset -> follows MCP_RERANK_POOL, so one dial governs both by default."""
        monkeypatch.delenv("MCP_RERANK_GLOBAL_POOL", raising=False)
        monkeypatch.setenv("MCP_RERANK_POOL", "30")
        assert mcp_db_server._rerank_global_pool() == 30

    def test_honors_own_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MCP_RERANK_GLOBAL_POOL decouples the global cap from the per-table pool."""
        monkeypatch.setenv("MCP_RERANK_POOL", "50")
        monkeypatch.setenv("MCP_RERANK_GLOBAL_POOL", "17")
        assert mcp_db_server._rerank_global_pool() == 17

    def test_clamps_to_minimum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A value below 1 is clamped up to the minimum of 1."""
        monkeypatch.setenv("MCP_RERANK_GLOBAL_POOL", "0")
        assert mcp_db_server._rerank_global_pool() == 1


# ---------------------------------------------------------------------------
# search -- global candidate pre-cut before reranking
# ---------------------------------------------------------------------------


class TestSearchGlobalPreCut:
    """The merged candidate pool is capped by fused_score before reranking."""

    def test_caps_merged_pool_to_global_cap(
        self, monkeypatch: pytest.MonkeyPatch, search_preamble: None
    ) -> None:
        """Multi-schema pool over the cap is trimmed to the cap in one rerank pass."""
        monkeypatch.setenv("MCP_RERANK_GLOBAL_POOL", "5")
        monkeypatch.setattr(
            mcp_db_server, "_discover_embedding_tables", lambda e, s: [f"{s}_emb"]
        )
        per_schema = {
            "cms_iom": [
                _cand(f"a{i}", sc)
                for i, sc in enumerate([0.9, 0.7, 0.5, 0.3, 0.1])
            ],
            "usc": [
                _cand(f"b{i}", sc)
                for i, sc in enumerate([0.8, 0.6, 0.4, 0.2, 0.05])
            ],
        }

        def fake_single_table(
            engine: object, schema: str, table: str, *a: object, **k: object
        ) -> list[dict]:
            return [dict(c) for c in per_schema[schema]]

        monkeypatch.setattr(
            mcp_db_server, "_search_single_table", fake_single_table
        )
        reranker, captured = _capturing_reranker()
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: reranker)

        # top_k below the cap so global_cap resolves to the cap (5), not top_k.
        search(
            database="policy_db",
            schema=["cms_iom", "usc"],
            query="q",
            top_k=3,
        )

        # Exactly one rerank pass over exactly the 5 highest-fused_score rows.
        assert reranker.predict.call_count == 1
        assert len(captured) == 5
        survived = {p[1] for p in captured}
        assert survived == {"a0", "b0", "a1", "b1", "a2"}

    def test_caps_single_schema_multi_table_pool(
        self, monkeypatch: pytest.MonkeyPatch, search_preamble: None
    ) -> None:
        """A single schema with multiple tables is trimmed too.

        The pre-cut fires on candidate count, i.e. the number of TABLES touched,
        not the number of schemas -- a single schema whose auto-discovery returns
        several embedding tables (e.g. qpp_cm) is trimmed just like a multi-schema
        search.
        """
        monkeypatch.setenv("MCP_RERANK_GLOBAL_POOL", "4")
        monkeypatch.setattr(
            mcp_db_server,
            "_discover_embedding_tables",
            lambda e, s: ["alpha_emb", "beta_emb"],
        )
        per_table = {
            "alpha_emb": [
                _cand(f"alpha{i}", sc) for i, sc in enumerate([0.9, 0.5, 0.1])
            ],
            "beta_emb": [
                _cand(f"beta{i}", sc) for i, sc in enumerate([0.8, 0.6, 0.2])
            ],
        }

        def fake_single_table(
            engine: object, schema: str, table: str, *a: object, **k: object
        ) -> list[dict]:
            return [dict(c) for c in per_table[table]]

        monkeypatch.setattr(
            mcp_db_server, "_search_single_table", fake_single_table
        )
        reranker, captured = _capturing_reranker()
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: reranker)

        search(database="policy_db", schema="qpp_cm", query="q", top_k=2)

        # 6 candidates from 2 tables in one schema -> trimmed to the top 4.
        assert len(captured) == 4
        assert {p[1] for p in captured} == {"alpha0", "beta0", "beta1", "alpha1"}

    def test_selects_exactly_highest_fused_score(
        self, monkeypatch: pytest.MonkeyPatch, search_preamble: None
    ) -> None:
        """Survivors equal sorted(pool, by fused_score desc)[:cap] -- pins the key."""
        monkeypatch.setenv("MCP_RERANK_GLOBAL_POOL", "4")
        monkeypatch.setattr(
            mcp_db_server, "_discover_embedding_tables", lambda e, s: [f"{s}_emb"]
        )
        scores = {
            "cms_iom": [0.31, 0.95, 0.42, 0.60],
            "usc": [0.50, 0.70, 0.20, 0.88],
        }

        def fake_single_table(
            engine: object, schema: str, table: str, *a: object, **k: object
        ) -> list[dict]:
            return [
                _cand(f"{schema}-{i}", sc)
                for i, sc in enumerate(scores[schema])
            ]

        monkeypatch.setattr(
            mcp_db_server, "_search_single_table", fake_single_table
        )
        reranker, captured = _capturing_reranker()
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: reranker)

        search(
            database="policy_db",
            schema=["cms_iom", "usc"],
            query="q",
            top_k=2,
        )

        merged = [
            (f"{schema}-{i}", sc)
            for schema, scs in scores.items()
            for i, sc in enumerate(scs)
        ]
        expected = {
            doc for doc, _ in sorted(merged, key=lambda t: t[1], reverse=True)[:4]
        }
        assert {p[1] for p in captured} == expected

    def test_retains_single_leg_within_cap(
        self, monkeypatch: pytest.MonkeyPatch, search_preamble: None
    ) -> None:
        """Selection is by fused_score alone; single-leg rows within the cap survive."""
        monkeypatch.setenv("MCP_RERANK_GLOBAL_POOL", "3")
        candidates = [
            _cand("dual_hi", 0.9, ["dense", "sparse"]),
            _cand("single_mid", 0.8, ["dense"]),
            _cand("dual_lo", 0.7, ["dense", "sparse"]),
            _cand("single_low", 0.2, ["dense"]),
        ]
        monkeypatch.setattr(
            mcp_db_server,
            "_search_single_table",
            lambda *a, **k: [dict(c) for c in candidates],
        )
        reranker, captured = _capturing_reranker()
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: reranker)

        search(
            database="policy_db",
            schema="qpp_cm",
            query="q",
            top_k=2,
            tables=["t"],
        )

        survived = {p[1] for p in captured}
        assert len(captured) == 3
        # The single-leg row above the cut is kept (not dropped for its leg count).
        assert "single_mid" in survived
        # The single-leg row below the cut is dropped -- on its score, not its legs.
        assert "single_low" not in survived

    def test_global_cap_never_below_top_k(
        self, monkeypatch: pytest.MonkeyPatch, search_preamble: None
    ) -> None:
        """top_k above the env cap raises the effective cap to top_k."""
        monkeypatch.setenv("MCP_RERANK_GLOBAL_POOL", "3")
        candidates = [_cand(f"d{i}", 1.0 - 0.05 * i) for i in range(10)]
        monkeypatch.setattr(
            mcp_db_server,
            "_search_single_table",
            lambda *a, **k: [dict(c) for c in candidates],
        )
        reranker, captured = _capturing_reranker()
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: reranker)

        search(
            database="policy_db",
            schema="qpp_cm",
            query="q",
            top_k=8,
            tables=["t"],
        )

        # global_cap = max(top_k=8, cap=3) = 8, so 8 (not 3) survive to rerank.
        assert len(captured) == 8

    def test_default_cap_is_noop_for_small_pool(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        search_preamble: None,
    ) -> None:
        """With the default cap (50), a small pool is not trimmed and does not log."""
        monkeypatch.delenv("MCP_RERANK_GLOBAL_POOL", raising=False)
        monkeypatch.delenv("MCP_RERANK_POOL", raising=False)
        candidates = [_cand(f"d{i}", 1.0 - 0.1 * i) for i in range(4)]
        monkeypatch.setattr(
            mcp_db_server,
            "_search_single_table",
            lambda *a, **k: [dict(c) for c in candidates],
        )
        reranker, captured = _capturing_reranker()
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: reranker)

        with caplog.at_level(logging.INFO, logger="mcp_db_server.server"):
            search(
                database="policy_db",
                schema="qpp_cm",
                query="q",
                top_k=10,
                tables=["t"],
            )

        # All 4 candidates reached the reranker; no pre-cut occurred.
        assert len(captured) == 4
        assert not any(
            "Pre-cut merged candidate pool" in r.getMessage()
            for r in caplog.records
        )


class TestSearchPreCutTripwire:
    """The structural (table-shape) warning for a dense-only table in a pre-cut."""

    @staticmethod
    def _one_hybrid_candidate(
        engine: object, schema: str, table: str, *a: object, **k: object
    ) -> list[dict]:
        return [_cand(f"{schema}-1", 0.5)]

    def _run_multi_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mcp_db_server, "_discover_embedding_tables", lambda e, s: [f"{s}_emb"]
        )
        monkeypatch.setattr(
            mcp_db_server, "_search_single_table", self._one_hybrid_candidate
        )
        reranker, _ = _capturing_reranker()
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: reranker)

    @staticmethod
    def _dense_only_warnings(
        caplog: pytest.LogCaptureFixture,
    ) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "dense-only" in r.getMessage()
        ]

    def test_warns_when_multi_table_includes_dense_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        search_preamble: None,
    ) -> None:
        """A dense-only (no tsvector) table in a multi-table search warns, named."""
        self._run_multi_schema(monkeypatch)
        # usc_emb is dense-only; cms_iom_emb is hybrid.
        monkeypatch.setattr(
            mcp_db_server,
            "_get_tsvector_columns",
            lambda e, s, t: [] if t == "usc_emb" else ["chunk_tsv"],
        )

        with caplog.at_level(logging.WARNING, logger="mcp_db_server.server"):
            search(
                database="policy_db",
                schema=["cms_iom", "usc"],
                query="q",
                top_k=10,
            )

        warnings = self._dense_only_warnings(caplog)
        assert any("usc.usc_emb" in m for m in warnings)

    def test_no_warning_when_all_hybrid_multi_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        search_preamble: None,
    ) -> None:
        """All-hybrid multi-table search does not warn (search_preamble default)."""
        self._run_multi_schema(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="mcp_db_server.server"):
            search(
                database="policy_db",
                schema=["cms_iom", "usc"],
                query="q",
                top_k=10,
            )

        assert self._dense_only_warnings(caplog) == []

    def test_no_warning_for_single_table_even_if_dense_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        search_preamble: None,
    ) -> None:
        """A single-table search faces no cross-table cut, so no tripwire fires."""
        monkeypatch.setattr(
            mcp_db_server, "_get_tsvector_columns", lambda e, s, t: []
        )
        monkeypatch.setattr(
            mcp_db_server,
            "_search_single_table",
            lambda *a, **k: [_cand("d0", 0.5)],
        )
        reranker, _ = _capturing_reranker()
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: reranker)

        with caplog.at_level(logging.WARNING, logger="mcp_db_server.server"):
            search(
                database="policy_db",
                schema="qpp_cm",
                query="q",
                tables=["t"],
            )

        assert self._dense_only_warnings(caplog) == []

    def test_no_warning_when_a_schema_has_no_matches(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        search_preamble: None,
    ) -> None:
        """A hybrid schema returning zero candidates is normal, not a tripwire."""
        monkeypatch.setattr(
            mcp_db_server, "_discover_embedding_tables", lambda e, s: [f"{s}_emb"]
        )

        # usc contributes nothing this query; both tables are hybrid.
        def fake_single_table(
            engine: object, schema: str, table: str, *a: object, **k: object
        ) -> list[dict]:
            return [] if schema == "usc" else [_cand(f"{schema}-1", 0.5)]

        monkeypatch.setattr(
            mcp_db_server, "_search_single_table", fake_single_table
        )
        reranker, _ = _capturing_reranker()
        monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: reranker)

        with caplog.at_level(logging.WARNING, logger="mcp_db_server.server"):
            search(
                database="policy_db",
                schema=["cms_iom", "usc"],
                query="q",
                top_k=10,
            )

        assert self._dense_only_warnings(caplog) == []


# ---------------------------------------------------------------------------
# Tuning overrides: engine pool args
# ---------------------------------------------------------------------------


class TestEnginePoolArgs:
    """create_engine receives the env-configured pool sizing."""

    def test_uses_env_pool_sizes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create_engine receives the env-configured pool_size / max_overflow.

        The ef_search connect-event registers via ``event.listens_for`` against
        the returned engine; with create_engine faked to a MagicMock, that
        registration would raise on a non-Engine, so ``event.listens_for`` is
        stubbed to a no-op decorator. The per-DB engine cache is reset so
        creation actually runs.
        """
        monkeypatch.setenv("POSTGRES_HOST", "localhost")
        monkeypatch.setenv("POSTGRES_PORT", "5432")
        monkeypatch.setenv("POSTGRES_USER", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        monkeypatch.setenv("MCP_DB_POOL_SIZE", "7")
        monkeypatch.setenv("MCP_DB_MAX_OVERFLOW", "13")

        # Reset the module-global engine cache so creation is not short-circuited.
        monkeypatch.setattr(mcp_db_server, "_engines", {})

        captured_kwargs: dict[str, object] = {}

        def fake_create_engine(conn_str: object, **kwargs: object) -> MagicMock:
            captured_kwargs.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(
            mcp_db_server, "create_engine", fake_create_engine
        )
        # event.listens_for(engine, "connect") cannot resolve against a
        # MagicMock; replace it with a no-op decorator factory.
        fake_event = MagicMock()
        fake_event.listens_for.return_value = lambda fn: fn
        monkeypatch.setattr(mcp_db_server, "event", fake_event)

        mcp_db_server.get_database_engine("policy_db")

        assert captured_kwargs["pool_size"] == 7
        assert captured_kwargs["max_overflow"] == 13


class TestConnectVectorWarmup:
    """The per-connection ef_search/pgvector warm-up connect handler.

    On a database WITHOUT pgvector the warm-up cast ``select '[1]'::vector``
    raises and leaves the DBAPI transaction aborted. The handler must roll the
    connection back so it is usable in the pool (otherwise the first real query
    on it fails with "current transaction is aborted"). On the success path
    (pgvector present) it must NOT roll back.
    """

    @staticmethod
    def _capture_connect_handler(
        monkeypatch: pytest.MonkeyPatch, database: str
    ) -> Callable[[Any, Any], None]:
        """Build an engine and return the registered ``connect`` handler."""
        monkeypatch.setenv("POSTGRES_HOST", "localhost")
        monkeypatch.setenv("POSTGRES_PORT", "5432")
        monkeypatch.setenv("POSTGRES_USER", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        monkeypatch.setattr(mcp_db_server, "_engines", {})
        monkeypatch.setattr(
            mcp_db_server, "create_engine", lambda *a, **k: MagicMock()
        )

        captured: dict[str, Callable[[Any, Any], None]] = {}

        def fake_listens_for(_engine: object, _name: str) -> Callable:
            def decorator(fn: Callable[[Any, Any], None]) -> Callable:
                captured["handler"] = fn
                return fn

            return decorator

        fake_event = MagicMock()
        fake_event.listens_for.side_effect = fake_listens_for
        monkeypatch.setattr(mcp_db_server, "event", fake_event)

        mcp_db_server.get_database_engine(database)
        return captured["handler"]

    @staticmethod
    def _fake_conn(execute_side_effect: object) -> MagicMock:
        """A DBAPI connection whose cursor.execute uses the given side effect."""
        cur = MagicMock()
        cur.execute.side_effect = execute_side_effect
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        return conn

    def test_rolls_back_when_vector_cast_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-pgvector DB: the aborted transaction is rolled back, not raised."""
        handler = self._capture_connect_handler(monkeypatch, "metadata_db")
        # First execute (the vector cast) raises, as on a DB without pgvector.
        conn = self._fake_conn(Exception("type \"vector\" does not exist"))

        handler(conn, None)  # must swallow the error, not propagate

        conn.rollback.assert_called_once()

    def test_no_rollback_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pgvector present: warm-up succeeds, so the connection is not reset."""
        handler = self._capture_connect_handler(monkeypatch, "policy_db")
        conn = self._fake_conn(None)  # both executes succeed

        handler(conn, None)

        conn.rollback.assert_not_called()


class _ASGIRecorder:
    """Records whether the wrapped app was reached and the response status."""

    def __init__(self) -> None:
        self.reached = False

    async def app(self, scope: dict, receive: object, send: object) -> None:
        self.reached = True


async def _send_request(
    middleware: BearerAuthMiddleware,
    path: str,
    headers: list[tuple[bytes, bytes]],
) -> int:
    """Drive the middleware once and return the response status code.

    Returns 200 sentinel when the wrapped app is reached (auth passed).
    """
    status: dict[str, int] = {}

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers,
        "query_string": b"",
    }
    await middleware(scope, receive, send)
    return status.get("code", 200)


def _bearer(token: str) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {token}".encode())]


@pytest.fixture
def bearer_middleware() -> tuple[BearerAuthMiddleware, _ASGIRecorder]:
    """Build the bearer-auth middleware wrapping a reach-recording app.

    Returns:
        A ``(middleware, recorder)`` pair; ``recorder.reached`` reports whether
        the wrapped app was reached (auth passed).
    """
    recorder = _ASGIRecorder()
    middleware = BearerAuthMiddleware(recorder.app, mcp_path="/mcp")
    return middleware, recorder


class TestAuth:
    """Bearer-token auth: token parsing and the ASGI middleware paths."""

    def test_parse_auth_tokens_label_token_pairs(self) -> None:
        """MCP_AUTH_TOKENS parses into a token -> label mapping."""
        assert parse_auth_tokens("alice:tok1, bob:tok2") == {
            "tok1": "alice",
            "tok2": "bob",
        }
        assert parse_auth_tokens("") == {}
        assert parse_auth_tokens(None) == {}
        # Malformed pairs are dropped.
        assert parse_auth_tokens("nocolon,carol:tok3") == {"tok3": "carol"}

    @pytest.mark.parametrize(
        ("env", "headers", "expected_code", "expected_reached"),
        [
            # A valid bearer token reaches the wrapped app.
            ("alice:secret123", _bearer("secret123"), 200, True),
            # A missing Authorization header is rejected with 401.
            ("alice:secret123", [], 401, False),
            # An unknown bearer token is rejected with 401.
            ("alice:secret123", _bearer("wrong"), 401, False),
            # No configured tokens fails closed: every MCP request is rejected.
            (None, _bearer("anything"), 401, False),
        ],
        ids=["valid", "missing", "bad", "fail-closed-unset"],
    )
    def test_mcp_auth_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
        bearer_middleware: tuple[BearerAuthMiddleware, _ASGIRecorder],
        env: str | None,
        headers: list[tuple[bytes, bytes]],
        expected_code: int,
        expected_reached: bool,
    ) -> None:
        """The /mcp auth paths: accept valid, reject missing/bad, fail closed."""
        if env is None:
            monkeypatch.delenv("MCP_AUTH_TOKENS", raising=False)
        else:
            monkeypatch.setenv("MCP_AUTH_TOKENS", env)
        middleware, recorder = bearer_middleware

        code = asyncio.run(_send_request(middleware, "/mcp", headers))

        assert code == expected_code
        assert recorder.reached is expected_reached

    def test_leaves_health_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        bearer_middleware: tuple[BearerAuthMiddleware, _ASGIRecorder],
    ) -> None:
        """The /health endpoint is reachable without a token (varies the path)."""
        monkeypatch.delenv("MCP_AUTH_TOKENS", raising=False)
        middleware, recorder = bearer_middleware

        asyncio.run(_send_request(middleware, "/health", []))
        assert recorder.reached is True


class TestAuthDisableFlag:
    """MCP_DISABLE_AUTH parsing and its effect on create_app wiring."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, False),
            ("", False),
            ("false", False),
            ("0", False),
            ("no", False),
            ("true", True),
            ("TRUE", True),
            ("1", True),
            ("yes", True),
            ("on", True),
        ],
    )
    def test_auth_disabled_parsing(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
    ) -> None:
        """MCP_DISABLE_AUTH is truthy only for 1/true/yes/on (case-insensitive)."""
        if value is None:
            monkeypatch.delenv("MCP_DISABLE_AUTH", raising=False)
        else:
            monkeypatch.setenv("MCP_DISABLE_AUTH", value)
        assert mcp_db_server._auth_disabled() is expected

    def test_create_app_wraps_with_auth_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """By default create_app wraps the MCP app in BearerAuthMiddleware."""
        monkeypatch.delenv("MCP_DISABLE_AUTH", raising=False)
        fake_mcp = MagicMock()
        fake_mcp.streamable_http_app.return_value = object()
        monkeypatch.setattr(mcp_db_server, "create_mcp", lambda name: fake_mcp)

        app = mcp_db_server.create_app("test")
        assert isinstance(app, BearerAuthMiddleware)

    def test_create_app_skips_auth_when_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """MCP_DISABLE_AUTH returns the raw app (no middleware) and warns loudly."""
        monkeypatch.setenv("MCP_DISABLE_AUTH", "true")
        sentinel = object()
        fake_mcp = MagicMock()
        fake_mcp.streamable_http_app.return_value = sentinel
        monkeypatch.setattr(mcp_db_server, "create_mcp", lambda name: fake_mcp)

        with caplog.at_level(logging.WARNING, logger="mcp_db_server.server"):
            app = mcp_db_server.create_app("test")

        assert app is sentinel
        assert not isinstance(app, BearerAuthMiddleware)
        assert any(
            "NO authentication" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )


class TestMainEntryPoint:
    """main()'s instance resolution: the env file is required, and it anchors
    the instance name and the log directory."""

    def test_missing_env_file_exits_with_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Neither --env-file nor MCP_ENV_FILE aborts with argparse's own error.

        parser.error writes the usage block to stderr and exits 2, so the
        message is read from stderr rather than from the SystemExit value
        (which carries only the code).
        """
        monkeypatch.delenv("MCP_ENV_FILE", raising=False)
        monkeypatch.setattr("sys.argv", ["mcp-db-server"])

        with pytest.raises(SystemExit) as excinfo:
            mcp_db_server.main()

        assert excinfo.value.code == 2
        stderr = capsys.readouterr().err
        assert "usage:" in stderr
        assert "--env-file" in stderr

    def test_env_var_alone_satisfies_the_requirement(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """MCP_ENV_FILE with no flag starts normally.

        This is the case a `required=True` declaration would have broken:
        argparse checks `required` against the flag's presence on the command
        line and ignores any default, so a set MCP_ENV_FILE would still have
        been rejected.
        """
        instance_dir = tmp_path / "env_var_instance"
        instance_dir.mkdir()
        env_path = instance_dir / ".env"
        env_path.write_text("")

        for name in ("MCP_INSTANCE_NAME", "MCP_HOST", "MCP_PORT"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MCP_ENV_FILE", str(env_path))
        monkeypatch.setenv("MCP_WARM_MODELS", "false")
        monkeypatch.setattr("sys.argv", ["mcp-db-server"])

        monkeypatch.setattr(mcp_db_server, "setup_logging", lambda **kwargs: None)
        app_names: list[str] = []
        monkeypatch.setattr(
            mcp_db_server, "create_app", lambda name: app_names.append(name)
        )
        monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)

        mcp_db_server.main()

        assert app_names == ["env_var_instance"]

    def test_missing_env_path_exits_instead_of_serving(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An --env-file path that does not exist aborts with exit 1.

        Distinct from the flag-less abort above, which is argparse's exit 2:
        a flag never supplied and a path that does not exist are different
        mistakes and must not be diagnosed as the same one.

        load_dotenv is silent on a missing file, so without this guard a typo
        (or a relative path run from the wrong directory) would serve an
        instance with no credentials and no tokens.
        """
        monkeypatch.delenv("MCP_ENV_FILE", raising=False)
        missing = tmp_path / "policy_db" / ".env"
        monkeypatch.setattr(
            "sys.argv", ["mcp-db-server", "--env-file", str(missing)]
        )

        with pytest.raises(SystemExit) as excinfo:
            mcp_db_server.main()

        assert excinfo.value.code != 2
        assert "env file not found" in str(excinfo.value)

    def test_env_flag_anchors_instance_name_and_log_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--env-file drives the instance name (its parent dir) and the log dir.

        The env file is empty: it satisfies the required flag while loading
        nothing, so the defaults under test are the ones the code computes.
        """
        instance_dir = tmp_path / "some_instance"
        instance_dir.mkdir()
        env_path = instance_dir / ".env"
        env_path.write_text("")

        for name in ("MCP_ENV_FILE", "MCP_INSTANCE_NAME", "MCP_HOST", "MCP_PORT"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MCP_WARM_MODELS", "false")
        monkeypatch.setattr(
            "sys.argv", ["mcp-db-server", "--env-file", str(env_path)]
        )

        logging_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            mcp_db_server,
            "setup_logging",
            lambda **kwargs: logging_calls.append(kwargs),
        )
        app_names: list[str] = []
        monkeypatch.setattr(
            mcp_db_server, "create_app", lambda name: app_names.append(name)
        )
        serve_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            uvicorn,
            "run",
            lambda app, **kwargs: serve_calls.append(kwargs),
        )

        mcp_db_server.main()

        assert app_names == ["some_instance"]
        assert logging_calls == [
            {
                "log_dir": instance_dir / "logs" / "mcp_db_server",
                "log_name": "some_instance",
                "level": logging.INFO,
            }
        ]
        assert serve_calls == [{"host": "127.0.0.1", "port": 8000}]
