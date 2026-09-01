---
name: cr-mcp_db_server
goal: Review the `mcp-search-perf` branch changes to code/mcp_db_server/mcp_db_server.py -- (1) startup model warm-up (`_warm_models_enabled` + `warm_models`, wired into `main()`) and (2) multi-schema `search` (`schema: str | list[str]`, per-(schema,table) candidate generation, single merged rerank, `source_schema` stamping) -- against the python-development and sql-development skills, plus confirmation that prior findings (v01-v03) and the recent trust_remote_code / describe_tables / instructions work are intact.
created: 2026-07-01
updated: 2026-07-01
---

## Implementation Plan

1. [pending] Docstring accuracy of the multi-schema perf claim - `code/mcp_db_server/mcp_db_server.py`
   - 1.1. [suggestion] Lines 1456-1460 (`search` docstring): the claim that a schema list "reranks the combined pool ONCE, which is much cheaper than one `search` call per schema (the cross-encoder rerank is the dominant cost)" overstates the saving. The merged rerank scores the *same total* `(query, chunk_text)` pairs as per-schema calls would (one pair per candidate either way, `pool_size` per table), so the cross-encoder compute is not reduced. The genuine wins are: a single batched `reranker.predict` call (better GPU/CPU batch utilisation than N smaller calls), one query encode instead of N, and -- most importantly for quality -- a **global** top_k cut across the merged pool rather than N independent top_k slices. Recommend rewording to describe the batching + global-cut benefit rather than "much cheaper... dominant cost", so the docstring is not making a compute-savings claim the code does not deliver. Same class of docstring-accuracy nit the prior CRs flagged (v03 1.1).

2. [pending] Warm-up entry-point coupling - `code/mcp_db_server/mcp_db_server.py`
   - 2.1. [suggestion] Lines 1912-1918 (`main`) + 1828 (`create_app`): warm-up runs only in `main()` immediately before `uvicorn.run`. `create_app(instance_name)` is a separately-exported ASGI factory; if this instance were ever served by an external runner pointing at `create_app` (e.g. `uvicorn mcp_db_server:create_app --factory`, gunicorn), warm-up would be silently bypassed and the first query would pay the cold start again. Verified: `create_app` is referenced only by its own definition and by `main()` (no deployment/Makefile/TOML factory reference), so this is a latent coupling, not an active gap at current scale. Worth a one-line note in the `warm_models`/`create_app` docstring that warm-up is a `main()`-path concern (not wired into the app factory), so a future factory-mode deployment does not lose it.

3. [pending] Warm-up failure aborts startup (acknowledged tradeoff) - `code/mcp_db_server/mcp_db_server.py`
   - 3.1. [suggestion] Lines 1915-1916 (`main`) + 640-652 (`warm_models`): `warm_models()` has no local error handling, so a model-load/inference failure (bad `MCP_EMBEDDING_MODEL`, missing weights, torch init error) propagates out of `main()` and the process never reaches `uvicorn.run` -- the instance fails to start rather than starting degraded. This is defensible (an instance that cannot load its models cannot serve `search` anyway, so failing loud at boot beats failing per-query), and the inline comment at 1912-1914 already documents that `/health` is down during the warm window. Flagging only so the fail-closed choice is a conscious one; if a degraded start (serve the non-search tools, let `search` surface the load error per-call as it did before this branch) is ever wanted, wrap the call and log-and-continue. No change required.

## Skills with No Issues

