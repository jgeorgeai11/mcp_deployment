---
name: cr-mcp_db_server
goal: Address code quality issues in the a0766e0 changes to code/mcp_db_server/mcp_db_server.py (global rerank pre-cut, _rerank_global_pool, structural tripwire) to align with python-development standards.
created: 2026-07-01
updated: 2026-07-01
---

## Implementation Plan

1. [completed] Docstring accuracy - `code/mcp_db_server/mcp_db_server.py`
   - 1.1. [minor] [FIXED] Line ~239 (`_rerank_global_pool` docstring): the parenthetical "a single-schema search (which produces at most that many candidates) is unaffected" repeats the schema-count-vs-table-count error corrected everywhere else in this change. The pre-cut fires on candidate COUNT (number of tables touched), not schema count; a single-schema search over a multi-table schema (e.g. `qpp_cm`, two embedding tables) produces up to 2x the pool and IS trimmed. The docstring should describe the single-TABLE case, matching the module docstring, the `search` docstring, and Decision #3 of the activity.
        - Current: `` ``_rerank_pool()`` -- so out of the box the global cap equals the per-table pool depth, and a single-schema search (which produces at most that many candidates) is unaffected. ``
        - Expected: `` ``_rerank_pool()`` -- so out of the box the global cap equals the per-table pool depth, and a search touching a single embedding table (at most that many candidates) is unaffected; searches touching multiple tables (multi-schema, or a single multi-table schema) are trimmed to the cap. ``

2. [pending] Redundant metadata query on the hot path - `code/mcp_db_server/mcp_db_server.py`
   - 2.1. [suggestion] Line ~1600 (tripwire): the structural tripwire calls `_get_tsvector_columns(engine, s, table)` for every table in a multi-table search, and `_search_single_table` already queries the same catalog per table a few lines later - so the tsvector-presence lookup is done twice per table on the search path this change is meant to speed up. The cost is negligible (an `information_schema` read vs. a multi-second rerank) and the current placement is clearer, so this is optional; if the area is touched again, surfacing tsvector-presence from within the candidate loop (or returning it from `_search_single_table`) would drop the duplicate round-trip.
        - Current: `if not _get_tsvector_columns(engine, s, table)  # also queried inside _search_single_table`
        - Expected: `# (optional) reuse a single tsvector-presence lookup per table across the tripwire and _search_single_table`

## Skills with No Issues

1. Type Hints: No issues found - `_rerank_global_pool() -> int` is annotated; the added `sort` lambda needs none.
2. Docstrings: Issue found (finding 1.1) - `_rerank_global_pool` has a Google-style docstring with a `Returns:` section, but its body makes an inaccurate claim.
3. Comments: No issues found - the tripwire and pre-cut blocks explain the "why" (fused_score leg-count asymmetry, cost bounding, stable-sort tie determinism) and are current.
4. Logging: No issues found - `logger.warning` for the misconfiguration tripwire and `logger.info` for the trim are the correct levels and match the module's existing f-string logging style.
5. Exception Handling: No issues found - the new code runs inside `search`'s existing try/except; `fused_score` is guaranteed on every candidate by `_search_single_table`, and any lookup failure is caught and re-raised by the surrounding handler.
6. Data Validation: N/A - no data file/table outputs; this is a search-path code change.
7. Executable Scripts: N/A - no CLI/entrypoint changes.
8. Unit Tests: Reviewed separately in `docs/code_review/mcp_db_server/unit_tests/cr_test_mcp_db_server.md`.

## Status & Next Steps

**Current Status**: Reviewed the a0766e0 diff to `mcp_db_server.py`. Finding 1.1 (docstring accuracy) applied; finding 2.1 left as an optional suggestion.
**Completed**:
1. Checked the added `_rerank_global_pool`, the `search` pre-cut, and the structural tripwire against type-hints, docstrings, comments, logging, and exception-handling standards.
2. Verified the `max(top_k, cap)` floor, stable-sort selection, and placement (after the empty-candidates guard, before rerank) are correct.
3. Applied finding 1.1: rewrote the `_rerank_global_pool` docstring to describe the single-TABLE unaffected case (matching the module/`search` docstrings and Decision #3), removing the last instance of the schema-vs-table claim.
**Next Steps**:
1. Optionally consider finding 2.1 (dedup the per-table `_get_tsvector_columns` lookup) if this code is revisited.
**Blockers**:
1. None.
**Notes**:
1. The pre-cut logic itself matches the design (RRF `fused_score` global sort, cost bounded by table count) and is verified by the new `TestSearchGlobalPreCut`/`TestSearchPreCutTripwire` suites.
