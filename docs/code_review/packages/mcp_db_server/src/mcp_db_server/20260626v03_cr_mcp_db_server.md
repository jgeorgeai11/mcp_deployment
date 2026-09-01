---
name: cr-mcp_db_server
goal: Third-pass (v03) review of code/mcp_db_server/mcp_db_server.py, focused on the new code added in commit 8bfb70e -- the search no-vector-column raise, the describe_table -> describe_tables (plural) refactor with is_primary_key / references enrichment, and the new helpers _get_foreign_keys, _list_table_names, _describe_one_table -- against the python-development and sql-development skills, plus confirmation that the v01/v02 findings stayed fixed.
created: 2026-06-26
updated: 2026-06-26
---

## Implementation Plan

1. [completed] Docstring accuracy - `code/mcp_db_server/mcp_db_server.py`
   - 1.1. [minor] Lines 918-923 (`list_tables` `Raises:`): the `Raises` block is copy-pasted from `search` and describes conditions that do not exist in `list_tables`. The function validates only `database` (line 924) and `schema` (line 925) and runs a single `pg_class` query; it never validates a `table`, has no embedding/vector logic, and no dimension guard. The docstring claiming it raises on "the schema/table contains unsafe characters; no embedding tables are found; an explicitly requested table has no embedding (vector) column; or the query model's dimension does not match a table's vector dimension" is misleading.
        - Current:
          ```python
          Raises:
              ValueError: If the database is not served; the schema/table contains
                  unsafe characters; no embedding tables are found; an explicitly
                  requested table has no embedding (vector) column; or the query
                  model's dimension does not match a table's vector dimension.
          ```
        - Expected:
          ```python
          Raises:
              ValueError: If the database is not served or the schema name
                  contains unsafe characters.
          ```
        - Rationale: docstrings skill -- the `Raises` section must match the actual behavior; a wrong list misleads callers about the failure surface of the tool.

2. [completed] Foreign-key map shape limitations - `code/mcp_db_server/mcp_db_server.py`
   - 2.1. [suggestion] Lines 677-685 (`_get_foreign_keys`): the return type `dict[str, str]` keys on the constrained column name, so a column that participates in **two** foreign keys is silently overwritten -- only the last FK iterated survives in `fk_map[col]`. This is rare (a single column rarely sits in two distinct FK constraints) and the composite-FK split itself is correct (`zip(constrained_columns, referred_columns)` is positionally paired per the SQLAlchemy inspector contract). Worth a one-line note in the docstring that the map keeps one reference per column (the last constraint wins) so the limitation is explicit.
        - Current: docstring says "for every FK column on the table" with no mention of the single-reference-per-column constraint.
        - Expected: add a sentence such as "When a column participates in more than one foreign key, the last constraint encountered wins (one reference per column)."
        - Rationale: comments/docstrings skill -- make a known shape limitation visible rather than silently lossy.
   - 2.2. [suggestion] Line 684 (`_get_foreign_keys`): `ref_table = fk.get("referred_table")` is interpolated into the reference string without a None guard, so a malformed/partial FK reflection would render `"schema.None.column"`. SQLAlchemy's inspector always populates `referred_table` for a real FK, so this is defensive only; a small guard (skip the FK when `ref_table` is falsy) would avoid the misleading literal in any pathological reflection.
        - Rationale: defensive-input skill -- avoid emitting a `None`-stringified reference; low likelihood, hence suggestion.

3. [completed] Per-table connection/inspector fan-out - `code/mcp_db_server/mcp_db_server.py`
   - 3.1. [suggestion] Lines 1064-1094 (`describe_tables`) + 1003-1012 (`_describe_one_table`): default-all mode issues `1 + 3N` round-trips for N discovered tables -- one `_list_table_names` query, then per table a columns query (`_describe_one_table`, its own `engine.connect()`), a `_get_primary_key_columns` (`inspect()`), and a `_get_foreign_keys` (`inspect()`). Each opens its own pooled connection/inspector. This is acceptable for an internal, role-scoped server over a modest schema, but is a textbook N+1 that would grow linearly on a large schema. If describe-all latency ever matters, the PK/FK lookups could be batched per schema (single `information_schema.table_constraints`/`key_column_usage` queries) instead of per table.
        - Rationale: SQL best-practices -- flag the N+1 so it is a conscious tradeoff, not a surprise; no change required at current scale.

4. [completed] Error-logging coverage - `code/mcp_db_server/mcp_db_server.py`
   - 4.1. [suggestion] Lines 1059-1080 (`describe_tables`): `_validate_database`, `validate_sql_identifier`, `get_database_engine`, and `_list_table_names` run **outside** the `try` that carries the "Failed to describe tables" error log (line 1103). A `SQLAlchemyError` raised by `_list_table_names` (default-all mode) therefore propagates without the module's error log, unlike `list_tables`, whose engine + query sit inside its `try`. Behavior is correct (the exception still propagates); only the diagnostic log is missed on that one path. Either move the `engine`/`_list_table_names` resolution inside the `try`, or accept the minor inconsistency.
        - Rationale: logging skill -- consistent error-path logging across the sibling tools.