1. `search` schema normalization + validation ordering: No issues found. `schemas = [schema] if isinstance(schema, str) else list(schema)` (1493) makes a bare string the single-element list, preserving single-schema behavior. `schema=""` -> `[""]` -> `validate_sql_identifier("", "schema")` (1497) fails `fullmatch` on `[a-z_][a-z0-9_]*` (empty string cannot match a required first char) and raises. An empty **list** -> `not schemas` -> raises `"At least one schema must be provided"` (1494-1495) before any validator runs. A list containing a bad identifier raises on that element at 1496-1497. Every schema is validated before any engine work.
2. `tables` + multi-schema guard placement: No issues found. The guard (1499-1503) sits **before** the `try` block and all engine/model access, so `search(..., schema=["a","b"], tables=[...])` raises `ValueError` with zero DB round-trips. Message correctly explains that `tables` names are schema-qualified and single-schema-only.
3. Single merged rerank across all schemas: No issues found. Candidate generation loops per `(schema, table)` (1577-1589), but `reranker.predict(pairs)` (1616) is called exactly ONCE over the fully-merged `candidates` list, then a single global `sort` + `[:top_k]` cut (1621-1622). The rerank is genuinely one pass over the cross-schema pool, not per-schema.
4. `source_schema` stamping and survival: No issues found. `c["source_schema"] = s` is stamped on every candidate at 1587-1588 -- *before* rerank -- so it is present on the dicts that survive the sort/cut into `results`. `source_table` is independently stamped inside `_search_single_table` (line 1431), so both keys coexist on each result. The `Returns` docstring (1479-1483) lists `source_schema` alongside `source_table`.
5. Result summary log correctness: No issues found. `schemas_with_hits` (1624) is the distinct set of result schemas; `tables_with_hits` (1625-1627) keys on the `(source_schema, source_table)` PAIR, so a same-named table in two schemas is counted as two tables (not collapsed). The log "N results across T table(s) in S schema(s)" (1628-1632) is accurate for the multi-schema case and correct for the single-schema case (S=1).
6. Backward-compatibility of `schema="cms_iom"` (string): No issues found. A string flows through `[schema]` (1493) -> single-element `schemas` -> single `(schema, table)` pairs -> `source_schema` stamped as before. Auto-discovery, dimension guard, candidate generation, and rerank are byte-identical to the pre-branch single-schema path; the only added output field is `source_schema`, which a single-schema caller previously did not receive (additive, non-breaking).
7. Injection / identifier safety on the multi-schema paths: No issues found. Every schema is validated at 1496-1497; every explicit `table` is validated at 1516-1517 (per schema in the resolve loop) and re-validated at 1532 in the dimension-guard loop; auto-discovered tables come from `_discover_embedding_tables` (catalog-sourced). All identifiers reaching `_get_vector_columns`, `_get_vector_dimension`, and `_search_single_table` are pre-validated names or catalog column names -- the string-interpolation sites are unchanged from prior passes and receive only validated input.
8. Dimension guard per `(schema, table)`: No issues found. `model_dim` is computed once (1508); the guard loops per pair (1531-1551), raising a schema-qualified `ValueError` on the no-vector-column case (1534-1541) and on dimension mismatch (1544-1551), and skipping the guard for an empty table (`table_dim is None`). Error messages now carry the correct `s.table` qualification.
9. `_warm_models_enabled` parse: No issues found. `os.environ.get("MCP_WARM_MODELS", "true").strip().lower() not in {"0","false","no","off"}` (631-636) defaults True (unset -> "true" -> not in falsy set), disables on the documented falsy tokens, is whitespace-tolerant via `.strip()`, and case-insensitive via `.lower()`. Any other value (e.g. "1", "yes") warms -- a safe default-on bias for a warm-up feature.
10. `warm_models` hits both singletons: No issues found. `get_embedding_model().encode("warmup")` (650) forces the embedding singleton load + one encode; `get_reranker().predict([("warmup query","warmup document")])` (651) forces the reranker singleton load + one predict. Both go through the double-checked lazy getters, so the warmed instances are the exact singletons `search` later reuses. Log lines bracket the work (649, 652).
11. `main()` warm-up placement: No issues found. Warm-up (1915-1918) runs after `load_dotenv` (1895) -- so `MCP_EMBEDDING_MODEL`/`MCP_RERANK_MODEL`/`MCP_WARM_MODELS` are in the environment -- and after `setup_logging` (1903-1905) -- so the warm/skip lines are captured -- and before `create_app`/`uvicorn.run` (1920-1921), so serving does not begin until models are ready. The skip branch logs an explicit reason (1918). The inline comment (1912-1914) documents that `/health` is unavailable during the warm window, which is acceptable: an instance whose models are not loaded is not truly ready, so a liveness probe timing out during warm-up correctly reflects not-ready.
12. Widened `schema: str | list[str]` and FastMCP registration: No issues found. The tool registration (`mcp.tool()(search)`, line 1811) is unchanged and requires no change: FastMCP derives the input JSON schema from the type hint via pydantic, and a `str | list[str]` union renders as a standard `anyOf` -- no manual schema edit is needed for the widened annotation. (Concluded from the unchanged registration + standard pydantic union handling; the emitted schema was not inspected against a live server.)
13. Type hints / docstrings on new + changed code: No issues found. `_warm_models_enabled() -> bool` and `warm_models() -> None` carry full hints and Google-style docstrings (Returns / body rationale). `search`'s signature updates `schema` to `str | list[str]`, `schema_tables: list[tuple[str, str]]` is annotated (1513), and the docstring's Args/Returns/Raises are all updated for the multi-schema contract (the `Raises` now correctly enumerates empty-schema, bad-identifier, `tables`+multi-schema, no-tables, and dimension-mismatch).
14. SQL best-practices: No issues found. This branch adds no new SQL statements; it re-plumbs existing helpers (`_discover_embedding_tables`, `_get_vector_columns`, `_get_vector_dimension`, `_search_single_table`) over a `(schema, table)` list. Their parameter-bound, lowercase-keyword queries are unchanged and were reviewed in prior passes.

## Prior findings intact

