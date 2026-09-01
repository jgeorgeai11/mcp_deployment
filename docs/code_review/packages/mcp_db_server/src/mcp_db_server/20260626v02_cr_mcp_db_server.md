---
name: cr-mcp_db_server
goal: Second-pass (v02) review of code/mcp_db_server/mcp_db_server.py, focused on the six new env-overridable tuning knobs added in commit 36e5926 (MCP_HNSW_EF_SEARCH connect-event handler, MCP_RERANK_POOL, MCP_MAX_ROWS, MCP_STATEMENT_TIMEOUT_S, MCP_DB_POOL_SIZE, MCP_DB_MAX_OVERFLOW, MCP_LOG_LEVEL) against the python-development and sql-development skills, and confirmation that the v01 findings stayed fixed.
created: 2026-06-26
updated: 2026-06-26
---

## Implementation Plan

1. [completed] Tuning-knob semantic validation - `code/mcp_db_server/mcp_db_server.py`
   - Resolution: `_env_int` now takes `minimum: int | None = None` and clamps the resolved value (override or default) up to the floor with a WARNING at a single return point. Applied `minimum=1` to `MCP_STATEMENT_TIMEOUT_S`, `MCP_MAX_ROWS`, `MCP_RERANK_POOL` (in `_rerank_pool`), `MCP_HNSW_EF_SEARCH`, and `MCP_DB_POOL_SIZE`; `minimum=0` to `MCP_DB_MAX_OVERFLOW`. All `_DEFAULT_*` constants already sit above their floors, so default behaviour is unchanged.
   - 1.1. [completed] [major] Lines 136-161 (`_env_int`) + lines 1006-1016 (`run_sql`): `_env_int` guards against injection (it parses to `int`) and against non-integer junk (falls back + warns), but it accepts `0` and negatives. The most consequential case is `MCP_STATEMENT_TIMEOUT_S=0`, which interpolates to `set local statement_timeout = '0s'` — in PostgreSQL `0` means **no timeout**, silently defeating the run_sql safety guard that the docstring promises ("Statement timeout ... default 5"). A negative also yields `'-5s'`, which Postgres rejects, raising mid-query. The same unbounded-below problem affects `MCP_MAX_ROWS=0` (run_sql returns zero rows and reports `truncated=True`), `MCP_RERANK_POOL<=0` (empty candidate pool / `limit 0`), and `MCP_DB_MAX_OVERFLOW<0`.
        - Current: `def _env_int(name: str, default: int) -> int:` returns any parsed int, including `0`/negatives; callers apply no floor.
        - Expected: add an optional floor and clamp-with-warning, e.g. `def _env_int(name: str, default: int, minimum: int | None = None) -> int:` that, after parsing, does `if minimum is not None and value < minimum: logger.warning(...); return minimum`. Call it with `minimum=1` for `MCP_STATEMENT_TIMEOUT_S`, `MCP_MAX_ROWS`, `MCP_RERANK_POOL`, and `MCP_HNSW_EF_SEARCH` (see 1.2); `minimum=0` for `MCP_DB_MAX_OVERFLOW`.
        - Rationale: data-validation / defensive-input skills — a knob that silently turns off a documented safety guard (timeout) is a footgun; int-guarding stops injection but not unsafe values.
   - 1.2. [completed] [minor] Lines 290-296 (`MCP_DB_POOL_SIZE`): `pool_size` flows straight into SQLAlchemy's QueuePool. `pool_size=0` there means **no limit** (unbounded pooled connections), not "tiny pool" — an easy way to exhaust Postgres connection slots by misconfiguration. Negative values raise at engine creation. Clamp to `>= 1` via the `minimum` argument from 1.1.
        - Current: `pool_size=_env_int("MCP_DB_POOL_SIZE", _DEFAULT_DB_POOL_SIZE)`.
        - Expected: `pool_size=_env_int("MCP_DB_POOL_SIZE", _DEFAULT_DB_POOL_SIZE, minimum=1)`.
        - Rationale: same class as 1.1; the `pool_size=0` = unbounded semantic is a sharper footgun than the others, so worth an explicit floor even though it is unlikely to be set by accident.

