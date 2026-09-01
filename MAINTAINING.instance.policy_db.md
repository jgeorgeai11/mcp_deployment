# Maintaining the policy_db instance

Operating the `policy_db` MCP instance. Engine concerns — the layout, the tools, the tuning knobs, the data interface the server reads — live in [MAINTAINING.server.md](MAINTAINING.server.md).

## 1. What it serves

`policy_db` is the CMS policy corpus, built by the sibling `ingestion_pipeline` repo. Three schemas are served, each with source tables and their `*_embedding` tables:

| Schema | Contents |
|--------|----------|
| `cms_iom` | CMS Internet-Only Manuals |
| `qpp_cm` | QPP cost-measure codes and forms |
| `usc` | Selected US Code titles |

The instance binds port **8000** (loopback by default). It is a full search instance: models are warmed at startup, so allow the warm-up window before the first request.

```bash
uv run mcp-db-server --env-file instances/policy_db/.env
```

Logs land in `instances/policy_db/logs/mcp_db_server/policy_db.jsonl`; the equivalence tool's logs in `instances/policy_db/logs/data_validation/`. Both trees are gitignored.

## 2. Credentials and the env file

Everything the instance needs is in `instances/policy_db/.env` — gitignored, because it holds the role password and every bearer token. `instances/policy_db/.env.example` is the committed template; copy it to `.env` and fill it in.

It connects as `mcp_ro_policy`, a dedicated **non-superuser, read-only** role. The server derives the databases it serves from that role's `CONNECT` grants, so the served set cannot drift from the grants — there is no allow-list to keep in sync.

`MCP_INSTRUCTIONS` in the env file is what a connecting agent is told this instance holds. Keep it describing the domain (the three schemas, the discover-then-query flow, read-only), not the tooling: it is the only orientation the agent gets before it starts calling tools, and it is the cheapest place to stop an agent from guessing at table names.

## 3. Grants

`policy_db`'s read-only grants live with the database's DDL owner, not here: `ingestion_pipeline`, at `instances/policy_db/sql/mcp_ro_policy_grants.sql` (alongside `policy_db_description.sql` and `create_policy_db.sql`).

- **Re-apply the grants after every corpus rebuild.** A rebuild drops and recreates the objects, and the grants go with them. The script is idempotent.
- Grants take effect **per query** — no server restart is needed after re-applying them.
- Adding a schema or a table to the corpus is usually a grants-only change on this side: the server introspects, so a new source table plus its embedding table needs no server code change, only `USAGE`/`SELECT` for `mcp_ro_policy`.

## 4. Token onboarding

Tokens are the `MCP_AUTH_TOKENS=label:token[,label:token]` list in this instance's `.env` (mechanics in MAINTAINING.server.md §4).

1. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Append `their_label:their_token` to `MCP_AUTH_TOKENS` and restart the instance.
3. The user sets `MCP_POLICY_DB_TOKEN` to their own token — `export MCP_POLICY_DB_TOKEN="…"` in `~/.zshrc`/`~/.bashrc`, or `[Environment]::SetEnvironmentVariable("MCP_POLICY_DB_TOKEN","…","User")` on Windows — and restarts their client. The repo-root `.mcp.json` reads that variable; it never holds a token.

Revoking is the same edit in reverse plus a restart.

## 5. The search-equivalence baseline

The gate for any change that could move search results. The tool captures; you compare (engine file §7).

```bash
uv run data-val-search-equivalence \
    --config instances/policy_db/config/data_val_search_equivalence.toml
```

The config holds ~15 queries spanning all three schemas, `top_k = 10`, and writes to `instances/policy_db/data/output/`. Relative paths in it resolve against `instances/policy_db/`, so the command works from any directory. The server must already be running and warmed.

**When to re-capture:** before and after anything that touches candidate generation, fusion, reranking, the pool knobs, the embedding or reranker model, or the model-stack pin — and before and after a structural change that claims to preserve behavior. Capture the "before" file *first*: once the change lands, the old behavior is gone and the baseline is unreconstructable.

**How to compare:** diff the `queries` arrays of the two JSON files. The ordered result identities must match exactly; `embedding_model` must match; `generated_at` differs by construction and is excluded. Any query returning zero rows fails the capture rather than silently producing an empty baseline.

**Where the outputs live:** `instances/policy_db/data/output/` — gitignored. A baseline you intend to compare against later must be preserved deliberately (kept out of the way, or copied outside the repo); nothing in the repo will carry it for you. The pair from the 2026-08-26 engine/instance reorg is `search_baseline_pre_reorg.json` / `search_baseline_post_reorg.json`.
