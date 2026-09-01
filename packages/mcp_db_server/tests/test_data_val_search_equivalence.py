"""Unit tests for the search-equivalence capture tool's own behavior.

The path-anchoring helpers this script used to own now live in
``mcp_db_server.paths`` and are tested in ``test_paths.py``. What remains
here is the script's own logic: reading the served model and token out of an
instance env file, reducing a tool result to its comparable identity, and
``main()``'s abort paths. The capture itself needs a live server and a live
corpus, which is what the two-run baseline workflow (see
MAINTAINING.instance.policy_db.md) exercises.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp_db_server.data_validation import data_val_search_equivalence as dvse


def write_instance(tmp_path: Path, env_body: str = "") -> Path:
    """Create a marked instance directory with an env file.

    Args:
        tmp_path: The test's temporary directory.
        env_body: Contents of the instance ``.env``.

    Returns:
        The instance directory, holding ``.env`` and an empty ``config/``.
    """
    instance = tmp_path / "policy_db"
    (instance / "config").mkdir(parents=True)
    (instance / ".env").write_text(env_body, encoding="utf-8")
    return instance


class TestReadServedEmbeddingModel:
    """The recorded model is the equivalence precondition."""

    def test_configured_model_is_returned(self, tmp_path: Path) -> None:
        """The value in the env file is what the instance serves."""
        env = tmp_path / ".env"
        env.write_text("MCP_EMBEDDING_MODEL=acme/embed-v2\n", encoding="utf-8")

        assert dvse.read_served_embedding_model(env) == "acme/embed-v2"

    def test_unset_falls_back_to_the_server_default(self, tmp_path: Path) -> None:
        """An unset variable means the server's own default, not nothing."""
        env = tmp_path / ".env"
        env.write_text("MCP_PORT=8000\n", encoding="utf-8")

        assert dvse.read_served_embedding_model(env) == (
            dvse._DEFAULT_EMBEDDING_MODEL
        )

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """A missing env file would report the default and mask a mismatch."""
        with pytest.raises(FileNotFoundError):
            dvse.read_served_embedding_model(tmp_path / "absent" / ".env")


class TestReadBearerToken:
    """Any one configured token authenticates, so the first is used."""

    def test_first_labelled_pair_is_used(self, tmp_path: Path) -> None:
        """label:token pairs yield the token, not the label."""
        env = tmp_path / ".env"
        env.write_text("MCP_AUTH_TOKENS=alice:tok-a,bob:tok-b\n", encoding="utf-8")

        assert dvse.read_bearer_token(env) == "tok-a"

    def test_bare_token_without_a_label_is_used_as_is(self, tmp_path: Path) -> None:
        """A pair with no colon is the token itself."""
        env = tmp_path / ".env"
        env.write_text("MCP_AUTH_TOKENS=tok-plain\n", encoding="utf-8")

        assert dvse.read_bearer_token(env) == "tok-plain"

    def test_no_tokens_sends_no_header(self, tmp_path: Path) -> None:
        """None means no Authorization header; the server then decides."""
        env = tmp_path / ".env"
        env.write_text("MCP_PORT=8000\n", encoding="utf-8")

        assert dvse.read_bearer_token(env) is None


class FakeBlock:
    """A minimal stand-in for an MCP text content block."""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeToolResult:
    """A minimal stand-in for a CallToolResult."""

    def __init__(
        self,
        content: list[Any] | None = None,
        structured: dict[str, Any] | None = None,
        is_error: bool = False,
    ) -> None:
        self.content = content or []
        self.structuredContent = structured
        self.isError = is_error


