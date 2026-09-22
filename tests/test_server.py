"""Every tool, called through a real MCP client over the protocol.

This layer exists because the unit tests call the analysis functions directly
and never see what the protocol does to a response. On the previous project a
union return type turned out to wrap that one tool's payload in a
`{"result": ...}` envelope -- invisible to a direct call, and an inconsistency
across tools that a model would have to notice. Nothing but a protocol-level
test catches that class of problem.

So these tests assert on shape and contract, not on genomics-style detail:
that every tool is listed, that every description is written for a model, that
structured output is flat and consistent, and that failures come back as
readable `is_error` results rather than crashes.
"""

from pathlib import Path

import pytest
from mcp import Client

# Applies to every test in the module, so no per-test decorator. anyio's plugin
# uses it to run the coroutine on the backend the `anyio_backend` fixture names.
pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = [
    "server_info",
    "summarise_run",
    "explain_run_failures",
    "explain_task_failure",
    "check_resource_usage",
]


async def test_the_server_lists_exactly_the_tools_it_implements(client: Client) -> None:
    listing = await client.list_tools()
    assert [tool.name for tool in listing.tools] == EXPECTED_TOOLS


async def test_server_info_agrees_with_the_tool_listing(client: Client) -> None:
    """A stale `implemented_tools` list would be a quiet lie in every audit trail."""
    result = await client.call_tool("server_info", {})
    assert result.structured_content is not None
    assert result.structured_content["implemented_tools"] == EXPECTED_TOOLS


async def test_every_tool_description_is_written_for_a_model(client: Client) -> None:
    """Descriptions are the only documentation a model gets, and it reads them on
    every call. Testing them is cheap and gets more valuable with each tool."""
    for tool in (await client.list_tools()).tools:
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 120, f"{tool.name} has a one-line description"
        assert tool.input_schema["type"] == "object"


async def test_the_workflow_tools_name_what_to_call_next(client: Client) -> None:
    """The calling sequence should be legible from the descriptions alone, so a
    model does not have to infer the workflow from tool names."""
    tools = {tool.name: (tool.description or "") for tool in (await client.list_tools()).tools}
    assert "explain_run_failures" in tools["summarise_run"]
    assert "explain_task_failure" in tools["explain_run_failures"]


async def test_every_tool_returns_a_flat_structured_payload(client: Client, run_dir: Path) -> None:
    """The regression guard for the envelope problem.

    A union return type makes the SDK nest that tool's payload under `result`
    while every other tool stays flat. That inconsistency is invisible to a
    direct function call and is exactly what this layer is for.
    """
    calls = [
        ("server_info", {}),
        ("summarise_run", {"run_dir": str(run_dir)}),
        ("explain_run_failures", {"run_dir": str(run_dir)}),
        ("explain_task_failure", {"run_dir": str(run_dir), "task_id": "7"}),
        ("check_resource_usage", {"run_dir": str(run_dir)}),
    ]
    for name, arguments in calls:
        result = await client.call_tool(name, arguments)
        assert result.is_error is False, f"{name} failed: {result.content}"
        assert result.structured_content is not None
        assert "result" not in result.structured_content, (
            f"{name} nests its payload under 'result'; every other tool is flat"
        )


async def test_the_whole_triage_path_works_over_the_protocol(client: Client, run_dir: Path) -> None:
    """Follow the sequence a model would: summarise, group, then drill in."""
    summary = (
        await client.call_tool("summarise_run", {"run_dir": str(run_dir)})
    ).structured_content
    assert summary is not None
    assert summary["failed_tasks"] == 3
    assert "explain_run_failures" in summary["headline"]

    failures = (
        await client.call_tool("explain_run_failures", {"run_dir": str(run_dir)})
    ).structured_content
    assert failures is not None
    worst = failures["groups"][0]
    assert worst["process"] == "SORT_BAM"

    detail = (
        await client.call_tool(
            "explain_task_failure",
            {"run_dir": str(run_dir), "task_id": worst["example_task_id"]},
        )
    ).structured_content
    assert detail is not None
    assert "samtools sort" in detail["command"]
    assert any("allocate memory" in line for line in detail["stderr_tail"])


async def test_resource_verdicts_arrive_with_the_requests_supplied(
    client: Client, run_dir: Path
) -> None:
    result = await client.call_tool(
        "check_resource_usage",
        {"run_dir": str(run_dir), "requested_memory": {"INDEX": "64 GB"}},
    )
    assert result.structured_content is not None
    index = next(p for p in result.structured_content["processes"] if p["process"] == "INDEX")
    assert index["verdict"] == "over_allocated"


async def test_a_missing_run_directory_is_a_readable_error(client: Client) -> None:
    """A tool error, not a crash: the model reads the message and can retry."""
    result = await client.call_tool("summarise_run", {"run_dir": "/nowhere/at/all"})
    assert result.is_error is True
    assert "To recover:" in str(result.content)


async def test_an_unknown_task_id_lists_the_ones_that_exist(client: Client, run_dir: Path) -> None:
    """The error carries what the caller needs to fix the call, not just a refusal."""
    result = await client.call_tool(
        "explain_task_failure", {"run_dir": str(run_dir), "task_id": "9999"}
    )
    assert result.is_error is True
    assert "failed_task_ids_in_this_run" in str(result.content)


async def test_a_bad_memory_string_names_the_accepted_format(client: Client, run_dir: Path) -> None:
    result = await client.call_tool(
        "check_resource_usage",
        {"run_dir": str(run_dir), "requested_memory": {"INDEX": "loads"}},
    )
    assert result.is_error is True
    assert "64 GB" in str(result.content)


async def test_the_server_instructions_state_the_calling_order(client: Client) -> None:
    """Cross-tool rules live in `instructions`, which the host puts in context
    alongside the tool list. It is the only place to say something that spans
    tools, so it is worth asserting it survived."""
    instructions = client.instructions or ""
    assert "summarise_run" in instructions
    assert "unbounded" in instructions
