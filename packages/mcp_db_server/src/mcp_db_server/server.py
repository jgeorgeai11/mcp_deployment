"""Role-scoped MCP server for querying a project PostgreSQL database.

Provides schema introspection, filtered table queries, and reranked semantic
search across the databases the server's read-only Postgres role can connect
to. Tools follow a discover-then-query pattern:
list_databases -> list_schemas -> list_tables -> describe_tables -> run_sql/search.

A single binary runs as N scoped instances: each instance is one process with
its own env file (``instances/<name>/.env``), its own scoped read-only role, its
own bearer-token set, and its own bind port. The served-database set is derived
from the role's ``CONNECT`` grants (not an app-level allow-list), so it cannot
drift from the grants.

Tools:
    list_databases  - Discover the databases the role can connect to
    list_schemas    - Discover schemas the role has USAGE on in a database
    list_tables     - List tables the role can SELECT in a schema
    describe_tables - Show column names and types for one or more tables
    run_sql         - Execute a read-only, single-statement SQL query
    search          - Hybrid (dense + sparse) candidate generation fused with
                      reciprocal rank fusion, then cross-encoder reranking

Configuration (environment variables):
    Connection / instance (no code defaults; required unless noted):
        POSTGRES_HOST       - PostgreSQL host
        POSTGRES_PORT       - PostgreSQL port
        POSTGRES_USER       - Scoped read-only role name
        POSTGRES_PASSWORD   - Role password
        POSTGRES_DB         - Home database for catalog introspection bootstrap
        MCP_AUTH_TOKENS     - Bearer tokens as ``label:token[,label:token]``;
                              when unset/empty every /mcp request is rejected
        MCP_DISABLE_AUTH    - Truthy (1/true/yes/on) serves /mcp with NO auth
                              (every request accepted). Removes the only gate in
                              front of the DB tools. The endpoint is still
                              network-reachable behind a reverse proxy (the
                              documented deployment) or on a shared host, so a
                              127.0.0.1 bind is not by itself protection. Enable
                              only on a genuinely isolated deployment.
                              Default: false (auth on).
        MCP_INSTANCE_NAME   - Instance label (default: the name of the directory
                              holding the env file, i.e. <instance> for
                              instances/<instance>/.env)
        MCP_INSTRUCTIONS    - Server instructions for the MCP initialize response
                              (default: a generic discover-then-query orientation);
                              override to describe this instance's data domain
        MCP_HOST            - Bind host (default: 127.0.0.1)
        MCP_PORT            - Bind port (default: 8000)
        MCP_ENV_FILE        - Path to the instance env file, the alternative to
                              the ``--env-file`` flag. One of the two is
                              REQUIRED: there is no default instance, and a
                              relative path resolves against the current
                              working directory. With neither supplied the
                              process exits 2 with the argparse usage error;
                              with a path that does not exist it exits 1.
                              Logs for the run are written under that file's
                              instance (``<instance>/logs/mcp_db_server/``)
        MCP_ALLOWED_HOSTS   - Comma-separated Host-header allowlist for the MCP
                              endpoint's DNS-rebinding protection. Required for
                              access by any hostname/IP other than localhost when
                              binding a non-loopback MCP_HOST (a remote request
                              arrives with Host: <that address>, which the SDK's
                              default localhost-only allowlist rejects with 421).
                              Each entry is an exact Host value (e.g.
                              "10.0.0.5:8000") or a port wildcard ("10.0.0.5:*").
                              Set to "*" to DISABLE rebinding protection entirely.
                              Unset (default) keeps the SDK's localhost-only
                              allowlist. Behind a Host-rewriting reverse proxy
                              this can stay unset.
        MCP_ALLOWED_ORIGINS - Comma-separated Origin-header allowlist (same format;
                              only consulted when MCP_ALLOWED_HOSTS names explicit
                              hosts). Default: empty (browser Origins rejected;
                              non-browser clients send no Origin and are allowed)

    Model selection:
        MCP_EMBEDDING_MODEL - Query-embedding model
                              (default: ibm-granite/granite-embedding-small-english-r2)
        MCP_RERANK_MODEL    - Cross-encoder reranker
                              (default: BAAI/bge-reranker-base)
        MCP_TRUST_REMOTE_CODE - Allow models to execute repo-shipped custom code
                              on load (default: false). Leave off unless an
                              instance uses a custom-architecture model.

    Tuning overrides (each falls back to a ``_DEFAULT_*`` code constant, read at
    use-time so the per-instance env file takes effect after load_dotenv):
        MCP_MAX_ROWS            - run_sql row cap (default: 500)
        MCP_STATEMENT_TIMEOUT_S - run_sql statement timeout, integer seconds
                                  (default: 5)
        MCP_RERANK_POOL         - Search candidate-pool depth (default: 50)
        MCP_RERANK_GLOBAL_POOL  - Cap on the merged cross-table candidate pool
                                  sent to the reranker, so multi-schema latency
                                  does not scale with schema count (default:
                                  MCP_RERANK_POOL; raised to top_k when larger).
                                  Lower it to trade a little recall for faster
                                  reranking.
        MCP_DB_POOL_SIZE        - SQLAlchemy connection-pool size (default: 5)
        MCP_DB_MAX_OVERFLOW     - SQLAlchemy pool max overflow (default: 10)
        MCP_HNSW_EF_SEARCH      - pgvector HNSW ef_search per connection
                                  (default: 100)
        MCP_LOG_LEVEL           - Logging level name (default: INFO)
        MCP_WARM_MODELS         - Eager-load + warm the embedding and reranker
                                  models at startup so the first search is not a
                                  cold start (default: true; set false to skip)
"""

import argparse
import logging
import math
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sentence_transformers import CrossEncoder, SentenceTransformer
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_db_server.logconfig import get_logger, setup_logging
from mcp_db_server.paths import resolve_log_dir

# Canonical SQL-identifier validator (imported, not re-implemented): the
# shared copy lives in mcp_db_server/validators.py.
from mcp_db_server.validators import validate_sql_identifier

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Tuning defaults (env-overridable at use-time)
# ---------------------------------------------------------------------------
# Each default below is a reviewable code constant. The matching MCP_* env var
# overrides it, but is read at USE-TIME (not import-time) because the per-
# instance ``instances/<name>/.env`` is loaded by ``load_dotenv`` in ``main()``,
# after this module is imported -- an import-time read would miss it. This
# mirrors how ``get_embedding_model_name`` reads its env var at call time.

# Default maximum rows returned by run_sql (override: MCP_MAX_ROWS).
_DEFAULT_MAX_ROWS = 500

# Default run_sql statement timeout in integer seconds
# (override: MCP_STATEMENT_TIMEOUT_S).
_DEFAULT_STATEMENT_TIMEOUT_S = 5

# Default per-table candidate-pool depth fed into the reranker, deeper than
# top_k so the reranker has a real shortlist to reorder
# (override: MCP_RERANK_POOL).
_DEFAULT_RERANK_POOL = 50

# Default SQLAlchemy connection-pool sizing (overrides: MCP_DB_POOL_SIZE,
# MCP_DB_MAX_OVERFLOW).
_DEFAULT_DB_POOL_SIZE = 5
_DEFAULT_DB_MAX_OVERFLOW = 10

# Default pgvector HNSW ef_search applied per connection. Set >= the rerank pool
# so the dense leg's ``limit pool_size`` is not recall-starved
# (override: MCP_HNSW_EF_SEARCH).
_DEFAULT_HNSW_EF_SEARCH = 100

# Default logging level name used in main() (override: MCP_LOG_LEVEL).
_DEFAULT_LOG_LEVEL = "INFO"

# Default server instructions returned in the MCP `initialize` response -- the
# server's orientation prompt (purpose + workflow + constraints), shown to the
# client agent at connect. Kept GENERIC because one codebase runs as many scoped
# instances; override per instance with MCP_INSTRUCTIONS to describe its specific
# data domain. The per-database descriptions (list_databases) carry the domain.
_DEFAULT_INSTRUCTIONS = (
    "Read-only access to one or more PostgreSQL databases reachable by this "
    "server's scoped role. Use a discover-then-query workflow: list_databases -> "
    "list_schemas -> list_tables -> describe_tables (columns + primary keys + "
    "foreign-key references = the join graph), then `search` for natural-language "
    "semantic retrieval over the embedding tables (hybrid dense + keyword, "
    "reranked) or `run_sql` for precise read-only SQL. Call list_databases first -- "
    "each database's description explains its data domain. All access is strictly "
    "read-only."
)