## Skills with No Issues

1. `search` no-vector-column raise (placement + reachability): No issues found. The raise (lines 1402-1409) fires inside the dimension-guard loop **before** candidate generation (the `_search_single_table` loop at line 1444), so an explicitly requested non-searchable table fails fast with an actionable message instead of silently contributing zero candidates. It genuinely cannot trigger for auto-discovered tables: `_discover_embedding_tables` (lines 756-765) filters on `udt_name = 'vector'`, so every discovered table has a vector column by construction -- the error message's parenthetical states exactly this. Correct change from the prior silent skip.
2. `_get_foreign_keys` composite pairing + cross-schema: No issues found. `zip(constrained_columns, referred_columns)` (line 683) is the correct positional split for a composite FK per the SQLAlchemy inspector contract (the two lists are index-aligned). A foreign key whose `referred_schema` differs from the current schema is handled by `fk.get("referred_schema") or schema` (line 679), so a cross-schema reference renders with the real target schema, and a same-schema FK (where the inspector may return `None` for `referred_schema`) falls back to the local schema. (Single-reference-per-column and `referred_table` None are noted as suggestions 2.1/2.2.)
3. `inspect()` under the read-only role: No issues found. `_get_primary_key_columns` and `_get_foreign_keys` use the SQLAlchemy `inspect()` path, which reads `pg_constraint`/`pg_attribute`/`pg_class`. Those catalogs are world-readable in PostgreSQL (visibility is not gated by table privileges), so PK/FK reflection succeeds under the scoped read-only role without any extra grant.
4. `_list_table_names` privilege filter parity: No issues found. The query (lines 732-740) byte-matches the `list_tables` table filter -- `relkind = 'r'` (ordinary tables only), `has_table_privilege(current_user, c.oid, 'SELECT')`, `order by c.relname`, `:schema` bound -- so default-all `describe_tables` describes exactly the set `list_tables` would list, in the same order. An empty schema yields `[]` -> `describe_tables` returns `{}` (not an error), consistent with `list_tables` returning `[]`.
5. `describe_tables` raise-vs-skip semantics: No issues found and intentional. An explicitly requested table that yields no columns RAISES `ValueError("Table {schema}.{table} not found or has no columns")` (lines 1089-1092), while a discovered-empty table is SKIPPED (`continue`, line 1093). This is the right asymmetry: the caller named the explicit table, so its absence is an error; a discovered table that vanished/has no columns between listing and describing is a benign skip. The `explicit` flag (lines 1072, 1075) cleanly drives the branch.
6. Injection / identifier safety on the new paths: No issues found. `schema` is validated via `validate_sql_identifier` in both `describe_tables` (line 1060) and `search` (line 1376); every explicit `table` is validated (`describe_tables` lines 1069-1070; `search` lines 1384-1385 and again at 1400). `_list_table_names` binds `:schema`; `_describe_one_table`'s columns query binds `:schema`/`:table`; `_get_table_columns`/`_get_vector_columns`/`_get_tsvector_columns` all bind their identifiers as parameters. The only string-interpolated identifiers (`_get_vector_dimension`, `_search_single_table`) use pre-validated names or catalog-sourced column names, unchanged since v01.
7. Type hints / docstrings on new code: No issues found. `_get_foreign_keys`, `_list_table_names`, `_describe_one_table`, and `describe_tables` all carry full type hints and Google-style Args/Returns/Raises sections. The `describe_tables` `Raises` block (lines 1053-1057) is accurate (database-not-served, unsafe schema/table, explicit-table-no-columns) -- contrast with the stale `list_tables` block (Finding 1.1).
8. Return-shape change (describe_table -> describe_tables): No issues found. The plural rename returns `dict[table -> list[column dict]]` with each column dict gaining `is_primary_key` and `references`; the docstring documents all five fields. This is a deliberate contract change (singular -> plural, list -> dict) consistent with the discover-then-query flow described in the module docstring (line 6, updated to `describe_tables`).
9. SQL style of new statements: No issues found. `_list_table_names` and `_describe_one_table` use lowercase keywords, explicit `join`, parameter binds, and `order by` for deterministic output; `_describe_one_table` resolves USER-DEFINED types to `udt_name` via a `case` (matching the vector/tsvector helpers), so a pgvector column reports `vector` rather than the opaque `USER-DEFINED`.

## v01/v02 findings confirmed fixed

