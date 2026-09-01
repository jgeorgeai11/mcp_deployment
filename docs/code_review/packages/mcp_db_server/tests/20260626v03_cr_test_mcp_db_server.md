---
name: cr-test_mcp_db_server (v03)
goal: Third-pass review of the mcp_db_server test suite after commit 8bfb70e reshaped TestDescribeTable -> TestDescribeTables for the plural `describe_tables` tool (`{table: [cols]}` return, default-all discovery, composite PK/FK splitting) and added a search "not searchable" test. Confirm v01 findings 1-8 and v02's two suggestions stayed fixed; go adversarial on the NEW describe_tables / search tests for assertion quality, fake faithfulness, and coverage gaps.
created: 2026-06-26
updated: 2026-06-26
---

## Scope

Reviewed file (test quality only, not the production module — its own CR covers the
module):
- `code/mcp_db_server/unit_tests/test_mcp_db_server.py` (61 tests)

`conftest.py` was looked at (pure path setup, no findings) and — matching the repo
convention (no module has a `cr_conftest`) — gets no separate CR. Prior reviews:
`20260626v01_cr_test_mcp_db_server.md` (findings 1-8 fixed) and
`20260626v02_cr_test_mcp_db_server.md` (3 findings fixed, 2 of them carrying v01's
suggestions). Production module under test: `code/mcp_db_server/mcp_db_server.py`.
Suite is green: `uv run pytest code/mcp_db_server/unit_tests/ -q` -> 61 passed.

This pass goes adversarial on the NEW surface (commit 8bfb70e): the reshaped
`TestDescribeTables` block (lines 485-704) and the new
`test_explicit_table_without_embedding_column_raises` (lines 1237-1263). It does not
re-raise prior findings (confirmed intact below).

## Implementation Plan

The new tests are strong: the `describe_tables` happy path asserts the full
`{table: [cols]}` shape with all five per-column fields (incl. bool-identity on
`nullable`), the explicit-vs-default raise/skip divergence is covered on both sides,
the connection budget is correct and not order-fragile, and the PK/FK monkeypatch
does NOT over-mock the merge (the dict-comprehension at prod
`mcp_db_server.py:1014-1023` still runs). See Strengths. Two genuine coverage
residuals remain on the new surface, both suggestion-level and both the items the
task explicitly asked about.

1. [completed] `_list_table_names`'s SELECT-privilege SQL is never asserted (only monkeypatched away) - `test_mcp_db_server.py:600-604, 638-642`
   - 1.1. `describe_tables` default-all mode calls `_list_table_names(engine, schema)` (prod `mcp_db_server.py:1074`), which issues its own `pg_class` + `has_table_privilege(..., 'SELECT')` + `relkind = 'r'` discovery query (prod lines 732-743). Both default-all tests
     (`test_default_all_describes_every_role_readable_table`,
     `test_default_all_skips_discovered_table_with_no_columns`) monkeypatch
     `_list_table_names` to a literal list, so its issued SQL — the SELECT-privilege
     filter and the `relkind = 'r'` ordinary-table gate — is never exercised or
     asserted anywhere in the suite. A regression that dropped the privilege filter
     (leaking non-readable tables into default-all describe) or the `relkind` gate
     (pulling in views/indexes) would not be caught.
        - Current: `monkeypatch.setattr(mcp_db_server, "_list_table_names", lambda e, s: [...])` in both default-all tests; no `_CapturingConnection` ever sees the listing query.
        - Expected: one focused test that drives the real `_list_table_names` through a `_CapturingConnection` (not patched) and asserts the issued SQL carries `has_table_privilege(current_user`, `'SELECT'`, and `relkind = 'r'` — mirroring `test_list_tables_filters_by_select_privilege` (lines 353-378). This is unit-testable today (no inspector needed; it is a plain `text()` query).
        - Rationale: assertion quality / coverage — the privilege *pattern* is already proven for `list_tables`, so this is a suggestion rather than a gap-with-teeth, but `_list_table_names` is a distinct query (its own `relkind` gate, its name-only projection) and its filter is currently unlocked. Keeping the default-all tests' patch for the merge-logic tests is fine; this just adds the one missing direct lock.

