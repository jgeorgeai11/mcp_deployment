"""Convention guards over the engine's source tree.

A convention that lives only in a maintaining file is one new file away from
being violated, so the portability rule is enforced here instead of stated
there.
"""

import ast
from pathlib import Path

# The single module allowed to ask this host whether a path is absolute --
# it is where the portable answer is implemented.
_ABSOLUTENESS_EXEMPT = {"paths.py"}

# Split so this file's own guard text never matches the greps and scans that
# look for the forbidden call.
_FORBIDDEN_ATTRIBUTE = "is_" + "absolute"


def source_root() -> Path:
    """Locate the engine package's ``src/`` directory.

    Walks up from this file rather than from the CWD: pytest's rootdir
    differs by invocation directory, and a guard that resolved its own
    inputs from the CWD would silently pass by finding nothing.

    Returns:
        The absolute ``packages/mcp_db_server/src`` directory.

    Raises:
        AssertionError: If the directory is not where the layout says.
    """
    src = Path(__file__).resolve().parent.parent / "src"
    assert src.is_dir(), f"Source root not found at {src}"
    return src


def find_forbidden_accesses(module_path: Path) -> list[int]:
    """Find line numbers where a module asks a path if it is absolute.

    Parses rather than greps, so the attribute name appearing inside a
    docstring, a comment, or an error message is not a finding.

    Args:
        module_path: Path to the Python module to scan.

    Returns:
        The 1-based line numbers of every attribute access whose name is the
        forbidden one (empty when the module is clean).
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == _FORBIDDEN_ATTRIBUTE
    ]


class TestPortablePathAnchoring:
    """No module may decide absoluteness for the running host alone."""

    def test_source_tree_is_not_empty(self) -> None:
        """The scan below is only meaningful if it has modules to scan.

        Without this, a wrong source root would make the guard pass
        vacuously -- the failure mode a guard exists to prevent.
        """
        modules = list(source_root().rglob("*.py"))

        assert len(modules) >= 4, f"Only {len(modules)} modules found to scan"

    def test_no_module_checks_absoluteness_directly(self) -> None:
        """Only paths.py may ask; everything else routes through it.

        ``Path.is_absolute()`` answers for the running host only, so a config
        value like a Windows drive-letter path reads as relative on POSIX and
        would be silently resolved under the instance root.
        """
        offenders: list[str] = []
        for module in sorted(source_root().rglob("*.py")):
            if module.name in _ABSOLUTENESS_EXEMPT:
                continue
            offenders.extend(
                f"{module}:{line}" for line in find_forbidden_accesses(module)
            )

        assert not offenders, (
            "Paths must be anchored through mcp_db_server.paths "
            "(resolve_config_path), which recognizes both platforms' rooted "
            "forms, rather than by asking this host directly. Offending "
            "sites: " + ", ".join(offenders)
        )