2. [completed] Connect-event handler resource cleanup - `code/mcp_db_server/mcp_db_server.py`
   - Resolution: the warm-up cursor is now context-managed (`with dbapi_connection.cursor() as cur:`), so it is closed on every path -- including when `select '[1]'::vector` raises on a non-vector DB -- while the outer `try/except` keeps degrading to DEBUG.
   - 2.1. [completed] [minor] Lines 322-332 (`_set_hnsw_ef_search`): the cursor is opened, used, and closed inside a single `try`, but `cur.close()` (line 326) is the last statement before the `except`. When the warm-up `cur.execute("select '[1]'::vector")` raises (the expected path for a database without the `vector` extension), control jumps to `except` and `cur.close()` is **skipped**, leaking the cursor on every physical connection to a non-vector DB. The leak is bounded (the connect event fires once per physical DBAPI connection, so at most `pool_size + max_overflow` open cursors, re-leaking only on pool recycle) and most DBAPI drivers reclaim the cursor when the connection is later returned/closed — so this is minor, not a runaway — but it is still an avoidable leak on the error path.
        - Current:
          ```python
          try:
              cur = dbapi_connection.cursor()
              cur.execute("select '[1]'::vector")
              cur.execute(f"set hnsw.ef_search = {int(ef_search)}")
              cur.close()
          except Exception as exc:
              logger.debug(...)
          ```
        - Expected: guarantee closure with a context manager (or `try/finally`):
          ```python
          try:
              with dbapi_connection.cursor() as cur:
                  cur.execute("select '[1]'::vector")
                  cur.execute(f"set hnsw.ef_search = {int(ef_search)}")
          except Exception as exc:
              logger.debug(...)
          ```
        - Rationale: exception-handling skill — release resources on every path; the `try/except` currently scopes the body correctly but not the cleanup.

## Skills with No Issues

1. Injection safety of new interpolations: No issues found. `set local statement_timeout = '{int(timeout_s)}s'` (line 1015) and `set hnsw.ef_search = {int(ef_search)}` (line 325) both wrap the value in `int(...)`, and the value already comes from `_env_int` (int-typed). Both interpolations are injection-safe; the concern with 0/negative values (Finding 1.1) is a *semantic* one, not an injection one.
2. Listener registration correctness: No issues found. `@event.listens_for(engine, "connect")` registers `_set_hnsw_ef_search` inside the `if database not in _engines:` block under `_engines_lock` with a double-checked re-check (lines 263-265), so exactly one listener is attached per cached engine — no duplication, no leak across calls. The handler closes over the creation-time `ef_search` (read at lines 301-303, after `load_dotenv`) and `database`, which is correct. The `try/except` scope wraps only the per-connection body, so a non-vector DB does not fail to connect (only the cursor-cleanup detail in Finding 2.1 is off).
3. Use-time vs import-time reads: No issues found. Every knob is read inside a function body (`_env_int` at call time, `_rerank_pool`, `run_sql`, `get_database_engine`, `main`), never at module import — correct, since the per-instance `.env.mcp.<name>` is loaded by `load_dotenv` in `main()` after import. The `_DEFAULT_*` constants are the reviewable fallbacks. Cached-engine implication (acceptable, noted): `MCP_DB_POOL_SIZE`, `MCP_DB_MAX_OVERFLOW`, and `MCP_HNSW_EF_SEARCH` are read at engine **creation** and are then frozen for the life of that cached engine — changing those env vars after first use of a database has no effect until process restart. This is acceptable for a per-instance server whose env file is fixed at launch, but is worth an operator note; `MCP_MAX_ROWS`/`MCP_STATEMENT_TIMEOUT_S`/`MCP_RERANK_POOL` are re-read on every call and do not have this property.
4. `MCP_LOG_LEVEL` parsing robustness: No issues found / acceptable. `logging.getLevelName(name.upper())` returns an `int` for a known level name and a `"Level X"` string otherwise; the `isinstance(level, int)` guard (lines 1588-1589) falls back to `logging.INFO` for any unknown name. Numeric strings (e.g. `MCP_LOG_LEVEL=10`) are not honored — `getLevelName("10")` returns the string `"Level 10"`, which fails the int check and falls to INFO. This is an acceptable, fail-safe limitation (defaults to INFO rather than erroring); worth a one-line docstring note if numeric levels are ever expected.
5. Docstring "Configuration" section accuracy: No issues found. All seven new entries in the module docstring (lines 44-54) match the constants exactly: `MCP_MAX_ROWS`=500 (`_DEFAULT_MAX_ROWS`, line 111), `MCP_STATEMENT_TIMEOUT_S`=5 (line 115), `MCP_RERANK_POOL`=50 (line 120), `MCP_DB_POOL_SIZE`=5 (line 124), `MCP_DB_MAX_OVERFLOW`=10 (line 125), `MCP_HNSW_EF_SEARCH`=100 (line 130), `MCP_LOG_LEVEL`=INFO (line 133). The "read at use-time" wording (lines 44-45) is accurate.
6. Type hints / docstrings on new code: No issues found. `_env_int`, `_rerank_pool`, and the nested `_set_hnsw_ef_search` all carry full type hints and Google-style docstrings; `_set_hnsw_ef_search` documents its `dbapi_connection`/`connection_record` args.
7. Logging on new code: No issues found. `_env_int` warns (not errors) on non-integer input; the connect handler logs at DEBUG on the tolerated non-vector path. All use `logconfig` logger + f-strings, consistent with the module.
8. SQL style of new statements: No issues found. The warm-up `select '[1]'::vector` and the two `set` statements are lowercase and minimal; `statement_timeout` uses `set local` (transaction-scoped, correct for the per-call connection).

