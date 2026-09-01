"""Search-equivalence baseline for the MCP server's ``search`` tool.

Calls the running MCP instance's ``search`` tool for every configured query
and records the ordered result identities — ``collection_path``, chunk id
(``chunk_number``), and RRF ``fused_score``, attributed by
``source_schema``/``source_table`` — plus the served ``MCP_EMBEDDING_MODEL``.

This is a CAPTURE-ONLY tool: it writes one JSON of ordered result identities
and compares nothing. Equivalence is a two-run workflow — capture a baseline
against the server BEFORE a change, capture again after, and diff the two
files. The ``queries`` arrays must match exactly (``generated_at`` differs by
construction; ``embedding_model`` must match).

The served embedding model is read from the same env file the instance was
launched with (the server exposes no tool that reports it), falling back to
the server's own default constant when the variable is unset — mirroring
``get_embedding_model_name`` in ``mcp_db_server.server``.

Relative ``env_file`` / ``output_json`` values in the config resolve against
the config's instance root (via ``mcp_db_server.paths``), so the command works
from any directory. A value that cannot be anchored -- no instance root, or a
path rooted under the other platform's rules -- aborts with exit 1 rather than
resolving to a guess.

Usage:
    uv run data-val-search-equivalence \
        --config instances/<instance>/config/data_val_search_equivalence.toml
"""

import argparse
import asyncio
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from mcp_db_server.logconfig import get_logger, setup_logging
from mcp_db_server.paths import (
    InstanceRootNotFoundError,
    resolve_config_path,
    resolve_log_dir,
)

logger = get_logger(__name__)

# Mirrors _DEFAULT_EMBEDDING_MODEL in server.py: the model served when
# MCP_EMBEDDING_MODEL is unset in the instance's env file.
_DEFAULT_EMBEDDING_MODEL = "ibm-granite/granite-embedding-small-english-r2"


def read_served_embedding_model(env_file: Path) -> str:
    """Read the embedding model the instance serves from its env file.

    Args:
        env_file: Path to the instance ``.env`` file the server was launched
            with (``instances/<instance>/.env``).

    Returns:
        The ``MCP_EMBEDDING_MODEL`` value, or the server's default model when
        the variable is unset/empty.

    Raises:
        FileNotFoundError: If the env file does not exist (a missing file
            would silently report the default and mask a model mismatch).
    """
    if not env_file.exists():
        logger.error(f"Env file not found: {env_file}")
        raise FileNotFoundError(f"Env file not found: {env_file}")
    values = dotenv_values(env_file)
    model = (values.get("MCP_EMBEDDING_MODEL") or "").strip()
    if not model:
        logger.warning(
            f"MCP_EMBEDDING_MODEL unset in {env_file}; "
            f"recording server default {_DEFAULT_EMBEDDING_MODEL}"
        )
        return _DEFAULT_EMBEDDING_MODEL
    logger.debug(f"Served embedding model from {env_file}: {model}")
    return model


def read_bearer_token(env_file: Path) -> str | None:
    """Extract a bearer token for the MCP endpoint from the instance env file.

    ``MCP_AUTH_TOKENS`` holds ``name:<token>`` pairs, comma-separated; any one
    token authenticates, so the first is used.

    Args:
        env_file: Path to the instance ``.env`` file the server was launched
            with (``instances/<instance>/.env``).

    Returns:
        The first configured token, or None when auth is disabled / no tokens
        are configured (the request is then sent without an Authorization
        header and the server decides).
    """
    values = dotenv_values(env_file)
    raw = (values.get("MCP_AUTH_TOKENS") or "").strip()
    if not raw:
        logger.warning(f"No MCP_AUTH_TOKENS in {env_file}; sending no auth header")
        return None
    first_pair = raw.split(",")[0].strip()
    # Pairs are name:<token>; a bare token (no colon) is used as-is
    token = first_pair.split(":", 1)[1] if ":" in first_pair else first_pair
    return token.strip() or None


def extract_result_rows(tool_result: Any) -> list[dict[str, Any]]:
    """Extract the list of search-result dicts from a CallToolResult.

    Prefers the structured content (``{"result": [...]}``) the server emits in
    ``json_response`` mode; falls back to parsing text content blocks as JSON.

    Args:
        tool_result: The ``CallToolResult`` returned by ``session.call_tool``.

    Returns:
        The search results as a list of dicts (may be empty).

    Raises:
        ValueError: If the tool reported an error or no content block parses
            as a JSON list.
    """
    if tool_result.isError:
        raise ValueError(f"search tool returned an error: {tool_result.content}")

    structured = getattr(tool_result, "structuredContent", None)
    if isinstance(structured, dict) and isinstance(structured.get("result"), list):
        return structured["result"]

    for block in tool_result.content:
        if getattr(block, "type", None) == "text":
            parsed = json.loads(block.text)
            if isinstance(parsed, list):
                return parsed
    raise ValueError("No content block in the tool result parsed as a JSON list")