2. [completed] `_get_foreign_keys` real `zip` / `referred_schema` fallback / multiple-FK behavior is never exercised - `test_mcp_db_server.py:525-529, 688-695`
   - 2.1. Every `describe_tables` test monkeypatches `_get_foreign_keys` to a hand-built `{column: "schema.table.column"}` dict, so the inspector-driven body (prod lines 676-685) — the positional `zip(constrained_columns, referred_columns)` that splits a composite FK per column, the `referred_schema or schema` fallback, and accumulation across multiple FK constraints — runs in no test. `test_composite_pk_and_composite_fk_split_per_column` (lines 655-704) proves the *merge* distributes a pre-split map onto the right columns (it asserts `columns[0]["references"] == "qpp_cm.document.doc_id"` and `columns[1]["references"] == "qpp_cm.document.chunk_id"`), but the dict it consumes is already split by the test — the production `zip` that does the splitting is bypassed.
        - Current: `_get_foreign_keys` only ever stubbed.
        - Expected: a focused `_get_foreign_keys` unit test that monkeypatches `mcp_db_server.inspect` to a fake inspector whose `get_foreign_keys` returns a composite constraint (e.g. `constrained_columns=["doc_id", "chunk_id"]`, `referred_columns=["id", "cid"]`, `referred_table="document"`, `referred_schema=None`) and asserts the returned map zips per column (`{"doc_id": "qpp_cm.document.id", "chunk_id": "qpp_cm.document.cid"}`) with the `referred_schema or schema` fallback applied. Optionally a second constraint to prove accumulation. This is unit-testable — `inspect` is a module-level import — not integration-level.
        - Rationale: coverage adequacy — the per-column FK split is the load-bearing new behavior `describe_tables` advertises (the docstring at prod lines 1049-1051 promises "composite FKs are split per column"); right now only the trivial half (re-distributing an already-split dict) is verified. This is one finding, not two (the `zip`, the schema fallback, and multi-FK accumulation are all the same untested helper body).

## Skills with No Issues / Genuine Strengths

The NEW tests (commit 8bfb70e):

1. **`describe_tables` happy path asserts the real `{table: [cols]}` shape and all five per-column fields, with bool identity** (`test_maps_columns_with_pk_and_references`, lines 501-560). It asserts full-dict equality on `{"measures_embedding": [...]}` covering `column_name` / `data_type` / `nullable` / `is_primary_key` / `references` for both rows, then re-asserts `nullable is False` / `nullable is True` (bool-identity, locking the `row[2] == "YES"` coercion at prod line 1018, not just truthiness), and locks the `udt_name` / `USER-DEFINED` CASE via the issued SQL. Crucially it is the test that satisfies the task's focus-#1 discriminator: the PK-member column (`embedding`) has `references=None` while the FK column (`chunk_text`) maps to `qpp_cm.document.id` — i.e. one PK-member column with `references=None` alongside an FK column that maps correctly. No issues.
2. **Composite-PK / composite-FK test proves distinct per-column FK targets and full PK membership** (`test_composite_pk_and_composite_fk_split_per_column`, lines 655-704). `all(col["is_primary_key"] for col in columns)` proves every member of a multi-column PK is marked, and the two distinct `references` assertions prove the merge maps each constrained column to *its own* referenced column (not a single shared target). Together with strength 1 this covers focus #1 — the only caveat being that the splitting itself is done by the test's input dict, not the production `zip` (finding 2). No issues with the test as written.
3. **Explicit-vs-default raise/skip divergence is covered on BOTH sides — the single most meaningful new branch** (prod `describe_tables` lines 1086-1093). `test_explicit_table_no_columns_raises` (lines 562-579) proves an explicitly named empty table raises `"not found or has no columns"`; `test_default_all_skips_discovered_table_with_no_columns` (lines 619-653) proves a *discovered* empty table (`ghost`) is silently skipped while `document` is kept and NO error is raised (`set(results.keys()) == {"document"}`). This is exactly the `explicit` flag's raise-vs-`continue` fork, and both arms are locked. The discovered-empty SKIP path the task asked about is tested. No issues.
4. **Connection budget for `_engine_yielding` is correct and not order-fragile** (default-all tests, lines 581-653). In default-all mode `_list_table_names` is patched (consumes no `.connect()`), and `_describe_one_table` opens exactly one connection per table (prod line 1003) before the patched PK/FK helpers (which never connect). So two discovered tables -> `_engine_yielding(conn, conn)` with two connections, matching `side_effect`'s one-per-`.connect()` contract. The order is keyed by which table is described first (driven by the patched `_list_table_names` list order), which is deterministic. The explicit-table tests use a single `_engine_yielding(conn)` for the single columns query. Budgets are exact, not loose. No issues.
5. **The PK/FK monkeypatch does NOT over-mock the logic under test.** Patching `_get_primary_key_columns` / `_get_foreign_keys` / `_list_table_names` stubs the *introspection* (inspector + privilege query), but the merge that the `describe_tables` tool actually owns — the `information_schema.columns` query, the `data_type` CASE, the `row[2] == "YES"` coercion, and the dict-comprehension that folds `pk_columns` membership and `fk_map.get(row[0])` into each column (prod lines 1003-1023) — still runs unmocked and is what the assertions check. The boundary is drawn at the right place. No issues.
6. **New search "not searchable" test reaches the right branch with the right matcher** (`test_explicit_table_without_embedding_column_raises`, lines 1237-1263). Stubs `_get_vector_columns -> []` for an explicitly requested table and asserts `pytest.raises(ValueError, match="not searchable")`, hitting the prod guard at lines 1402-1409 (the explicit-table-with-no-vector-column path that auto-discovery can never produce). The docstring correctly explains why this only happens for an explicitly named table. No issues.

