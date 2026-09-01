---
name: cr-test_mcp_db_server
goal: Assess the quality (coverage adequacy, assertion strength, mocking faithfulness, structure/conventions, correctness) of the mcp_db_server test suite `test_mcp_db_server.py` — the dedicated per-file review (one CR per code file, paired with 20260626v01_cr_mcp_db_server.md for the module).
created: 2026-06-26
updated: 2026-06-26 (findings 1-7 fixed; suite 39 -> 46 passed)
---

## Scope

Reviewed file (test quality only, not the production module):
- `code/mcp_db_server/unit_tests/test_mcp_db_server.py` (39 tests)

`conftest.py` was looked at (pure path setup, no findings) and — matching the repo
convention (no module has a `cr_conftest`) — gets no separate CR. Production module
under test: `code/mcp_db_server/mcp_db_server.py` (its own CR:
`20260626v01_cr_mcp_db_server.md`). Sibling suites (`file_ingestion`,
`excel_ingestion`, `embedding_generation`) skimmed for convention reference only.
Suite is green: `uv run pytest code/mcp_db_server/unit_tests/ -q` -> 39 passed.

This review goes deep on tests that pass for the wrong reason and on genuinely-missing
branches (the module's own CR no longer carries a tests section — test findings live
here, one CR per code file).

## Implementation Plan

1. [completed] Regression-lock test does not lock the regression it names - `test_mcp_db_server.py:235-263`
   - 1.1. `test_tool_drives_real_served_databases_fresh_query` claims (docstring lines 238-244) to confirm "the served set is resolved by a fresh catalog query (no process-lifetime cache)". This is the test that replaced the v01 finding-1.3 fix (removal of the process-lifetime `_served_databases` cache). But it calls `list_schemas(database="policy_db")` exactly **once**. A reintroduced process-lifetime cache populated on the first call is indistinguishable from a fresh query on a single call -- so if a future change re-added the cache, this test would still pass. It therefore does not catch the regression it is named for (dimension 5: passes for the wrong reason).
        - Current: one `list_schemas` call, one assertion on the returned shape; `bootstrap_conn`/`schema_conn` are each single-use `_CapturingConnection`s whose single canned result is consumed on the first call, so a second served-set resolution would exhaust the fakes (the emptied `_CapturingConnection._results` raises `IndexError` on the next `pop(0)`) rather than surface a stale cache.
        - Expected: drive the served-set path at least twice with the underlying grant set changing between calls (e.g. a `_fetch_databases` / `_get_bootstrap_engine` stub whose result differs on the 2nd call), and assert the second call observes the new set (a cache would still return the first). Equivalently, assert the bootstrap engine's `.connect()` is invoked once per served-set resolution (count > 1 across two tool calls), proving no caching.
        - Rationale: correctness / unit-tests skill -- a regression test must fail when the regression returns; this one cannot.
   - 1.2. Note on the task's named "nested-asyncio / event-loop regression": this test is the renamed descendant of the old `..._no_nested_asyncio` case. After v01's removal of the `_served_databases` cache, the served-set path (`_validate_database` -> `get_served_databases` -> `_fetch_databases`) is pure synchronous code with no `asyncio.run` anywhere, so resolving the served set from inside a running tool can no longer raise "asyncio.run() cannot be called from a running event loop" -- the regression is now structurally impossible, not merely untested. The test still usefully proves the real (un-stubbed) served-set path executes end-to-end; it just no longer needs to (and cannot meaningfully) assert the absence of nested asyncio.