def result_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Reduce one search result to its comparable identity.

    Identity is the attributed chunk (``source_schema``/``source_table`` +
    ``collection_path`` + ``chunk_number``) and its RRF ``fused_score``; the
    chunk text and other display columns are deliberately dropped so the
    baseline stays small and the diff readable.

    Args:
        row: One result dict from the ``search`` tool.

    Returns:
        Dict with source_schema, source_table, collection_path, chunk_number,
        and fused_score (missing keys recorded as None).
    """
    return {
        "source_schema": row.get("source_schema"),
        "source_table": row.get("source_table"),
        "collection_path": row.get("collection_path"),
        "chunk_number": row.get("chunk_number"),
        "fused_score": row.get("fused_score"),
    }


async def run_queries(
    mcp_url: str,
    token: str | None,
    database: str,
    queries: list[dict[str, Any]],
    default_top_k: int,
) -> list[dict[str, Any]]:
    """Run every configured query against the server's ``search`` tool.

    Args:
        mcp_url: Streamable-HTTP MCP endpoint URL (e.g. http://host:8000/mcp).
        token: Bearer token for the endpoint, or None for no auth header.
        database: Database name passed to the ``search`` tool.
        queries: Query specs, each with ``schema`` and ``query`` (and an
            optional per-query ``top_k``).
        default_top_k: ``top_k`` used when a query spec does not set its own.

    Returns:
        One record per query: the spec plus the ordered result identities.

    Raises:
        ValueError: If any query returns zero rows (an empty baseline must
            not silently pass the post-split comparison).
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    captured: list[dict[str, Any]] = []

    async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            logger.info(f"MCP session initialized against {mcp_url}")

            for spec in queries:
                schema = spec["schema"]
                query = spec["query"]
                top_k = int(spec.get("top_k", default_top_k))

                tool_result = await session.call_tool(
                    "search",
                    {
                        "database": database,
                        "schema": schema,
                        "query": query,
                        "top_k": top_k,
                    },
                )
                rows = extract_result_rows(tool_result)
                if not rows:
                    logger.error(f"Query returned no rows: {schema} / {query!r}")
                    raise ValueError(
                        f"Query returned no rows (schema={schema}, "
                        f"query={query!r}); an empty baseline cannot gate "
                        "the post-split comparison"
                    )
                identities = [result_identity(row) for row in rows]
                logger.info(
                    f"schema={schema} top_k={top_k} rows={len(rows)} "
                    f"query={query!r}"
                )
                captured.append(
                    {
                        "schema": schema,
                        "query": query,
                        "top_k": top_k,
                        "result_count": len(rows),
                        "results": identities,
                    }
                )
    return captured


def main() -> None:
    """Run the configured search queries and write the baseline JSON."""
    parser = argparse.ArgumentParser(
        description="Capture an MCP search-equivalence baseline"
    )
    parser.add_argument(
        "--config", type=str, required=True, help="Path to TOML configuration file"
    )
    args = parser.parse_args()

    config_path = Path(args.config)

    # Setup logging AFTER argparse so --help doesn't create log files, and
    # under the config's own instance so a run from any directory logs to the
    # instance it validated rather than to the CWD. A config with no
    # resolvable instance falls back to a temp root rather than aborting --
    # the config keys below are the ones that must resolve or stop the run.
    setup_logging(log_dir=resolve_log_dir("data_validation", config_path))
    logger.info("=" * 60)

    if not config_path.exists():
        logger.error(f"Config file not found: {args.config}")
        sys.exit(1)

    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.error(f"Failed to read config file: {e}")
        sys.exit(1)

    try:
        mcp_url = config["mcp_url"]
        env_file = resolve_config_path(config["env_file"], config_path)
        database = config["database"]
        output_json = resolve_config_path(config["output_json"], config_path)
        queries = config["queries"]
        default_top_k = int(config.get("top_k", 10))
    except KeyError as e:
        logger.error(f"Missing required config field: {e}")
        sys.exit(1)
    except (InstanceRootNotFoundError, ValueError) as e:
        # A path that cannot be anchored is not a path this run may guess at:
        # a wrong guess reads the wrong corpus or writes a baseline that
        # compares nothing. Same single abort path as every other config error.
        logger.error(f"Cannot resolve a configured path: {e}")
        sys.exit(1)

    try:
        embedding_model = read_served_embedding_model(env_file)
        token = read_bearer_token(env_file)
        captured = asyncio.run(
            run_queries(mcp_url, token, database, queries, default_top_k)
        )

        output = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mcp_url": mcp_url,
            "database": database,
            "embedding_model": embedding_model,
            "query_count": len(captured),
            "queries": captured,
        }
        # The recorded model is the equivalence precondition: assert it made
        # it into the output so a model mismatch shows up in the diff.
        if not output["embedding_model"]:
            raise ValueError("Served embedding model missing from output")

        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(output, indent=2) + "\n", encoding="utf-8"
        )
        logger.info(
            f"SUCCESS: {len(captured)} queries captured -> {output_json} "
            f"(model={embedding_model})"
        )
        logger.info("=" * 60)
    except Exception as e:  # noqa: BLE001
        # Entry-point boundary: every failure of the capture (transport, tool
        # error, empty result set, write) is one reported abort with exit 1.
        logger.error(f"Failed: {e}")
        logger.info("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
