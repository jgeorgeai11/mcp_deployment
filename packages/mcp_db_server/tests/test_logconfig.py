"""Unit tests for the package's logging configuration (mcp_db_server.logconfig)."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from mcp_db_server.logconfig import HANDLER_NAME, get_logger, setup_logging
from pytest_mock import MockerFixture


def _read_json_lines(log_file: Path) -> list[dict]:
    """Read a .jsonl log file, asserting every line parses as JSON.

    Args:
        log_file: Path to the .jsonl file written by setup_logging.

    Returns:
        The parsed records, one dict per line.
    """
    lines = log_file.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


class LogconfigHarness:
    """Tracks the handler setup_logging adds so a test can end its run.

    setup_logging recognises its own handler by HANDLER_NAME and ignores
    everything else on the root logger, so it configures normally under
    pytest's capture handlers. The harness only records what it added so
    end_run can close and detach it; a HANDLER_NAME handler left by
    ANOTHER test module would still no-op setup(), which is why the
    logconfig_harness fixture detaches foreign ones first.
    """

    def __init__(self) -> None:
        """Start with no handlers of our own."""
        self.our_handlers: list[logging.Handler] = []

    def setup(self, **kwargs) -> None:
        """Call setup_logging as an entry-point script would.

        Args:
            **kwargs: Passed through to setup_logging.
        """
        setup_logging(**kwargs)
        root = logging.getLogger()
        self.our_handlers = [h for h in root.handlers if h.name == HANDLER_NAME]

    def end_run(self) -> None:
        """Close and detach the handlers setup() added, as at process exit.

        Lets the test read the log file (Windows holds it open otherwise)
        and lets the next setup() call simulate a fresh run.
        """
        root = logging.getLogger()
        for handler in self.our_handlers:
            handler.close()
            root.removeHandler(handler)
        self.our_handlers.clear()


@pytest.fixture()
def logconfig_harness() -> Iterator[LogconfigHarness]:
    """Provide a LogconfigHarness and restore the root logger afterwards.

    setup_logging mutates the ROOT logger (adds a file handler, sets the
    level), so each test must leave the root exactly as it found it. In a
    workspace-wide pytest run, a test module elsewhere that called
    setup_logging at import would leave a HANDLER_NAME handler on the root
    that made every setup() here a no-op — no module in this repo does that
    today, but the guard detaches such a handler for the test and restores
    it afterwards so the next import-time call degrades nothing.
    """
    root = logging.getLogger()
    saved_level = root.level
    foreign = [h for h in root.handlers if h.name == HANDLER_NAME]
    for handler in foreign:
        root.removeHandler(handler)
    harness = LogconfigHarness()
    yield harness
    harness.end_run()
    for handler in foreign:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_setup_logging_writes_jsonl_at_log_dir_log_name(
    logconfig_harness: LogconfigHarness, tmp_path: Path
) -> None:
    logconfig_harness.setup(log_dir=tmp_path, log_name="my_run")
    logger = get_logger("test_module")
    logger.info("hello")
    logger.debug("world")
    logconfig_harness.end_run()

    log_file = tmp_path / "my_run.jsonl"
    assert log_file.exists()
    records = _read_json_lines(log_file)
    assert len(records) == 2
    assert records[0]["message"] == "hello"
    assert records[1]["message"] == "world"


def test_records_carry_run_timestamp_and_funcname(
    logconfig_harness: LogconfigHarness, tmp_path: Path
) -> None:
    logconfig_harness.setup(log_dir=tmp_path, log_name="fields")
    get_logger("test_module").info("check fields")
    logconfig_harness.end_run()

    (record,) = _read_json_lines(tmp_path / "fields.jsonl")
    assert record["run_timestamp"]
    assert record["funcName"] == "test_records_carry_run_timestamp_and_funcname"


def test_both_record_timestamps_are_utc(
    logconfig_harness: LogconfigHarness, tmp_path: Path
) -> None:
    """Both timestamps on a record are UTC, and run_timestamp says so.

    Each is compared against the test's OWN datetime.now(UTC) rather than
    against each other: run_timestamp is the run's START time, stamped once
    per run by RunTimestampFilter, while asctime is per-record, so the two
    legitimately diverge within a long run. The now(UTC) comparison is what
    stops either one reverting to local time -- on both development machines
    a local-time value is hours away from UTC, so a generous tolerance still
    catches it. On a host whose local time IS UTC the reversion is
    unobservable by any test; that is accepted.
    """
    logconfig_harness.setup(log_dir=tmp_path, log_name="utc")
    get_logger("test_module").info("check timestamps")
    logconfig_harness.end_run()

    (record,) = _read_json_lines(tmp_path / "utc.jsonl")

    # The marker that keeps an old local-time value distinguishable from a
    # new UTC one rather than silently reinterpreted.
    assert record["run_timestamp"].endswith("Z")

    tolerance_seconds = 300
    now = datetime.now(UTC)
    run_started = datetime.strptime(
        record["run_timestamp"], "%Y.%m.%d_%H.%M.%SZ"
    ).replace(tzinfo=UTC)
    # asctime is ISO-8601 with milliseconds and a Z zone, so fromisoformat
    # reads it directly and the parsed value is already aware.
    record_written = datetime.fromisoformat(record["asctime"])
    assert record["asctime"].endswith("Z")

    assert abs((now - run_started).total_seconds()) < tolerance_seconds
    assert abs((now - record_written).total_seconds()) < tolerance_seconds


def test_overwrite_true_replaces_existing_file(
    logconfig_harness: LogconfigHarness, tmp_path: Path
) -> None:
    logconfig_harness.setup(log_dir=tmp_path, log_name="run")
    get_logger("test_module").info("first run")
    logconfig_harness.end_run()

    logconfig_harness.setup(log_dir=tmp_path, log_name="run", overwrite=True)
    get_logger("test_module").info("second run")
    logconfig_harness.end_run()

    records = _read_json_lines(tmp_path / "run.jsonl")
    assert [r["message"] for r in records] == ["second run"]


def test_overwrite_true_deletes_rotated_backups(
    logconfig_harness: LogconfigHarness, tmp_path: Path
) -> None:
    # Backups are matched by their numeric suffix; a neighbour with a
    # non-numeric suffix is not the handler's and must be left alone.
    (tmp_path / "run.jsonl").write_text("old run\n", encoding="utf-8")
    (tmp_path / "run.jsonl.1").write_text("old backup\n", encoding="utf-8")
    (tmp_path / "run.jsonl.2").write_text("old backup\n", encoding="utf-8")
    (tmp_path / "run.jsonl.bak").write_text("not ours\n", encoding="utf-8")

    logconfig_harness.setup(log_dir=tmp_path, log_name="run", overwrite=True)
    get_logger("test_module").info("fresh run")
    logconfig_harness.end_run()

    assert not (tmp_path / "run.jsonl.1").exists()
    assert not (tmp_path / "run.jsonl.2").exists()
    assert (tmp_path / "run.jsonl.bak").exists()
    records = _read_json_lines(tmp_path / "run.jsonl")
    assert [r["message"] for r in records] == ["fresh run"]


def test_default_appends_to_existing_file(
    logconfig_harness: LogconfigHarness, tmp_path: Path
) -> None:
    # No overwrite flag: the default appends, so a retried run keeps the
    # failed run's records.
    logconfig_harness.setup(log_dir=tmp_path, log_name="run")
    get_logger("test_module").info("first run")
    logconfig_harness.end_run()

    logconfig_harness.setup(log_dir=tmp_path, log_name="run")
    get_logger("test_module").info("second run")
    logconfig_harness.end_run()

    records = _read_json_lines(tmp_path / "run.jsonl")
    assert [r["message"] for r in records] == ["first run", "second run"]


def test_appended_runs_carry_distinct_run_timestamps(
    logconfig_harness: LogconfigHarness, tmp_path: Path, mocker: MockerFixture
) -> None:
    # run_timestamp has one-second granularity, so two back-to-back runs
    # would collide; mock datetime.now() to give each run its own second.
    mock_datetime = mocker.patch("mcp_db_server.logconfig.datetime")
    mock_now = MagicMock()
    mock_datetime.now.return_value = mock_now

    mock_now.strftime.return_value = "2026.08.13_12.00.00Z"
    logconfig_harness.setup(log_dir=tmp_path, log_name="run", overwrite=False)
    get_logger("test_module").info("first run")
    logconfig_harness.end_run()

    mock_now.strftime.return_value = "2026.08.13_12.00.01Z"
    logconfig_harness.setup(log_dir=tmp_path, log_name="run", overwrite=False)
    get_logger("test_module").info("second run")
    logconfig_harness.end_run()

    records = _read_json_lines(tmp_path / "run.jsonl")
    timestamps = [r["run_timestamp"] for r in records]
    assert timestamps == ["2026.08.13_12.00.00Z", "2026.08.13_12.00.01Z"]


def test_foreign_root_handler_does_not_suppress_setup(
    logconfig_harness: LogconfigHarness, tmp_path: Path
) -> None:
    """A handler someone else attached does not silence setup_logging.

    The old check treated ANY root handler as "already configured", so under
    a test runner's capture handlers setup_logging registered nothing. The
    named-handler check defers only to its own handler, so the file handler
    is added alongside the foreign one and records still reach the file.
    """
    root = logging.getLogger()
    stub = logging.NullHandler()
    root.addHandler(stub)
    try:
        logconfig_harness.setup(log_dir=tmp_path, log_name="foreign")
        assert any(h.name == HANDLER_NAME for h in root.handlers)
        get_logger("test_module").info("reaches the file")
        logconfig_harness.end_run()
    finally:
        root.removeHandler(stub)

    records = _read_json_lines(tmp_path / "foreign.jsonl")
    assert [r["message"] for r in records] == ["reaches the file"]


def test_log_name_none_defaults_to_caller_script_name(
    logconfig_harness: LogconfigHarness, tmp_path: Path
) -> None:
    # The caller of setup_logging is the harness method in this test
    # module, so the default log name must be this file's stem.
    logconfig_harness.setup(log_dir=tmp_path)
    get_logger("test_module").info("default name")
    logconfig_harness.end_run()

    assert (tmp_path / "test_logconfig.jsonl").exists()


def test_log_name_falls_back_to_log_when_no_caller_frame(
    logconfig_harness: LogconfigHarness, tmp_path: Path, mocker: MockerFixture
) -> None:
    # Non-CPython / embedded callers may have no frame chain; the default
    # log name must then fall back to "log".
    mocker.patch("mcp_db_server.logconfig.inspect.currentframe", return_value=None)
    logconfig_harness.setup(log_dir=tmp_path)
    get_logger("test_module").info("fallback name")
    logconfig_harness.end_run()

    assert (tmp_path / "log.jsonl").exists()


def test_second_setup_call_is_noop_while_configured(
    logconfig_harness: LogconfigHarness, tmp_path: Path
) -> None:
    logconfig_harness.setup(log_dir=tmp_path, log_name="first")
    # Handler for "first" is still attached, so this must not add a second
    # handler or create a second file.
    logconfig_harness.setup(log_dir=tmp_path, log_name="second")
    get_logger("test_module").info("only once")
    logconfig_harness.end_run()

    assert (tmp_path / "first.jsonl").exists()
    assert not (tmp_path / "second.jsonl").exists()


def test_get_logger_returns_named_logger() -> None:
    logger = get_logger("some.module")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "some.module"