2. [completed] `describe_table` has no happy-path test (whole tool's real logic uncovered) - `test_mcp_db_server.py:284-292`
   - 2.1. The only `describe_table` test is `test_describe_table_rejects_invalid_table` (invalid-identifier rejection). Untested: the issued SQL, the `USER-DEFINED -> udt_name` CASE (prod lines 763-766), the return shape (`column_name`/`data_type`/`nullable` with the `row[2] == "YES"` boolean coercion, prod lines 783-790), and the empty-rows `ValueError("... not found or has no columns")` branch (prod lines 778-781). `describe_table` is one of the six tools and its mapping logic is entirely unexercised.
        - Current: rejection-only coverage.
        - Expected: add a happy-path test (mirroring `test_list_tables_filters_by_select_privilege`): a `_CapturingConnection` returning rows including a `USER-DEFINED`/`udt_name` case and a `YES`/`NO` nullable, asserting the returned dicts (including `nullable is True/False`); plus a test that an empty result raises the "not found or has no columns" `ValueError`.
        - Rationale: coverage adequacy -- the task asks "is every tool covered, and every meaningful branch"; the `udt_name` CASE and the no-columns guard are meaningful branches with zero coverage.

3. [completed] New empty-`chunk_text` warning branch in `search` is untested - `test_mcp_db_server.py` (no test) / prod `mcp_db_server.py:1147-1160`
   - 3.1. v01 finding 2.1 added the empty/missing-`chunk_text` detection: `search` counts candidates whose `_RERANK_TEXT_COLUMN` is empty and emits `logger.warning(...)`, while still coercing to `""` for the reranker pair. The task explicitly flags this branch. No test feeds a candidate lacking `chunk_text`; the warning path, the count, and the `str(text_value or "")` coercion are all uncovered. This is genuinely missing, not by-nature hard to test.
        - Current: every search test supplies candidates that include a non-empty `chunk_text` (e.g. lines 577-591), so `empty_text_count` is always 0 and the warning never fires.
        - Expected: a test whose `_search_single_table` stub returns at least one candidate dict with no `chunk_text` key (or an empty one); assert via `caplog` that the warning fires with the count, and that the reranker still receives a `[query, ""]` pair for that candidate (no crash, low rank).
        - Rationale: coverage adequacy + data-validation -- the whole point of finding 2.1 was to make a silent quality regression loud; an untested warning can rot silently.

4. [completed] `_search_single_table` "issues both legs" never asserts the leg SQL it claims - `test_mcp_db_server.py:416-479`
   - 4.1. `test_search_single_table_hybrid_issues_both_legs` and `..._dense_only_without_tsvector` assert only on the fused output (`matched_legs`, `fused_score`, `source_table`). Neither inspects `executed_sql`. The fakes return canned rows regardless of the SQL, so these tests would still pass if the dense leg dropped its `min_similarity`/`<=>` clause or the sparse leg used the wrong `websearch_to_tsquery`/`ts_rank_cd` SQL -- the "both legs issued" claim is inferred from `matched_legs` (which is driven by the test's own canned overlap), not from the actual SQL having run. Contrast the `list_*` tests, which correctly assert the real SQL text (`'CONNECT'`/`'USAGE'`/`'SELECT'`); that contrast is the assertion-quality gap.
        - Current: no `executed_sql` assertions in either `_search_single_table` test; the test name "issues both legs" is verified indirectly.
        - Expected: assert `len(conn.executed_sql) == 1` per connection and that the dense SQL contains the `<=>` cosine operator + `min_similarity` predicate, and (hybrid case) that the sparse SQL contains `websearch_to_tsquery` / `ts_rank_cd`; assert the dense-only case issues exactly one (dense) statement and no `websearch_to_tsquery`.
        - Rationale: assertion quality -- the test should fail if the leg SQL is wrong, which it currently does not.
   - 4.2. Related fake limitation: `_CapturingConnection.execute` (line 84) records only `str(statement)` and **discards `params`**. So even tests that do assert SQL cannot verify bound values (`:min_similarity`, `:leg_depth`, `:query`, `:schema`, `:table`). Acceptable for identifier-text assertions but means parameter binding is never verified anywhere in the suite.
        - Expected (suggestion-level): have `execute` also append `(str(statement), params)` (or a second `executed_params` list) so the truncation/timeout and leg tests can assert bound values when relevant.

5. [completed] Dead monkeypatch misrepresents what is covered - `test_mcp_db_server.py:545`
   - 5.1. `test_search_dimension_guard_skips_empty_table` stubs `_search_single_table` to return `[]` (lines 540-544), so `search` hits the empty-candidates early return (prod line 1136) and returns `[]` **before** `get_reranker` is ever called. The `monkeypatch.setattr(mcp_db_server, "get_reranker", lambda: MagicMock())` on line 545 is therefore unreachable dead setup -- it implies the rerank path is exercised here when it is not.
        - Current: an unused reranker stub.
        - Expected: delete line 545 (the test is about the dimension guard not tripping on `None`, which it correctly proves); reranker coverage already lives in `test_search_reranker_reorders_rrf_candidates`.
        - Rationale: correctness/clarity -- dead test setup misleads the next reader about coverage.

6. [completed] `run_sql` single-trailing-semicolon acceptance branch untested - `test_mcp_db_server.py:300-358`
   - 6.1. The multi-statement guard is `stripped = sql.strip().rstrip(";"); if ";" in stripped: raise` (prod lines 841-846). The rejection branch is tested (`SELECT 1; SELECT 2`), and clean single statements are tested, but the documented-acceptable case -- a single statement with **one trailing** `;` (e.g. `"SELECT 1;"`) -- is not. That trailing-`;`-stripping is the subtle half of the guard and is the boundary a future refactor is most likely to break.
        - Expected: a parametrized case asserting `"SELECT 1;"` (single trailing semicolon) is accepted while `"SELECT 1; SELECT 2"` is rejected.
        - Rationale: coverage adequacy -- the accepted-boundary of a safety guard deserves an explicit lock.

7. [completed] `search` auto-discovery path (`tables=None`) untested - `test_mcp_db_server.py:494-647`
   - 7.1. Every successful `search` test passes `tables=[...]` explicitly. The `tables=None` branch that calls `_discover_embedding_tables` and then proceeds to search the discovered list (prod lines 1071-1072) is exercised only on its failure side (`test_search_no_embedding_tables_raises`, which stubs discovery to `[]`). The success path -- discovery returns a non-empty list that then flows into the dimension guard + candidate generation -- has no test.
        - Expected: a test with `_discover_embedding_tables` stubbed to a non-empty list and `tables=None`, asserting the discovered tables are searched (e.g. via the captured rerank pairs or a `_search_single_table` spy).
        - Rationale: coverage adequacy -- the default (auto-discover) mode of `search` is the common real-world path and is currently only smoke-tested on its empty case.
   - 7.2. The no-primary-key fusion-key fallback in `_search_single_table` (prod lines 943-945, the full-display-row key used when a table has no PK) is untested: both `_search_single_table` tests patch `_get_primary_key_columns` to `["doc_id"]` (lines 427, 461), so only the PK-key branch ever runs. Add a case with `pk_cols=[]` asserting rows still fuse correctly via the display-row key.

8. [completed] Structure diverges from sibling suites; duplication invites parametrization - `test_mcp_db_server.py` (whole file)
   - Resolution: the suite is now grouped into `class Test...` blocks matching the sibling convention (`TestModelNameResolution`, `TestEnvInt`, `TestResolveLogLevel`, `TestValidateSqlIdentifier`, `TestListTools`, `TestDescribeTable`, `TestRunSql`, `TestReciprocalRankFusion`, `TestSearchSingleTable`, `TestSearch`, `TestEnginePoolArgs`, `TestAuth`). A `bearer_middleware` fixture replaces the repeated `_ASGIRecorder` + `BearerAuthMiddleware(...)` arrange block, and a matched-dim `search_preamble` fixture replaces the repeated engine/model/`_get_vector_columns`/`_get_vector_dimension` preamble (the reranker, empty-chunk, auto-discover, and rerank-pool tests consume it). The four `/mcp` auth cases are parametrized over `(env, headers, expected_code, expected_reached)` (ids: valid/missing/bad/fail-closed-unset); `test_leaves_health_open` stays a separate method because it varies the path, not the tuple. The two dimension-guard tests intentionally keep their own distinct dims (1024-vs-384 mismatch; `None` empty table) rather than the shared 384/384 fixture, since varying that dim is the point. No coverage lost; suite 46 (v01) -> 50 (v02) -> 57.
   - 8.1. The sibling suites group tests in `class TestX:` blocks (e.g. `file_ingestion/unit_tests/test_utils.py:95 class TestValidateSqlIdentifier`, `test_file_parser.py:37 class TestParseFilesDocling`); this suite uses flat module-level functions with comment-banner section dividers. Both are valid pytest, but the divergence is a convention inconsistency worth a note for alignment.
   - 8.2. Duplication: the four auth tests (lines 708-748) repeat the same arrange block (`setenv`/`delenv` + `_ASGIRecorder` + `BearerAuthMiddleware(...)`) and differ only in env/headers/expected code -- a parametrize or a small fixture would tighten them. The dimension-guard tests (lines 494-554) and the reranker test repeat the same five-line monkeypatch preamble (engine/model/`_get_vector_columns`/`_get_vector_dimension`); a `search` setup fixture would remove the repetition and make the per-test intent (the one line that varies) obvious.
        - Expected: optionally group into `class Test...` blocks to match siblings; extract a shared auth-middleware fixture and a `search`-preamble fixture; parametrize the four auth cases over `(env, headers, expected_code, expected_reached)`.
        - Rationale: structure/conventions + duplication -- lower-priority polish, not a behaviour gap.

## Skills with No Issues / Genuine Strengths

1. **Reranker reordering test is the strongest in the suite** (`test_search_reranker_reorders_rrf_candidates`, lines 557-628). It does not merely run -- it captures the `(query, chunk_text)` pairs handed to the cross-encoder, inverts the RRF order via a fake `predict`, and then asserts the **output order flips to `[B, A]`** with the right `rerank_score`s, and that `source_table`/`matched_legs` survive reranking. This proves rerank score overrides fused score (the actual contract), asserts the right things, and is order-independent (fresh dict copies). No issues.
2. **Privilege-filter SQL assertions check the right thing, not smoke** (`list_databases`/`list_schemas`/`list_tables`, lines 169-232). Each asserts the literal privilege SQL actually issued -- `has_database_privilege(current_user` + `'CONNECT'` + `datistemplate = false`; `has_schema_privilege` + `'USAGE'`; `has_table_privilege` + `'SELECT'` -- so the privilege-filter intent is genuinely verified via `_CapturingConnection.executed_sql`, plus the return shape. No issues.
3. **RRF pure tests are correct and meaningful** (lines 366-385). `..._in_both_legs_outranks_single_leg_rows` asserts the both-legs row ranks first AND records `[0, 1]`; the empty-legs test covers the degenerate `[[], []] -> []` and partial-empty cases. They exercise real fusion math, not mocks. No issues.
4. **All five auth paths covered with correct assertions** (lines 708-758): valid -> app reached; missing/bad/fail-closed-unset -> 401 and app NOT reached; `/health` open without a token. The `_ASGIRecorder` + `_send_request` harness drives the real ASGI middleware (genuinely async, correctly kept under `asyncio.run`) and asserts both the status code and whether the wrapped app was reached -- the right two things. `parse_auth_tokens` covers label:token pairs, empty/None, and malformed-pair dropping. No issues.
5. **Identifier validation parametrized over valid/invalid sets** (lines 136-152), matching the canonical sibling pattern in `file_ingestion/unit_tests/test_utils.py`. No issues.
6. **Model-name resolution (default + env override)** for both embedding and rerank models (lines 101-128) correctly uses `monkeypatch.delenv`/`setenv` and asserts the corrected granite default + the documented bge fallback. No issues.
7. **`run_sql` timeout + truncation guards** (lines 318-358) assert the real `set local statement_timeout = '5s'` SQL fired, the `{rows,row_count,truncated}` shape, and -- via the `_MAX_ROWS` monkeypatch to 3 against 5 rows -- the truncation flag. The `_FakeResult.fetchmany`-then-`fetchone` truncation probe is faithfully modelled. No issues (beyond the trailing-`;` gap in finding 6).

### By-nature hard to unit-test (explicitly not counted as misses)

- **The `threading.Lock` double-checked-locking singleton guards** (`_engines_lock`, `_embedding_model_lock`, `_reranker_lock`): correctness under concurrency is not reliably observable in a single-threaded mocked unit test (it requires deterministic interleaving of concurrent first-calls). Acceptable to leave to design review / documentation rather than a unit test.
- **Event-loop-free behaviour of sync tools in FastMCP's worker threadpool** (v01 finding 1.1): that the plain-`def` tools do not block the loop is a runtime/integration property of FastMCP's anyio threadpool, not something a mocked unit test can assert. Correctly out of scope here.

## Status & Next Steps

**Current Status**: All findings (1-8) fixed and the suite is green (57 passed, fully mocked -- no live DB/model/server). Finding 8 (structural polish: `class Test...` grouping and parametrize/fixture deduplication) was completed in the v02 fix pass alongside the v02 module/test findings. `conftest.py` was not touched.

**Completed**:
1. Read both test files, the production module, and v01 in full; confirmed suite green.
2. Verified each finding against source (line-checked) before fixing.
3. Finding 1: reworked `test_tool_drives_real_served_databases_fresh_query` to drive the real served-set path TWICE through a single fake bootstrap engine whose `.connect()` yields `{policy_db}` on the first resolution and `{policy_db, other_db}` on the second; the second call validates `other_db` (served only on resolution 2), so it passes only with a fresh query and a reintroduced process-lifetime cache would reject it. Added a structural lock: `bootstrap_engine.connect.call_count == 2`. `get_database_engine` now yields a fresh connection per call so the second call is not starved by an exhausted single-use fake.
4. Finding 2: added `test_describe_table_maps_columns_and_nullable` (asserts the `row[2] == "YES"` -> True/False coercion via YES/NO rows, the dict shape with the udt-resolved `data_type`, and the `udt_name`/`USER-DEFINED` CASE in the issued SQL) and `test_describe_table_raises_when_no_columns`.
5. Finding 3: added `test_search_warns_on_empty_chunk_text` -- a candidate missing `chunk_text` triggers the `logger.warning` (asserted via `caplog.at_level(..., logger="mcp_db_server")` for "1 of 2 rerank candidates"), the reranker still receives a `[query, ""]` pair, and that candidate ranks low.
6. Finding 4: added per-leg `executed_sql` assertions (dense: `<=>` + `min_similarity`, no `websearch_to_tsquery`; sparse: `websearch_to_tsquery` + `ts_rank_cd`; dense-only: exactly one statement, no `websearch_to_tsquery`). Finding 4.2: extended `_CapturingConnection.execute` with an additive `executed_params` list of `(str(statement), params)` and asserted bound values (`min_similarity`, `query`).
7. Finding 5: deleted the dead `get_reranker` stub in `test_search_dimension_guard_skips_empty_table`.
8. Finding 6: replaced the single rejection test with a parametrized `test_run_sql_semicolon_boundary` accepting `"select 1;"`/`"select 1"` and rejecting `"select 1; select 2"`.
9. Finding 7.1: added `test_search_auto_discovers_tables_when_none` (a `_search_single_table` spy proves both discovered tables are searched). Finding 7.2: added `test_search_single_table_no_pk_fuses_via_display_row` (`pk_cols=[]`, identical display tuple fuses across legs via the fallback key).
10. Validated: `uv run pytest code/mcp_db_server/unit_tests/ -q` -> 46 passed.

**Next Steps**:
1. None -- all findings (1-8) resolved.

**Blockers**:
1. None.

**Notes**:
1. Severity tags use the repo `[critical]/[major]/[minor]/[suggestion]` convention, matching `20260626v01_cr_mcp_db_server.md`.
2. v01 already addressed (and resolved) the `_FakeResult.fetchmany` docstring note and the conversion of tool-call tests away from `asyncio.run`; those are not re-raised here.
3. The `threading.Lock` guards and the event-loop-free-thread behaviour are classified as by-nature-hard-to-unit-test (above), separate from the genuinely-missing items 2 and 3, per the task's request to distinguish them.
