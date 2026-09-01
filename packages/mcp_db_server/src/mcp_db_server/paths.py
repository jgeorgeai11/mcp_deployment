"""Portable path anchoring for the engine and its instances.

An installed console script runs from any working directory, so no
configured path may be resolved against the CWD: a run from the wrong
directory would read no env file, scatter output beside the caller, and log
somewhere other than the instance it acted on. Every relative path anchors to
the *instance root* instead — the ``instances/<name>/`` directory the run
belongs to — which is discovered by walking up for a marker rather than by
counting directory levels, so the layout can move without the helpers
following.

The marker is a directory holding ``.env`` or ``.env.example``. This repo's
instances are config-only (they carry no ``pyproject.toml``, and the sibling
``ingestion_pipeline`` walks up for one only because its instances hold code),
and requiring a ``config/`` or ``logs/`` directory alongside would fail
``instances/metadata_db/``, which has no ``config/`` at all and whose
``logs/`` does not exist until the first run creates it. If instances ever
gain code, the marker should switch to ``pyproject.toml`` to match the
sibling.

The two anchors fail differently, on purpose:

* A *config* path with no resolvable instance root raises and aborts the run.
  Guessing where a data file belongs risks reading or writing the wrong
  corpus.
* A *log* directory with no resolvable instance root falls back to a temp
  path. Losing logs must never abort a run that is otherwise valid.

Absoluteness is decided by :func:`is_rooted_path`, never by
``Path.is_absolute()``. ``Path.is_absolute()`` answers for the running host
only, so a config value like ``C:\\corpus\\out.json`` reads as *relative* on
POSIX and would be silently resolved under the instance root. A convention
test (``tests/test_conventions.py``) forbids ``.is_absolute`` anywhere under
``src/`` except this module.
"""

import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

# The instance marker: either name identifies a directory as an instance
# root. `.env.example` is included so an instance that has been checked out
# but not yet configured still anchors -- otherwise the first run of a fresh
# clone would resolve nothing.
_INSTANCE_MARKERS = (".env", ".env.example")


class InstanceRootNotFoundError(Exception):
    """Raised when no instance root can be found for a configured path.

    Carries the path that was searched from, so the abort message names the
    file the operator actually passed rather than the directory the walk gave
    up in.
    """


def is_rooted_path(raw: str) -> bool:
    """Report whether ``raw`` carries a drive or a root under EITHER platform's rules.

    Anchors against nothing -- this is a pure syntactic test. It is
    deliberately broader than ``Path.is_absolute()``, which answers for the
    running host only: on POSIX, ``Path("C:\\corpus").is_absolute()`` is
    False, so a Windows-authored config value would be silently joined onto
    the instance root and read from a path that does not exist. Recognising
    the foreign form here lets the caller reject it instead.

    Testing ``is_absolute()`` under BOTH rule sets would not be sufficient
    either: a drive-relative value (``D:data``) and a drive-less-rooted value
    (``\\etc\\passwd``) are absolute under neither rule set, yet joining
    either under an anchor on Windows leaves the anchor -- ``D:data`` for
    another drive's working directory, ``\\etc\\passwd`` for the drive root.
    What a caller actually needs to know is "is this safe to join under a
    root?", so the test is for a drive or a root rather than for
    absoluteness.

    Args:
        raw: The path exactly as written in the config file, in string form.
            A caller holding a :class:`~pathlib.Path` must pass
            ``path.as_posix()``, never ``str(path)``: ``str()`` on a Windows
            ``Path`` renders ``/etc/passwd`` as ``\\etc\\passwd``, so the
            separator the check sees would depend on the host all over
            again.

    Returns:
        True when ``raw`` carries a drive or a root under either Windows or
        POSIX rules, and is therefore unsafe to join under an anchor; False
        for a genuinely relative value such as ``data/out.json`` or the
        empty string (note that ``..`` escapes are a separate concern this
        predicate does not judge).
    """
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw)
    # The empty string has neither a drive nor a root under either rule set,
    # so is_rooted_path("") stays False without an explicit guard.
    return bool(windows.drive or windows.root or posix.root)


def find_instance_root(start: str | Path) -> Path | None:
    """Walk up from ``start`` for the instance marker.

    Anchors against the filesystem: ``start`` and each of its parents are
    tested for a ``.env`` or ``.env.example`` file, nearest first, so a config
    nested any number of levels inside an instance still resolves to that
    instance. ``start`` may name a file or a directory and need not exist --
    a not-yet-created output path still anchors to the instance that will
    hold it.

    Args:
        start: The path to walk up from (a config file, an env file, or a
            directory).

    Returns:
        The absolute instance root, or None when no marker is found before
        the filesystem root. Returning None rather than raising is what lets
        :func:`resolve_log_dir` fall back while
        :func:`require_instance_root` aborts.

    Raises:
        Nothing. Discovery failure is reported as None.
    """
    current = Path(start).resolve()
    # A path that names an existing directory is itself a candidate; anything
    # else (a file, or a path that does not exist yet) starts at its parent.
    if not current.is_dir():
        current = current.parent

    for candidate in (current, *current.parents):
        if any((candidate / marker).is_file() for marker in _INSTANCE_MARKERS):
            return candidate
    return None


