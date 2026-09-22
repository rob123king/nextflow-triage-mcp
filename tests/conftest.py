"""Fixtures shared by every test module.

`conftest.py` is discovered by pytest automatically -- never imported by name --
which is why fixtures live here. Shared helper *functions* live in
`tests/factories.py`, which test modules import normally.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp import Client

from nextflow_triage.server import mcp as server
from tests.factories import WORK_CONTENT, write_trace


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A complete fake run: nf-core style trace, plus work dirs for the failures.

    `tmp_path` is a pytest built-in -- a fresh directory per test, cleaned up
    automatically, never shared. It is why none of these tests can affect each
    other.
    """
    root = tmp_path / "run"
    write_trace(root / "pipeline_info" / "execution_trace_2026-09-22_09-00-00.txt")

    for task_hash, (exit_code, command, stderr) in WORK_CONTENT.items():
        bucket, prefix = task_hash.split("/")
        # Real work directories carry the full 32-char hash while the trace
        # shows a prefix, so padding here exercises the prefix-matching path.
        directory = root / "work" / bucket / (prefix + "cdef0123456789abcdef0123")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".exitcode").write_text(exit_code + "\n", encoding="utf-8")
        (directory / ".command.sh").write_text(command, encoding="utf-8")
        (directory / ".command.err").write_text(stderr, encoding="utf-8")
        (directory / ".command.out").write_text("done\n", encoding="utf-8")
    return root


@pytest.fixture
def anyio_backend() -> str:
    """Pin anyio's plugin to asyncio.

    Without this the plugin parametrises every async test across asyncio *and*
    trio, and everything runs twice. It is a fixture rather than a setting
    because that is the hook anyio exposes.
    """
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    """A client connected to the server in memory: no subprocess, no port.

    `raise_exceptions=True` is test-only: it stops the server sanitising an
    unexpected crash into "Internal server error" so a broken test shows the
    real traceback. It does not change how failures *inside* a tool are
    reported -- those are still is_error=True results.
    """
    async with Client(server, raise_exceptions=True) as connected:
        yield connected
