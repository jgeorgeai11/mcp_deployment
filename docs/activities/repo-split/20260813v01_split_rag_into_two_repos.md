---
name: 20260813v01_split_rag_into_two_repos
goal: Split the `rag` monorepo into two history-preserving repos — `ingestion_pipeline` (acquisition, ingestion, embedding) and `mcp_deployment` (the read-only MCP service plus its Postgres grants/descriptions) — so the long-running server stops inheriting the document-parsing dependency stack and its `transformers<5` / `torch<2.11` reproducibility pin. Before carving, remove the two couplings that would break both repos: the untracked `.claude/` logconfig import and the cross-module `importlib` load of `file_ingestion/_utils.py`.
created: 2026-08-13 10:19:06
updated: 2026-08-13 12:35:00
---

## Implementation Plan

### Phase 1 — Decouple in place

1. [completed] Create the vendored shared logging package - `code/lib/logconfig/logconfig.py`
   - 1.1. Copy the package from either source — the 2026-08-13 `.claude/` overhaul left this repo's `.claude/skills/python-development/scripts/logconfig/` byte-identical to `metadata_db`'s tracked `metadata_db\code\lib\logconfig\` (verified by diff of both files). Both already use the non-deprecated `pythonjsonlogger.json.JsonFormatter` and carry full type hints and Google-style docstrings, so this is a straight copy, not a port; subtasks 1.3-1.6 are verification checks on the copied code
   - 1.2. Keep the package layout as copied — directory `code/lib/logconfig/` with `logconfig.py` and `__init__.py` — so `from logconfig import get_logger` continues to resolve once `code/lib` is on `sys.path`, keeping the change at each of the 27 call sites down to the `sys.path` line
   - 1.3. Public surface must stay exactly `setup_logging(log_dir, log_name=None, level=logging.DEBUG, overwrite=True)` and `get_logger(name)` — every call site depends on these signatures
   - 1.4. Preserve `RunTimestampFilter` attached to the file handler (not the logger) so every record from any module carries its run's timestamp
   - 1.5. Preserve the `{log_dir}/{log_name}.jsonl` output convention and the caller-script-name default for `log_name`
   - 1.6. Keep the docstring's statement of intent — vendored so CI never depends on untracked `.claude/` content, and deliberately forked from the skill copy

2. [completed] Create and run unit tests for the vendored logging package - `code/lib/unit_tests/test_logconfig.py`
   - 2.1. Add `code/lib/unit_tests/conftest.py` putting `code/lib` on `sys.path` via `Path(__file__).resolve().parents[1]`
   - 2.2. Use `tmp_path` to assert `setup_logging` writes `{log_dir}/{log_name}.jsonl` and that every line parses as JSON
   - 2.3. Assert records carry `run_timestamp` and `funcName`
   - 2.4. Assert `overwrite=True` (default) replaces the file and `overwrite=False` appends
   - 2.5. Assert two successive runs appending to one file carry distinct `run_timestamp` values
   - 2.6. Assert `log_name=None` defaults to the caller script's name
   - 2.7. Run `uv run pytest code/lib/unit_tests/test_logconfig.py --cov=code/lib/logconfig --cov-report=term-missing`; investigate any uncovered lines

3. [completed] Create the vendored shared validators module - `code/lib/validators.py`
   - 3.1. Move `validate_sql_identifier(name, label)` and `validate_collection_path(path)` here from `code/file_ingestion/_utils.py`, along with `_SAFE_IDENTIFIER_RE` and `_LTREE_RE`
   - 3.2. Keep both as pure validators using `fullmatch` (never `match`) — the existing docstrings explain that `match` would accept a trailing newline; carry that reasoning across verbatim
   - 3.3. Keep `ensure_schema()` in `code/file_ingestion/_utils.py` — it is pipeline-only (needs SQLAlchemy and the DDL template) and must not follow the validators into `mcp_deployment`
   - 3.4. Leave `code/file_ingestion/_utils.py` re-exporting both names from the new module so `file_ingestion`'s own call sites and tests keep working unchanged
   - 3.5. Delete the now-stale comment at `code/file_ingestion/_utils.py:17-18` claiming this is "the canonical copy"

4. [completed] Create and run unit tests for the shared validators - `code/lib/unit_tests/test_validators.py`
   - 4.1. Parametrize `validate_sql_identifier` over valid identifiers; assert each is returned unchanged
   - 4.2. Assert `"public\n"` is REJECTED — the regression guard for `fullmatch` vs `match`
   - 4.3. Parametrize invalid identifiers (uppercase, leading digit, dashes, spaces, quotes, semicolons, empty string); assert `pytest.raises(ValueError, match=...)` and that the message names both the offending value and the `label`
   - 4.4. Parametrize `validate_collection_path` over valid single- and multi-label ltree values; assert returned unchanged
   - 4.5. Assert `"a.b\n"` is REJECTED (same `fullmatch` guard)
   - 4.6. Parametrize invalid paths: uppercase, dashes, spaces, a `.ext` leaf, empty label, leading dot, trailing dot, doubled dot, empty string, whitespace-only
   - 4.7. Run `uv run pytest code/lib/unit_tests/test_validators.py --cov=code/lib/validators --cov-report=term-missing`; investigate any uncovered lines

5. [completed] Repoint `data_acquisition` imports to the vendored library - `code/data_acquisition/`
   - 5.1. In each file, replace `sys.path.insert(0, ".claude/skills/python-development/scripts")` with the path-relative form established in `metadata_db`: `sys.path.insert(0, str(Path(__file__).resolve().parents[N] / "code" / "lib"))`, retaining the explanatory comment above it. The `from logconfig import ...` line itself does not change
   - 5.2. `N` is the depth from the file to the repo root and differs per file — `cms_iom/download_cms_iom.py` and `usc_titles/download_usc_titles.py` are 3; `cms_iom/data_validation/data_val_downloaded_pdfs.py`, `usc_titles/data_validation/data_val_downloaded_zips.py`, and `usc_titles/unit_tests/test_download_usc_titles.py` are 4. Verify each by running the file, not by inspection
   - 5.3. Replace the CWD-relative `sys.path.insert(0, "code/data_acquisition/usc_titles")` at `usc_titles/unit_tests/test_download_usc_titles.py:9` with the path-relative equivalent

6. [completed] Repoint `file_ingestion` imports to the vendored library - `code/file_ingestion/`
   - 6.1. Apply the task 5.1 substitution in: `_utils.py`, `ingest.py`, `file_parser.py`, `docling_section_parser.py`, `quality_report.py`, `data_validation/data_val_cleaned_json.py`, `data_validation/data_val_loaded_documents.py`
   - 6.2. Replace the CWD-relative `sys.path.insert(0, "code/file_ingestion")` at `data_validation/data_val_loaded_documents.py:51` and `unit_tests/conftest.py:5` with path-relative resolution

7. [completed] Repoint `excel_ingestion` imports and remove its cross-module `importlib` load - `code/excel_ingestion/`
   - 7.1. Delete the `importlib.util.spec_from_file_location` block at `_utils.py:32-43` and import `validate_sql_identifier`, `validate_collection_path` from the vendored `validators` module instead
   - 7.2. Apply the task 5.1 substitution in: `_utils.py`, `excel_parser.py`, `ingest_excel.py`, `structured_table.py`, `data_validation/data_val_excel_inputs.py`, `data_validation/data_val_excel_outputs.py`, `unit_tests/test_excel_parser.py`, `unit_tests/test_ingest_excel.py`, `unit_tests/test_structured_table.py`, `unit_tests/test_utils.py`
   - 7.3. Keep `_utils.py`'s `__all__` unchanged so downstream imports are unaffected
   - 7.4. Remove the now-obsolete comment at `_utils.py:25-31` explaining the `_utils` name clash, since the clash no longer exists

8. [completed] Repoint `embedding_generation` imports and remove its cross-module `importlib` load - `code/embedding_generation/`
   - 8.1. Delete the `importlib.util.spec_from_file_location` block at `_utils.py:23-31` and import `validate_sql_identifier` from the vendored `validators` module
   - 8.2. Apply the task 5.1 substitution in: `_utils.py`, `chunker.py`, `generate_embeddings.py`, `data_validation/data_val_embeddings.py`
   - 8.3. Replace the CWD-relative `sys.path.insert(0, "code/embedding_generation")` at `data_validation/data_val_embeddings.py:40` with path-relative resolution
   - 8.4. Remove the now-obsolete comment at `_utils.py:17-22` explaining the `_utils` name-clash workaround, the same cleanup task 7.4 applies in `excel_ingestion`

9. [completed] Repoint `mcp_db_server` imports and remove its cross-module `importlib` load - `code/mcp_db_server/mcp_db_server.py`
   - 9.1. Delete the `importlib.util.spec_from_file_location` block at lines 131-139 and import `validate_sql_identifier` from the vendored `validators` module. This is the dependency that would otherwise make the carved server repo fail at import time
   - 9.2. Verify all 6 call sites still resolve — lines 1084, 1219, 1222, 1565, 1585, 1600
   - 9.3. Apply the task 5.1 substitution at lines 118-119
   - 9.4. Fix `unit_tests/conftest.py`: its second `sys.path` insert resolves to `<repo>/skills/python-development/scripts` — the `.claude` segment is missing, so the path does not exist and the entry is dead code today (tests pass only because `mcp_db_server.py:118`'s own CWD-relative insert fires when pytest runs from the repo root). Replace it with path-relative resolution of `code/lib`

10. [completed] Correct the dependency declarations - `pyproject.toml`
    - 10.1. Add `openpyxl` — imported by `excel_parser.py`, `ingest_excel.py`, `data_val_excel_inputs.py`, `test_excel_parser.py`, and `test_ingest_excel.py` but currently resolved only transitively; it is on the approved list
    - 10.2. Remove `python-docx`, `python-pptx`, `graphviz`, `keyring`, `sqlglot` — imported nowhere under `code/`
    - 10.3. Do NOT remove `einops` on import evidence alone. Verify first whether `ibm-granite/granite-embedding-small-english-r2` requires it under `MCP_TRUST_REMOTE_CODE`, by loading the model with `einops` absent in a throwaway environment. Keep it if the load fails, and add a comment recording the finding either way
    - 10.4. Leave `constraint-dependencies = ["transformers<5", "torch<2.11"]` in place for this phase; it is re-decided per repo in task 16

11. [completed] Run the full test suite against the decoupled tree
    - 11.1. Run `uv run pytest code --cov=code --cov-report=term-missing`; all tests pass
    - 11.2. Confirm zero remaining matches for `\.claude/skills/python-development/scripts` under `code/`
    - 11.3. Confirm zero remaining matches for `spec_from_file_location` under `code/`
    - 11.4. Re-run the suite from a non-root CWD, proving the CWD-relative import coupling is gone

12. [completed] Create and run the pre-split MCP search equivalence baseline - `code/mcp_db_server/data_validation/data_val_search_equivalence.py`
    - 12.1. Parameters: `--config`, a TOML path per the executable-scripts convention
    - 12.2. Add `code/mcp_db_server/config/data_val_search_equivalence.toml` specifying the MCP instance URL, ~15 queries spanning the `cms_iom`, `qpp_cm`, and `usc` schemas, and the output JSON path
    - 12.3. For each query, capture the ordered result identities from the `search` tool: `collection_path`, chunk id, and fused score
    - 12.4. Write results to `data/output/search_baseline_pre_split.json`
    - 12.5. Assert every query returns at least one row, so an empty baseline cannot silently pass the task 20 comparison
    - 12.6. Assert the served `MCP_EMBEDDING_MODEL` is recorded in the output, so a model mismatch is visible in the diff rather than inferred
    - 12.7. Run against the running `policy_db` server on port 8000. Commit the script and config; the output stays local since `data/` is gitignored

13. [completed] Document the cross-repo SQL contract - `docs/contracts/corpus-schema-contract.md`
    - 13.1. This becomes the only binding interface between the two repos once carved, so it must exist before the carve
    - 13.2. Specify the source-table shape from `code/file_ingestion/sql/schema.sql`: `{schema}.document`, `{schema}.document_content`, the `ltree` `collection_path` primary-key convention, and `source_binary_hash` as an unsigned 64-bit value
    - 13.3. Specify the Excel-side equivalent from `code/excel_ingestion/sql/excel_schema.sql`
    - 13.4. Specify the embedding-table contract the server discovers by introspection: `{source_table}_embedding` (overridable per table in config) with at least one `vector` column — this is what `_discover_embedding_tables` queries via `udt_name = 'vector'` at `mcp_db_server.py:921`
    - 13.5. State the embedding-model identity rule explicitly: the pipeline's `model_name` and the server's `MCP_EMBEDDING_MODEL` must match, currently enforced by nothing but a comment in `.env.mcp.*.example`. Record `ibm-granite/granite-embedding-small-english-r2` as the value the existing corpus was built with
    - 13.6. State that changing the embedder is a re-embed event gated on a reproducibility check, and that the server must not be upgraded past the `transformers` / `torch` pin without it
    - 13.7. Record which repo owns DDL (pipeline) and which owns grants (server)

### Phase 2 — Carve the repos

14. [completed] Install the carving tool and archive the pre-split state
    - 14.1. `uv tool install git-filter-repo` — a one-off developer tool, so `uv tool install` rather than `uv add`; it must not enter `pyproject.toml`
    - 14.2. Commit all Phase 1 work on `main`
    - 14.3. Tag the pre-split commit `pre-repo-split` and push the tag, so the monorepo state is recoverable by name
    - 14.4. Clone `rag` twice into scratch working copies. Run `filter-repo` only on the clones, never on the live repo — the rewrite is destructive and irreversible

15. [completed] Carve `ingestion_pipeline`
    - 15.1. `git filter-repo --path code/data_acquisition --path code/file_ingestion --path code/excel_ingestion --path code/embedding_generation --path code/lib --path docs --path readme --path pyproject.toml --path uv.lock --path .gitignore`
    - 15.2. Drop `docs/activities/mcp-server/` and the MCP-specific files under `docs/code_review/`; their history survives in the archive tag and in the server repo
    - 15.3. Drop `.env.mcp.*.example` and `.mcp.json`
    - 15.4. Remove `mcp` from `pyproject.toml` dependencies — the carve carries the monorepo's `pyproject.toml`/`uv.lock` over unchanged, and no ingestion module imports `mcp` — then run `uv lock` so `uv.lock` reflects the trimmed set. Without this, task 18.5's "`mcp` is not installed" check fails by construction
    - 15.5. Verify `git log --follow` reaches the original commits for a sample file in each of the four modules

16. [completed] Carve `mcp_deployment`
    - 16.1. `git filter-repo --path code/mcp_db_server --path code/pg_metadata --path code/lib --path .env.mcp.policy_db.example --path .env.mcp.metadata_db.example --path .mcp.json --path .gitignore`
    - 16.2. Keep only `docs/activities/mcp-server/` and the MCP-related files under `docs/code_review/`
    - 16.3. Reduce the vendored `code/lib/validators.py` to `validate_sql_identifier` only — `validate_collection_path` has no call site in this repo
    - 16.4. Author a fresh `pyproject.toml` declaring `mcp`, `starlette`, `sentence-transformers`, `sqlalchemy`, `psycopg2-binary`, `python-dotenv`, `python-json-logger`, `pytest`, `pytest-mock` (plus `einops` if task 10.3 proved it necessary). `starlette` is transitive via `mcp` but imported directly at `mcp_db_server.py:114-116`, so it must be declared. Exclude `docling`, `pdfplumber`, `openpyxl`, `pydantic`, `requests`, `beautifulsoup4`. Omit the `readme` field until task 17.1 authors `README.md`, then add it there
    - 16.5. Decide the `constraint-dependencies` pin deliberately. Keep `transformers<5` / `torch<2.11` for now, since the server must load the same embedder the corpus was built with — but record in the README that the pin is inherited from the corpus rather than from the server's own needs, and is liftable only alongside a re-embed
    - 16.6. Verify `git log --follow code/mcp_db_server/mcp_db_server.py` reaches the pre-split commits

17. [in-progress] Provision each repo to run standalone
    - 17.1. Write a `README.md` per repo covering purpose, setup, and a pointer to `docs/contracts/corpus-schema-contract.md`, which is copied into both. This also makes each `pyproject.toml`'s `readme = "README.md"` declaration valid for the first time — the monorepo declares it but has never had a root `README.md` (only the `readme/` directory), so add the field to `mcp_deployment`'s fresh `pyproject.toml` in this task per 16.4
    - 17.2. Copy `.claude/` into both working directories out of band — it is gitignored and will not survive `filter-repo`
    - 17.3. Recreate `.env` / `.env.mcp.*` in each working directory from the `.example` files; these are gitignored and do not carry over
    - 17.4. Confirm each `.gitignore` still excludes `data/`, `logs/`, `.venv/`, `.env`, `.env.mcp.*`, `.claude/`
    - 17.5. Create the two remotes and push `main` plus the `pre-repo-split` tag

18. [completed] Verify `ingestion_pipeline` from a clean clone
    - 18.1. Clone fresh into a scratch directory, `uv sync`, then place `.claude/` and `.env`
    - 18.2. Run `uv run pytest code --cov=code --cov-report=term-missing`; all tests pass
    - 18.3. Run a smoke ingest end to end: `uv run code/file_ingestion/ingest.py --config code/file_ingestion/config/test/ingest_test_document.toml`
    - 18.4. Run the matching output validation: `uv run code/file_ingestion/data_validation/data_val_loaded_documents.py`
    - 18.5. Confirm `docling` is installed and `mcp` is not

19. [completed] Verify `mcp_deployment` from a clean clone
    - 19.1. Clone fresh, `uv sync`, then place `.claude/` and `.env.mcp.policy_db`
    - 19.2. Run `uv run pytest code --cov=code --cov-report=term-missing`; all tests pass
    - 19.3. Confirm `docling` and `pdfplumber` are absent, and record the resulting install size and `uv sync` time against the monorepo baseline
    - 19.4. Launch detached with stdout/stderr redirected to `NUL` per project convention: `uv run python code/mcp_db_server/mcp_db_server.py --env .env.mcp.policy_db`
    - 19.5. Allow ~1 minute for model warm-up, then confirm `GET http://localhost:8000/health` returns 200
    - 19.6. Repeat for `.env.mcp.metadata_db` on port 8002, confirming the carved server still serves a database this repo does not build