def get_instructions() -> str:
    """Return the server instructions (``MCP_INSTRUCTIONS`` env or the default).

    Read at call time (after ``load_dotenv`` in ``main()``) so a per-instance env
    file can supply domain-specific instructions.

    Returns:
        The instructions string for the MCP ``initialize`` response.
    """
    return os.environ.get("MCP_INSTRUCTIONS", _DEFAULT_INSTRUCTIONS)


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    """Read an integer tuning knob from the environment, with a fallback.

    Reads the env var ``name`` at call time and parses it as an int. A missing
    or non-integer value falls back to ``default`` (a non-integer value is
    additionally logged at WARNING). Reading at call time -- not at import time
    -- is required: the per-instance env file is loaded after this module is
    imported, so an import-time read would miss the override.

    When ``minimum`` is given, the resolved value (override or default) is
    floored at it: a value below ``minimum`` is clamped to ``minimum`` and logged
    at WARNING. This guards against a misconfiguration silently disabling a
    safety knob -- e.g. ``MCP_STATEMENT_TIMEOUT_S=0`` means "no timeout" in
    PostgreSQL, and ``MCP_DB_POOL_SIZE=0`` means an unbounded SQLAlchemy pool.

    Args:
        name: Environment variable name.
        default: Value used when the variable is unset or not an integer.
        minimum: Optional inclusive floor; values below it are clamped to it
            (with a WARNING). ``None`` (the default) applies no floor.

    Returns:
        The parsed integer override (or ``default`` on absence/parse failure),
        clamped up to ``minimum`` when one is supplied.
    """
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                f"Ignoring non-integer {name}={raw!r}; using default {default}"
            )
            value = default

    if minimum is not None and value < minimum:
        logger.warning(
            f"{name}={value} is below the minimum {minimum}; "
            f"clamping to {minimum}"
        )
        return minimum
    return value


def _rerank_pool() -> int:
    """Resolve the reranker candidate-pool depth from the environment.

    Reads ``MCP_RERANK_POOL`` at call time and falls back to
    ``_DEFAULT_RERANK_POOL``. Shared by ``search`` (pool cut) and
    ``_search_single_table`` (per-leg depth floor) so both stay consistent.

    Returns:
        The candidate-pool depth.
    """
    return _env_int("MCP_RERANK_POOL", _DEFAULT_RERANK_POOL, minimum=1)


def _rerank_global_pool() -> int:
    """Resolve the global (cross-table) reranker-pool cap from the environment.

    Reads ``MCP_RERANK_GLOBAL_POOL`` at call time and falls back to
    ``_rerank_pool()`` -- so out of the box the global cap equals the per-table
    pool depth, and a search touching a single embedding table (at most that
    many candidates) is unaffected; searches touching multiple tables (multiple
    schemas, or a single multi-table schema) are trimmed to the cap. ``search``
    uses this to bound the merged candidate pool sent to the cross-encoder, so
    rerank cost does not scale with the number of schemas/tables searched.

    Returns:
        The global candidate-pool cap.
    """
    return _env_int("MCP_RERANK_GLOBAL_POOL", _rerank_pool(), minimum=1)


# Default query-embedding model. Set per instance via MCP_EMBEDDING_MODEL; this
# fallback matches the granite embeddings produced during ingestion
# (generate_embeddings.py uses ibm-granite/granite-embedding-small-english-r2,
# 384-dim) so the default already lines up with the stored vectors.
_DEFAULT_EMBEDDING_MODEL = "ibm-granite/granite-embedding-small-english-r2"

# Default cross-encoder reranker. Set per instance via MCP_RERANK_MODEL.
# The activity decided on "Ettin-reranker-150M", but cross-encoder/ettin-reranker-150m-v1
# declares an unavailable tokenizer class ("TokenizersBackend") and fails to load
# under transformers 4.57.6 / sentence-transformers 5.2.2. We fall back to the
# activity's listed alternative, BAAI/bge-reranker-base (~278M, well-established,
# CPU-feasible for the ~50-candidate shortlist). Override via MCP_RERANK_MODEL.
_DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"

# Column whose text is scored by the cross-encoder during reranking.
# Invariant: reranking assumes the embedded-text column is literally named
# ``chunk_text``, as produced by generate_embeddings.py (which always
# materializes the embedded text into a ``chunk_text`` column, even when the
# source ``embed_columns`` is ``row_text``). A vector table built by any other
# path whose text column is named differently would feed the cross-encoder an
# empty string; ``search`` logs a warning in that case rather than silently
# degrading rank quality (see the empty-chunk_text count there).
_RERANK_TEXT_COLUMN = "chunk_text"

# Global database engines keyed by database name (lazy loaded).
_engines: dict[str, Engine] = {}

# Global query-embedding model (lazy loaded on first search).
_embedding_model: SentenceTransformer | None = None

# Global cross-encoder reranker (lazy loaded on first search).
_reranker: CrossEncoder | None = None

# Locks guarding the lazy singletons above. Tools now run in FastMCP's worker
# threadpool (the tool functions are plain ``def``; see the "Core tool
# functions" section), so two concurrent first-calls can otherwise both observe
# an uninitialized singleton and double-build an engine or double-load a model.
# Each lazy init uses double-checked locking under these locks.
_engines_lock = threading.Lock()
_embedding_model_lock = threading.Lock()
_reranker_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Engine / connection management
# ---------------------------------------------------------------------------


def _get_home_database() -> str:
    """Resolve the bootstrap (home) database for introspection connections.

    Returns:
        The home database name from ``POSTGRES_DB``.

    Raises:
        KeyError: If ``POSTGRES_DB`` is not set in the environment.
    """
    return os.environ["POSTGRES_DB"]


def get_database_engine(database: str) -> Engine:
    """Get or create the SQLAlchemy engine for a database (per-DB cache).

    Reads PostgreSQL connection parameters from environment variables
    (POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD). The engine
    is cached per database name. The Postgres role itself enforces which
    databases are reachable -- a disallowed connection is rejected by the server.

    Connection-pool sizing is read from ``MCP_DB_POOL_SIZE`` /
    ``MCP_DB_MAX_OVERFLOW`` at creation time (engine creation is lazy, so this
    runs after the instance env file is loaded). A pool ``connect`` event applies
    ``MCP_HNSW_EF_SEARCH`` to every new DBAPI connection (after a pgvector
    warm-up that triggers the extension's lazily-registered GUC); the handler
    tolerates a database without the ``vector`` extension by logging at DEBUG and
    continuing rather than failing the connection.

    Args:
        database: Name of the database to connect to.

    Returns:
        SQLAlchemy Engine instance for the specified database.

    Raises:
        RuntimeError: If required environment variables are missing.
    """
    # Double-checked locking: the fast path skips the lock once the engine is
    # cached; concurrent first-calls serialize on the lock and re-check inside.
    if database not in _engines:
        with _engines_lock:
            if database not in _engines:
                try:
                    host = os.environ["POSTGRES_HOST"]
                    port = os.environ["POSTGRES_PORT"]
                    user = os.environ["POSTGRES_USER"]
                    password = os.environ["POSTGRES_PASSWORD"]
                except KeyError as e:
                    raise RuntimeError(
                        f"Missing Postgres environment variable: {e}. "
                        "Ensure the env file has POSTGRES_HOST, POSTGRES_PORT, "
                        "POSTGRES_USER, POSTGRES_PASSWORD"
                    ) from e

                try:
                    conn_str = URL.create(
                        drivername="postgresql",
                        username=user,
                        password=password,
                        host=host,
                        port=int(port),
                        database=database,
                    )
                    engine = create_engine(
                        conn_str,
                        pool_pre_ping=True,
                        pool_size=_env_int(
                            "MCP_DB_POOL_SIZE",
                            _DEFAULT_DB_POOL_SIZE,
                            minimum=1,
                        ),
                        max_overflow=_env_int(
                            "MCP_DB_MAX_OVERFLOW",
                            _DEFAULT_DB_MAX_OVERFLOW,
                            minimum=0,
                        ),
                    )

                    # Apply HNSW ef_search per new DBAPI connection. Read the ef
                    # value now (creation time, after load_dotenv) so the handler
                    # closes over the resolved value.
                    ef_search = _env_int(
                        "MCP_HNSW_EF_SEARCH",
                        _DEFAULT_HNSW_EF_SEARCH,
                        minimum=1,
                    )

                    @event.listens_for(engine, "connect")
                    def _set_hnsw_ef_search(
                        dbapi_connection: Any, connection_record: Any
                    ) -> None:
                        """Warm up pgvector and set hnsw.ef_search per connection.

                        pgvector registers ``hnsw.ef_search`` lazily on first use
                        of a vector op, so a warm-up vector cast must run before
                        the ``SET`` succeeds. A database without the ``vector``
                        extension makes both the warm-up and the SET raise; that
                        is tolerated (logged at DEBUG) so connecting to a non-
                        vector database does not fail.

                        Args:
                            dbapi_connection: The raw DBAPI connection.
                            connection_record: SQLAlchemy's pool record (unused).
                        """
                        try:
                            # Context-manage the cursor so it is closed on every
                            # path, including when the warm-up cast raises on a
                            # database without the ``vector`` extension.
                            with dbapi_connection.cursor() as cur:
                                cur.execute("select '[1]'::vector")
                                cur.execute(
                                    f"set hnsw.ef_search = {int(ef_search)}"
                                )
                        except Exception as exc:  # noqa: BLE001 - see below
                            # Deliberately broad: any DBAPI error here means the
                            # optional pgvector warm-up is unavailable, and the
                            # connection must still be usable.
                            # No vector extension (or SET unsupported): continue.
                            # The failed cast leaves the DBAPI transaction in an
                            # aborted state; roll it back so this pooled connection
                            # is usable. Without this, the first real query on it
                            # fails with "current transaction is aborted" -- only
                            # hit on databases without pgvector.
                            try:
                                dbapi_connection.rollback()
                            except Exception:  # noqa: BLE001
                                # Best-effort cleanup on an already-failing
                                # path: a driver that cannot roll back leaves
                                # the pool to discard this connection.
                                pass
                            logger.debug(
                                "Skipping hnsw.ef_search setup on "
                                f"{database}: {exc}"
                            )

                    _engines[database] = engine
                    logger.info(f"Database engine created for {database}")

                except SQLAlchemyError as e:
                    logger.error(
                        f"Failed to create database engine for {database}: {e}"
                    )
                    raise

    return _engines[database]


