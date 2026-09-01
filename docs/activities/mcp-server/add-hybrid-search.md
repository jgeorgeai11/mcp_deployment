---
name: add-hybrid-search
goal: Rework the all-db MCP server's `search` tool into hybrid retrieval (dense vector + sparse full-text, fused with reciprocal rank fusion). Also fix two correctness bugs the embedding rework introduced — the query model no longer matches ingestion, and the new `chunk_tsv` column leaks into results. Keep the server generic across any embedding table.
created: 2026-06-20 09:23:37
updated: 2026-06-20 09:31:00
---

## Implementation Plan

1. [completed] Rework `search` into hybrid retrieval - `code/mcp_servers/mcp_all_db/mcp_all_db.py`
   - 1.1. **Fix the query model (correctness bug).** `get_embedding_model` currently hardcodes `thenlper/gte-large`; ingestion now uses `Alibaba-NLP/gte-large-en-v1.5`. Load the model from `os.environ.get("MCP_EMBEDDING_MODEL", "Alibaba-NLP/gte-large-en-v1.5")` with `trust_remote_code=True`, so the default tracks ingestion and is overridable without a code change. Update the docstring (no longer "gte-large").
   - 1.2. **Exclude `tsvector` columns from results (correctness bug).** Results currently include every non-`vector` column, so the new `chunk_tsv` (a `tsvector`) is dumped as a raw blob. Discover `tsvector` columns and exclude them (alongside `vector` columns) from the display/select set. Add a `_get_tsvector_columns(engine, schema, table)` helper mirroring `_get_vector_columns`.
   - 1.3. **Add the sparse (keyword) leg.** For a table that has a `tsvector` column, run a full-text query: `where <tsv_col> @@ websearch_to_tsquery('english', :query)` ordered by `ts_rank_cd(<tsv_col>, websearch_to_tsquery('english', :query)) desc` limited to an internal per-leg depth. The query is bound as a parameter (no interpolation).
   - 1.4. **Add RRF fusion.** Add a pure helper `reciprocal_rank_fusion(ranked_lists, k=60, key=...)` that fuses the dense and sparse ranked lists by `sum(1 / (k + rank))` over the legs a row appears in, returning rows ordered by fused score. Each fused result records its fused score and which legs matched (`dense`, `sparse`, or `both`).
   - 1.5. **Rework `_search_single_table` for hybrid + fallback.** Run the dense leg (existing cosine, with `min_similarity` applied only to the dense leg now) and, when a `tsvector` column exists, the sparse leg; fuse with RRF. When the table has no `tsvector` column, run dense-only (current behavior preserved). Return the identity/text columns (excluding `vector` and `tsvector`), the fused score, and the matched-legs field.
   - 1.6. **Update `search`.** Merge per-table fused results across tables and return the global top_k. Keep the existing signature (`database`, `schema`, `query`, `top_k`, `min_similarity`, `tables`). Update the `search` Tool description (`list_tools`) to state it is hybrid (semantic + keyword).
   - 1.7. **Update the `handle_search` formatter.** It currently pops `similarity_score`/`source_table` to build the response text; the hybrid result no longer keys the same way. Surface each hit's fused score, its source table, and which legs matched (`dense`/`sparse`/`both`) in place of the old cosine-only line.

2. [completed] Update and run unit tests - `code/mcp_servers/mcp_all_db/unit_tests/test_mcp_all_db.py`
   - 2.1. Test `reciprocal_rank_fusion` as a pure function: a row in both legs outranks rows in one leg; ordering matches `1/(k+rank)`; empty legs handled.
   - 2.2. With a mocked engine/connection, assert: hybrid path issues both a dense and a sparse query when a `tsvector` column is present; dense-only when absent (fallback); `vector` and `tsvector` columns are excluded from returned rows; the matched-legs field is populated. Mock the model so no real model loads.
   - 2.3. Assert the configured model name resolves from `MCP_EMBEDDING_MODEL` with the `gte-large-en-v1.5` default.
   - 2.4. Run `uv run pytest code/mcp_servers/mcp_all_db/unit_tests/`.

3. [completed] Integration check against live embeddings - `code/mcp_servers/mcp_all_db/mcp_all_db.py`
   - 3.1. Call the `search` core function directly against `policy_db.cms_iom` (the loaded/embedded sample) with the real model. Verify: results return with a fused score and a matched-legs field; a query containing an exact term/code (e.g. a section number or acronym present verbatim in the text) surfaces the keyword-matching chunk via the sparse leg, ranked at or above where vector-only places it.
   - 3.2. Verify dense-only fallback returns results unchanged for a table without a `tsvector` column (mock or a non-FTS embedding table if available); confirm no `tsvector`/`vector` blob appears in any result.
   - 3.3. Record the before/after for one exact-term query (vector-only vs hybrid) to confirm the keyword leg adds the expected recall.

