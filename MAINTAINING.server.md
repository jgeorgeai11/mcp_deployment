# Maintaining the MCP database server

Everything a maintainer needs to work on the engine — the package under `packages/`. Operating a particular instance (its credentials, grants, corpus, and baselines) lives in the per-instance `MAINTAINING.instance.*.md` files.

## 1. Workspace layout

The repo is a uv workspace. The root `pyproject.toml` is a virtual project — no `[build-system]`, never installed — that lists the members, holds the shared dev dependencies, pins the interpreter floor, and constrains the model stack. `uv sync` at the root installs every member editable into one shared `.venv` with a single `uv.lock`.

```
pyproject.toml                    workspace root: members, dev deps, model-stack pin, ruff/mypy
packages/mcp_db_server/           the engine (one installable distribution)
  src/mcp_db_server/server.py     the server: tools, auth middleware, main()
  src/mcp_db_server/validators.py canonical SQL-identifier validator
  src/mcp_db_server/logconfig.py  JSON logging setup
  src/mcp_db_server/paths.py      portable path anchoring (instance-root discovery)
  src/mcp_db_server/data_validation/data_val_search_equivalence.py
  tests/                          the full suite
instances/<name>/                 config only: .env, .env.example, config/, logs/, data/
.mcp.json                         client wiring for both instances (no secrets)
```

The package uses the src layout with hatchling and exposes its entry points as console scripts (`mcp-db-server`, `data-val-search-equivalence`). Tests live outside the package, in `packages/mcp_db_server/tests/`, so they exercise the installed distribution — what ships is what gets tested.

Two rules the layout enforces:

- **The engine never references an instance.** No path, name, or default under `packages/` may name anything under `instances/`; the generic convention string `instances/<instance>/.env` in help text is the only allowed mention. The guard is a grep (`grep -rn "instances/policy_db" packages/`), not the type system.
- **Dependencies move with code.** Every distribution the package imports is declared in `packages/mcp_db_server/pyproject.toml` — including the ones that would otherwise ride in transitively via `mcp` (`starlette`, `uvicorn`). The shared venv masks a missing declaration; the honest check is installing the package alone into a scratch environment.

There is exactly one package because there is exactly one serving component. `validators` and `logconfig` are ordinary modules of it, not a second distribution; if a second serving component ever appears, the `packages/*` glob already has a slot for it.

## 2. Development setup and testing

1. `uv sync` at the workspace root (Python >= 3.13).
2. `uv run pytest` at the repo root (`testpaths` in the root pyproject selects the package suite). From another directory pass the path — `uv run pytest <repo>/packages` — since pytest honors `testpaths` only from the rootdir; the tests import the installed distribution, so they pass from anywhere.
3. `uv run pytest --cov=mcp_db_server --cov-report=term-missing` for coverage (`pytest-cov` is in the `dev` group, so a plain `uv sync` already has it). Investigate every uncovered line rather than accepting a percentage.

Testing conventions:

- The suite is **fully mocked**: no live database, no model downloads, no live server. Fakes stand in for the SQLAlchemy connection/engine layer, and `mcp.tool()` returns the wrapped function unchanged, so every tool function is directly callable.
- A test that invokes `main()` passes `--env-file` pointing at an empty file under `tmp_path`: that satisfies the required flag while loading nothing, so the defaults under test are the ones the code computes.
- Static analysis: `uv run ruff check` and `uv run mypy` both run clean (config in the root `pyproject.toml`, every exclusion documented inline). Keep them clean.
- Behavior-preserving changes to the search path are gated on a **search-equivalence capture** rather than on unit tests alone — see section 6 and the instance file.

### Portability

Keep the virtualenv out of any file-sync service. A synced venv corrupts itself — duplicated `lib 2/` directories, `.pth` files flagged hidden which CPython then skips, and stale console scripts left beside a half-replaced interpreter — after which every import fails with `ModuleNotFoundError` while `uv sync` reports "Audited N packages" and repairs nothing. On a machine whose checkout sits in a synced folder, create the venv as `.venv.nosync` and symlink `.venv` to it (iCloud skips paths ending `.nosync`); `uv` and every tool follow the symlink normally. Other providers use their own exclusion mechanism — Dropbox marks a folder ignored, OneDrive has per-folder exclusions.