def _get_bootstrap_engine() -> Engine:
    """Get an engine on the home database for catalog introspection.

    Connects to ``POSTGRES_DB`` (the home database from the env creds) to run the
    ``has_database_privilege`` introspection that derives the served-database
    set. This runs before the served set is known, so a home database must be
    configured explicitly.

    Returns:
        SQLAlchemy Engine on the home database.

    Raises:
        RuntimeError: If ``POSTGRES_DB`` is not set.
    """
    try:
        home = _get_home_database()
    except KeyError as e:
        raise RuntimeError(
            "Cannot bootstrap introspection: set POSTGRES_DB to a home "
            "database the role can connect to."
        ) from e
    return get_database_engine(home)


def _fetch_databases() -> list[dict[str, str | None]]:
    """Query the role-derived set of connectable databases.

    Runs the ``has_database_privilege ... 'CONNECT'`` introspection on the home
    database. Called fresh on every ``list_databases`` call and on every
    ``get_served_databases`` resolution so the served set reflects the role's
    current grants without a process restart.

    Returns:
        List of dicts with ``database_name`` and ``description`` keys.
    """
    engine = _get_bootstrap_engine()

    sql = text("""
        select
            d.datname as database_name,
            pg_catalog.shobj_description(d.oid, 'pg_database') as description
        from pg_catalog.pg_database d
        where d.datistemplate = false
          and has_database_privilege(current_user, d.datname, 'CONNECT')
        order by d.datname
    """)

    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()

    return [{"database_name": row[0], "description": row[1]} for row in rows]


def get_served_databases() -> set[str]:
    """Return the set of databases the role can CONNECT to (queried fresh).

    Derived from the role's grants via ``has_database_privilege`` so the served
    set tracks the Postgres grants and cannot drift from an app-level list. This
    queries the catalog fresh on every call (no process-lifetime cache) so a
    ``GRANT``/``REVOKE CONNECT`` is reflected immediately and this function
    always agrees with ``list_databases`` (both route through
    ``_fetch_databases``). The introspection is one cheap query, and tools run
    in a worker thread, so the per-call cost is fine for an internal server.

    Returns:
        Set of database names the current role may connect to.
    """
    # The row dicts are typed str | None because ``description`` is a
    # nullable catalog comment; ``database_name`` is pg_database.datname,
    # which is NOT NULL.
    return {r["database_name"] for r in _fetch_databases()}  # type: ignore[misc]


