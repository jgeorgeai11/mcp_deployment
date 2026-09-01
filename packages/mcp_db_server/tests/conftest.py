"""Shared fixtures for mcp_db_server unit tests.

The real ``mcp`` (FastMCP) package is installed, so it is imported directly --
``mcp.tool()`` returns the wrapped function unchanged, keeping the core tool
functions directly callable from these unit tests.
"""
