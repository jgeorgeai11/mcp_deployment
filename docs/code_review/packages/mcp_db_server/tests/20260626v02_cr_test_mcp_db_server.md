---
name: cr-test_mcp_db_server (v02)
goal: Second-pass review of the mcp_db_server test suite after commit 36e5926 added config-knob tests (statement-timeout override, non-int timeout fallback, rerank-pool override, create_engine pool-args, env-driven max-rows). Confirm v01 findings 1-7 stayed fixed, assess the NEW tests for assertion quality / mocking faithfulness / isolation / coverage gaps.
created: 2026-06-26
updated: 2026-06-26 (suite 46 -> 50 passed)
---

## Scope

Reviewed file (test quality only, not the production module — its own CR covers the
module):
- `code/mcp_db_server/unit_tests/test_mcp_db_server.py` (50 tests)

`conftest.py` was looked at (pure path setup, no findings) and — matching the repo
convention — gets no separate CR. Prior review:
`20260626v01_cr_test_mcp_db_server.md` (findings 1-7 fixed, finding 8 pending).
Suite is green: `uv run pytest code/mcp_db_server/unit_tests/ -q` -> 50 passed.

This pass goes adversarial on the four NEW knob tests and the converted max-rows
test; it does not re-raise v01 findings 1-7 (confirmed fixed below).

## Implementation Plan

