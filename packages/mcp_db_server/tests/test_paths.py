"""Unit tests for the shared path-anchoring helpers.

Absorbs the anchoring tests that lived in
``test_data_val_search_equivalence.py`` while the helpers were private to the
capture script: the behavior is the same, so it is tested once here under the
new names rather than twice.
"""

from pathlib import Path

import pytest
from mcp_db_server.paths import (
    InstanceRootNotFoundError,
    fallback_log_root,
    find_instance_root,
    is_rooted_path,
    require_instance_root,
    resolve_config_path,
    resolve_log_dir,
)


def make_instance(root: Path, name: str = "policy_db") -> Path:
    """Create a marked instance directory under ``root``.

    Args:
        root: Directory to create the instance in (usually ``tmp_path``).
        name: Instance directory name.

    Returns:
        The instance directory, holding an empty ``.env`` marker.
    """
    instance = root / name
    instance.mkdir(parents=True)
    (instance / ".env").write_text("", encoding="utf-8")
    return instance


class TestIsRootedPath:
    """A drive or a root under either platform's rules, not just this host's."""

    @pytest.mark.parametrize(
        "raw",
        [
            "/var/data/out.json",
            "C:\\corpus\\out.json",
            "c:/corpus/out.json",
            "\\\\server\\share\\out.json",
            "//server/share/out.json",
            "C:relative.json",
            "D:data/out.json",
            "\\etc\\passwd",
        ],
    )
    def test_rooted_forms_are_recognized(self, raw: str) -> None:
        """Anything carrying a drive or a root reports rooted.

        Beyond the absolute forms (POSIX, Windows drive-letter, UNC), the
        last three are the regression guard for the widened rule: a
        drive-relative value ("C:x", "D:x") and a drive-less-rooted value
        ("\\etc\\passwd") are absolute under NEITHER platform's
        is_absolute(), yet joining either under an anchor on Windows leaves
        the anchor -- another drive's working directory, or the drive root.
        """
        assert is_rooted_path(raw) is True

    @pytest.mark.parametrize(
        "raw",
        [
            "data/output/out.json",
            "./out.json",
            "../sibling/out.json",
            "out.json",
            "",
        ],
    )
    def test_relative_forms_are_not_rooted(self, raw: str) -> None:
        """Genuinely relative forms -- no drive, no root -- are not rooted."""
        assert is_rooted_path(raw) is False


class TestFindInstanceRoot:
    """Marker discovery by walking up, not by counting levels."""

    def test_config_under_a_marked_instance_resolves_to_it(
        self, tmp_path: Path
    ) -> None:
        """instances/<name>/config/x.toml anchors to instances/<name>."""
        instance = make_instance(tmp_path)
        config_dir = instance / "config"
        config_dir.mkdir()

        assert find_instance_root(config_dir / "x.toml") == instance

    def test_env_example_alone_marks_an_instance(self, tmp_path: Path) -> None:
        """A checked-out but unconfigured instance still anchors."""
        instance = tmp_path / "metadata_db"
        instance.mkdir()
        (instance / ".env.example").write_text("", encoding="utf-8")

        assert find_instance_root(instance / "config" / "x.toml") == instance

    def test_nearest_marker_wins(self, tmp_path: Path) -> None:
        """A marked instance nested inside another resolves to the inner one."""
        outer = make_instance(tmp_path, "outer")
        inner = make_instance(outer, "inner")

        assert find_instance_root(inner / "x.toml") == inner

    def test_directory_start_is_its_own_candidate(self, tmp_path: Path) -> None:
        """Passing the instance directory itself resolves to that directory."""
        instance = make_instance(tmp_path)

        assert find_instance_root(instance) == instance

    def test_unmarked_tree_returns_none(self, tmp_path: Path) -> None:
        """No marker anywhere up the tree is reported, not guessed at.

        tmp_path sits under the system temp root, which holds no instance
        marker, so the walk reaches the filesystem root without a hit.
        """
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        assert find_instance_root(scratch / "x.toml") is None


