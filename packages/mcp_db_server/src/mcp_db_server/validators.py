"""Shared SQL-identifier validator.

The canonical copy of ``validate_sql_identifier`` for this repo: a pure,
dependency-free regex validator, duplicated across the repo split from the
ingestion pipeline (which keeps its own copy). It lives here, in the server
package, so every call site imports one implementation.

``validate_collection_path`` (the ltree validator) deliberately does NOT
exist in this copy: the read-only server has no call site for it.
"""

import re

__all__ = ["validate_sql_identifier"]

# Regex for safe SQL identifiers (lowercase letters, digits, underscores; must start with letter or underscore)
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def validate_sql_identifier(name: str, label: str) -> str:
    """Validate that a string is a safe SQL identifier to prevent injection.

    Args:
        name: The identifier string to validate.
        label: Descriptive label for error messages (e.g., "db_schema").

    Returns:
        The validated identifier string.

    Raises:
        ValueError: If the identifier contains unsafe characters.
    """
    # fullmatch (not match): with re.match, the anchored ``$`` would also match
    # just before a trailing newline, so "public\n" would pass. fullmatch requires
    # the entire string to match.
    if not _SAFE_IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            f"Unsafe SQL identifier for {label}: {name!r}. "
            "Must match pattern [a-z_][a-z0-9_]*"
        )
    return name