def require_instance_root(config_path: str | Path) -> Path:
    """Return the instance root for a config path, or abort.

    Anchors against the filesystem via :func:`find_instance_root`. There is
    no fallback: a config whose instance cannot be identified would resolve
    its ``env_file`` and ``output_json`` against a guess, and a wrong guess
    means reading the wrong corpus or writing a baseline that silently
    compares nothing.

    Args:
        config_path: Path to the config file whose relative values need an
            anchor.

    Returns:
        The absolute instance root directory.

    Raises:
        InstanceRootNotFoundError: If no parent of ``config_path`` holds
            ``.env`` or ``.env.example``.
    """
    root = find_instance_root(config_path)
    if root is None:
        raise InstanceRootNotFoundError(
            f"No instance root found for {config_path}: neither it nor any "
            f"parent directory holds {' or '.join(_INSTANCE_MARKERS)}. "
            "Configs anchor to their instance (convention: "
            "instances/<instance>/config/<name>.toml)."
        )
    return root


def resolve_config_path(raw: str | Path, config_path: str | Path) -> Path:
    """Resolve one config-supplied path against its config's instance root.

    Anchors relative values against the instance root that
    :func:`require_instance_root` finds for ``config_path`` -- never against
    the CWD, so the command behaves identically from any directory.

    Args:
        raw: The path as written in the config file.
        config_path: Path to the config file ``raw`` came from.

    Returns:
        ``raw`` unchanged (as a Path) when it is rooted on this platform,
        else ``raw`` joined onto the instance root.

    Raises:
        ValueError: If ``raw`` carries a drive or a root but is not absolute
            on this host (e.g. ``C:\\data\\out.json`` or the drive-relative
            ``D:data`` read on POSIX, or ``/data`` read on Windows). Such a
            value escapes the instance root when joined, so joining it would
            answer a question nobody asked.
        InstanceRootNotFoundError: If ``config_path`` has no instance root
            and ``raw`` is relative.
    """
    raw_path = Path(raw)

    if raw_path.is_absolute():
        return raw_path
    # Judge the normalized POSIX rendering, not str(raw): str() on a Windows
    # Path renders separators by the host's rules, so what the predicate saw
    # would depend on where it runs (see is_rooted_path's Args note).
    raw_str = raw_path.as_posix()
    if is_rooted_path(raw_str):
        raise ValueError(
            f"Config path {raw_str!r} is rooted under another platform's "
            "rules and cannot be resolved on this host. Use a path relative "
            "to the instance root, or an absolute path for this platform."
        )
    return require_instance_root(config_path) / raw_path


def fallback_log_root() -> Path:
    """Return the log root used when no instance root can be found.

    Anchors against the system temp directory rather than the CWD, so an
    engine-only run never writes a ``logs/`` tree into whatever directory it
    happened to start in (including a source checkout).

    Returns:
        ``<tempdir>/mcp_db_server/logs``.
    """
    return Path(tempfile.gettempdir()) / "mcp_db_server" / "logs"


def resolve_log_dir(subdir: str, anchor_path: str | Path | None = None) -> Path:
    """Resolve the directory a run's log file belongs in.

    Anchors against the instance root found for ``anchor_path``, giving
    ``<instance>/logs/<subdir>``. Unlike :func:`resolve_config_path` this
    never raises on a failed lookup: losing the logs of an otherwise valid
    run is a smaller harm than aborting it, and the temp fallback keeps the
    logs somewhere findable instead of somewhere wrong.

    Args:
        subdir: Log subdirectory naming the component
            (e.g. ``"mcp_db_server"``, ``"data_validation"``).
        anchor_path: A path inside the instance -- typically the env file or
            the config file. None (or a path with no resolvable instance
            root) selects the fallback.

    Returns:
        ``<instance root>/logs/<subdir>`` when an instance root is found,
        else ``fallback_log_root() / subdir``.

    Raises:
        Nothing. A failed lookup falls back rather than aborting.
    """
    if anchor_path is not None:
        root = find_instance_root(anchor_path)
        if root is not None:
            return root / "logs" / subdir
    return fallback_log_root() / subdir