class TestRequireInstanceRoot:
    """The no-fallback anchor: a config either resolves or the run aborts."""

    def test_marked_instance_is_returned(self, tmp_path: Path) -> None:
        """The success path matches find_instance_root."""
        instance = make_instance(tmp_path)

        assert require_instance_root(instance / "config" / "x.toml") == instance

    def test_unmarked_tree_raises_naming_the_path(self, tmp_path: Path) -> None:
        """The abort message names the config the operator actually passed."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        config = scratch / "x.toml"

        with pytest.raises(InstanceRootNotFoundError, match=r"x\.toml"):
            require_instance_root(config)


class TestResolveConfigPath:
    """Config values anchor to the instance, never to the CWD."""

    def test_relative_resolves_under_the_instance_root(
        self, tmp_path: Path
    ) -> None:
        """The common case: data/output/... lands inside the instance."""
        instance = make_instance(tmp_path)
        config = instance / "config" / "x.toml"

        result = resolve_config_path("data/output/baseline.json", config)

        assert result == instance / "data" / "output" / "baseline.json"

    def test_absolute_path_is_returned_unchanged(self, tmp_path: Path) -> None:
        """An absolute config value means exactly what it says."""
        instance = make_instance(tmp_path)
        absolute = tmp_path / "elsewhere" / "baseline.json"

        result = resolve_config_path(absolute, instance / "config" / "x.toml")

        assert result == absolute

    def test_foreign_rooted_path_raises(self, tmp_path: Path) -> None:
        """C:\\... is absolute where it was written, so it is not joined.

        Path.is_absolute() would call it relative on this host and silently
        resolve it under the instance root -- the portability bug the
        convention test exists to prevent.
        """
        instance = make_instance(tmp_path)

        with pytest.raises(ValueError, match="another platform"):
            resolve_config_path(
                "C:\\corpus\\out.json", instance / "config" / "x.toml"
            )

    def test_drive_relative_path_raises(self, tmp_path: Path) -> None:
        """D:data is absolute NOWHERE, yet on Windows it escapes the anchor.

        Joining it under a C:-anchored instance root yields D:data -- another
        drive's working directory -- so it is rejected rather than resolved.
        This is the caller-visible half of the widened is_rooted_path rule.
        """
        instance = make_instance(tmp_path)

        with pytest.raises(ValueError, match="another platform"):
            resolve_config_path(
                "D:data/out.json", instance / "config" / "x.toml"
            )

    def test_relative_without_an_instance_root_raises(
        self, tmp_path: Path
    ) -> None:
        """No anchor means no guess: the run aborts rather than resolving."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        with pytest.raises(InstanceRootNotFoundError):
            resolve_config_path("data/out.json", scratch / "x.toml")


class TestResolveLogDir:
    """Logs anchor to the instance when they can, and never abort a run."""

    def test_anchors_to_the_instance_when_found(self, tmp_path: Path) -> None:
        """The server's case: the env file's own instance holds the logs."""
        instance = make_instance(tmp_path)

        result = resolve_log_dir("mcp_db_server", instance / ".env")

        assert result == instance / "logs" / "mcp_db_server"

    def test_falls_back_to_the_temp_root_when_unanchored(
        self, tmp_path: Path
    ) -> None:
        """An engine-only run logs under temp, not beside the caller."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        result = resolve_log_dir("mcp_db_server", scratch / "x.toml")

        assert result == fallback_log_root() / "mcp_db_server"

    def test_falls_back_when_no_anchor_is_given(self) -> None:
        """anchor_path=None takes the fallback without touching the disk."""
        assert resolve_log_dir("data_validation") == (
            fallback_log_root() / "data_validation"
        )

    def test_fallback_root_is_outside_the_working_directory(self) -> None:
        """The fallback is absolute and under temp, so no CWD is polluted."""
        root = fallback_log_root()

        assert root.is_absolute()
        assert root.name == "logs"
        assert root.parent.name == "mcp_db_server"