1. **v01 1.1 (event-loop blocking)** -- Confirmed: all six tools are plain `def` (`list_databases` 838, `list_schemas` 862, `list_tables` 905, `describe_tables` 1026, `run_sql` 1108, `search` 1332); the section comment (lines 829-835) documents the sync-in-threadpool rationale. No `async def` tool, no `asyncio` import.
2. **v01 1.2 (singleton races)** -- Confirmed: three module-scope `threading.Lock`s (`_engines_lock`, `_embedding_model_lock`, `_reranker_lock`, lines 232-234) each guard a double-checked lazy init (`get_database_engine` 281-283, `get_embedding_model` 500-502, `get_reranker` 547-549).
3. **v01 1.3 (fresh served-set)** -- Confirmed: no `_served_databases` global; `get_served_databases()` (lines 427-441) queries fresh via `_fetch_databases()` every call, so `list_databases` and `_validate_database` always agree.
4. **v01 2.1 (reranker chunk_text warning)** -- Confirmed: `_RERANK_TEXT_COLUMN = "chunk_text"` invariant documented (lines 208-216); `search` counts empty/missing `chunk_text` candidates and logs a WARNING (lines 1469-1482) while retaining `or ""`.
5. **v01 3.1 (granite default + dimension guard)** -- Confirmed: `_DEFAULT_EMBEDDING_MODEL` is the granite model (line 198); the dimension guard (lines 1396-1421) raises a clear `ValueError` on mismatch and skips empty tables; reranker docstrings reference `BAAI/bge-reranker-base` (no stale Ettin text).
6. **v01 4.1 (run_sql guard)** -- Confirmed: docstring (lines 1118-1123) and inline comment (lines 1140-1145) state read-only is enforced solely by the role and the `;` check is a multi-statement guard with an accepted in-literal false-positive.
7. **v02 1.1/1.2 (`_env_int` clamp)** -- Confirmed: `_env_int` takes `minimum: int | None` and clamps with a WARNING (lines 136-179); `minimum=1` applied to `MCP_STATEMENT_TIMEOUT_S`/`MCP_MAX_ROWS` (1159-1164), `MCP_RERANK_POOL` (`_rerank_pool`, 192), `MCP_HNSW_EF_SEARCH` (323-327), `MCP_DB_POOL_SIZE` (308-312); `minimum=0` to `MCP_DB_MAX_OVERFLOW` (313-317).
8. **v02 2.1 (connect-event cursor)** -- Confirmed: the warm-up cursor is context-managed (`with dbapi_connection.cursor() as cur:`, lines 350-354), so it closes on the non-vector-DB error path; the outer `try/except` degrades to DEBUG.

## Strengths

1. The `search` no-vector-column raise is a clean, honest upgrade from a silent skip: it fires before any candidate work, names the table, tells the caller exactly how to fix it, and documents (in the message itself) why it can only happen for an explicitly requested table.
2. `_describe_one_table` is well-factored: it returns `[]` for a no-column table **before** touching the PK/FK helpers, leaving the raise-vs-skip policy entirely in the caller -- a clean separation that makes the explicit/discovered asymmetry easy to read.
3. The PK/FK enrichment uses the SQLAlchemy inspector for the catalog-shape-sensitive parts (composite PK ordering, composite FK column pairing, cross-schema targets) rather than hand-rolled catalog SQL, which is exactly where the inspector earns its keep.
4. `_list_table_names` deliberately reuses the `list_tables` privilege filter verbatim, so default-all `describe_tables` cannot drift from what `list_tables` advertises -- the same role-derived discipline applied throughout the module.
5. The new helpers carry complete type hints and Google-style docstrings, and the `describe_tables` docstring accurately enumerates all five column fields and the explicit-vs-discovered raise/skip rule.

## Status & Next Steps

**Current Status**: All v03 findings addressed. The [minor] `list_tables` stale `Raises` is rewritten to the accurate condition; `_get_foreign_keys` documents the one-reference-per-column (last-wins) limitation and guards a falsy `referred_table`; `describe_tables` carries a one-line N+1 tradeoff comment and now resolves the engine + default-all listing INSIDE the `try` so a `SQLAlchemyError` there is logged by the existing handler (arg validation kept up front, schema-before-table order preserved). No critical or major findings. `uv run pytest code/mcp_db_server/unit_tests/ -q` -> 66 passed (mocked; no live server/DB).

**Completed**:
1. Reviewed the new `search` no-vector-column raise, the `describe_tables` plural refactor, and the new helpers `_get_foreign_keys`, `_list_table_names`, `_describe_one_table`.
2. Confirmed all v01 and v02 findings remain fixed.
3. Addressed findings 1.1, 2.1, 2.2, 3.1, 4.1.
4. Ran the unit suite (66 passed).

**Next Steps**:
1. None.

**Blockers**:
1. None.

**Notes**:
1. Severity tags use the repo's `[critical]/[major]/[minor]/[suggestion]` convention.
2. The test file (`unit_tests/test_mcp_db_server.py`) is covered by a separate review per repo convention and was not reviewed here.
3. `inspect()`-based PK/FK reflection works under the read-only role because PostgreSQL system catalogs (`pg_constraint`, `pg_attribute`, `pg_class`) are world-readable.