20. [completed] Run and compare the post-split search equivalence check - `code/mcp_db_server/data_validation/data_val_search_equivalence.py`
    - 20.1. Re-run the task 12 script against the server launched from the carved repo, writing `data/output/search_baseline_post_split.json`
    - 20.2. Diff against the pre-split baseline. Result identities and their order must match exactly for every query
    - 20.3. Treat any ordering difference as a blocker, not a tolerance — an identical corpus plus an identical model must produce an identical fused ranking
    - 20.4. Confirm the recorded `MCP_EMBEDDING_MODEL` matches between the two runs
    - 20.5. Confirm no `*_embedding` table was written during verification, i.e. nothing triggered a re-embed
    - 20.6. If issues found, debug and iterate

21. [in-progress] Retire the monorepo
    - 21.1. Confirm tasks 18-20 are all green before touching `rag`
    - 21.2. Mark every task in this activity file `[completed]` in both carved copies
    - 21.3. Archive `rag` read-only with a README pointing at the two successors and the `pre-repo-split` tag
    - 21.4. Update the MCP server launch commands in the project memory note and any local runbook to the new repo paths

## Key Data Decisions and Considerations

1. Split on the write/read seam rather than by data domain — `mcp_db_server` learns its schema, tables, and embedding columns from Postgres at runtime, so the interface is SQL, not Python. Splitting policy-vs-proposals instead would mean duplicating one pipeline behind two config sets.
2. Phase 1 must fully precede Phase 2, and all Phase 1 work lands in `rag` so the fix carries into both carved histories. `filter-repo` rewrites history, so any coupling still present at carve time is baked into both rewritten histories. Fixing imports first means both repos inherit a working tree at every commit `--follow` can reach.
3. `.claude/` is gitignored (`.gitignore:11`) and therefore invisible to `filter-repo`; it must be copied into both working directories by hand. This is also why the logconfig vendoring in tasks 1-9 is a prerequisite rather than a nicety — a fresh clone of `rag` today cannot execute a single script, because all 27 entry points import a module that is not in version control.
4. Vendor `logconfig` per repo rather than creating a third shared-library repo. This follows the precedent already set in `metadata_db`, whose tracked copy at `code/lib/logconfig/` documents the intent: *"Vendored under code/lib/ so CI never depends on untracked .claude/ content; this copy intentionally forks from the skill copy (skill updates do not propagate by design)."* The cost is a known fork; the benefit is that neither repo depends on a private package registry at build time.
5. Mirroring `metadata_db`'s `code/lib/logconfig/` package layout keeps the 27-file diff to one line each — only the `sys.path` target changes, and `from logconfig import ...` is untouched. A flatter `code/lib/logconfig.py` would have required editing the import line too, for no gain.
6. Resolve `code/lib` from `__file__`, not from the CWD. The current `.claude/`-relative inserts are CWD-relative, which silently requires every script and every pytest run to start from the repo root. `metadata_db` already uses `Path(__file__).resolve().parents[N] / "code" / "lib"`; adopting it removes an undocumented operational constraint. The `parents[N]` depth varies by file nesting and must be verified by execution — `data_acquisition` is one level deeper than the other modules.
7. Tasks 5-9 group the 27 import call sites by module rather than one task per file, deviating from the one-code-file-per-task rule. The edit is a single mechanical substitution repeated across near-identical files, so 27 tasks would obscure rather than clarify; every affected file is still named in a subtask. The `importlib` removals in tasks 7-9 are the genuinely distinct work and are called out separately.
8. `validate_sql_identifier` is the one piece of pipeline code the server genuinely needs, so it must be duplicated across the split (task 16.3). Both copies are ~10 lines of pure regex validation with no dependencies. The `fullmatch`-not-`match` behavior is the security-relevant part; tasks 4.2 and 4.5 pin it with tests so a later edit to either copy cannot silently loosen it.
9. `ensure_schema` stays with `file_ingestion` even though it currently lives beside the validators. It reads a DDL template and executes it, which is a write-side concern; letting it follow the validators into the server repo would hand a read-only service a schema-creation function it must never call.
10. This activity produces no new data tables, so per-table `data_val_*` outputs do not apply. The output actually under test is behavioral equivalence of the MCP server, which tasks 12 and 20 cover as a real before/after gate. Task 12 must run while the monorepo is still intact — the baseline is unrecoverable afterwards.
11. The `.claude/skills/python-development` docs are deliberately left unchanged (user decision). Consequence to accept: `core/logging.md:58`, `core/logging.md:82`, and `core/executable-scripts.md:25` still instruct new code to `sys.path.insert(0, ".claude/skills/python-development/scripts")` (verified still present after the 2026-08-13 `.claude/` overhaul), so the next module written in either repo will reintroduce the untracked-path coupling unless the author notices. Task 11.2's grep is the only guard, and it is not automated. Worth a follow-up activity.
12. `einops` is treated as suspected-required rather than dead (task 10.3). It appears in no import under `code/`, but `granite-embedding-*-r2` loads with `trust_remote_code`, and remote model code commonly imports `einops` outside the repo's own import graph. Dropping it on grep evidence alone would break model loading at runtime in the server repo only — the worst place to discover it.
13. `git filter-repo` is a developer tool, not a project dependency, so it installs via `uv tool install` and never enters `pyproject.toml`. It is not on the approved-packages list, but the history-preserving approach was chosen explicitly, which authorizes it.
14. The approved-packages list (`.claude/skills/package-management/references/approved_packages.txt`, 23 entries after the 2026-08-13 `.claude/` overhaul) is still out of sync with `pyproject.toml` — `mcp`, `starlette`, `requests`, `beautifulsoup4`, `python-dotenv`, `torch`, `transformers`, `pytest-mock`, and `einops` are all in use but unlisted. Task 10 does not reconcile this; it is a separate concern, and reconciling per repo is easier after the split than before.
15. Volume splits roughly 78/22 — 14,358 lines of pipeline code against 4,227 for the server and its grants. The smaller side is the only long-running network-facing service, which is the reason for the split, not the line count.
16. Audit provenance: the coupling and dependency audit ran against the working tree at commit `981ed64` — 27 files import `logconfig` from gitignored `.claude/`, and three modules `importlib`-load `file_ingestion/_utils.py` by explicit path (`excel_ingestion/_utils.py:32`, `embedding_generation/_utils.py:23`, `mcp_db_server.py:131`). All line citations were re-verified on 2026-08-13 after the `.claude/` overhaul; they will drift as Phase 1 lands, so re-verify by grep before editing, not by trust.
17. No blockers at planning time: `git filter-repo` is not yet installed (task 14.1), but `uv 0.11.1` and `git 2.53.0` are present.
18. Repo names are `ingestion_pipeline` and `mcp_deployment`, snake_case to match the existing sibling projects (`metadata_db`, `gh_claude_code_resources`, `helpful_concepts`). Task 17.5 (remote creation) is the last cheap moment to change them. `ingestion_pipeline` is deliberately not `file_ingestion_pipeline`: the repo holds `code/data_acquisition`, `code/file_ingestion`, `code/excel_ingestion`, and `code/embedding_generation`, so prefixing with `file_` would overload the name of one module inside it and understate the other three. `mcp_deployment` likewise carries the server's full source (3,962 lines), not just deploy configuration.
19. Both MCP servers are currently running detached — `policy_db` on port 8000, `metadata_db` on port 8002 — and must stay up for the task 12 baseline.
20. `metadata_db` is a separate project that this server already serves. Task 19.6 verifies the carved server still does, which is independent evidence that `mcp_deployment` is shared infrastructure rather than a `rag` component.
21. The grants scripts are idempotent and take effect per-query with no server restart; re-apply as the relevant maintainer role after any target database rebuild. (2026-08-16 update: `pg_metadata` moved out of `mcp_deployment` — grants and descriptions now live with each database's DDL owner: `policy_db`'s in `ingestion_pipeline/code/pg_metadata/`, `metadata_db`'s in the `metadata_db` repo's `code/apply_ddl/`; the `proposals_db` descriptions were retired. See the contract's Ownership section.)
22. BLOCKER (task 17.5): creating the two remotes was denied by the permission system (`gh repo create Warehouse/ingestion_pipeline` / `Warehouse/mcp_deployment` on github.example.com, following the `metadata_db` precedent — the monorepo's own origin is gitlab.example.com/warehouse-common/rag.git, so the host choice also needs confirmation). The user must create the remotes (or grant the permission), then in each working copy: `git remote add origin <url> && git push -u origin main pre-repo-split`. Task 21.3's read-only archival of `rag` needs the same remote admin access. Tasks 18-20 were verified against clean LOCAL clones of the carved working copies instead, which preserves the verification intent (fresh tree, fresh `uv sync`, no monorepo leftovers).
23. Implementation deviations and findings (2026-08-13 execution):
    - `logconfig` was copied verbatim except the docstring's first line ("for the metadata-db loaders" would be wrong here); the vendoring-intent sentence required by task 1.6 is verbatim.
    - `pytest-cov` is not a project dependency in any repo; coverage runs use `uv run --with pytest-cov` (ephemeral overlay, no pyproject change).
    - Pre-existing whole-suite blockers surfaced by task 11.1 and fixed: (a) test-written TOML configs embedded Windows backslash `tmp_path` values, an invalid `\U` escape — fixed with `as_posix()` in `test_ingest_excel.py`; (b) the three same-named `_utils` modules collided via the `sys.modules` bare-name cache in one pytest process — each module's `unit_tests/conftest.py` now evicts a foreign cached `_utils`, and the two test-time lazy `_utils` imports were hoisted to module level; (c) the two same-named `test_utils.py` files collided under pytest's prepend import mode — `[tool.pytest.ini_options] addopts = "--import-mode=importlib"` added. Full suite: 624 passed (monorepo), from the repo root and from a non-root CWD.
    - Task 10.3 verdict: `einops` REMOVED — in a clean venv without it (transformers<5/torch<2.11), `ibm-granite/granite-embedding-small-english-r2` loads and encodes under `trust_remote_code=True` and `BAAI/bge-reranker-base` loads and scores. Finding recorded as a comment in both repos' `pyproject.toml`.
    - `mcp_deployment` pins `mcp>=1.26.0,<2`: a fresh resolve pulled mcp 2.0.0, which drops the 1.x `mcp.server.fastmcp` API the server targets. The 2.x migration is a separate activity.
    - Tasks 19.4-19.6/20 ran with the carved servers on ports 8010/8012 (env copies `.env.mcp.*.verify` in the verification clone): stopping the monorepo servers (on 8000/8002) was denied by the permission system. The monorepo servers still hold 8000/8002; the carved verification servers (launched from the temp clone `%TEMP%\vmd`) still hold 8010/8012. User follow-up: stop all four and relaunch from `..\mcp_deployment` on 8000/8002.
    - Task 19.3 metrics: mcp_deployment clean-clone `.venv` 799M vs monorepo 1.4G (ingestion_pipeline clone 1.3G); `uv sync` 6-15s with a warm uv cache; `docling`/`pdfplumber` absent; suite 135 passed (ingestion_pipeline clean clone: 516 passed).
    - Task 20 results: PASS — 15 queries, result identities (`source_schema`, `source_table`, `collection_path`, `chunk_number`, `fused_score`) identical in content and order; `MCP_EMBEDDING_MODEL` identical; embedding-table row counts identical before/after (cms_iom 33,154 / qpp_cm 3,969 + 1,340,050 / usc 264,857) — nothing wrote to any `*_embedding` table.
    - Task 15.5 nuance: `git log --follow` for `file_ingestion` files stops at the `pdf_ingestion`->`file_ingestion` rename commit because the planned `--path` list keeps only the current module paths; the pre-rename history survives in the `pre-repo-split` tag. The other three modules reach their original commits.
    - The `search` tool has no endpoint reporting the served model, so `data_val_search_equivalence.py` records `MCP_EMBEDDING_MODEL` from the env file the instance was launched with (falling back to the server's default constant, mirroring `get_embedding_model_name`).
