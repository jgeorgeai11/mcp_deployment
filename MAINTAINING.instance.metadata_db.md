# Maintaining the metadata_db instance

Operating the `metadata_db` MCP instance. Engine concerns — the layout, the tools, the tuning knobs, the data interface the server reads — live in [MAINTAINING.server.md](MAINTAINING.server.md).

## 1. What it serves

`metadata_db` is a structured metadata catalog (a data dictionary describing other databases): systems, data sources, schemas, tables, columns, table relationships, and column-level lineage, each mirrored by a `_hstry` temporal-history table, plus load-tracking tables. Two schemas are served, `catalog` and `reference`. The database is owned and built by the separate `metadata_db` repo.

The instance binds port **8002** (loopback by default; `policy_db` holds 8000, and 8001 is taken by the client application).

```bash
uv run mcp-db-server --env-file instances/metadata_db/.env
```

**It runs without models.** The catalog has no embedding tables, so `search` has no targets and returns a clean "no embedding tables found" error; the other five tools are fully functional. The env file therefore sets `MCP_WARM_MODELS=false`, which skips the startup warm-up entirely — this instance answers `GET /health` in about a second, not the ~36s a search instance takes. `MCP_EMBEDDING_MODEL` is irrelevant here.

Logs land in `instances/metadata_db/logs/mcp_db_server/metadata_db.jsonl` (gitignored).

Because there are no embeddings, `run_sql` is the working interface: the orientation in `MCP_INSTRUCTIONS` steers an agent through `list_databases` → `list_schemas` → `list_tables` → `describe_tables` (columns plus PK and foreign-key references — the join graph) and then to precise read-only SQL.

## 2. Credentials and the env file

Everything the instance needs is in `instances/metadata_db/.env` — gitignored, because it holds the role password and every bearer token. `instances/metadata_db/.env.example` is the committed template; copy it to `.env` and fill it in.

It connects as `mcp_ro_metadata`, a dedicated **non-superuser, read-only** role holding `SELECT` and nothing else. Note that this cluster listens on a different Postgres port than `policy_db`'s — the value is in the env file; do not assume 5432.

## 3. Grants

`metadata_db`'s read-only grants live with the database's DDL owner, not here: the `metadata_db` repo, at `code/apply_ddl/grants/mcp_ro_metadata.sql` (verified current 2026-08-26). One file carries the role's complete privilege model across both served schemas: `CONNECT` on the database, `USAGE` on `catalog` and `reference`, `SELECT` on all their tables, and matching default privileges for future ones.

- **Re-apply after every database rebuild.** `DROP DATABASE` drops every database-, schema-, and table-level grant; the LOGIN role itself is cluster-level and survives.
- Grants take effect **per query** — no server restart. (The one exception noted in that script is its `search_path` setting, which applies to new sessions only and therefore does want a restart.)
- The script takes the schema and database names as psql variables, defaulting to `catalog` / `reference`.

## 4. Token onboarding

Tokens are the `MCP_AUTH_TOKENS=label:token[,label:token]` list in this instance's `.env` (mechanics in MAINTAINING.server.md §4).

1. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Append `their_label:their_token` to `MCP_AUTH_TOKENS` and restart the instance.
3. The user sets `MCP_METADATA_DB_TOKEN` to their own token — `export MCP_METADATA_DB_TOKEN="…"` in `~/.zshrc`/`~/.bashrc`, or `[Environment]::SetEnvironmentVariable("MCP_METADATA_DB_TOKEN","…","User")` on Windows — and restarts their client. The repo-root `.mcp.json` reads that variable; it never holds a token.

Revoking is the same edit in reverse plus a restart. This instance's tokens are separate from `policy_db`'s: they are different roles on different databases, and a token issued for one is meaningless to the other.

## 5. No equivalence baseline here

The search-equivalence tool exists to prove that ranked search results did not move. This instance has no embeddings and no ranked search, so it has no baseline and no `config/` directory — a smoke launch (health check, one `run_sql` through the client) is the whole verification.
