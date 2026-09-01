---
name: cr-test_mcp_db_server (20260701v01)
goal: Review the NEW tests added on branch `mcp-search-perf` (git diff main..mcp-search-perf) that cover the search-performance rework — MCP_WARM_MODELS warm-up, multi-schema search with a single merged rerank, single-schema string backward-compat, and the `tables`+multi-schema guard. Go adversarial on whether the multi-schema test actually proves ONE merged rerank, whether the fakes match the real `_search_single_table` signature, and on the coverage residuals (empty-schema-list raise, dimension guard in the multi-schema path, main()'s warm-skip branch).
created: 2026-07-01
updated: 2026-07-01
---

## Scope

Reviewed file (test quality only — the production module `mcp_db_server.py` has its
own CR, `20260626v03_cr_mcp_db_server.md` / branch equivalent):
- `code/mcp_db_server/unit_tests/test_mcp_db_server.py`

Review is scoped to the branch changes: `git diff main..mcp-search-perf -- code/mcp_db_server/unit_tests/test_mcp_db_server.py`. Five new tests:
1. `test_warm_models_enabled_default_and_overrides` (lines 218-231)
2. `test_warm_models_loads_and_warms_both` (lines 233-244)
3. `test_multi_schema_searches_all_and_reranks_once` (lines 1421-1464)
4. `test_single_schema_string_still_works` (lines 1466-1491)
5. `test_tables_with_multiple_schemas_raises` (lines 1493-1506)

Prior reviews of this file (`20260626v01/v02/v03_cr_test_mcp_db_server.md`) — v01
findings 1-8 and v02/v03 findings all previously confirmed fixed; this pass does not
re-raise them (spot-checked intact below) and scopes only the new branch surface.

Suite is green: `uv run pytest code/mcp_db_server/unit_tests/ -q` -> **73 passed**;
the 5 tests under review are among them. `.coverage` was removed after the run.

## Implementation Plan

The new tests are strong and correctly targeted; the severity ceiling is
suggestion-level. The multi-schema test genuinely proves a single merged rerank (not
per-schema reranking), the fakes are faithful to the real `_search_single_table`
signature, and the `tables`+multi-schema guard is locked with a specific matcher. The
findings below are coverage residuals the task explicitly asked about — each is a
cheap, unit-testable add, not a defect in the tests as written.

1. [suggestion] Empty-`schema`-list raise is untested — `test_mcp_db_server.py:1421-1506`; prod `mcp_db_server.py:1494-1495`
   - 1.1. `search` normalizes `schema` to a list and raises `ValueError("At least one schema must be provided")` when the list is empty (prod lines 1493-1495). No test passes `schema=[]`; the multi-schema tests only exercise the non-empty 2-schema list. A regression that dropped this guard (letting an empty schema list fall through to `_discover_embedding_tables` never being called, then the `No embedding tables` raise with a misleading message, or an empty-loop no-op) would not be caught.
        - Current: no test drives `schema=[]`.
        - Expected: a focused test — `search(database="policy_db", schema=[], query="q")` under `_stub_served_databases` — asserting `pytest.raises(ValueError, match="At least one schema")`. Cheap; raises before any engine/model resolution, so no `search_preamble` needed.
        - Rationale: coverage — this is the empty-list arm of the schema normalization that the multi-schema feature introduced; its sibling raise (`tables` + multi-schema) IS tested (`test_tables_with_multiple_schemas_raises`), leaving this one asymmetrically unlocked.

2. [suggestion] The dimension guard is never asserted in the multi-schema path — `test_mcp_db_server.py:1421-1464`; prod `mcp_db_server.py:1531-1551`
   - 2.1. The dimension guard now loops per `(schema, table)` (prod lines 1531-1551). `test_multi_schema_searches_all_and_reranks_once` uses the `search_preamble` fixture, which stubs `_get_vector_dimension -> 384` for a 384-dim model, so the guard PASSES for every schema and the loop is exercised but never asserted. The only mismatch test, `test_dimension_guard_raises_on_mismatch` (lines 1268-1299), is single-schema / single-table. So a regression where a mismatch in a NON-FIRST schema fails to raise (e.g. the loop `break`ing early, or the guard keyed off only the first schema) would not be caught.
        - Current: multi-schema path only ever runs the guard in its passing configuration.
        - Expected: a multi-schema mismatch test — e.g. `_get_vector_dimension` returning 384 for `cms_iom` and 1024 for `usc` (keyed off the `schema` arg) against a 384-dim model — asserting `pytest.raises(ValueError, match="dimension mismatch")`, proving the per-`(schema, table)` guard fires on a later schema, not just the first.
        - Rationale: coverage — the per-(schema,table) guarding is new behavior of this branch; the passing path is exercised but the raise arm in the multi-schema loop is not.

3. [suggestion] Backward-compat test asserts only `source_schema`, not that the result flowed through the full rerank pipeline — `test_mcp_db_server.py:1466-1491`
   - 3.1. `test_single_schema_string_still_works` asserts `results[0]["source_schema"] == "cms_iom"` — good, and the load-bearing new claim. But it does not assert the result carries a `rerank_score`, which is the field proving the candidate actually traversed the reranker (prod lines 1616-1619) rather than being returned pre-rerank. Since `_search_single_table` is stubbed and the reranker is a `MagicMock` with `predict.return_value = [0.9]`, adding `assert results[0]["rerank_score"] == 0.9` would cheaply lock that the single-schema string path still routes through the (now shared) merged-rerank pipeline.
        - Current: only `source_schema` asserted.
        - Expected: additionally `assert results[0]["rerank_score"] == 0.9`.
        - Rationale: assertion strength — a one-token addition that turns a shape check into a pipeline-traversal check for the backward-compat guarantee.

4. [suggestion] `_warm_models_enabled` empty-string (`MCP_WARM_MODELS=""`) case is untested and is asymmetric with `_trust_remote_code` — `test_mcp_db_server.py:218-231`; prod `mcp_db_server.py:635-640`
   - 4.1. `_warm_models_enabled` returns `os.environ.get("MCP_WARM_MODELS", "true").strip().lower() not in {"0","false","no","off"}` — so an empty string returns **True** (warm on), because `""` is not in the falsy set. `_trust_remote_code` (tested at lines 214-216) treats `""` as **False**. That asymmetry is a deliberate default-on-vs-default-off design, but the warm test's falsy tuple `("false","0","no","off","False")` omits `""`, so the `""` -> True behavior is never pinned. If someone "fixed" `_warm_models_enabled` to also treat `""` as falsy (mirroring `_trust_remote_code`), no test would flag the behavior change.
        - Current: falsy tuple omits `""`.
        - Expected: add `""` to the truthy assertions (it should stay enabled), or an explicit `monkeypatch.setenv("MCP_WARM_MODELS", ""); assert _warm_models_enabled() is True` — documenting the intentional asymmetry.
        - Rationale: coverage — pins the one edge that differs from the sibling boolean-env helper.

## Skills with No Issues / Genuine Strengths

The five new tests, checked against the python-development unit-tests skill (type
hints on all test signatures and fakes: present; Google-style docstrings on every
test: present; no over-mocking of the logic under test) and against the task's
adversarial focuses:

1. **`test_multi_schema_searches_all_and_reranks_once` genuinely proves ONE merged rerank — it cannot pass under per-schema reranking** (lines 1421-1464). The proof is airtight and worth stating precisely:
   - `len(predict_calls) == 1` (line 1463) rules out per-schema reranking outright — a per-schema design would resolve `get_reranker().predict` once per schema and append twice.
   - `_discover_embedding_tables` is stubbed to `[f"{s}_emb"]` (line 1426, one table per schema) and `fake_single_table` emits exactly one row per schema, so the merged pool is exactly 2 candidates — one per schema. `len(predict_calls[0]) == 2` (line 1464) therefore means the SINGLE `predict` call spanned a pool built from both schemas.
   - `{r["source_schema"] for r in results} == {"cms_iom", "usc"}` (line 1461) confirms both schemas actually contributed to the reranked output (the fake stamps distinct `doc_id`/`chunk_text` per schema and prod stamps `source_schema` at prod line 1588).
   Together these three assertions are mutually reinforcing: one rerank call, over a pool of exactly one-candidate-per-schema, whose survivors carry both schemas. **No issues.** (One optional refinement is captured only implicitly — the test asserts the pair COUNT (2) rather than the pair CONTENTS; inferring cross-schema spanning from the pool structure is sound, but asserting `predict_calls[0]` contains both `text from cms_iom` and `text from usc` would make the span explicit. Not a gap given the structural proof.)

2. **The multi-schema fake is faithful to the real `_search_single_table` signature** (lines 1429-1440). Real signature is `_search_single_table(engine, schema, table, query, query_embedding_str, pool_size, min_similarity)` (prod lines 1304-1312); prod calls it positionally as `_search_single_table(engine, s, table, query, query_embedding_str, pool_size, min_similarity)` (prod lines 1578-1586), so `s` is the 2nd positional arg. The fake `fake_single_table(engine, schema, table, *a, **k)` keys off `schema` = the 2nd positional, matching production exactly, and absorbs the remaining four args via `*a`. The per-schema tagging (`doc_id`/`chunk_text` = `f"{schema}-1"` / `f"text from {schema}"`) is therefore driven by the SAME arg production binds. **No issues.**

3. **`test_tables_with_multiple_schemas_raises` locks the guard with a specific, non-generic matcher** (lines 1493-1506). It asserts `pytest.raises(ValueError, match="cannot be combined with multiple schemas")` — a substring of the exact prod message (prod line 1502), not a bare `ValueError` — so a regression that raised a DIFFERENT `ValueError` (e.g. reordered the guard so `tables` validation fired first, or the empty-schema raise) would fail the match. It stubs only the served-database set and raises before any engine work (prod lines 1499-1503 run before `get_database_engine`), so the test is minimal and correctly does not use `search_preamble`. **No issues.**

4. **`test_single_schema_string_still_works` proves the string-vs-list normalization keeps single-schema behavior** (lines 1466-1491). Passing `schema="cms_iom"` (a bare string) and asserting `source_schema == "cms_iom"` exercises the `isinstance(schema, str)` branch of the normalization (prod line 1493) and proves the backward-compatible path still stamps `source_schema`. Combined with the multi-schema test, both arms of the string|list normalization are covered. **No issues** (assertion-strength refinement in finding 3).

5. **`test_warm_models_enabled_default_and_overrides` covers default-on + both truthy/falsy directions** (lines 218-231). It deletes the env (asserts default `True`), iterates the four falsy tokens plus `"False"` (asserting `False`), and iterates truthy tokens plus `"anything"` (asserting the default-on semantics — anything not explicitly falsy stays enabled). This correctly mirrors the prod set-membership logic (prod lines 635-640) including the "unknown value -> enabled" default-on behavior, the inverse of `_trust_remote_code`'s default-off. **No issues** (empty-string edge in finding 4).

6. **`test_warm_models_loads_and_warms_both` asserts exactly-once warm-up per model without over-asserting internals** (lines 233-244). It monkeypatches both getters to return `MagicMock`s and asserts `emb.encode.assert_called_once()` / `rer.predict.assert_called_once()` — matching prod's one `encode` + one `predict` warm-up (prod lines 652-653). It correctly does NOT assert the warm-up ARGUMENTS (`"warmup"` / the `("warmup query","warmup document")` pair), which would be brittle string-coupling to an internal detail; call-count is the right invariant (the point is that both models are touched once, not what text warms them). **No issues.**

### Over-mocking is NOT a gap here (task focus)

Stubbing `_search_single_table` away in tests 3 and 4 means the per-table dense/sparse
SQL + RRF fusion is not exercised in these tests — but that is deliberately owned by
`TestSearchSingleTable` (`test_hybrid_issues_both_legs`, `test_dense_only_without_tsvector`,
`test_no_pk_fuses_via_display_row`, lines 1130-1257), which drives the real
`_search_single_table` through `_CapturingConnection`s and asserts the dense `<=>` /
`min_similarity` + sparse `websearch_to_tsquery` / `ts_rank_cd` SQL and bound params.
The boundary is drawn at the right place: the multi-schema/backward-compat tests own
the ORCHESTRATION (per-schema fan-out, source_schema stamping, single merged rerank),
`TestSearchSingleTable` owns the per-table SQL. No double-coverage, no gap.

### By-nature hard to unit-test (explicitly not counted as misses)

- **`main()`'s warm-skip branch** (prod lines 1915-1918): the DECISION helper
  `_warm_models_enabled()` is tested both directions (test 1) and `warm_models()`
  itself is tested (test 2), but the `main()` wiring that calls one-or-logs-the-skip
  sits alongside the uvicorn/`create_app` startup path, which the suite does not drive
  (same bucket as the existing untested `main()` / server-run path). The two testable
  pieces of the warm-up feature ARE covered; only the startup glue is not, and that is
  acknowledged-hard, not a testable miss.
- Unchanged acknowledged-hard items from v03 (the `ef_search` connect-event, the
  `threading.Lock` double-checked singletons) remain out of scope of this branch.

## Prior findings confirmed intact

Spot-checked against the current (branch) source — no regressions introduced by the
branch changes:
- **v01.3 empty-`chunk_text` warning** — `test_warns_on_empty_chunk_text` (lines 1536-1605) still asserts the caplog WARNING "1 of 2 rerank candidates", the `[query, ""]` pair, and the low rank; the merged-rerank rework preserved this path (prod lines 1603-1615). Intact.
- **v01.4 per-leg SQL + bound params** — `test_hybrid_issues_both_legs` (lines 1133-1185) intact.
- **v03.1/2 `_list_table_names` SELECT SQL + `_get_foreign_keys` real split** — `test_list_table_names_filters_by_select_privilege` (lines 762-782) and the four `_get_foreign_keys` inspector tests (lines 784-888) intact.
- The reranker-reordering test (`test_reranker_reorders_rrf_candidates`, lines 1340-1399), the RRF pure tests, and the five auth paths are untouched by the branch and remain valid.

## Status & Next Steps

**Current Status**: Suite green at **73 passed**, fully mocked (no live DB/model/server).
The 5 new branch tests are strong; the multi-schema single-rerank proof is airtight
and the fakes are faithful to the real `_search_single_table` signature. Four
suggestion-level coverage residuals remain, all on the new branch surface, all
cheap and unit-testable.

**Completed**:
1. Read the branch diff, the full test file, all three prior reviews, and the relevant production paths (`search`, `_search_single_table`, `warm_models`, `_warm_models_enabled`, `main`) in full.
2. Went adversarial on the task's three focuses: the single-merged-rerank proof (airtight), fake faithfulness to the real signature (faithful), and the over-mock boundary (correctly drawn). Verified the empty-schema-list raise, the multi-schema dimension guard, and the `main()` warm-skip against source.
3. Ran the suite (73 passed) read-only and removed `.coverage`.

**Next Steps**:
1. (Optional) Add the four suggestion-level tests: empty-schema-list raise, multi-schema dimension mismatch, `rerank_score` in the backward-compat test, and the `MCP_WARM_MODELS=""` edge.

**Blockers**:
1. None.

**Notes**:
1. Severity tags use the repo `[critical]/[major]/[minor]/[suggestion]` convention. No `[critical]`/`[major]`/`[minor]` findings — the tests are green, correctly targeted, and the residuals are additive coverage, not defects.
2. Test count is reported as an absolute (73 passed) rather than a delta from the last review's 66; intervening commits beyond the 5 tests under review affect the total, so no delta narrative is forced.

## Resolution (2026-07-01)

- [suggestion 1] empty `schema=[]` raise — **added** `test_empty_schema_list_raises`.
- [suggestion 2] multi-schema dimension guard — **added** `test_multi_schema_dimension_mismatch_raises` (mismatch in the non-first schema).
- [suggestion 3] backward-compat pipeline — **added** a `rerank_score == 0.9` assertion to `test_single_schema_string_still_works`.
- [suggestion 4] `MCP_WARM_MODELS=""` edge — **added** `""` to the default-on cases (locks empty=on, documented as intentional vs trust_remote_code).
