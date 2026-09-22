"""Triage for Nextflow pipeline runs, exposed over MCP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nextflow-triage-mcp")
except PackageNotFoundError:  # pragma: no cover - only in a bare source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