The symlink itself is the fragile part during a changeover. A sync service that already holds a real `.venv` in the cloud will see the new symlink as a conflict, resolve it by creating a `.venv 2`, and begin re-materializing the old directory over the top — which shadows the good venv with dataless placeholder files. Expect this only while the service drains its backlog for the switch; once it has settled, the arrangement is stable. The same mechanism can restore directories a commit deleted, so after a large structural change confirm `git status` still matches what you committed.

## 3. Running an instance

```bash
uv run mcp-db-server --env-file instances/<instance>/.env
```

- **The env file is required, and the two ways of getting it wrong exit differently.** With neither `--env-file` nor `MCP_ENV_FILE` set, argparse prints its usage block and the process exits **2**; with a path that does not exist it prints `error: env file not found: <path>` and exits **1**. A flag never supplied and a path that does not exist are different mistakes and are worth telling apart in a service unit's restart logic. There is no default instance, so a forgotten flag is a loud failure rather than a silent serve of the wrong database. A relative path resolves against the working directory, ordinary CLI semantics.
- **The instance name and the logs follow the env file.** `MCP_INSTANCE_NAME` defaults to the name of the directory holding the env file, and the run's logs are written to `<that directory>/logs/mcp_db_server/<instance>.jsonl` — resolved through `mcp_db_server.paths.resolve_log_dir`, which falls back to a temp root when the env file sits outside any instance. An installed console script therefore never scatters CWD-relative `logs/` trees.
- **Warm-up window.** Startup eagerly loads and warms the embedding and reranker models (measured: ~36s for policy_db) and `/health` is unavailable until it finishes — the instance is not ready until the models load, and a model-load failure aborts startup by design. Poll `GET /health` for 200 with a budget of at least 120s, and abort the poll if the process dies rather than waiting out the timeout. `MCP_WARM_MODELS=false` skips warm-up (correct for an instance with no embeddings; a dev convenience otherwise).
- **Client wiring** is the repo-root `.mcp.json`: an HTTP entry per instance with `Authorization: Bearer ${MCP_<INSTANCE>_TOKEN}`. It holds URLs and variable references, never tokens, and is discovered by Claude Code at the project root.
- **Deployment posture**: the server binds `127.0.0.1` by default and is meant to run behind a TLS reverse proxy. A loopback bind is NOT by itself protection — the proxy makes the port reachable, and on a shared host so does any local user. `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` control the transport's DNS-rebinding allowlist; leave them unset for a loopback bind or behind a Host-rewriting proxy, and set them only when clients connect directly to a non-loopback `MCP_HOST` (their `Host:` header otherwise draws a 421).

## 4. Auth operations

Auth is bearer-token, per instance, configured entirely in that instance's `.env`.

- **Token format**: `MCP_AUTH_TOKENS=label:token[,label:token]` — one pair per person, any one of which authenticates. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **Onboard**: append a `label:token` pair and restart the instance; the user exports `MCP_<INSTANCE>_TOKEN` in their shell profile and restarts their client.
- **Revoke**: remove their pair and restart. There is no token store and no expiry — the env file is the whole registry, which is why revocation is a restart.
- **Fail-closed**: when `MCP_AUTH_TOKENS` is unset or empty, every `/mcp` request is rejected. `/health` is deliberately open (no token) so a proxy or a poll loop can check readiness.
- **`MCP_DISABLE_AUTH`** (truthy) serves `/mcp` with NO auth and ignores `MCP_AUTH_TOKENS`, removing the only gate in front of `run_sql` and `search`. It logs a loud warning at startup. Enable it only on a genuinely isolated box; never on anything a reverse proxy fronts.

Defense in depth does not stop at the token: each instance connects as its own **non-superuser, read-only role**, and the set of databases it serves is derived from that role's `CONNECT` grants rather than an app-level allow-list, so the served set cannot drift from the grants.

## 5. Tuning knobs

Every knob is an `MCP_*` variable read at use-time (so the instance `.env` takes effect after `load_dotenv`) over a reviewable code default. Integer knobs clamp to a documented floor and warn rather than accepting an unsafe value.