The four new override tests are strong and assert the RIGHT effect (not just "code
runs"); see Strengths. Only two genuine (suggestion-level) residuals remain on the
new surface, both coverage notes the task explicitly asked about.

1. [completed] `_env_int` non-integer WARNING log is exercised but never asserted - `test_mcp_db_server.py:495-511`
   - Resolution: `TestRunSql.test_statement_timeout_non_int_falls_back_and_warns` now wraps the call in `caplog.at_level(logging.WARNING, logger="mcp_db_server")` and asserts the `"Ignoring non-integer" / MCP_STATEMENT_TIMEOUT_S` record fired (alongside the existing fallback-value assertion). A dedicated `TestEnvInt` class also asserts the non-int WARNING directly and adds the NEW clamp coverage from M1/M2: `MCP_STATEMENT_TIMEOUT_S=0` -> `'1s'` + a "below the minimum" WARNING (via `run_sql`), and `MCP_DB_POOL_SIZE=0` -> `1` directly through `_env_int(..., minimum=1)`.
   - 1.1. `test_run_sql_statement_timeout_non_int_falls_back` sets `MCP_STATEMENT_TIMEOUT_S="not-a-number"` and asserts the emitted SQL falls back to `'5s'` (`_DEFAULT_STATEMENT_TIMEOUT_S`). That correctly proves fallback-to-**default** (not merely no-crash) — the task's Q3 is satisfied. The residual: `_env_int` (prod `mcp_db_server.py:157-160`) also emits `logger.warning("Ignoring non-integer ...")` on the parse failure, and no test `caplog`-asserts that warning fires. The WARNING branch is executed but its observable side effect (the operator-facing log) is unverified, so a future change that silently dropped the warning would not be caught.
        - Current: asserts only the fallback value in `conn.executed_sql[0]`.
        - Expected (suggestion): wrap the `run_sql` call in `caplog.at_level(logging.WARNING, logger="mcp_db_server")` and assert a record matching `"Ignoring non-integer"` / `"MCP_STATEMENT_TIMEOUT_S"` fired — mirroring the existing `test_search_warns_on_empty_chunk_text` pattern.
        - Rationale: assertion quality / data-validation — a warning meant to make a misconfiguration loud should itself be locked.

2. [completed] `MCP_LOG_LEVEL` fallback in `main()` is untested - `test_mcp_db_server.py` (no test) / prod `mcp_db_server.py:1582-1586`
   - Resolution: the level resolution was extracted from `main()` into `_resolve_log_level() -> int` (which `main()` now calls), making it unit-testable. `TestResolveLogLevel` asserts a known name (`debug` -> `logging.DEBUG`), an unknown name (`not-a-level` -> `logging.INFO`), and unset (-> INFO).
   - 2.1. `main()` resolves the logging level via `os.environ.get("MCP_LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper()` and `getLevelName(...)` (with a str-return fallback path for an unknown level name). No test exercises this. This is acceptable — `main()` wires uvicorn + load_dotenv and is integration-level, not cleanly unit-testable — but it should be acknowledged rather than silently uncovered.
        - Current: no coverage of the log-level resolution.
        - Expected (suggestion / acknowledge-only): either a small unit test that monkeypatches `MCP_LOG_LEVEL` to a bogus value and asserts the default level is used, or an explicit "by-nature hard to unit-test" note. No behaviour gap if left as the latter.
        - Rationale: coverage adequacy — completeness of the env-knob surface the task asked about.

3. [completed] (carried from v01.8) Structure diverges from sibling suites; auth/search preamble duplication invites parametrization - `test_mcp_db_server.py` (whole file)
   - Resolution (see v01 finding 8 for the full note): tests are grouped into `class Test...` blocks; a `bearer_middleware` fixture and a matched-dim `search_preamble` fixture remove the duplicated arrange blocks; the four `/mcp` auth cases are parametrized over `(env, headers, expected_code, expected_reached)` with `test_leaves_health_open` kept separate (it varies the path, not the tuple). The two dimension-guard tests deliberately do NOT use `search_preamble` (they vary the dim, which is their point). All assertion intent preserved; suite 50 -> 57 passed.
   - 3.1. Sibling suites group tests in `class Test...:` blocks; this suite uses flat module-level functions with comment-banner dividers. Both are valid pytest; the divergence is a convention inconsistency.
   - 3.2. Duplication: the five auth tests repeat the same `setenv`/`delenv` + `_ASGIRecorder` + `BearerAuthMiddleware(...)` arrange block; the search tests (dimension-guard, reranker, empty-chunk, auto-discover, rerank-pool) repeat the same ~five-line monkeypatch preamble (engine / model / `_get_vector_columns` / `_get_vector_dimension`). A shared auth fixture, a `search`-preamble fixture, and a parametrize over the auth cases would tighten this. Lower-priority polish, not a behaviour gap.

## Skills with No Issues / Genuine Strengths

The NEW tests (commit 36e5926):

1. **Statement-timeout override test discriminates correctly** (`test_run_sql_statement_timeout_env_override`, lines 477-492). Sets `MCP_STATEMENT_TIMEOUT_S="12"` and asserts the literal emitted `set local statement_timeout = '12s'` SQL — distinct from the default `'5s'`, so it fails if the override is ignored. Asserts the right effect, not "code ran".
2. **Non-int timeout fallback proves fallback-to-default, not just no-crash** (`test_run_sql_statement_timeout_non_int_falls_back`, lines 495-511). Asserts the SQL falls back to `f"'{_DEFAULT_STATEMENT_TIMEOUT_S}s'"` (= `'5s'`) — referencing the production constant rather than a hard-coded literal, so it stays correct if the default changes. (Residual: the WARNING log itself is unasserted — finding 1.)
3. **Rerank-pool override asserts the pool that actually reaches `_search_single_table`** (`test_search_rerank_pool_env_override_changes_pool_size`, lines 1012-1062). A `spy_single_table` captures the per-table `pool_size`; with `top_k=5` and `MCP_RERANK_POOL=37` it asserts `captured_pool == [37]` (= `max(top_k, pool)`). `37` is distinct from both `top_k=5` and the default `50`, so the test discriminates the override from both the floor and the default. Verifies the real `pool_size = max(top_k, _rerank_pool())` contract (prod line 1282), not a smoke run.
4. **`create_engine` pool-args test — faithful-with-necessary-stubs, clean isolation** (`test_get_database_engine_uses_env_pool_sizes`, lines 1065-1101).
   - It verifies the env pool sizes reach `create_engine`: `fake_create_engine` captures `**kwargs` and the test asserts `pool_size == 7` / `max_overflow == 13` from `MCP_DB_POOL_SIZE=7` / `MCP_DB_MAX_OVERFLOW=13`. This is the right effect (prod lines 290-295).
   - Isolation is correct: `monkeypatch.setattr(mcp_db_server, "_engines", {})` reverts to the original module dict at teardown; the MagicMock engine lands in the discarded fresh dict (not the real one); and no other test reaches real engine-creation (every other test stubs `get_database_engine` / `_get_bootstrap_engine`), so there is no cross-test leak from this reset.
   - Stubbing `event` to a no-op decorator factory is **necessary, not brittle**: `@event.listens_for(engine, "connect")` cannot register against a MagicMock engine, so the stub is required to exercise the create_engine call at all. The test docstring acknowledges this. The trade-off — the real `_set_hnsw_ef_search` connect-event (warm-up vector cast + `set hnsw.ef_search` + extension-absent tolerance, prod lines 305-332) is bypassed — is correctly an integration-level path, not a hidden unit gap. The test scopes itself to the two new knobs and does not assert `pool_pre_ping`; that is fine (it is not the knob under test).
5. **Max-rows test converted to the real env knob** (`test_run_sql_flags_truncation_at_max_rows`, lines 457-474). Now uses `monkeypatch.setenv("MCP_MAX_ROWS", "3")` (instead of patching `_MAX_ROWS` directly), so it exercises the real `_env_int("MCP_MAX_ROWS", ...)` resolution (prod line 1006) against 5 available rows, asserting `row_count == 3` and `truncated is True`. `3` vs the default `500` discriminates the override; the `fetchmany(max_rows)`-then-`fetchone()` truncation probe is faithfully modelled by `_FakeResult`.

(Pre-existing strengths from v01 — reranker reordering test, privilege-filter SQL assertions, RRF pure tests, the five auth paths, identifier-validation parametrization, model-name resolution — remain valid and are not repeated here.)

### By-nature hard to unit-test (explicitly not counted as misses)

- **The `ef_search` connect-event** (`_set_hnsw_ef_search`): a per-connection warm-up cast + `SET` on a real pgvector connection. Not unit-testable with the SQLAlchemy fakes (no real DBAPI connection, no `connect` event on a MagicMock). Correctly acknowledged via the create_engine test's docstring and left to integration.
- **`MCP_LOG_LEVEL` resolution in `main()`** (finding 2): integration/uvicorn wiring; acceptable to leave, but should be acknowledged.
- **`threading.Lock` double-checked-locking singletons** (`_engines_lock` etc.): concurrency correctness not reliably observable single-threaded. Unchanged from v01.

## v01 findings 1-7 confirmed fixed; finding 8 still pending

Spot-checked each reworked test against the current source:

1. **Served-set fresh-query regression** — `test_tool_drives_real_served_databases_fresh_query` (lines 240-294) now drives the real served-set path TWICE through one fake bootstrap engine yielding `{policy_db}` then `{policy_db, other_db}`, validates `other_db` on the 2nd call, and asserts `bootstrap_engine.connect.call_count == 2`. A reintroduced process-lifetime cache would connect once and reject `other_db`. **Confirmed fixed.**
2. **`describe_table` happy-path + no-columns** — `test_describe_table_maps_columns_and_nullable` (lines 326-368, asserts `nullable is True/False` coercion + the `udt_name`/`USER-DEFINED` CASE in the issued SQL) and `test_describe_table_raises_when_no_columns` (lines 371-386). **Confirmed fixed.**
3. **Empty-`chunk_text` warning** — `test_search_warns_on_empty_chunk_text` (lines 868-940) asserts the `caplog` WARNING "1 of 2 rerank candidates", the `[query, ""]` pair reaching the reranker, and the low rank. **Confirmed fixed.**
4. **Per-leg `executed_sql` assertions** — `test_search_single_table_hybrid_issues_both_legs` (lines 569-621) asserts dense `<=>` + `min_similarity` (no `websearch_to_tsquery`), sparse `websearch_to_tsquery` + `ts_rank_cd`, one statement per connection, AND bound params via `executed_params` (`min_similarity == 0.3`, `query == "beta"`). The `_CapturingConnection.execute` now records `executed_params` (lines 88-91). **Confirmed fixed.**
5. **Dead reranker stub removed** — `test_search_dimension_guard_skips_empty_table` (lines 739-772) no longer stubs `get_reranker`; the explanatory comment "No get_reranker stub: search early-returns on empty candidates" (lines 762-763) documents why. **Confirmed fixed.**
6. **`run_sql` semicolon boundary** — parametrized `test_run_sql_semicolon_boundary` (lines 394-422) accepts `"select 1;"` and `"select 1"`, rejects `"select 1; select 2"`. **Confirmed fixed.**
7. **Auto-discovery + no-PK fusion** — `test_search_auto_discovers_tables_when_none` (lines 943-1004, spy proves both discovered tables searched) and `test_search_single_table_no_pk_fuses_via_display_row` (lines 662-695, `pk_cols=[]` fuses via the full-display-row key). **Confirmed fixed.**

8. **Structure / parametrize polish** — still `[pending]` (carried here as finding 3). Convention alignment only; not a behaviour gap.

## Status & Next Steps

**Current Status**: All three findings fixed. The `_env_int` WARNING is now `caplog`-asserted and the new clamp behaviour is covered (finding 1); `MCP_LOG_LEVEL` resolution was extracted to `_resolve_log_level()` and unit-tested (finding 2); the suite was restructured into `class Test...` blocks with shared `bearer_middleware`/`search_preamble` fixtures and a parametrized auth path (finding 3). `uv run pytest code/mcp_db_server/unit_tests/ -q` -> 57 passed (up from 50).

**Completed**:
1. Read the test file, v01 review, and the relevant production paths (`_env_int`, `get_database_engine`, `_rerank_pool`, run_sql timeout/max-rows) in full.
2. Ran the suite read-only: 50 passed.
3. Audited the four new knob tests for assertion quality, mocking faithfulness, and isolation; spot-checked v01 findings 1-7 against current source.

**Next Steps**:
1. (suggestion) Add a `caplog` WARNING assertion to `test_run_sql_statement_timeout_non_int_falls_back` (finding 1).
2. (suggestion) Add or explicitly acknowledge `MCP_LOG_LEVEL` coverage (finding 2).
3. (pending polish) Finding 3: `class Test...` grouping + auth/search fixtures + auth parametrize.

**Blockers**:
1. None.

**Notes**:
1. Severity tags use the repo `[critical]/[major]/[minor]/[suggestion]` convention.
2. The create_engine test's `event` stub and `_engines` reset were specifically checked for over-mocking and cross-test leakage; both are justified and isolated (see strength 4). Not raised as findings.
