#!/usr/bin/env python3
"""Call the triage tools against a run and save each raw result as JSON.

Useful for comparing payload sizes (a rough token proxy) without a model in the loop.

    uv run python scripts/run_tools.py <run_dir_or_trace_file> <label>

Writes results/<label>_<tool>.json and prints the size of each file.
"""

import json
import sys
from pathlib import Path

import anyio
from mcp import Client

from nextflow_triage.server import mcp

TOOLS = ["summarise_run", "explain_run_failures"]


async def run(run_dir: str, label: str) -> None:
    out = Path("results")
    out.mkdir(exist_ok=True)
    async with Client(mcp) as client:
        for tool in TOOLS:
            result = await client.call_tool(tool, {"run_dir": run_dir})
            payload = result.structured_content if not result.is_error else str(result.content)
            path = out / f"{label}_{tool}.json"
            path.write_text(json.dumps({"is_error": result.is_error, "result": payload}, indent=2))
            print(f"{path}  {path.stat().st_size} bytes")


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: run_tools.py <run_dir_or_trace_file> <label>")
    anyio.run(run, sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