class TestExtractResultRows:
    """Structured content is preferred; text blocks are the fallback."""

    def test_structured_content_is_preferred(self) -> None:
        """json_response mode gives the rows directly."""
        rows = [{"collection_path": "a"}]

        assert dvse.extract_result_rows(
            FakeToolResult(structured={"result": rows})
        ) == rows

    def test_text_block_is_parsed_when_unstructured(self) -> None:
        """Without structured content, a JSON list in a text block is used."""
        rows = [{"collection_path": "b"}]
        result = FakeToolResult(content=[FakeBlock(json.dumps(rows))])

        assert dvse.extract_result_rows(result) == rows

    def test_tool_error_raises(self) -> None:
        """An error result must not be captured as an empty baseline."""
        with pytest.raises(ValueError, match="returned an error"):
            dvse.extract_result_rows(FakeToolResult(is_error=True))

    def test_no_parsable_block_raises(self) -> None:
        """Nothing to capture is a failure, not an empty capture."""
        with pytest.raises(ValueError, match="parsed as a JSON list"):
            dvse.extract_result_rows(FakeToolResult(content=[]))


class TestResultIdentity:
    """Identity is the attributed chunk plus its fused score."""

    def test_display_columns_are_dropped(self) -> None:
        """chunk_text and friends are excluded so the diff stays readable."""
        row = {
            "source_schema": "usc",
            "source_table": "t_embedding",
            "collection_path": "title42",
            "chunk_number": 7,
            "fused_score": 0.5,
            "chunk_text": "a very long passage",
        }

        assert dvse.result_identity(row) == {
            "source_schema": "usc",
            "source_table": "t_embedding",
            "collection_path": "title42",
            "chunk_number": 7,
            "fused_score": 0.5,
        }

    def test_missing_keys_are_recorded_as_none(self) -> None:
        """A shape change shows up in the diff rather than raising."""
        assert dvse.result_identity({}) == {
            "source_schema": None,
            "source_table": None,
            "collection_path": None,
            "chunk_number": None,
            "fused_score": None,
        }


class TestMainAbortPaths:
    """Every config failure is one reported abort with exit 1."""

    def test_missing_config_file_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A config path that does not exist stops before any network call."""
        instance = write_instance(tmp_path)
        config = instance / "config" / "absent.toml"
        monkeypatch.setattr(
            "sys.argv", ["data-val-search-equivalence", "--config", str(config)]
        )

        with pytest.raises(SystemExit) as excinfo:
            dvse.main()

        assert excinfo.value.code == 1

    def test_unparsable_config_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A malformed TOML is a config error like any other."""
        instance = write_instance(tmp_path)
        config = instance / "config" / "bad.toml"
        config.write_text("this is not = = toml\n", encoding="utf-8")
        monkeypatch.setattr(
            "sys.argv", ["data-val-search-equivalence", "--config", str(config)]
        )

        with pytest.raises(SystemExit) as excinfo:
            dvse.main()

        assert excinfo.value.code == 1

    def test_missing_required_key_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The keys main() subscripts are the ones it aborts on."""
        instance = write_instance(tmp_path)
        config = instance / "config" / "partial.toml"
        config.write_text('mcp_url = "http://localhost:8000/mcp"\n', encoding="utf-8")
        monkeypatch.setattr(
            "sys.argv", ["data-val-search-equivalence", "--config", str(config)]
        )

        with pytest.raises(SystemExit) as excinfo:
            dvse.main()

        assert excinfo.value.code == 1

    def test_foreign_rooted_path_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A Windows-rooted output path aborts instead of resolving oddly.

        Path.is_absolute() would call it relative on this host, and the
        capture would then be written under the instance root at a path
        nobody asked for.
        """
        instance = write_instance(tmp_path)
        config = instance / "config" / "foreign.toml"
        config.write_text(
            'mcp_url = "http://localhost:8000/mcp"\n'
            'env_file = ".env"\n'
            'database = "policy_db"\n'
            'output_json = "C:\\\\corpus\\\\out.json"\n'
            "queries = []\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "sys.argv", ["data-val-search-equivalence", "--config", str(config)]
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(SystemExit) as excinfo:
                dvse.main()

        assert excinfo.value.code == 1
        assert any(
            "Cannot resolve a configured path" in record.getMessage()
            for record in caplog.records
        )