| Variable | Default | What it trades |
|----------|---------|----------------|
| `MCP_MAX_ROWS` | 500 | `run_sql` row cap; results beyond it are truncated and flagged |
| `MCP_STATEMENT_TIMEOUT_S` | 5 | `run_sql` statement timeout, seconds |
| `MCP_RERANK_POOL` | 50 | Per-table candidate-pool depth: recall vs. rerank cost |
| `MCP_RERANK_GLOBAL_POOL` | `MCP_RERANK_POOL` | Cap on the merged cross-table pool sent to the reranker (raised to `top_k` when larger), so multi-schema latency does not scale with schema count |
| `MCP_DB_POOL_SIZE` / `MCP_DB_MAX_OVERFLOW` | 5 / 10 | SQLAlchemy connection pool |
| `MCP_HNSW_EF_SEARCH` | 100 | pgvector HNSW search effort per connection: recall vs. latency |
| `MCP_LOG_LEVEL` | `INFO` | Log level; an unknown name falls back to INFO rather than erroring |
| `MCP_WARM_MODELS` | `true` | Eager model load at startup vs. a cold first search |
| `MCP_INSTRUCTIONS` | generic orientation | What the connecting agent is told this instance holds |

## 6. The data interface the server reads

The server's input is plain PostgreSQL. It never imports pipeline code and reads nothing but the catalog and the tables themselves, so this section — the mirror of `ingestion_pipeline`'s MAINTAINING.packages.md §3, "the data interface the engine writes" — is what this server requires of ANY corpus it is pointed at. It is deliberately free of file paths, in either repo, so that restructuring either one cannot stale it.

- **Search targets are discovered, not configured.** Every table in a schema with at least one column of `udt_name = 'vector'` is a search target. The `*_embedding` naming convention is the writer's, not a requirement — and the corollary binds the writer: an incidental vector column in a served schema silently becomes a search target.
- **The reranked text column is literally `chunk_text`.** The server reranks and returns that column by name; a table with a vector column but no `chunk_text` is not servable.
- **A `tsvector` column enables the sparse leg.** Hybrid search runs a dense leg (cosine over the vector column) and a sparse leg (full-text over the tsvector column), fused with reciprocal rank fusion. A table without a tsvector column degrades cleanly to dense-only — quietly, so its absence shows up as weaker keyword recall rather than as an error.
- **The table's primary key is the fusion identity.** The PK columns are what the two legs are fused on and what a result is attributed by; they must uniquely identify a chunk.
- **Dimensions must match, and are guarded.** The query model's output dimension must equal each table's `vector(N)`; a mismatch raises rather than returning nonsense.
- **Embedding-model identity — the rule with no runtime guard.** `MCP_EMBEDDING_MODEL` MUST name the exact model the corpus was embedded with. The dimension guard catches only dimension mismatches: a *different* model of the *same* dimension produces no error at all, just silently garbage rankings. Nothing enforces this but this rule and the comment in each `.env.example`. Changing the embedder is therefore a re-embed event — regenerate every embedding table and update `MCP_EMBEDDING_MODEL` together, gated on a search-equivalence capture before and after.

## 7. Gotchas