(Pre-existing strengths from v01/v02 — reranker reordering, privilege-filter SQL assertions, RRF pure tests, the five auth paths, identifier-validation parametrization, model-name resolution, the knob-override tests — remain valid and are not repeated here.)

### By-nature hard to unit-test (explicitly not counted as misses)

- **The `ef_search` connect-event** (`_set_hnsw_ef_search`): per-connection pgvector warm-up + `SET`; not reachable with the SQLAlchemy fakes. Unchanged from v02.
- **`threading.Lock` double-checked-locking singletons** (`_engines_lock` etc.): concurrency correctness not observable single-threaded. Unchanged from v01.
- Note: `_get_foreign_keys` and `_list_table_names` are NOT in this bucket — both are unit-testable (findings 1-2). They are listed as suggestions, not acknowledged-hard.

## Prior findings confirmed intact

v01 findings 1-8 and the v02 suggestions, spot-checked against the current (reshaped) source:

1. **Served-set fresh-query regression (v01.1)** — `test_tool_drives_real_served_databases_fresh_query` (lines 380-442) still drives the real served-set path TWICE through one fake bootstrap engine yielding `{policy_db}` then `{policy_db, other_db}`, validates `other_db` on the 2nd call, and asserts `bootstrap_engine.connect.call_count == 2`. A reintroduced process-lifetime cache would connect once and reject `other_db`. **Intact.**
2. **describe happy-path + no-columns (v01.2)** — survived the rename to `TestDescribeTables`: `test_maps_columns_with_pk_and_references` (lines 501-560, with the `udt_name`/`USER-DEFINED` CASE assertion and `nullable is True/False` coercion) and `test_explicit_table_no_columns_raises` (lines 562-579). **Intact and extended.**
3. **Empty-`chunk_text` warning (v01.3)** — `test_warns_on_empty_chunk_text` (lines 1265-1334) still asserts the `caplog` WARNING "1 of 2 rerank candidates", the `[query, ""]` pair reaching the reranker, and the low rank. **Intact.**
4. **Per-leg `executed_sql` + bound params (v01.4)** — `test_hybrid_issues_both_legs` (lines 949-1001) asserts dense `<=>` + `min_similarity` (no `websearch_to_tsquery`), sparse `websearch_to_tsquery` + `ts_rank_cd`, one statement per connection, AND bound params via `executed_params` (`min_similarity == 0.3`, `query == "beta"`). `_CapturingConnection.execute` still records `executed_params` (lines 95-98). **Intact.**
5. **Dead reranker stub removed (v01.5)** — `test_dimension_guard_skips_empty_table` (lines 1117-1154) carries no `get_reranker` stub; the explanatory comment (lines 1144-1146) documents the empty-candidates early return. **Intact.**
6. **`run_sql` semicolon boundary (v01.6)** — parametrized `test_semicolon_boundary` (lines 715-747) accepts `"select 1;"` and `"select 1"`, rejects `"select 1; select 2"`. **Intact.**
7. **Auto-discovery + no-PK fusion (v01.7)** — `test_auto_discovers_tables_when_none` (lines 1336-1386, spy proves both discovered tables searched) and `test_no_pk_fuses_via_display_row` (lines 1040-1073, `pk_cols=[]` fuses via the full-display-row key). **Intact.**
8. **Structure / parametrize polish (v01.8)** — suite remains grouped in `class Test...` blocks; `bearer_middleware` and `search_preamble` fixtures present; the four `/mcp` auth cases parametrized over `(env, headers, expected_code, expected_reached)` with ids `valid/missing/bad/fail-closed-unset` (lines 1553-1586); `test_leaves_health_open` kept separate (varies the path). **Intact.**
9. **v02 finding 1 — `_env_int` non-int WARNING asserted** — `TestEnvInt.test_non_integer_warns_and_falls_back_to_default` (lines 199-216) and `test_statement_timeout_non_int_falls_back_and_warns` (lines 823-853) both `caplog`-assert the "Ignoring non-integer" record. The clamp-and-warn cases (`MCP_DB_POOL_SIZE=0 -> 1`, `MCP_STATEMENT_TIMEOUT_S=0 -> '1s'`) are present (lines 218-235, 855-886). **Intact.**
10. **v02 finding 2 — `MCP_LOG_LEVEL` resolution** — `TestResolveLogLevel` (lines 245-267) covers known name -> int, unknown -> INFO, unset -> INFO. **Intact.**

