"""Logging configuration for this package's entry-point scripts.

Provides `setup_logging()` for entry-point scripts (a rotating JSON file
handler on the root logger, tagged with a per-run timestamp) and
`get_logger()` for every module. It originated as a copy of the
python-development skill's logconfig and intentionally forks from it:
skill updates do not propagate by design, and nothing here depends on
untracked `.claude/` content. On 2026-08-27 the fork was re-synced
wholesale to the skill's canonical implementation (UTC timestamps,
append-by-default with rotation, and the named-handler configured
check) as the reviewed pick-up the skill's install note prescribes.
"""

import inspect
import logging
import os
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pythonjsonlogger.json import JsonFormatter

# The log appends across runs, so it needs a ceiling. 10 MB holds many
# runs of a DEBUG-level script; three backups bound one script's logs at
# roughly 40 MB before the oldest records are dropped.
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# The name `setup_logging` tags its handler with, and looks for on a second call.
# A name rather than the handler's type: a project can end up with two copies of
# this module under different import names, whose handler classes are unrelated
# objects, and an isinstance check would then install the file handler twice.
HANDLER_NAME = "logconfig_run_file"


class RunTimestampFilter(logging.Filter):
    """Filter that adds a run_timestamp to each log record.

    Attached to the file handler (not a logger) so every record that
    reaches the file — from any module's logger — carries the timestamp
    of the run that produced it, letting one appended log file separate
    its runs.
    """

    def __init__(self, run_timestamp: str) -> None:
        """Store the run timestamp stamped onto every record.

        Args:
            run_timestamp: The run's start time in UTC, preformatted
                (`%Y.%m.%d_%H.%M.%SZ`, the `Z` marking the zone).
        """
        super().__init__()
        self.run_timestamp = run_timestamp

    def filter(self, record: logging.LogRecord) -> bool:
        """Stamp the run timestamp onto a record; never drop it.

        Args:
            record: The log record passing through the handler.

        Returns:
            True always (the filter annotates, it does not filter).
        """
        record.run_timestamp = self.run_timestamp
        return True


def setup_logging(
    log_dir: str | Path,
    log_name: str | None = None,
    level: int = logging.DEBUG,
    overwrite: bool = False,
) -> None:
    """Configure logging for the application. Call ONCE from entry point script.

    Sets up a rotating JSON file handler on the ROOT logger so all child
    loggers inherit it, bounded at `MAX_LOG_BYTES` per file and
    `LOG_BACKUP_COUNT` backups. A second call is a no-op, so libraries can
    never double-register the file handler; handlers put there by anything
    else are left alone and do not stop this one being added.

    Args:
        log_dir: Directory path for the log file.
        log_name: Name of the log file (without extension). Defaults to
            the caller script's name.
        level: Logging level. Defaults to logging.DEBUG.
        overwrite: If True, delete the log file and its rotated backups
            at the start of the run. Defaults to False: the file is
            appended to, so a retried run keeps the failed run's records,
            and each run's records are separable by their
            `run_timestamp`. Pass True for a short script re-run freely
            during development, where a fresh file each time is the
            convenience. The deletion is subject to the same lock a
            rotation is: on Windows it raises while another process
            holds the log open.
    """
    # Get the root logger
    root_logger = logging.getLogger()

    # Only this module's own handler means "already configured". Asking whether
    # the root logger has any handler at all would turn this into a silent
    # no-op whenever something else got there first -- logging.basicConfig, or
    # pytest, which attaches its capture handlers before it imports a single
    # test module -- and the script would go on with no log directory, no file,
    # and no error to say so.
    if any(h.name == HANDLER_NAME for h in root_logger.handlers):
        return

    root_logger.setLevel(level)
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    if log_name is None:
        # Default the log name to the caller's script (the frame that
        # invoked setup_logging). Every step of the frame chain is
        # optional (non-CPython implementations, interactive or
        # embedded callers), so guard each and fall back to "log".
        frame = inspect.currentframe()
        caller_filepath = (
            frame.f_back.f_globals.get("__file__")
            if frame and frame.f_back
            else None
        )
        if caller_filepath is None:
            log_name = "log"
        else:
            caller_script_name = os.path.basename(caller_filepath)
            log_name = (
                caller_script_name.split(".")[0]
                if "." in caller_script_name
                else caller_script_name
            )
    log_filename = f"{log_name}.jsonl"

    # UTC, marked with Z, so records written on different machines
    # order against one another.
    run_timestamp = datetime.now(UTC).strftime("%Y.%m.%d_%H.%M.%SZ")
    timestamp_filter = RunTimestampFilter(run_timestamp)

    log_file = log_path / log_filename
    if overwrite:
        # RotatingFileHandler forces append mode whenever maxBytes is
        # set, so a fresh file means removing the previous run's log
        # — and the backups it rotated out — before the handler opens.
        # Match the backups by their numeric suffix rather than counting up
        # to LOG_BACKUP_COUNT: lowering that constant would otherwise strand
        # every file above the new ceiling, with no later run ever clearing
        # them. A `.bak` or `.gz` sitting beside the log is left alone.
        log_file.unlink(missing_ok=True)
        for backup in log_path.glob(f"{log_filename}.*"):
            if backup.suffix[1:].isdigit():
                backup.unlink(missing_ok=True)

    handler = RotatingFileHandler(
        log_file,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.set_name(HANDLER_NAME)
    handler.addFilter(timestamp_filter)  # Add filter to HANDLER, not logger
    formatter = JsonFormatter(
        "%(run_timestamp)s %(asctime)s %(name)s %(funcName)s "
        "%(levelname)s %(message)s"
    )
    # asctime defaults to local time in a shape no ISO parser accepts (a space
    # before the time, a comma before the milliseconds). A .jsonl log is read
    # by machines, so make it UTC ISO-8601 -- 2026-08-27T16:42:51.946Z, which
    # datetime.fromisoformat reads directly. Setting datefmt instead would drop
    # the milliseconds: formatTime ignores default_msec_format when datefmt is
    # set.
    formatter.converter = time.gmtime
    formatter.default_time_format = "%Y-%m-%dT%H:%M:%S"
    formatter.default_msec_format = "%s.%03dZ"
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Get a logger for a module.

    This is a thin wrapper around logging.getLogger(). Use this in library
    modules that should not configure logging themselves.

    Args:
        name: Logger name (typically __name__ from the calling module).

    Returns:
        Logger instance.
    """
    return logging.getLogger(name)