def _validate_database(database: str) -> str:
    """Validate a database arg against the role-derived served set.

    Args:
        database: Database name to validate.

    Returns:
        The validated database name.

    Raises:
        ValueError: If the database is not in the role's served set.
    """
    served = get_served_databases()
    if database not in served:
        raise ValueError(
            f"Database {database!r} is not served by this instance. "
            f"Available databases: {sorted(served)}"
        )
    return database


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def get_embedding_model_name() -> str:
    """Resolve the query-embedding model name from the environment.

    Reads ``MCP_EMBEDDING_MODEL`` and falls back to the granite model used
    during ingestion so the query model tracks the stored embeddings without a
    code change.

    Returns:
        HuggingFace model name to load for query encoding.
    """
    return os.environ.get("MCP_EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL)


def _trust_remote_code() -> bool:
    """Whether model loading may execute repo-shipped custom code.

    Reads ``MCP_TRUST_REMOTE_CODE`` at call time; defaults to False (no remote
    code execution at load). The default granite embedding and bge reranker
    models both load fine with it off. Set it true only for an instance that
    deliberately uses a custom-architecture model shipping its own modeling code
    -- doing so re-opens the code-execution-at-load surface, so scope it to that
    instance's env file.

    Returns:
        True only when MCP_TRUST_REMOTE_CODE is a truthy string
        (1/true/yes/on, case-insensitive); False otherwise.
    """
    return os.environ.get("MCP_TRUST_REMOTE_CODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def get_embedding_model() -> SentenceTransformer:
    """Get or create the query-embedding model (lazy, cached).

    Loads the model named by ``MCP_EMBEDDING_MODEL`` (default the granite
    embedding model) on first use. This model must match the one used during
    ingestion, otherwise query and stored vectors live in different spaces and
    cosine scores are meaningless; the dimension guard in ``search`` catches the
    common dimension-mismatch case loudly. ``trust_remote_code`` defaults to
    False (set per instance via ``MCP_TRUST_REMOTE_CODE``); the default model
    loads fine without it.

    Returns:
        SentenceTransformer model for generating query embeddings.
    """
    global _embedding_model

    # Double-checked locking: tools run concurrently in the threadpool, so guard
    # the one-time load against two first-calls both observing None.
    if _embedding_model is None:
        with _embedding_model_lock:
            if _embedding_model is None:
                try:
                    model_name = get_embedding_model_name()
                    logger.info(f"Loading embedding model: {model_name}")
                    _embedding_model = SentenceTransformer(
                        model_name, trust_remote_code=_trust_remote_code()
                    )
                    logger.info(
                        f"Model loaded: {model_name}, embedding dimension: "
                        f"{_embedding_model.get_sentence_embedding_dimension()}"
                    )

                except (OSError, RuntimeError) as e:
                    logger.error(f"Failed to load embedding model: {e}")
                    raise

    return _embedding_model


def get_rerank_model_name() -> str:
    """Resolve the cross-encoder reranker model name from the environment.

    Reads ``MCP_RERANK_MODEL`` and falls back to the default reranker constant,
    currently ``BAAI/bge-reranker-base``.

    Returns:
        HuggingFace model name to load for cross-encoder reranking.
    """
    return os.environ.get("MCP_RERANK_MODEL", _DEFAULT_RERANK_MODEL)


def get_reranker() -> CrossEncoder:
    """Get or create the cross-encoder reranker (lazy, cached).

    Loads the model named by ``MCP_RERANK_MODEL`` (default the reranker
    constant, currently ``BAAI/bge-reranker-base``) on first use. Used to rerank
    the RRF candidate shortlist by scoring ``(query, chunk_text)`` pairs.

    Returns:
        CrossEncoder model for reranking search candidates.
    """
    global _reranker

    # Double-checked locking: tools run concurrently in the threadpool, so guard
    # the one-time load against two first-calls both observing None.
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                try:
                    model_name = get_rerank_model_name()
                    logger.info(f"Loading reranker model: {model_name}")
                    _reranker = CrossEncoder(
                        model_name, trust_remote_code=_trust_remote_code()
                    )
                    logger.info(f"Reranker loaded: {model_name}")

                except (OSError, RuntimeError) as e:
                    logger.error(f"Failed to load reranker model: {e}")
                    raise

    return _reranker


def _warm_models_enabled() -> bool:
    """Whether to eager-load + warm the models at startup.

    Reads ``MCP_WARM_MODELS`` at call time; defaults to True (warm on). Disable
    with a falsy value (0/false/no/off) for a fast dev start that accepts the
    first query paying the model-load cost.

    Returns:
        True unless MCP_WARM_MODELS is explicitly falsy.
    """
    return os.environ.get("MCP_WARM_MODELS", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def warm_models() -> None:
    """Eager-load and warm the embedding + reranker models.

    Loads both singletons via the lazy getters and runs one tiny inference on
    each to trigger torch's first-call initialization, so no user query pays the
    ~model-load + first-inference cold start. Called at startup from ``main()``
    when ``MCP_WARM_MODELS`` is enabled. Only ``main()`` warms; a factory-mode
    deployment that builds the app via ``create_app`` directly would instead warm
    lazily on the first search.
    """
    logger.info("Warming models (embedding + reranker)...")
    get_embedding_model().encode("warmup")
    get_reranker().predict([("warmup query", "warmup document")])
    logger.info("Models warmed")


# ---------------------------------------------------------------------------
# Table introspection helpers
# ---------------------------------------------------------------------------


def _get_table_columns(engine: Engine, schema: str, table: str) -> list[str]:
    """Get column names for a table from information_schema.

    Args:
        engine: SQLAlchemy Engine instance.
        schema: PostgreSQL schema name (pre-validated).
        table: Table name (pre-validated).

    Returns:
        List of column names in ordinal order.
    """
    sql = text("""
        select column_name
        from information_schema.columns
        where table_schema = :schema and table_name = :table
        order by ordinal_position
    """)
    with engine.connect() as conn:
        result = conn.execute(sql, {"schema": schema, "table": table})
        return [row[0] for row in result.fetchall()]


def _get_vector_columns(engine: Engine, schema: str, table: str) -> list[str]:
    """Get vector column names for a table.

    Args:
        engine: SQLAlchemy Engine instance.
        schema: PostgreSQL schema name (pre-validated).
        table: Table name (pre-validated).

    Returns:
        List of column names with data type 'vector'.
    """
    sql = text("""
        select column_name
        from information_schema.columns
        where table_schema = :schema
          and table_name = :table
          and udt_name = 'vector'
        order by ordinal_position
    """)
    with engine.connect() as conn:
        result = conn.execute(sql, {"schema": schema, "table": table})
        return [row[0] for row in result.fetchall()]


def _get_tsvector_columns(engine: Engine, schema: str, table: str) -> list[str]:
    """Get tsvector (full-text) column names for a table.

    Args:
        engine: SQLAlchemy Engine instance.
        schema: PostgreSQL schema name (pre-validated).
        table: Table name (pre-validated).

    Returns:
        List of column names with data type 'tsvector'.
    """
    sql = text("""
        select column_name
        from information_schema.columns
        where table_schema = :schema
          and table_name = :table
          and udt_name = 'tsvector'
        order by ordinal_position
    """)
    with engine.connect() as conn:
        result = conn.execute(sql, {"schema": schema, "table": table})
        return [row[0] for row in result.fetchall()]


def _get_primary_key_columns(
    engine: Engine, schema: str, table: str
) -> list[str]:
    """Get the primary-key column names for a table.

    Used as the row-identity key for reciprocal rank fusion so the same row is
    recognised across the dense and sparse legs.

    Args:
        engine: SQLAlchemy Engine instance.
        schema: PostgreSQL schema name (pre-validated).
        table: Table name (pre-validated).

    Returns:
        Primary-key column names in key order, or an empty list when the table
        has no primary key.
    """
    inspector = inspect(engine)
    pk_constraint = inspector.get_pk_constraint(table, schema=schema)
    return pk_constraint.get("constrained_columns") or []


def _get_foreign_keys(engine: Engine, schema: str, table: str) -> dict[str, str]:
    """Map each foreign-key column to its referenced ``schema.table.column``.

    Uses the SQLAlchemy inspector, whose ``constrained_columns`` and
    ``referred_columns`` lists are positionally paired -- so a composite FK is
    split correctly into per-column references.

    Args:
        engine: SQLAlchemy Engine instance.
        schema: PostgreSQL schema name (pre-validated).
        table: Table name (pre-validated).

    Returns:
        ``{column_name: "ref_schema.ref_table.ref_column"}`` for every FK column
        on the table (empty when the table has no foreign keys). The map keeps
        one reference per column: when a column participates in more than one
        foreign key, the last constraint encountered wins (the dict is keyed on
        the constrained column).
    """
    inspector = inspect(engine)
    fk_map: dict[str, str] = {}
    for fk in inspector.get_foreign_keys(table, schema=schema):
        ref_schema = fk.get("referred_schema") or schema
        ref_table = fk.get("referred_table")
        # Defensive: a real FK always populates referred_table, but a malformed
        # reflection could omit it -- skip rather than emit "schema.None.column".
        if not ref_table:
            continue
        constrained = fk.get("constrained_columns") or []
        referred = fk.get("referred_columns") or []
        for col, ref_col in zip(constrained, referred):
            fk_map[col] = f"{ref_schema}.{ref_table}.{ref_col}"
    return fk_map


def _get_vector_dimension(
    engine: Engine, schema: str, table: str, column: str
) -> int | None:
    """Get the pgvector dimension of a vector column from sample data.

    Args:
        engine: SQLAlchemy Engine instance.
        schema: PostgreSQL schema name (pre-validated).
        table: Table name (pre-validated).
        column: Vector column name (pre-validated).

    Returns:
        The vector dimension, or None when the table is empty (no row to
        inspect, so the dimension cannot be determined).
    """
    # vector_dims() reports the dimension of an individual vector value, so a
    # sample row is needed; an empty table yields no rows -> None.
    sql = text(f"""
        select vector_dims({column}) as dims
        from {schema}.{table}
        where {column} is not null
        limit 1
    """)
    with engine.connect() as conn:
        result = conn.execute(sql)
        row = result.fetchone()
    return int(row[0]) if row is not None else None


def _list_table_names(engine: Engine, schema: str) -> list[str]:
    """List the names of tables the role can SELECT in a schema.

    Runs the same SELECT-privilege-filtered ``pg_class`` query as ``list_tables``
    (ordinary tables, ``relkind = 'r'``, gated by ``has_table_privilege(...,
    'SELECT')``), but returns only the table names. Used by ``describe_tables``
    in default-all mode to discover every role-readable table to describe.

    Args:
        engine: SQLAlchemy Engine instance.
        schema: PostgreSQL schema name (pre-validated).

    Returns:
        Role-readable table names in alphabetical order.
    """
    sql = text("""
        select c.relname as table_name
        from pg_catalog.pg_class c
        join pg_catalog.pg_namespace n on n.oid = c.relnamespace
        where n.nspname = :schema
          and c.relkind = 'r'
          and has_table_privilege(current_user, c.oid, 'SELECT')
        order by c.relname
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"schema": schema}).fetchall()
    return [row[0] for row in rows]


def _discover_embedding_tables(engine: Engine, schema: str) -> list[str]:
    """Find all tables in a schema that have at least one vector column.

    Args:
        engine: SQLAlchemy Engine instance.
        schema: PostgreSQL schema name (pre-validated).

    Returns:
        List of table names that contain vector columns, sorted alphabetically.
    """
    sql = text("""
        select distinct table_name
        from information_schema.columns
        where table_schema = :schema
          and udt_name = 'vector'
        order by table_name
    """)
    with engine.connect() as conn:
        result = conn.execute(sql, {"schema": schema})
        return [row[0] for row in result.fetchall()]


# ---------------------------------------------------------------------------
# Reciprocal rank fusion
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    ranked_lists: list[list[dict[str, Any]]],
    k: int = 60,
    key: Callable[[dict[str, Any]], Any] = lambda row: id(row),
) -> list[tuple[dict[str, Any], float, list[int]]]:
    """Fuse multiple ranked result lists with reciprocal rank fusion (RRF).

    Each list is assumed to be ordered best-first. A row's fused score is the
    sum over the lists it appears in of ``1 / (k + rank)``, where ``rank`` is its
    1-based position in that list. Cosine similarity and ``ts_rank_cd`` live on
    incomparable scales, so fusing by rank (not raw score) needs no per-system
    calibration and is robust.

    The first occurrence of a row (by ``key``) is kept as the representative
    payload; later occurrences only contribute to the fused score and record
    that the row matched in that list.

    Args:
        ranked_lists: List of ranked result lists, each best-first. The list
            index identifies the leg (e.g. 0=dense, 1=sparse); the caller maps
            indices to leg names.
        k: RRF damping constant. Defaults to 60 (the standard value).
        key: Callable mapping a row to a hashable identity, used to recognise the
            same row across lists. Defaults to object identity.

    Returns:
        List of ``(row, fused_score, list_indices)`` tuples ordered by fused
        score descending, where ``list_indices`` are the (sorted) indices of the
        lists the row appeared in.
    """
    fused_scores: dict[Any, float] = {}
    representatives: dict[Any, dict[str, Any]] = {}
    matched_indices: dict[Any, set[int]] = {}

    for list_index, ranked_list in enumerate(ranked_lists):
        for position, row in enumerate(ranked_list):
            rank = position + 1
            row_key = key(row)
            fused_scores[row_key] = (
                fused_scores.get(row_key, 0.0) + 1.0 / (k + rank)
            )
            matched_indices.setdefault(row_key, set()).add(list_index)
            if row_key not in representatives:
                representatives[row_key] = row

    fused = [
        (representatives[row_key], score, sorted(matched_indices[row_key]))
        for row_key, score in fused_scores.items()
    ]
    fused.sort(key=lambda item: item[1], reverse=True)
    return fused


# ---------------------------------------------------------------------------
# Core tool functions
# ---------------------------------------------------------------------------
# The six tools are plain ``def`` (not ``async def``) on purpose: they do
# synchronous, blocking SQLAlchemy I/O. FastMCP runs sync tools in an anyio
# worker thread, so the event loop stays free (the /health liveness probe and
# other requests are not blocked by a slow query). Because they now run
# concurrently across threads, the lazy singletons they touch are guarded by
# locks (see the "Engine / connection management" and "Model resolution"
# sections).


def list_databases() -> list[dict[str, str | None]]:
    """List the databases this instance's role can connect to.

    The served set is role-derived: a database appears only when the current
    role has ``CONNECT`` privilege on it. Descriptions come from
    ``COMMENT ON DATABASE``.

    Returns:
        List of dicts, each containing:
        - database_name: Name of the database
        - description: COMMENT ON DATABASE value (None if not set)
    """
    logger.info("Listing databases (role-derived CONNECT set)")

    try:
        results = _fetch_databases()
        logger.info(f"Found {len(results)} databases")
        return results

    except Exception as e:
        logger.error(f"Failed to list databases: {e}")
        raise


def list_schemas(database: str) -> list[dict[str, str | None]]:
    """List schemas in a database the role has USAGE on.

    Args:
        database: Database name (must be in the role's served set).

    Returns:
        List of dicts, each containing:
        - schema_name: Name of the schema
        - description: COMMENT ON SCHEMA value (None if not set)
    """
    _validate_database(database)
    logger.info(f"Listing schemas in {database}")

    try:
        engine = get_database_engine(database)

        sql = text("""
            select
                n.nspname as schema_name,
                obj_description(n.oid, 'pg_namespace') as description
            from pg_namespace n
            where n.nspname not in ('information_schema', 'public')
              and n.nspname not like 'pg_%'
              and has_schema_privilege(current_user, n.nspname, 'USAGE')
            order by n.nspname
        """)

        with engine.connect() as conn:
            rows = conn.execute(sql).fetchall()

        results = [
            {"schema_name": row[0], "description": row[1]} for row in rows
        ]

        logger.info(f"Found {len(results)} schemas in {database}")
        return results

    except Exception as e:
        logger.error(f"Failed to list schemas: {e}")
        raise


def list_tables(database: str, schema: str) -> list[dict[str, Any]]:
    """List tables the role can SELECT in a schema, with descriptions and counts.

    Args:
        database: Database name (must be in the role's served set).
        schema: PostgreSQL schema name.

    Returns:
        List of dicts, each containing:
        - table_name: Name of the table
        - description: COMMENT ON TABLE value (None if not set)
        - approximate_row_count: Approximate number of rows

    Raises:
        ValueError: If the database is not served or the schema name contains
            unsafe characters.
    """
    _validate_database(database)
    validate_sql_identifier(schema, "schema")

    logger.info(f"Listing tables in {database}.{schema}")

    try:
        engine = get_database_engine(database)

        sql = text("""
            select
                c.relname as table_name,
                pg_catalog.obj_description(c.oid, 'pg_class') as description,
                coalesce(s.n_live_tup, 0) as approximate_row_count
            from pg_catalog.pg_class c
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            left join pg_stat_user_tables s
                on s.schemaname = n.nspname and s.relname = c.relname
            where n.nspname = :schema
              and c.relkind = 'r'
              and has_table_privilege(current_user, c.oid, 'SELECT')
            order by c.relname
        """)

        with engine.connect() as conn:
            rows = conn.execute(sql, {"schema": schema}).fetchall()

        results: list[dict[str, Any]] = [
            {
                "table_name": row[0],
                "description": row[1],
                "approximate_row_count": row[2],
            }
            for row in rows
        ]

        logger.info(f"Found {len(results)} tables in {database}.{schema}")
        return results

    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to list tables: {e}")
        raise


def _describe_one_table(
    engine: Engine, schema: str, table: str
) -> list[dict[str, Any]]:
    """Build the per-column description dicts for a single table.

    Runs the ``information_schema.columns`` query (resolving USER-DEFINED types
    to their ``udt_name`` via the SQL CASE) and enriches each column with its
    primary-key membership and foreign-key reference. The raise-vs-skip decision
    for a table with no columns lives in the caller (``describe_tables``): this
    helper simply returns ``[]`` when the table yields no columns, and does so
    before touching the PK/FK helpers.

    Args:
        engine: SQLAlchemy Engine instance.
        schema: PostgreSQL schema name (pre-validated).
        table: Table name (pre-validated).

    Returns:
        List of column dicts (see ``describe_tables`` for the five fields), or an
        empty list when the table has no columns.
    """
    sql = text("""
        select
            column_name,
            case
                when data_type = 'USER-DEFINED' then udt_name
                else data_type
            end as data_type,
            is_nullable
        from information_schema.columns
        where table_schema = :schema and table_name = :table
        order by ordinal_position
    """)

    with engine.connect() as conn:
        rows = conn.execute(
            sql, {"schema": schema, "table": table}
        ).fetchall()

    if not rows:
        return []

    pk_columns = set(_get_primary_key_columns(engine, schema, table))
    fk_map = _get_foreign_keys(engine, schema, table)

    return [
        {
            "column_name": row[0],
            "data_type": row[1],
            "nullable": row[2] == "YES",
            "is_primary_key": row[0] in pk_columns,
            "references": fk_map.get(row[0]),
        }
        for row in rows
    ]


def describe_tables(
    database: str, schema: str, tables: list[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Describe the columns of one or more tables in a schema.

    Pass an explicit ``tables`` list to describe just those tables, or omit it
    (``None``/empty) to describe every table the role can SELECT in the schema
    (discovered fresh from the catalog). The result maps each table name to its
    ordered list of column descriptions.

    Args:
        database: Database name (must be in the role's served set).
        schema: PostgreSQL schema name.
        tables: Optional list of specific table names to describe. When ``None``
            or empty, all role-readable tables in the schema are described.

    Returns:
        Ordered dict mapping each table name to a list of column dicts, each
        containing:
        - column_name: Name of the column
        - data_type: PostgreSQL data type (uses udt_name for user-defined types)
        - nullable: Whether the column allows NULL values
        - is_primary_key: Whether the column is part of the primary key
        - references: For a foreign-key column, the referenced
          ``schema.table.column`` (composite FKs are split per column); None
          otherwise. Lets the caller see how these tables join to others.

    Raises:
        ValueError: If the database is not served; the schema or any explicitly
            requested table name contains unsafe characters; or an explicitly
            requested table has no columns. In default-all mode, a discovered
            table that yields no columns is skipped rather than raising.
    """
    _validate_database(database)
    validate_sql_identifier(schema, "schema")
    if tables:
        for table in tables:
            validate_sql_identifier(table, "table")

    try:
        # Resolve the engine once and thread it through the listing and
        # per-table queries so the connection budget is predictable. The engine
        # resolution and default-all listing live inside the try so a
        # SQLAlchemyError from either is caught and logged by the handler below.
        engine = get_database_engine(database)

        # Empty list and None both mean "describe all role-readable tables"; an
        # explicit non-empty list is used verbatim (validated above).
        if tables:
            target_tables = tables
            explicit = True
        else:
            target_tables = _list_table_names(engine, schema)
            explicit = False

        logger.info(
            f"Describing {len(target_tables)} table(s) in {database}.{schema} "
            f"({'explicit' if explicit else 'all role-readable'})"
        )

        results: dict[str, list[dict[str, Any]]] = {}
        # Default-all mode issues 1 + 3N round-trips (one listing query, then a
        # columns + PK + FK lookup per table). This per-table cost is a conscious
        # tradeoff: acceptable for an internal, role-scoped server over a modest
        # schema rather than batching PK/FK lookups across all tables.
        for table in target_tables:
            columns = _describe_one_table(engine, schema, table)
            if not columns:
                # Explicit request -> the table is missing/empty, which is an
                # error; discovered table -> just skip it.
                if explicit:
                    raise ValueError(
                        f"Table {schema}.{table} not found or has no columns"
                    )
                continue
            results[table] = columns

        logger.info(
            f"Described {len(results)} table(s) in {database}.{schema}"
        )
        return results

    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to describe tables: {e}")
        raise


def run_sql(database: str, sql: str) -> dict[str, Any]:
    """Execute a read-only, single-statement SQL query and return results.

    Enforces safety via:
    - Single statement only (rejects queries containing semicolons)
    - Statement timeout (``MCP_STATEMENT_TIMEOUT_S`` seconds, default 5, via
      ``set local statement_timeout``)
    - Row limit (``MCP_MAX_ROWS``, default 500) with a truncation flag
    - The database role is read-only (enforced at the connection level)

    Read-only is enforced SOLELY by the Postgres ``mcp_ro_policy`` role's
    grants, not by parsing the SQL: a single ``update``/``delete``/``drop`` with
    no semicolon passes the multi-statement guard untouched and is rejected only
    by the role. The ``;`` check below is a multi-statement guard, never a write
    guard.

    Args:
        database: Database name (must be in the role's served set).
        sql: A single SQL SELECT statement.

    Returns:
        Dict with:
        - rows: List of row dicts with column names as keys
        - row_count: Number of rows returned
        - truncated: True when more rows existed beyond the row cap

    Raises:
        ValueError: If the database is not served or the query contains multiple
            statements.
    """
    _validate_database(database)

    # Reject multiple statements: strip a single trailing ';' then reject any
    # remaining ';'. This is a multi-statement guard, NOT a write guard --
    # read-only is enforced solely by the mcp_ro_policy role (see the docstring).
    # Accepted limitation: a legitimate single statement with a ';' inside a
    # string/identifier literal (e.g. ``select ';' as x``) is a false positive
    # and gets rejected.
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise ValueError(
            "Only single SQL statements are allowed. "
            "Remove semicolons and submit one query at a time."
        )

    logger.info(f"Running SQL on {database}: {stripped[:200]}")

    try:
        engine = get_database_engine(database)

        # Read tuning knobs at use-time so the per-instance env file applies.
        max_rows = _env_int("MCP_MAX_ROWS", _DEFAULT_MAX_ROWS, minimum=1)
        timeout_s = _env_int(
            "MCP_STATEMENT_TIMEOUT_S",
            _DEFAULT_STATEMENT_TIMEOUT_S,
            minimum=1,
        )

        with engine.connect() as conn:
            # Cap runtime so a pathological query cannot tie up the connection.
            # int() on timeout_s keeps the interpolated value injection-safe.
            conn.execute(
                text(f"set local statement_timeout = '{int(timeout_s)}s'")
            )

            result = conn.execute(text(stripped))
            rows = result.fetchmany(max_rows)
            col_names = list(result.keys())

            # One extra fetch reveals whether the result was truncated.
            has_more = result.fetchone() is not None

        records = [dict(zip(col_names, row)) for row in rows]

        logger.info(
            f"Returned {len(records)} rows{' (truncated)' if has_more else ''}"
        )
        return {
            "rows": records,
            "row_count": len(records),
            "truncated": has_more,
        }

    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to run SQL: {e}")
        raise


def _search_single_table(
    engine: Engine,
    schema: str,
    table: str,
    query: str,
    query_embedding_str: str,
    pool_size: int,
    min_similarity: float,
) -> list[dict[str, Any]]:
    """Generate hybrid candidates for one embedding table (dense + sparse, RRF).

    Runs the dense leg (cosine similarity, with ``min_similarity`` applied to the
    dense leg only) and, when the table has a tsvector column, the sparse leg
    (``websearch_to_tsquery`` full-text match). The two ranked lists are fused
    with reciprocal rank fusion. A table without a tsvector column degrades
    gracefully to dense-only, still routed through RRF so every row carries a
    fused score and matched-legs field uniformly.

    This returns a deeper candidate pool (``pool_size`` rows) rather than the
    final top_k: the cross-encoder reranker downstream reorders the merged pool.

    Args:
        engine: SQLAlchemy Engine instance.
        schema: PostgreSQL schema name (pre-validated).
        table: Embedding table name (pre-validated).
        query: Natural-language query text (bound as a parameter for the sparse
            leg).
        query_embedding_str: Pre-encoded query embedding as a pgvector literal.
        pool_size: Number of fused candidates to return for this table.
        min_similarity: Minimum cosine similarity threshold (0-1) for the dense
            leg.

    Returns:
        List of candidate dicts, each with the display columns (excluding vector
        and tsvector columns), ``fused_score``, ``source_table``, and
        ``matched_legs`` (some non-empty subset of ``["dense", "sparse"]``).
    """
    vector_columns = _get_vector_columns(engine, schema, table)
    if not vector_columns:
        return []

    embedding_column = vector_columns[0]
    tsvector_columns = _get_tsvector_columns(engine, schema, table)

    all_columns = _get_table_columns(engine, schema, table)
    excluded = set(vector_columns) | set(tsvector_columns)
    display_columns = [c for c in all_columns if c not in excluded]
    select_csv = ", ".join(display_columns)

    # Per-leg candidate depth: pull at least the pool size from each leg so the
    # fusion has enough overlap before the pool cut.
    leg_depth = max(pool_size, _rerank_pool())

    # Fusion key: the table's primary-key columns identify the same row across
    # legs. Both legs select the identical display-column set, so the PK tuple
    # matches. Fall back to the full display row when the table has no PK.
    pk_columns = _get_primary_key_columns(engine, schema, table)
    fusion_pk_columns = [c for c in pk_columns if c in display_columns]
    if fusion_pk_columns:
        def fusion_key(row: dict[str, Any]) -> Any:
            return tuple(row[c] for c in fusion_pk_columns)
    else:
        def fusion_key(row: dict[str, Any]) -> Any:
            return tuple(row[c] for c in display_columns)

    # -- Dense leg: cosine similarity ------------------------------------
    # pgvector requires the <=> operand as a SQL literal (it cannot be bound), so
    # query_embedding_str is interpolated; it is validated upstream with
    # math.isfinite. The schema/table/column identifiers are validated.
    dense_sql = f"""
        select
            {select_csv}
        from {schema}.{table}
        where 1 - ({embedding_column} <=> '{query_embedding_str}'::vector)
            >= :min_similarity
        order by ({embedding_column} <=> '{query_embedding_str}'::vector) asc
        limit :leg_depth
    """
    with engine.connect() as conn:
        dense_result = conn.execute(
            text(dense_sql),
            {"min_similarity": min_similarity, "leg_depth": leg_depth},
        )
        dense_rows = [
            dict(zip(dense_result.keys(), row))
            for row in dense_result.fetchall()
        ]

    # -- Sparse leg: full-text keyword match (when a tsvector exists) ------
    sparse_rows: list[dict[str, Any]] = []
    if tsvector_columns:
        tsv_column = tsvector_columns[0]
        sparse_sql = f"""
            select
                {select_csv}
            from {schema}.{table}
            where {tsv_column} @@ websearch_to_tsquery('english', :query)
            order by
                ts_rank_cd({tsv_column}, websearch_to_tsquery('english', :query))
                desc
            limit :leg_depth
        """
        with engine.connect() as conn:
            sparse_result = conn.execute(
                text(sparse_sql),
                {"query": query, "leg_depth": leg_depth},
            )
            sparse_rows = [
                dict(zip(sparse_result.keys(), row))
                for row in sparse_result.fetchall()
            ]

    # -- Fuse with RRF -----------------------------------------------------
    # List index -> leg name. The dense leg is always present; the sparse leg is
    # included only when a tsvector column exists (dense-only fallback).
    ranked_lists = [dense_rows]
    leg_names = ["dense"]
    if tsvector_columns:
        ranked_lists.append(sparse_rows)
        leg_names.append("sparse")

    fused = reciprocal_rank_fusion(ranked_lists, key=fusion_key)

    results: list[dict[str, Any]] = []
    for row, fused_score, list_indices in fused[:pool_size]:
        row_dict = {c: row[c] for c in display_columns}
        row_dict["fused_score"] = fused_score
        row_dict["source_table"] = table
        row_dict["matched_legs"] = [leg_names[i] for i in list_indices]
        results.append(row_dict)

    return results


def search(
    database: str,
    schema: str | list[str],
    query: str,
    top_k: int = 10,
    min_similarity: float = 0.3,
    tables: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Hybrid-search + cross-encoder rerank embedding tables for a query.

    Pipeline: hybrid dense (pgvector cosine) + sparse (Postgres FTS) per table,
    fused with reciprocal rank fusion to build a deep candidate pool; the merged
    pool across all searched tables is then reranked by a single cross-encoder
    pass over ``(query, chunk_text)`` pairs and cut to ``top_k`` by reranker
    score. Auto-discovers all tables with vector columns in each schema, or (for
    a single schema) searches only the specified tables. Tables without a
    tsvector column degrade to dense-only.

    ``schema`` may be a single schema name or a list of schema names. Passing a
    list searches all of them in one call: the query is encoded once, the merged
    candidate pool is reranked in a single batched cross-encoder pass, and the
    ``top_k`` cut is GLOBAL across schemas (so results are ranked comparably
    across domains). Each result carries ``source_schema`` so multi-schema hits
    are attributable.

    Before reranking, the merged candidate pool is capped globally at
    ``MCP_RERANK_GLOBAL_POOL`` (default ``MCP_RERANK_POOL``, raised to ``top_k``
    when larger): candidates are ordered by their RRF ``fused_score`` and only
    the top cap survive to the cross-encoder. This bounds rerank cost regardless
    of how many schemas/tables are searched, so multi-schema latency stays close
    to a single-schema call instead of scaling with schema count.

    A dimension guard compares the loaded query model's output dimension against
    each table's pgvector dimension and raises a clear error on mismatch instead
    of the cryptic pgvector failure.

    Args:
        database: Database name (must be in the role's served set).
        schema: A PostgreSQL schema name, or a list of schema names to search
            together (one merged rerank across all of them).
        query: Natural-language search query.
        top_k: Maximum number of results to return across all schemas/tables.
            Defaults to 10.
        min_similarity: Minimum cosine similarity threshold (0-1) applied to the
            dense leg only. Defaults to 0.3.
        tables: Optional list of specific embedding table names to search. Only
            valid with a SINGLE schema (the names are schema-qualified). When
            None, auto-discovers all vector tables in each schema.

    Returns:
        List of result dicts, each with the display columns (excluding vector and
        tsvector columns), ``fused_score``, ``source_schema``, ``source_table``,
        ``matched_legs``, and ``rerank_score``. Sorted by ``rerank_score``
        descending, limited to top_k.

    Raises:
        ValueError: If the database is not served; ``schema`` is empty or any
            schema/table name is invalid; ``tables`` is combined with more than
            one schema; no embedding tables are found; or the query model's
            dimension does not match a table's vector dimension.
    """
    _validate_database(database)
    # Normalize schema to a list; a bare string keeps the single-schema behavior.
    schemas = [schema] if isinstance(schema, str) else list(schema)
    if not schemas:
        raise ValueError("At least one schema must be provided")
    for s in schemas:
        validate_sql_identifier(s, "schema")
    # `tables` names are schema-qualified, so they only make sense for one schema.
    if tables and len(schemas) > 1:
        raise ValueError(
            "The `tables` argument names embedding tables within a single "
            "schema and cannot be combined with multiple schemas."
        )

    try:
        engine = get_database_engine(database)
        model = get_embedding_model()
        model_dim = model.get_sentence_embedding_dimension()

        # Resolve the (schema, table) pairs to search across every requested
        # schema. With `tables` (single-schema only, guarded above) those tables
        # are used verbatim; otherwise auto-discover the vector tables per schema.
        schema_tables: list[tuple[str, str]] = []
        for s in schemas:
            if tables:
                for t in tables:
                    validate_sql_identifier(t, "table")
                s_tables = tables
            else:
                s_tables = _discover_embedding_tables(engine, s)
            schema_tables.extend((s, t) for t in s_tables)

        if not schema_tables:
            raise ValueError(
                f"No embedding tables found in {database} for schema(s) "
                f"{schemas}. Ensure embeddings have been generated."
            )

        # Dimension guard per (schema, table): the model's output dim must match
        # each table's vector dim, else cosine search compares incompatible spaces.
        for s, table in schema_tables:
            validate_sql_identifier(table, "table")
            vector_columns = _get_vector_columns(engine, s, table)
            if not vector_columns:
                raise ValueError(
                    f"Table {s}.{table} is not searchable: it has no embedding "
                    "(vector) column. Generate embeddings for it, or omit it "
                    "from the `tables` argument. (Auto-discovery only returns "
                    "tables that have an embedding column, so this can only "
                    "happen for an explicitly requested table.)"
                )
            table_dim = _get_vector_dimension(engine, s, table, vector_columns[0])
            # Empty table -> dimension undeterminable; skip the guard for it.
            if table_dim is not None and table_dim != model_dim:
                raise ValueError(
                    f"Embedding dimension mismatch for {s}.{table}: table is "
                    f"{table_dim}-dim but the query model "
                    f"({get_embedding_model_name()!r}) produces {model_dim}-dim."
                    " Set MCP_EMBEDDING_MODEL to the model used to build these "
                    "embeddings."
                )

        logger.info(
            f"Searching {database} schema(s) {schemas} "
            f"({len(schema_tables)} table(s)): query={query!r}, top_k={top_k}, "
            f"min_similarity={min_similarity}"
        )

        # Structural pre-cut tripwire: the global fused_score pre-cut below
        # assumes every searched table is hybrid (dense + sparse) so candidates
        # share a comparable fused_score scale. A dense-only table (no tsvector)
        # can only produce single-leg rows, which top out at half a dual-leg
        # row's fused_score, so across >1 table the pre-cut can under-represent
        # it. Warn on that misconfiguration -- keyed on table shape (metadata),
        # not on the trim outcome, so it never fires on the normal case of a
        # schema simply having no strong matches for this query. Only meaningful
        # across more than one table (a single table faces no cross-table cut).
        if len(schema_tables) > 1:
            single_leg_tables = [
                f"{s}.{table}"
                for s, table in schema_tables
                if not _get_tsvector_columns(engine, s, table)
            ]
            if single_leg_tables:
                logger.warning(
                    f"Multi-table search includes dense-only table(s) "
                    f"{single_leg_tables} with no tsvector column: their "
                    "candidates compete at the single-leg fused_score ceiling "
                    "and may be under-represented by the global rerank pre-cut "
                    "(every embedding table is expected to be hybrid: dense + "
                    "sparse)."
                )

        # Encode the query once. This tool runs in a FastMCP worker thread (it
        # is a plain ``def``), so the CPU-bound encode does not block the loop.
        raw_embedding = model.encode(query)
        query_embedding = raw_embedding.tolist()

        # Guard against non-finite values before interpolating into SQL.
        if not all(math.isfinite(v) for v in query_embedding):
            raise ValueError("Embedding contains non-finite values")

        # pgvector requires vector literals in the SQL string; parameterized
        # binding is not supported for the <=> operator. Values are
        # model-generated floats validated with math.isfinite above.
        query_embedding_str = "[" + ",".join(map(str, query_embedding)) + "]"

        # Build a deep candidate pool per (schema, table). Stamp source_schema so
        # multi-schema results stay attributable; the pool merges across schemas.
        pool_size = max(top_k, _rerank_pool())
        candidates: list[dict[str, Any]] = []
        for s, table in schema_tables:
            table_candidates = _search_single_table(
                engine,
                s,
                table,
                query,
                query_embedding_str,
                pool_size,
                min_similarity,
            )
            for c in table_candidates:
                c["source_schema"] = s
            candidates.extend(table_candidates)

        if not candidates:
            logger.info(f"No candidates for query {query!r}")
            return []

        # Global pre-cut: bound the merged candidate pool before the expensive
        # cross-encoder rerank, so rerank cost does not scale with the number of
        # schemas/tables searched. Keep the highest-fused_score rows (the RRF
        # score each candidate already carries); never drop below top_k, which
        # the final cut needs. A stable sort keeps cross-table ties deterministic.
        global_cap = max(top_k, _rerank_global_pool())
        if len(candidates) > global_cap:
            pre_cut_count = len(candidates)
            candidates.sort(key=lambda r: r["fused_score"], reverse=True)
            candidates = candidates[:global_cap]
            logger.info(
                f"Pre-cut merged candidate pool from {pre_cut_count} to "
                f"{global_cap} by fused_score before reranking"
            )

        # Rerank the merged pool with the cross-encoder over (query, chunk_text).
        # The text column is the hardcoded _RERANK_TEXT_COLUMN invariant; warn
        # (don't silently absorb via ``or ""``) when a candidate's chunk_text is
        # missing/empty, since that points at a misnamed text column degrading
        # rank quality rather than a genuinely empty chunk.
        reranker = get_reranker()
        pairs: list[list[str]] = []
        empty_text_count = 0
        for c in candidates:
            text_value = c.get(_RERANK_TEXT_COLUMN)
            if not text_value:
                empty_text_count += 1
            pairs.append([query, str(text_value or "")])
        if empty_text_count:
            logger.warning(
                f"{empty_text_count} of {len(candidates)} rerank candidates "
                f"have an empty/missing {_RERANK_TEXT_COLUMN!r} column; these "
                "are scored against an empty string and will rank low. Check "
                "that the embedding table's text column is named "
                f"{_RERANK_TEXT_COLUMN!r}."
            )
        # sentence-transformers types predict() with an invariant list of a
        # large multimodal union; a list[list[str]] of text pairs is the
        # documented cross-encoder input and works at runtime.
        scores = reranker.predict(pairs)  # type: ignore[arg-type]

        for candidate, score in zip(candidates, scores):
            candidate["rerank_score"] = float(score)

        candidates.sort(key=lambda r: r["rerank_score"], reverse=True)
        results = candidates[:top_k]

        schemas_with_hits = {r["source_schema"] for r in results}
        tables_with_hits = {
            (r["source_schema"], r["source_table"]) for r in results
        }
        logger.info(
            f"Found {len(results)} reranked results across "
            f"{len(tables_with_hits)} table(s) in {len(schemas_with_hits)} "
            f"schema(s) for query {query!r}"
        )
        return results

    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to search: {e}")
        raise


# ---------------------------------------------------------------------------
# Authentication middleware
# ---------------------------------------------------------------------------


def parse_auth_tokens(raw: str | None) -> dict[str, str]:
    """Parse ``MCP_AUTH_TOKENS`` into a ``{token: label}`` mapping.

    Args:
        raw: The raw env value in ``label:token,label:token`` form (or None).

    Returns:
        Mapping of token -> label. Empty when ``raw`` is None/empty or contains
        no well-formed pairs.
    """
    tokens: dict[str, str] = {}
    if not raw:
        return tokens
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        label, token = pair.split(":", 1)
        label, token = label.strip(), token.strip()
        if label and token:
            tokens[token] = label
    return tokens


def _match_token(presented: str, valid_tokens: dict[str, str]) -> str | None:
    """Constant-time match a presented bearer token against the valid set.

    Args:
        presented: The token from the Authorization header.
        valid_tokens: Mapping of token -> label.

    Returns:
        The matched label, or None when no token matches. Every entry is
        compared (no early return) so the comparison does not leak which token
        matched via timing.
    """
    matched: str | None = None
    for token, label in valid_tokens.items():
        if secrets.compare_digest(presented, token):
            matched = label
    return matched


class BearerAuthMiddleware:
    """ASGI middleware enforcing bearer-token auth on the MCP endpoint.

    Requires ``Authorization: Bearer <token>`` on ``/mcp`` requests, with the
    token in the set parsed from ``MCP_AUTH_TOKENS``. ``/health`` is left open.
    Fails closed: when ``MCP_AUTH_TOKENS`` is unset/empty, every ``/mcp`` request
    is rejected and an operator error is logged. The matched label (not the
    token) is logged per accepted request for attribution.
    """

    def __init__(self, app: ASGIApp, mcp_path: str = "/mcp") -> None:
        """Initialize the middleware.

        Args:
            app: The wrapped ASGI application.
            mcp_path: Path prefix that requires authentication.
        """
        self.app = app
        self.mcp_path = mcp_path

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Authenticate ``/mcp`` requests; pass everything else through.

        Args:
            scope: ASGI scope dict.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path

        # Only guard the MCP endpoint; /health stays open for liveness probes.
        if not path.startswith(self.mcp_path):
            await self.app(scope, receive, send)
            return

        valid_tokens = parse_auth_tokens(os.environ.get("MCP_AUTH_TOKENS"))

        # Fail closed: no configured tokens means no access.
        if not valid_tokens:
            logger.error(
                "MCP_AUTH_TOKENS is unset or empty -- rejecting all MCP "
                "requests. Set MCP_AUTH_TOKENS to 'label:token[,label:token]' "
                "to enable access."
            )
            await self._reject(scope, receive, send)
            return

        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            logger.warning(
                "Rejected MCP request: missing or malformed bearer token"
            )
            await self._reject(scope, receive, send)
            return

        label = _match_token(presented, valid_tokens)
        if label is None:
            logger.warning("Rejected MCP request: unknown bearer token")
            await self._reject(scope, receive, send)
            return

        logger.info(f"Authenticated MCP request for user label: {label}")
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        """Send a 401 Unauthorized response.

        Args:
            scope: ASGI scope dict.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# Server and application setup
# ---------------------------------------------------------------------------


def _transport_security() -> TransportSecuritySettings | None:
    """Build DNS-rebinding transport-security settings from the environment.

    The MCP streamable-HTTP transport validates the request ``Host`` header
    against an allowlist (DNS-rebinding protection). FastMCP is constructed
    without a ``host`` argument, so it defaults to ``127.0.0.1`` and auto-enables
    protection allowing only localhost -- correct for a loopback bind, but it
    rejects (HTTP 421) any request whose Host is a real hostname/IP once the
    server is bound to a non-loopback ``MCP_HOST`` for remote access.

    ``MCP_ALLOWED_HOSTS`` opens that allowlist:

    * unset  -> return None; FastMCP keeps its localhost-only default (the safe,
      unchanged behavior for a loopback / behind-a-Host-rewriting-proxy deploy).
    * "*"    -> disable rebinding protection entirely (any Host accepted).
    * a comma-separated list -> enable protection with those exact Host values
      (or ``host:*`` port wildcards), plus the Origins in ``MCP_ALLOWED_ORIGINS``.

    Returns:
        A ``TransportSecuritySettings`` when ``MCP_ALLOWED_HOSTS`` is set, else
        None (defer to FastMCP's localhost-only default).
    """
    raw = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    if not raw:
        return None
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    if hosts == ["*"]:
        # Explicit opt-out: accept any Host (e.g. a trusted/isolated bind).
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = [o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def create_mcp(instance_name: str) -> FastMCP:
    """Create the FastMCP server with the six tools and a health route.

    Tools are registered as ``@mcp.tool()`` functions: type-hinted args become
    the input schema and docstrings become tool descriptions. ``mcp.tool()``
    returns the wrapped function unchanged, so the core functions stay directly
    callable (e.g. from tests).

    The transport's DNS-rebinding Host allowlist is configured from
    ``MCP_ALLOWED_HOSTS`` (see :func:`_transport_security`); unset preserves the
    SDK's localhost-only default.

    Args:
        instance_name: Human-readable instance name (shown to clients).

    Returns:
        Configured FastMCP server (stateless streamable HTTP, JSON responses).
    """
    mcp = FastMCP(
        name=instance_name,
        instructions=get_instructions(),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=_transport_security(),
    )

    mcp.tool()(list_databases)
    mcp.tool()(list_schemas)
    mcp.tool()(list_tables)
    mcp.tool()(describe_tables)
    mcp.tool()(run_sql)
    mcp.tool()(search)

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> Response:
        """Return 200 OK if the server is running.

        Args:
            request: The incoming Starlette request.

        Returns:
            A 200 plain-text response.
        """
        return Response("ok", status_code=200)

    return mcp


def _auth_disabled() -> bool:
    """Whether bearer-token auth on the MCP endpoint is disabled.

    Reads ``MCP_DISABLE_AUTH`` at call time; defaults to False (auth ON). Set it
    truthy (1/true/yes/on) to serve the MCP endpoint with NO authentication --
    every request is accepted. This removes the only access gate in front of the
    database tools (``run_sql`` executes SQL, ``search`` reads embeddings). The
    endpoint stays network-reachable whenever a reverse proxy fronts it (the
    documented deployment; see the modernize-rag-mcp-server activity) or the host
    is shared, so binding ``MCP_HOST=127.0.0.1`` is NOT by itself sufficient
    protection -- a bind host cannot be inspected to prove the port is private.
    Enable only on a genuinely isolated/trusted deployment.

    Returns:
        True only when MCP_DISABLE_AUTH is a truthy string (1/true/yes/on,
        case-insensitive); False otherwise (auth enabled).
    """
    return os.environ.get("MCP_DISABLE_AUTH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def create_app(instance_name: str) -> ASGIApp:
    """Create the ASGI app for the MCP endpoint.

    Bearer-token auth wraps the endpoint by default. When ``MCP_DISABLE_AUTH`` is
    truthy the endpoint is served with NO auth (every request accepted) and a
    loud warning is logged -- intended only for a trusted-interface bind.

    Args:
        instance_name: Human-readable instance name.

    Returns:
        ASGI application ready to serve with uvicorn.
    """
    mcp = create_mcp(instance_name)
    app = mcp.streamable_http_app()
    if _auth_disabled():
        logger.warning(
            "MCP_DISABLE_AUTH is set: the MCP endpoint is served with NO "
            "authentication -- every request is accepted and the database tools "
            "(run_sql, search) are exposed to anyone who can reach the port. The "
            "port is network-reachable whenever a reverse proxy fronts it (the "
            "documented deployment) or the host is shared, so binding 127.0.0.1 "
            "is NOT by itself sufficient protection. Enable this only on a "
            "genuinely isolated/trusted deployment."
        )
        return app
    return BearerAuthMiddleware(app, mcp_path="/mcp")


def _parse_args() -> argparse.Namespace:
    """Parse CLI flags and enforce that an instance env file was supplied.

    Reads the environment as well as the command line: the env file may come
    from ``--env-file`` or from ``MCP_ENV_FILE``, and exactly one of the two
    is required. The check lives here, with the parser in scope, so the
    failure is argparse's own usage error (stderr, exit 2) rather than a
    hand-rolled message.

    The flag is deliberately NOT declared ``required=True``: argparse
    evaluates ``required`` against the flag's presence on the command line
    and ignores any default, so declaring it would reject a correctly-set
    ``MCP_ENV_FILE``. The two do not compose.

    Returns:
        Parsed arguments. ``env_file`` is non-empty on return -- either from
        the flag or from ``MCP_ENV_FILE``; ``host`` and ``port`` may be None.

    Raises:
        SystemExit: With code 2 (via ``parser.error``) when neither
            ``--env-file`` nor ``MCP_ENV_FILE`` supplies a path.
    """
    parser = argparse.ArgumentParser(
        description="Role-scoped MCP database server."
    )
    parser.add_argument(
        "--env-file",
        help=(
            "Path to the instance env file (convention: "
            "instances/<instance>/.env). Required unless MCP_ENV_FILE is set."
        ),
    )
    parser.add_argument("--host", help="Bind host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, help="Bind port (default 8000).")
    args = parser.parse_args()

    args.env_file = args.env_file or os.environ.get("MCP_ENV_FILE")
    if not args.env_file:
        parser.error("the following arguments are required: --env-file")
    return args


def _resolve_log_level() -> int:
    """Resolve the logging level integer from ``MCP_LOG_LEVEL``.

    Reads ``MCP_LOG_LEVEL`` (default ``INFO``) and maps the name to a logging
    level int. ``logging.getLevelName`` returns a ``"Level X"`` string for an
    unknown name, so an unknown/empty value falls back to ``logging.INFO``.
    Numeric strings (e.g. ``"10"``) are not honored and likewise fall back to
    INFO -- a fail-safe, not an error.

    Returns:
        The resolved logging level integer (``logging.INFO`` on an unknown name).
    """
    level = logging.getLevelName(
        os.environ.get("MCP_LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper()
    )
    if not isinstance(level, int):
        return logging.INFO
    return level


def main() -> None:
    """Start a scoped MCP database server instance.

    The instance env file is REQUIRED, from ``--env-file`` or
    ``MCP_ENV_FILE`` (convention: ``instances/<instance>/.env``): the process
    never silently serves some default or credential-less instance. The two
    failures are distinct, because a flag that was never supplied and a path
    that does not exist are different mistakes:

    * Neither source supplies a path -- argparse usage error on stderr,
      exit 2 (raised by ``_parse_args``).
    * The path supplied does not exist -- ``error: env file not found:
      <path>`` on stderr, exit 1.

    A relative path resolves against the current working directory, ordinary
    CLI semantics.

    Remaining instance config comes from CLI flags and/or env: ``MCP_HOST``
    (default ``127.0.0.1``), ``MCP_PORT`` (default ``8000``),
    ``MCP_INSTANCE_NAME`` (default: the env file's parent directory name).
    Loads the env file, configures logging under the instance's own
    ``logs/mcp_db_server`` directory at the level named by ``MCP_LOG_LEVEL``
    (default INFO), and serves the app with uvicorn.
    """
    import uvicorn

    args = _parse_args()

    # _parse_args has already enforced that one of --env-file / MCP_ENV_FILE
    # supplied a path, aborting with the argparse usage error if neither did.
    env_path = Path(args.env_file).resolve()
    # load_dotenv is silent on a missing file, so an env path that does not
    # exist -- a typo, or a relative path run from the wrong directory -- would
    # otherwise warm the models, bind the port, and then reject every request
    # for want of tokens, with nothing saying why.
    if not env_path.is_file():
        sys.exit(f"error: env file not found: {env_path}")
    load_dotenv(dotenv_path=env_path)

    host = args.host or os.environ.get("MCP_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("MCP_PORT", "8000"))
    # The env file is always named .env, so its stem carries no instance
    # identity; the instance directory holding it does.
    instance_name = os.environ.get("MCP_INSTANCE_NAME", env_path.parent.name)

    level = _resolve_log_level()

    # Logs belong to the instance, not to whatever directory the (installed)
    # console script happened to be run from.
    setup_logging(
        log_dir=resolve_log_dir("mcp_db_server", env_path),
        log_name=instance_name,
        level=level,
    )
    logger.info("=" * 60)
    logger.info(
        f"Starting MCP database server instance {instance_name!r} "
        f"(env={env_path}, host={host}, port={port})"
    )

    # Warm the models before serving so the first search does not pay the
    # ~model-load + first-inference cold start. /health is unavailable during
    # this window (the instance is not truly ready until the models load). A
    # model-load failure here aborts startup by design (fail-closed: do not begin
    # serving an instance whose models cannot load).
    if _warm_models_enabled():
        warm_models()
    else:
        logger.info("Model warm-up skipped (MCP_WARM_MODELS disabled)")

    app = create_app(instance_name)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