## Key Data Decisions and Considerations

1. Two correctness fixes ride along because the embedding rework broke the existing vector search: the query model drifted from ingestion (`gte-large` vs `gte-large-en-v1.5` — same 1024-dim so no error, but a different vector space, making cosine scores meaningless), and the new `chunk_tsv` `tsvector` column leaks into results. Both are fixed here regardless of hybrid.
2. Model name is env-driven (`MCP_EMBEDDING_MODEL`, default `Alibaba-NLP/gte-large-en-v1.5`) rather than hardcoded, so it tracks ingestion and the drift that just occurred cannot recur silently. `trust_remote_code=True` + `einops` (already a project dependency) are required by the model.
3. Fusion is **reciprocal rank fusion (RRF)**, not score-weighted blending. Cosine similarity and `ts_rank_cd` live on incomparable scales; RRF fuses by rank, so it needs no per-system calibration and is robust. Default `k=60` (the standard), with a per-leg candidate depth of `max(top_k, 50)` so each leg contributes enough rows to fuse before the global `top_k` cut.
4. `min_similarity` is reinterpreted as a floor on the **dense leg only** (it never mapped onto fused ranks). The sparse leg is naturally filtered by `websearch_to_tsquery` matching. Final ordering is the fused score, cut to `top_k`.
5. The server stays **generic**: the `tsvector` column is discovered (not hardcoded to `chunk_tsv`), and any embedding table lacking one degrades gracefully to vector-only — preserving today's behavior for non-FTS tables (e.g. in `proposals_db`).
6. Each result exposes its fused score and which legs matched (`dense`/`sparse`/`both`) for transparency and debuggability.
7. **Out of scope (separate follow-on): small-to-big retrieval.** Returning the parent `document_content` section for a matched chunk would help answer quality, but it is schema-specific (assumes the document/content layout) and fights the server's generic design. It belongs in a later activity, possibly as an opt-in flag or a dedicated tool. This activity returns the matched chunk rows (which already carry per-chunk headings).
8. The `search` tool signature is unchanged (no new required params); RRF depth/`k` are internal defaults to keep the tool simple. An optional `mode` (hybrid/vector/keyword) switch is a possible future addition, not included now.

## Status & Next Steps

**Current Status**: Complete — hybrid (dense + sparse, RRF-fused) `search` implemented, hermetic tests added and green, and the live check against `policy_db.cms_iom` confirms the keyword leg surfaces the exact-term chunk and no vector/tsvector blob leaks.
**Completed**:
1. Task 1: reworked `search` into hybrid retrieval in `mcp_all_db.py` — env-driven model (`MCP_EMBEDDING_MODEL`, default `Alibaba-NLP/gte-large-en-v1.5`, `trust_remote_code=True`), `_get_tsvector_columns` + `_get_primary_key_columns` helpers, the pure `reciprocal_rank_fusion` helper, dense + sparse legs fused per table with PK-tuple fusion keys, dense-only fallback routed through RRF, `min_similarity` applied to the dense leg only, vector+tsvector excluded from results, and updated tool description/formatter.
2. Task 2: added pure RRF tests, mocked hybrid/dense-only tests (both-leg vs dense-only SQL issued, vector+tsvector exclusion, matched-legs populated), and model-name resolution tests. Updated the live search assertions from `similarity_score` to `fused_score`.
3. Task 3: live check against `policy_db.cms_iom` with the real model — exact-term query `"DRG"` (1 chunk) surfaces via the sparse leg (rank 2, `matched_legs=["dense"|"sparse"]`), absent from the dense-only baseline; all hybrid rows carry `fused_score` + `matched_legs`; no vector/tsvector blob in any result.
**Next Steps**:
1. Separate task: migrate the pre-existing live test suite from the emptied `qpp_cm` schema (tables `measures`/`triggers` etc. no longer exist) to `cms_iom`; 21 pre-existing live failures are pure data drift (stash-confirmed unchanged before/after this work), not hybrid regressions.
2. Follow-ons remain small-to-big retrieval and loading/embedding the remaining corpora.
**Blockers**:
1. None.
**Notes**:
1. The server's `load_dotenv` targets `.env.mcp.all_db`, which did not exist; created it by copying `.env` (gitignored via `.env.mcp.*`) so `POSTGRES_*` resolve.
2. No data tables are produced; "output validation" is the task-3 integration check, which passed.
