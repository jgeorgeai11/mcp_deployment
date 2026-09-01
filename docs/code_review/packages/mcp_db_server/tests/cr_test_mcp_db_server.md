---
name: cr-test_mcp_db_server
goal: Review the a0766e0 test additions in code/mcp_db_server/unit_tests/test_mcp_db_server.py (pre-cut, global-pool knob, and tripwire tests) against unit-tests standards.
created: 2026-07-01
updated: 2026-07-01
---

## Implementation Plan

1. [pending] No changes required - `code/mcp_db_server/unit_tests/test_mcp_db_server.py`
   - 1.1. [suggestion] Line ~38 (`_cand` helper): the return annotation is a bare `dict`; `dict[str, Any]` would be marginally more precise. Left as-is to match the file's established style (existing fakes are annotated `-> list[dict]`), so this is optional and not worth diverging from local convention.

## Skills with No Issues

1. Unit Tests: No issues found - pytest (not unittest); files/functions follow `test_<scenario>_<expected>`; each test is single-behavior with clear arrange-act-assert; `monkeypatch`/`caplog` used for env and log boundaries; new shared helpers (`_cand`, `_capturing_reranker`) reduce duplication; assertions target observable behavior (reranker input count/content, log presence) not private state.
2. Coverage: No issues found - the new tests cover the cap trim (multi-schema and single-schema-multi-table), exact fused_score selection, single-leg retention, the `max(top_k, cap)` floor, the default no-op (incl. asserting no trim log), the knob's default/override/clamp, and the tripwire's fire + three silent cases; `--cov-report=term-missing` shows no new uncovered lines.
3. Type Hints: No issues found - all added test functions and helpers are annotated (fixtures typed `pytest.MonkeyPatch` / `pytest.LogCaptureFixture`; helpers return `dict` / `tuple[MagicMock, list[list[str]]]`).
4. Docstrings: No issues found - every new test and helper has a concise one-line (or short multi-line) docstring stating the behavior under test.
5. Comments: No issues found - inline comments explain the fused_score arithmetic behind the expected survivor sets (the "why"), matching the file's style.

## Status & Next Steps

**Current Status**: Reviewed the a0766e0 test additions. Clean; no required changes.
**Completed**:
1. Checked the 14 added tests (`TestRerankGlobalPool`, `TestSearchGlobalPreCut`, `TestSearchPreCutTripwire`) and the two shared helpers against unit-tests standards.
2. Confirmed the tripwire tests assert both the firing case (named dense-only table) and the must-stay-silent cases (all-hybrid, single-table, and the normal "no strong matches" outcome).
**Next Steps**:
1. None required.
**Blockers**:
1. None.
**Notes**:
1. Suite is green at 89 tests (75 -> 89). The `search_preamble` fixture was extended to stub `_get_tsvector_columns` as hybrid, keeping existing multi-table search tests valid against the new tripwire's metadata lookup.