## Status & Next Steps

**Current Status**: Suite green at 66 passed (up from 61), fully mocked — no live DB/model/server. Both v03 suggestions addressed: a direct `test_list_table_names_filters_by_select_privilege` now drives the real query through a `_CapturingConnection` and asserts `has_table_privilege(current_user`/`'SELECT'`/`relkind = 'r'` plus the bound `:schema` param, and four `_get_foreign_keys` tests patch the module-level `inspect` to exercise the real per-column `zip` split, the `referred_schema or schema` fallback (None and explicit cross-schema), the multi-FK last-wins behavior, and the falsy `referred_table` skip. v01 findings 1-8 and v02 findings 1-3 confirmed intact after the `TestDescribeTables` reshape.

**Completed**:
1. Read the test file, both prior reviews, and the relevant production paths (`describe_tables`, `_describe_one_table`, `_list_table_names`, `_get_foreign_keys`, `_get_primary_key_columns`, the search "not searchable" guard) in full.
2. Went adversarial on the new `describe_tables` assertions (full-dict shape, bool identity, per-column FK split, raise-vs-skip), the `_engine_yielding` connection budget, the degree of monkeypatching over the merge, and the new search "not searchable" test; spot-checked v01 1-8 and v02 1-3 against current source.
3. Addressed findings 1 and 2 with five new tests.
4. Ran the suite: 66 passed.

**Next Steps**:
1. None.

**Blockers**:
1. None.

**Notes**:
1. Severity tags use the repo `[critical]/[major]/[minor]/[suggestion]` convention.
2. The composite-FK test was specifically checked against focus #1's "one PK-member column has `references=None` while FK columns map correctly" — that exact discriminator is satisfied by `test_maps_columns_with_pk_and_references` (PK member `embedding` -> `references` None; FK `chunk_text` -> `qpp_cm.document.id`), while the composite-FK test proves distinct per-column FK *targets*. Together they cover focus #1.
3. Findings 1 and 2 are framed as unit-testable, not integration-level: `_list_table_names` is a plain `text()` query and `_get_foreign_keys` reads a module-level `inspect` that can be monkeypatched.
