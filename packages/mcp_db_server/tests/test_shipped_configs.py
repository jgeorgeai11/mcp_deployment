"""Guards on the configuration files this repo ships.

The example env files and the validation TOML are the documented starting
point for every deployment, so a drift between them and the code that reads
them is an operator-visible break that no other test would catch: nothing
imports them, and the server that would notice needs a live database.

Expectations are DERIVED from the code rather than restated here. A guard
whose expected set is hardcoded drifts in exactly the way the guard exists to
catch, so the required env variables come out of ``server.py``'s AST.
"""

import ast
import tomllib
from pathlib import Path

import pytest
from dotenv import dotenv_values
from mcp_db_server.paths import resolve_config_path

# Keys whose absence sends main() down its `except KeyError` abort path.
_REQUIRED_TOML_KEYS = ("mcp_url", "env_file", "database", "output_json", "queries")


def repo_root() -> Path:
    """Locate the workspace root by walking up from this file.

    Never from the CWD: pytest's rootdir differs by invocation directory (a
    bare ``uv run pytest`` from ``instances/policy_db/`` collects nothing),
    and a guard that resolved its fixtures from the CWD would silently pass
    by finding none.

    Returns:
        The directory holding the ``pyproject.toml`` that declares
        ``[tool.uv.workspace]``.

    Raises:
        AssertionError: If no such directory is found before the filesystem
            root.
    """
    for candidate in Path(__file__).resolve().parents:
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file():
            continue
        if "tool" in (parsed := tomllib.loads(pyproject.read_text(encoding="utf-8"))):
            if "workspace" in parsed["tool"].get("uv", {}):
                return candidate
    raise AssertionError(
        f"No [tool.uv.workspace] pyproject.toml above {Path(__file__).resolve()}"
    )


def required_env_variables() -> set[str]:
    """Derive the env variables the server hard-requires, from its source.

    A direct ``os.environ["X"]`` subscript raises ``KeyError`` when absent,
    which makes those reads the authority on what an env file MUST declare.
    ``os.environ.get(...)`` reads carry inline defaults and are deliberately
    not included -- a fuller contract would need a config-validation function
    the server does not have.

    Returns:
        The variable names read by direct subscript in ``server.py``.
    """
    server = repo_root() / "packages/mcp_db_server/src/mcp_db_server/server.py"
    tree = ast.parse(server.read_text(encoding="utf-8"))

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        target = node.value
        if not (
            isinstance(target, ast.Attribute)
            and target.attr == "environ"
            and isinstance(target.value, ast.Name)
            and target.value.id == "os"
        ):
            continue
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            names.add(node.slice.value)
    return names


def example_env_files() -> list[Path]:
    """Collect every shipped instance env template.

    Returns:
        Sorted ``instances/*/.env.example`` paths.
    """
    return sorted((repo_root() / "instances").glob("*/.env.example"))


def validation_config_path() -> Path:
    """Return the shipped search-equivalence config.

    Returns:
        Path to ``instances/policy_db/config/data_val_search_equivalence.toml``.
    """
    return (
        repo_root() / "instances/policy_db/config/data_val_search_equivalence.toml"
    )


class TestRequiredEnvVariablesDerivation:
    """The derivation itself, so the guards below cannot pass vacuously."""

    def test_subscript_reads_are_found(self) -> None:
        """A matcher that finds nothing would make every guard trivially true."""
        assert required_env_variables() == {
            "POSTGRES_DB",
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
        }

    def test_examples_are_found(self) -> None:
        """Both shipped instances must be picked up by the glob."""
        names = {path.parent.name for path in example_env_files()}

        assert names == {"policy_db", "metadata_db"}


class TestShippedEnvExamples:
    """Every instance template parses, is complete, and holds no secret."""

    @pytest.mark.parametrize(
        "example", example_env_files(), ids=lambda p: p.parent.name
    )
    def test_example_parses(self, example: Path) -> None:
        """A template that does not parse is a broken starting point."""
        values = dotenv_values(example)

        assert values, f"{example} parsed to no variables at all"

    @pytest.mark.parametrize(
        "example", example_env_files(), ids=lambda p: p.parent.name
    )
    def test_example_declares_every_hard_required_variable(
        self, example: Path
    ) -> None:
        """Missing any of these makes the copied .env fail at connect time."""
        declared = {key for key, value in dotenv_values(example).items() if value}
        missing = required_env_variables() - declared

        assert not missing, f"{example} does not declare {sorted(missing)}"

    @pytest.mark.parametrize(
        "example", example_env_files(), ids=lambda p: p.parent.name
    )
    def test_password_is_a_placeholder(self, example: Path) -> None:
        """The templates are committed; a real password here is a leak.

        A placeholder is the ``<...>`` form the templates use, which also
        fails loudly at connect time if someone forgets to fill it in.
        """
        password = dotenv_values(example).get("POSTGRES_PASSWORD") or ""

        assert password.startswith("<") and password.endswith(">"), (
            f"{example} POSTGRES_PASSWORD is not a <placeholder>; a committed "
            "template must never carry a real credential"
        )


class TestShippedValidationConfig:
    """The search-equivalence config stays runnable and fully anchored."""

    def test_config_parses(self) -> None:
        """tomllib is what the script itself uses to read it."""
        config = tomllib.loads(validation_config_path().read_text(encoding="utf-8"))

        assert isinstance(config, dict)

    @pytest.mark.parametrize("key", _REQUIRED_TOML_KEYS)
    def test_required_key_is_present(self, key: str) -> None:
        """These are the keys whose absence aborts main() with exit 1."""
        config = tomllib.loads(validation_config_path().read_text(encoding="utf-8"))

        assert key in config

    def test_relative_paths_resolve_under_the_instance_root(self) -> None:
        """Anchoring is what makes the command work from any directory."""
        config_path = validation_config_path()
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        instance = config_path.parent.parent

        for key in ("env_file", "output_json"):
            resolved = resolve_config_path(config[key], config_path)

            assert resolved.is_relative_to(instance), (
                f"{key} resolves to {resolved}, outside {instance}"
            )

    def test_env_file_target_exists(self) -> None:
        """The template it points at must be there, or a fresh clone cannot run.

        The real ``.env`` is gitignored, so the committed ``.env.example``
        beside it is what this asserts on.
        """
        config_path = validation_config_path()
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        env_file = resolve_config_path(config["env_file"], config_path)

        template = env_file.parent / ".env.example"

        assert env_file.is_file() or template.is_file(), (
            f"config env_file resolves to {env_file}, and no {template} "
            "template sits beside it"
        )

    def test_every_query_names_a_schema_and_a_query(self) -> None:
        """An empty or malformed entry must fail here, not mid-capture.

        run_queries subscripts ``schema`` and ``query`` on each entry, so a
        missing key is a KeyError partway through a run that has already
        made network calls.
        """
        config = tomllib.loads(validation_config_path().read_text(encoding="utf-8"))
        queries = config["queries"]

        assert queries, "the config declares no queries"
        for index, spec in enumerate(queries):
            assert spec.get("schema"), f"queries[{index}] has no schema"
            assert isinstance(spec.get("query"), str) and spec["query"].strip(), (
                f"queries[{index}] has no query string"
            )
