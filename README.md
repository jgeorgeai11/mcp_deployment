# mcp_deployment

The read-only MCP database server — a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) holding one engine package and the config-only instances that run it. Carved from the `rag` monorepo on 2026-08-13 (see the `pre-repo-split` tag for the joint pre-split history); the pipeline that builds the corpus lives in the sibling `ingestion_pipeline` repo.

## The engine

One installable distribution, credential-free and instance-free: it learns schemas, tables, and embedding columns from PostgreSQL at runtime and serves six tools (`list_databases`, `list_schemas`, `list_tables`, `describe_tables`, `run_sql`, `search`) over streamable HTTP with bearer-token auth.

| Package | Purpose |
|---------|---------|
| `packages/mcp_db_server/` | The server: catalog introspection, guarded read-only SQL, and reranked hybrid (dense + sparse) search, plus the search-equivalence capture tool |

Installing the workspace (`uv sync`) installs both entry points as console scripts:

```
mcp-db-server  data-val-search-equivalence
```

## Instances

An instance is a directory under `instances/` that owns everything about one served database: its `.env` (connection credentials, bearer tokens, bind port, instructions), its validation config, and its logs and data outputs. Instances own no code — they are configuration, so they are not workspace members.

Current instances: `instances/policy_db/` (the CMS policy corpus, port 8000) and `instances/metadata_db/` (the metadata catalog, port 8002). The server serves databases this repo does not build: it is shared infrastructure, not a pipeline component, and each database's read-only grants live with that database's DDL owner — `policy_db`'s in `ingestion_pipeline` (`instances/policy_db/sql/mcp_ro_policy_grants.sql`), `metadata_db`'s in the `metadata_db` repo (`code/apply_ddl/grants/mcp_ro_metadata.sql`).

Onboarding a new instance is `mkdir instances/<name>`, copy an `.env.example` to `instances/<name>/.env`, and add the role's grants in the repo that owns that database.

## Quick start

```bash
uv sync                                            # Python >= 3.13
uv run pytest                                      # the full suite, from any directory
uv run mcp-db-server --env-file instances/policy_db/.env
```

The `--env-file` flag is required — there is no default instance. Allow ~1 minute for model warm-up (measured: 36s), then `GET /health` returns 200.

## Documentation

- [MAINTAINING.server.md](MAINTAINING.server.md) — maintaining the engine: workspace layout, setup and testing, running an instance, auth operations, tuning knobs, the data interface the server reads, and gotchas.
- [MAINTAINING.instance.policy_db.md](MAINTAINING.instance.policy_db.md) — operating the policy_db instance: the served corpus, credentials and grants, token onboarding, and the search-equivalence baseline workflow.
- [MAINTAINING.instance.metadata_db.md](MAINTAINING.instance.metadata_db.md) — operating the metadata_db instance: the served catalog, credentials and grants, and why it runs without models.

`docs/activities/` and `docs/code_review/` are dated work records produced by the development workflow, not documentation — see the maintaining files for current truth.
