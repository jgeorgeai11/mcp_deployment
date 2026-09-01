---
name: speed-up-mcp-search
goal: Cut MCP search latency for the client application's fan-out by (1) warming the embedding + reranker models at server startup so the first query does not pay the ~55s cold start, and (2) letting `search` span multiple schemas in one call so a "4 queries x 3 schemas" pattern collapses from 12 cross-encoder rerank passes to 4 (one merged rerank per query). Both are rag-server-side changes; the search-signature widening is backward-compatible (`schema` still accepts a single string, or now a list of schemas).
created: 2026-06-30
updated: 2026-06-30
---

## Implementation Plan

1. [completed] Warm the embedding + reranker models at startup - `code/mcp_db_server/mcp_db_server.py`
   - 1.1. Add `warm_models()` that eagerly loads both models via the existing lazy getters (`get_embedding_model`, `get_reranker`) and runs a tiny inference on each (`model.encode("warmup")`, `reranker.predict([("q", "d")])`) to trigger torch's first-call initialization, so the singletons are fully hot.
   - 1.2. Add an `MCP_WARM_MODELS` env knob (default true, truthy parse like the trust-remote-code knob). In `main()`, after `setup_logging` and before `uvicorn.run`, call `warm_models()` when enabled; log the warm start and the skip case.
   - 1.3. Document `MCP_WARM_MODELS` in the module Configuration docstring.

2. [completed] Multi-schema search with one merged rerank - `code/mcp_db_server/mcp_db_server.py`
   - 2.1. Widen `search`'s `schema` parameter to `str | list[str]`: a string preserves today's single-schema behavior; a list searches those schemas. Normalize to a list internally and validate each schema identifier (`validate_sql_identifier`).
   - 2.2. Loop schemas x their discovered embedding tables, generating candidates per table via the unchanged `_search_single_table` (dense + sparse + RRF), and stamp `source_schema` on each candidate; merge all candidates across schemas + tables into one pool. Keep the per-table dimension guard.
   - 2.3. Run ONE cross-encoder rerank over the merged pool, sort by `rerank_score`, cut to `top_k` - one rerank per call instead of one per schema.
   - 2.4. Add `source_schema` to each result dict (multi-schema results must be distinguishable; also removes the caller's need to carry the schema).
   - 2.5. Scope `tables` to a single schema: raise `ValueError` if `tables` is given with more than one schema (the `tables` names are schema-qualified).
   - 2.6. Update the `search` docstring (multi-schema semantics, `source_schema` output field, the `tables` single-schema constraint).

3. [completed] Tests - `code/mcp_db_server/unit_tests/test_mcp_db_server.py`
   - 3.1. `warm_models`: with `get_embedding_model` / `get_reranker` monkeypatched to fakes, assert both are loaded and each gets one warm-up inference call.
   - 3.2. Multi-schema `search`: a `schema` list searches each schema, results carry `source_schema`, and the reranker is invoked exactly once over the merged pool (assert a single `predict` call spanning candidates from >1 schema).
   - 3.3. Backward-compat: `schema` as a plain string still works (single-schema path, `source_schema` set to that schema).
   - 3.4. `tables` with a multi-schema `schema` raises `ValueError`.
   - 3.5. Run `uv run pytest code/mcp_db_server/unit_tests/ -q` to green.

4. [completed] Document the warm-start knob in the instance template - `.env.mcp.policy_db.example`
   - 4.1. Add a commented `MCP_WARM_MODELS` entry (default true; note it moves the ~one-time model-load cost to startup off the first query).

5. [completed] Validate live - (validation; no new code file)
   - 5.1. Multi-schema: call `search('policy_db', ['cms_iom','usc','qpp_cm'], <query>)` and confirm it returns a single merged, reranked list with `source_schema` present and drawn from more than one schema, with exactly one rerank pass.
   - 5.2. Warm start: start the server with warming on, then time the first `search` - confirm it is a warm (~seconds) latency, not the ~55s cold start.

## Key Data Decisions and Considerations

1. Backward-compatible signature widening - `schema: str | list[str]` keeps every existing single-schema call working (a string behaves exactly as today) while adding the multi-schema list form. The client application opts in by turning its three single-schema calls into one list call; no other caller breaks.
2. One merged rerank is the actual win - the cross-encoder rerank is CPU-bound and is both the latency driver and the serialization bottleneck (concurrent searches contend for the same cores). Merging candidates across schemas and reranking once turns a 4 queries x 3 schemas fan-out from 12 rerank passes into 4. Candidate generation (dense + sparse + RRF) stays per-table and is comparatively cheap.
3. Warm synchronously at startup, before serving - the server is not truly ready until the models are loaded, so warming before `uvicorn.run` (gated by `MCP_WARM_MODELS`, default true) is the correct semantics: the ~55s moves off the first user query onto startup. `/health` is down during the warm-up window (acceptable - do not route traffic until ready); a background-thread warm-up is the alternative only if `/health` must answer during warm-up.
4. `source_schema` on results - multi-schema results must be attributable to a schema, and this doubles as the "echo the schema back" enrichment (the caller no longer carries the schema it passed in).
5. `tables` is single-schema-only - the `tables` argument names embedding tables within a schema, so it is only meaningful with exactly one schema; given with several, raise rather than guess which schema owns them.
6. Out of scope - cross-schema candidate dedup (rows in different schemas are different documents, no dedup needed), ONNX/GPU reranker acceleration (separate, larger levers), and tuning `MCP_RERANK_POOL` (already an env knob).

## Status & Next Steps

**Current Status**: Implemented and validated on branch `mcp-search-perf` (off `main`); ready for code review + merge.
**Completed**:
1. Diagnosed the two costs (cold-start model load; per-schema rerank fan-out) and confirmed both are rag-server-side.
2. Task 1 - `warm_models()` + `_warm_models_enabled()` (`MCP_WARM_MODELS`, default true); called in `main()` before `uvicorn.run`; documented in the module docstring + .env.example.
3. Task 2 - `search` now takes `schema: str | list[str]`, loops (schema, table) pairs, stamps `source_schema`, and reranks the merged pool ONCE; `tables` restricted to a single schema; docstring updated.
4. Task 3 - tests for warm-up (enable-default + loads/warms both), multi-schema (one rerank pass + source_schema), single-schema-string backward-compat, and the tables+multi-schema guard. 68 -> 73 mcp_db_server tests.
5. Task 5.1 - live: `search('policy_db', ['cms_iom','usc','qpp_cm'], 'Medicare eligibility and payment rules', top_k=6)` returned 6 results across cms_iom(5)+usc(1), interleaved by rerank_score, each with `source_schema` - one merged rerank across schemas.
**Next Steps**:
1. Code-review the changed files, then merge `mcp-search-perf` -> `main`.
2. The client application switches its per-schema search calls to a single `schema=[...]` call to realize the fan-out win.
**Blockers**:
1. Task 5.2 (fresh-server warm-start timing) was validated by the unit test + the `main()` wiring, but not re-timed against the running server (to avoid disrupting the client application's connection); the timing benefit applies on the next server restart. A GPU on the serving host would be a larger, separate reranker win but is not required here.
**Notes**:
1. Complementary, out-of-scope levers the client application can also use: fewer query variations, routing to the relevant schema instead of all, and lowering `MCP_RERANK_POOL` (e.g. 50 -> 30) for a ~40% cheaper rerank at a small recall cost.