## v01 findings confirmed fixed

1. **1.1 (event-loop blocking)** — Confirmed: the six tools are plain `def` (lines 753, 777, 820, 882, 955, 1177), and the section comment (lines 744-751) documents the sync-in-threadpool rationale. No `async def` tool, no `asyncio` import.
2. **1.2 (singleton races)** — Confirmed: three module-scope `threading.Lock`s (`_engines_lock`, `_embedding_model_lock`, `_reranker_lock`, lines 214-216) each guard a double-checked lazy init (`get_database_engine` lines 263-265, `get_embedding_model` lines 472-474, `get_reranker` lines 519-521).
3. **1.3 (cache divergence / fresh served-set)** — Confirmed: no `_served_databases` global remains; `get_served_databases()` (lines 399-413) queries fresh via `_fetch_databases()` on every call, so `list_databases` and `_validate_database` always agree and a GRANT/REVOKE is reflected immediately.
4. **2.1 (reranker chunk_text warning)** — Confirmed: `_RERANK_TEXT_COLUMN = "chunk_text"` invariant is documented (lines 190-198); `search` counts empty/missing `chunk_text` candidates and logs a WARNING (lines 1308-1321) while retaining `or ""` so the reranker does not break.
5. **3.1 (granite default + dimension guard)** — Confirmed: `_DEFAULT_EMBEDDING_MODEL = "ibm-granite/granite-embedding-small-english-r2"` (line 180); the dimension guard in `search` (lines 1241-1260) raises a clear `ValueError` on mismatch and skips empty tables. The reranker docstrings reference `BAAI/bge-reranker-base` (lines 494-502, 505-514), no stale Ettin text.
6. **4.1 (run_sql comment / security model)** — Confirmed: the docstring (lines 965-969) and inline comment (lines 987-998) state read-only is enforced solely by the `mcp_ro_policy` role and that the `;` check is a multi-statement guard with an accepted in-literal false-positive.

## Strengths

1. The connect-event handler is registered cleanly: one listener per cached engine, created under the engine-cache lock, closing over the creation-time resolved `ef_search` and `database` — exactly the pattern that avoids duplicate/leaked listeners.
2. Defaults are reviewable code constants (`_DEFAULT_*`) with comments explaining each value's intent (e.g. ef_search >= rerank pool so the dense leg is not recall-starved), and the use-time read is correctly justified against `load_dotenv` ordering.
3. Injection is handled correctly on both new interpolations (`int(...)` wrap + already-int source), keeping the SQL-literal knobs safe.
4. The pgvector warm-up + tolerate-non-vector-DB design is thoughtful (the GUC is registered lazily, so the warm-up cast is genuinely necessary), and degrading at DEBUG rather than failing the connection is the right call for a multi-database role.
5. The docstring "Configuration" section is fully accurate against the constants — a common drift point that is correct here.

## Status & Next Steps

**Current Status**: All three findings fixed. `_env_int` now clamps below a per-knob floor with a WARNING (M1/M2) and the warm-up cursor is context-managed (M3). `uv run pytest code/mcp_db_server/unit_tests/ -q` -> 57 passed (mocked; no live server/DB).

**Completed**:
1. Reviewed all six new env knobs + the `_env_int` helper and the connect-event handler.
2. Confirmed the six v01 findings remained fixed.
3. Ran the unit suite (50 passed).

**Next Steps**:
1. None -- all findings resolved.

**Blockers**:
1. None.

**Notes**:
1. Severity tags use the repo's `[critical]/[major]/[minor]/[suggestion]` convention.
2. The test file (`unit_tests/test_mcp_db_server.py`) is covered by a separate review per repo convention and was not reviewed here.
3. The cached-engine freeze of pool/ef_search knobs (point 3 under Skills with No Issues) is acceptable for this launch-fixed-env design but is the one behavioral subtlety an operator should know.