The branch diff touches only `search()` and adds `_warm_models_enabled` / `warm_models`; every other function is structurally untouched, so their prior findings (v01/v02/v03) carry over by construction. Spot-checked the two prior findings that live **inside** `search`:

1. **v01 2.1 (reranker empty-`chunk_text` warning)** -- Intact. The empty/missing `chunk_text` counter + WARNING survives at lines 1602-1615, still retaining `str(text_value or "")` so an empty chunk scores against an empty string rather than crashing.
2. **v01 3.1 / dimension guard** -- Intact and extended: the guard now runs per `(schema, table)` (1531-1551) with the same clear mismatch and no-vector-column messages, schema-qualified.
3. **v03 1.1-4.1 (list_tables stale `Raises`, `_get_foreign_keys` docs/guard, describe_tables N+1 note + in-`try` engine resolution, error-log coverage)** -- Not touched by this diff; those functions are unchanged on the branch.
4. **Recent trust_remote_code / describe_tables / instructions work** -- Intact and unregressed: `_trust_remote_code` (520) still feeds `SentenceTransformer(..., trust_remote_code=_trust_remote_code())` (567); `describe_tables` (1125) and its plural/enrichment contract are unchanged; `get_instructions()` (161) still supplies `FastMCP(instructions=...)` at 1800. The tool registration block (1806-1811) still registers all six tools including `describe_tables`.

## Strengths

1. The multi-schema design is the right shape: candidate generation fans out per `(schema, table)` but the expensive cross-encoder rerank is a single batched `predict` over the merged pool with a global top_k cut -- correct both for batch efficiency and for cross-schema relevance (a strong hit in schema B can outrank a weak hit in schema A, which N independent per-schema calls could not achieve).
2. `source_schema` is stamped on every candidate before the rerank, so attribution survives the sort/cut with zero special-casing, and the summary log keys on the `(schema, table)` pair so identically-named tables across schemas are not miscounted.
3. Validation discipline is preserved and extended: empty-string, empty-list, and bad-identifier schemas all fail fast before any DB work, and the `tables`+multi-schema combination is rejected up front with an explanatory message -- the same fail-loud, injection-safe posture the module maintains throughout.
4. Backward compatibility is clean: a bare-string `schema` normalizes to a single-element list and takes a path byte-identical to the pre-branch behavior, adding only the additive `source_schema` field.
5. `_warm_models_enabled` / `warm_models` are small, well-documented, correctly wired after `load_dotenv`+`setup_logging` and before serving, and route through the exact lazy singletons `search` reuses -- so the warm-up genuinely eliminates the first-query cold start, and the `/health`-down-during-warm tradeoff is documented inline.

## Status & Next Steps

**Current Status**: Reviewed the `mcp-search-perf` branch changes to `search` (multi-schema) and the new warm-up feature. No critical or major findings -- the diff is clean, injection-safe, backward-compatible, and the merged-rerank + global-cut design is correct. Three [suggestion]-level items: the docstring "much cheaper" perf claim overstates a compute saving the code does not deliver (1.1); warm-up is coupled to the `main()` path and not the `create_app` factory (2.1, latent only -- no factory-mode deployment exists today); and warm-up failure aborts startup (3.1, an acknowledged fail-closed tradeoff). Prior v01/v02/v03 findings and the recent trust_remote_code / describe_tables / instructions work are intact. `uv run pytest code/mcp_db_server/unit_tests/ -q` -> 73 passed.

**Completed**:
1. Reviewed the multi-schema `search` normalization, guard placement, per-pair candidate generation, single merged rerank, `source_schema` stamping, and summary log.
2. Reviewed `_warm_models_enabled` / `warm_models` and their `main()` wiring.
3. Confirmed no FastMCP registration change is needed for the widened `schema` union.
4. Confirmed prior findings and the recent trust_remote_code / describe_tables / instructions work are unregressed.
5. Ran the unit suite (73 passed) and removed `.coverage`.

**Next Steps**:
1. Optional: reword the `search` docstring perf claim (1.1) and add a one-line warm-up-is-`main()`-path note (2.1).

**Blockers**:
1. None.

**Notes**:
1. Severity tags use the repo's `[critical]/[major]/[minor]/[suggestion]` convention.
2. The test file (`unit_tests/test_mcp_db_server.py`) is covered by a separate review per repo convention and was not reviewed here.
3. FastMCP union-schema handling (12) was concluded from the unchanged registration + standard pydantic `anyOf` semantics; the emitted schema was not inspected against a live server.

## Resolution (2026-07-01)

- [suggestion 1] search docstring perf claim — **reworded** to the accurate framing (one encode, one batched rerank pass, GLOBAL top_k; not less rerank compute).
- [suggestion 2] warm-up main()-only coupling — **noted** in warm_models docstring (factory-mode would warm lazily).
- [suggestion 3] warm-up failure aborts startup — **noted** as an intentional fail-closed choice in the main() comment.