- **The model-stack pin is inherited from the corpus.** `[tool.uv] constraint-dependencies = ["transformers<5", "torch<2.11"]` in the root pyproject holds the stack that built the served embeddings, NOT this server's own requirement. Lifting it is a re-embed event (section 6), not a dependency bump. When regenerating `uv.lock`, diff the resolved `torch` / `transformers` / `sentence-transformers` versions and investigate any movement rather than accepting it.
- **The FastMCP pin is a hard API boundary.** `mcp>=1.26.0,<2`: `mcp.server.fastmcp` was removed in mcp 2.0.0, so an upgrade is a deliberate migration of the whole server surface, not a version bump.
- **`einops` is deliberately absent.** Verified 2026-08-13 that both served models load and run without it in a clean venv. Do not add it back on the strength of a model card.
- **Nothing here creates database objects.** The server role is read-only by design: no schema creation, no extensions, no grants. `CREATE EXTENSION` and grant application are provisioning acts performed in the repo that owns the database.
- **The equivalence tool captures; it does not compare.** `data-val-search-equivalence` writes one JSON of ordered result identities. Equivalence is a two-run workflow you perform: capture, change, capture, diff the `queries` arrays (`generated_at` differs by construction; `embedding_model` must match). Its outputs live under the instance's `data/output/`, which is gitignored — a baseline that matters must be preserved deliberately.
- **A strange environment is a sync problem until proven otherwise.** The requirement and its rationale are in section 2 under Portability. Recovery: rebuild rather than debug — `rm -rf .venv .venv.nosync && mkdir .venv.nosync && ln -s .venv.nosync .venv && uv sync`. If the sync service is still actively re-materializing files, `rm` loses that race and hangs with "Directory not empty"; rename the broken directory aside in place instead (an instant inode relink) and delete it once the service has settled. Which piece to rename depends on what the sync service broke, so check `ls -ld .venv` first: if `.venv` is a REAL directory, the service replaced the symlink with a placeholder copy that shadows the healthy target — rename `.venv` aside and re-create the symlink (the observed 2026-08-26 failure); if `.venv` is still a symlink, the corruption is in the target — rename `.venv.nosync` aside (renaming the 12-byte symlink would leave the corruption where it was). Do not move it to another filesystem to delete it — that forces a byte-copy and hangs the same way.

## 8. Conventions a new file must follow

Four rules are enforced by tests rather than by this document, because a convention documented only here is one new file away from being violated.

- **Paths anchor through `mcp_db_server.paths`, never through `Path.is_absolute`.** `is_absolute()` answers for the running host only, so a config value like `C:\corpus\out.json` reads as *relative* on macOS and would be silently resolved under the instance root. And testing absoluteness under BOTH platforms' rules would still not be enough: a drive-relative value (`D:data`) and a drive-less-rooted value (`\etc\passwd`) are absolute under NEITHER platform's `is_absolute()`, yet joining either under an anchor on Windows leaves the anchor — another drive's working directory, or the drive root — which is why `is_rooted_path` tests for a drive or a root rather than for absoluteness. Use `resolve_config_path` for configured paths (it raises on a foreign-rooted value and on an unresolvable instance) and `resolve_log_dir` for log directories (it falls back to a temp root rather than aborting — losing logs must not kill an otherwise valid run). `packages/mcp_db_server/tests/test_conventions.py` parses every module under `src/` and fails on the direct call, exempting `paths.py` itself.
- **Every timestamp written to a log is UTC.** `asctime` is machine-parseable ISO-8601 with milliseconds (`2026-08-27T16:42:51.946Z`, read directly by `datetime.fromisoformat`); `run_timestamp` is a filename-safe run label (`2026.08.27_16.42.51Z`) for grouping one run's records, not for parsing. This matches `ingestion_pipeline`, so an ingest log and a server log can be read against each other — with one side local time the two cannot be ordered, and neither file would say so. Retention changed in the same adoption: logs append across runs and rotate at 10 MiB with 3 backups per file, `run_timestamp` separates one run's records from the next, and `logs/` stays gitignored and disposable — copy out anything worth keeping. `packages/mcp_db_server/tests/test_logconfig.py` asserts both timestamp shapes against `datetime.now(UTC)`.
- **`docs/code_review/` mirrors the source tree.** A review for `packages/mcp_db_server/src/mcp_db_server/server.py` belongs at `docs/code_review/packages/mcp_db_server/src/mcp_db_server/`. Reviews move with their code when the code moves, but their contents are never rewritten: a review records what its reviewer saw at the time, including the source paths it cites, and the directory says where the file is now.
- **A shipped example config carries a guard test.** `instances/*/.env.example` and the validation TOML are the documented starting point for every deployment and nothing imports them, so `packages/mcp_db_server/tests/test_shipped_configs.py` checks that they parse, declare every variable the server reads by direct `os.environ[...]` subscript, and hold placeholders rather than credentials. The required set is derived from `server.py`'s AST, not hardcoded — a guard with a hardcoded expectation drifts in exactly the way the guard exists to catch.
