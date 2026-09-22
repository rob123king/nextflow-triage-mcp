"""The MCP server and its four tools.

Everything that is *served* (transport, logging) lives in `__main__.py`, so
importing this module never starts anything. That is what lets the tests talk to
`mcp` in memory.

The tool descriptions are written for a model, not a human: they state the
calling order, name the tool to call next, and say what to read before drawing a
conclusion. A tool description is the only documentation a model gets and it is
read on every call, so it is a design artifact rather than a comment.

Every tool returns exactly one Pydantic model. No union return types: a union
makes the SDK wrap the payload in a `{"result": ...}` envelope for that tool
alone, and one tool shaped differently from the rest is exactly the kind of
inconsistency that makes tool use unreliable.
"""

import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from mcp.server import MCPServer
from pydantic import BaseModel, Field

from nextflow_triage import __version__
from nextflow_triage.analysis import (
    FailureReport,
    ResourceReport,
    RunSummary,
    assess_resources,
    group_failures,
    summarise,
)
from nextflow_triage.errors import TriageError
from nextflow_triage.trace import load_run
from nextflow_triage.units import parse_memory_bytes
from nextflow_triage.workdir import read_diagnostics

SERVER_NAME = "nextflow-triage"

SERVER_INSTRUCTIONS = """\
Triage for Nextflow pipeline runs: what happened, what failed, and whether the
resources were right.

Start with summarise_run. It tells you whether anything failed and names the
tool to call next. The usual path when something went wrong is:

  summarise_run -> explain_run_failures -> explain_task_failure

Three rules govern every tool here:

1. Nothing is guessed. Where a value cannot be established -- a work directory
   that has been cleaned up, a memory request only the user knows -- the tool
   says so rather than substituting a default.
2. No tool returns an unbounded list. Responses show a bounded sample and state
   how many items exist in total. Nothing is silently truncated.
3. Every response names the trace file it read. An nf-core directory keeps one
   trace per run, so check it matches the run you meant before quoting numbers.
"""

mcp = MCPServer(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)


def _distribution_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:  # pragma: no cover - only in a bare source tree
        return "not installed"


def _run_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        raise TriageError(
            f"Nothing exists at {path}.",
            recover=(
                "Pass the absolute path of the directory the pipeline was launched "
                "from -- the one containing work/ and either trace.txt or "
                "pipeline_info/. A trace file path works too."
            ),
        )
    return path


class ServerInfo(BaseModel):
    """Versions behind every result on this server."""

    server_name: str
    server_version: str
    python_version: str
    component_versions: dict[str, str]
    implemented_tools: list[str]


@mcp.tool()
def server_info() -> ServerInfo:
    """Report this server's version and the versions of what it is built on.

    Useful when recording how a triage conclusion was reached, or when checking
    which tools this build actually serves. Takes no arguments and reads
    nothing, so it is always safe to call.
    """
    return ServerInfo(
        server_name=SERVER_NAME,
        server_version=__version__,
        python_version=platform.python_version(),
        component_versions={
            "mcp": _distribution_version("mcp"),
            "pydantic": _distribution_version("pydantic"),
        },
        implemented_tools=[
            "server_info",
            "summarise_run",
            "explain_run_failures",
            "explain_task_failure",
            "check_resource_usage",
        ],
    )


@mcp.tool()
def summarise_run(run_dir: str) -> RunSummary:
    """Summarise a Nextflow run: what ran, what failed, where the time went. Call this first.

    run_dir: absolute path to the directory the pipeline was launched from (the
      one containing work/ and either trace.txt or pipeline_info/). The path of
      a trace file directly also works.

    Read `headline` first. If it reports failures, call explain_run_failures
    next with the same run_dir; if it reports none, the run is fine and there is
    nothing further to investigate.

    Then check `trace_file`: an nf-core pipeline_info directory keeps one trace
    per run, and this server takes the newest, which may not be the run you
    meant.

    `total_cpu_time_human` is summed task runtime, NOT wall-clock elapsed time.
    A run with 100 parallel tasks will report far more compute than the clock
    time it took. Do not quote it as how long the pipeline took.

    If every task shows as cached, the run did no work; that is usually -resume
    doing its job, but it is worth confirming that was intended.
    """
    return summarise(load_run(_run_dir(run_dir)))


@mcp.tool()
def explain_run_failures(run_dir: str) -> FailureReport:
    """Group a run's failed tasks by likely shared cause. Call after summarise_run reports failures.

    Failures are grouped by process and exit code, because tasks that share both
    almost always share a cause. Each group carries an interpretation of the
    exit code and an `example_task_id`.

    The interpretations are conventions, not guarantees: 137 is usually the
    out-of-memory killer and 127 is usually a missing command, but a tool may
    return any code it likes. Treat them as the first hypothesis to check, and
    confirm by calling explain_task_failure with the group's example_task_id to
    read the actual stderr.

    Note whether a group's failures are all one sample (`affected_tags` has one
    entry) or spread across several. One sample points at the input; several
    point at the process configuration. That distinction usually decides what to
    fix, so report it rather than just the count.
    """
    return group_failures(load_run(_run_dir(run_dir)))


class TaskFailureDetail(BaseModel):
    """Everything the work directory says about one task."""

    task_id: str
    process: str
    tag: str = Field(description="The sample or label on this task, if it had one.")
    status: str
    exit_code: str
    work_dir: str | None = Field(
        description="Null when the directory has been cleaned up or belongs to a resumed run."
    )
    command: str | None = Field(description="Contents of .command.sh, truncated.")
    stderr_tail: list[str] = Field(description="Last lines of .command.err. Usually the answer.")
    stdout_tail: list[str]
    note: str


@mcp.tool()
def explain_task_failure(run_dir: str, task_id: str) -> TaskFailureDetail:
    """Read the command and stderr of one failed task, using an example_task_id.

    run_dir: as for summarise_run.
    task_id: from `example_task_id` in an explain_run_failures group.

    This is where the actual cause lives: the trace only records that a task
    exited non-zero, while `.command.err` in the work directory records why.
    Read `stderr_tail` before drawing any conclusion -- it is the evidence, and
    the exit-code interpretation from explain_run_failures is only a hypothesis
    until it agrees with what is here.

    `work_dir` comes back null when the directory has been cleaned up or the run
    was resumed and the task belongs to an earlier one. That is normal and not
    an error; `note` explains which case applies. Say so rather than reporting
    the failure as unexplained.
    """
    trace = load_run(_run_dir(run_dir))
    matches = [task for task in trace.tasks if task.task_id == task_id]
    if not matches:
        failed_ids = [task.task_id for task in trace.tasks if task.failed][:10]
        raise TriageError(
            f"No task with id {task_id!r} in {trace.path.name}.",
            recover=(
                "Use an example_task_id from explain_run_failures, or call "
                "summarise_run to confirm you are pointing at the right run."
            ),
            detail={"failed_task_ids_in_this_run": failed_ids},
        )

    task = matches[-1]
    diagnostics = read_diagnostics(_run_dir(run_dir), task.work_hash)
    return TaskFailureDetail(
        task_id=task.task_id,
        process=task.process,
        tag=task.tag,
        status=task.status,
        exit_code=task.exit_code or (diagnostics.exit_code or "unknown"),
        work_dir=diagnostics.work_dir,
        command=diagnostics.command,
        stderr_tail=diagnostics.stderr_tail,
        stdout_tail=diagnostics.stdout_tail,
        note=diagnostics.note,
    )


@mcp.tool()
def check_resource_usage(
    run_dir: str, requested_memory: dict[str, str] | None = None
) -> ResourceReport:
    """Compare observed memory and CPU use against what each process asked for.

    run_dir: as for summarise_run.
    requested_memory: optional map of process name to the memory directive it
      was given, e.g. {"ALIGN": "64 GB", "SORT": "8 GB"}. Supply what the config
      actually asks for; this server does not read nextflow.config, because
      memory directives are Groovy expressions that can scale with attempt and
      input size, so the request cannot be established by reading the file.

    Without requested_memory you still get observed peak usage per process,
    which is enough to answer "how much does this need". With it you also get an
    over-allocation or near-limit verdict.

    Two findings matter and they pull in opposite directions: over-allocation
    wastes nothing in money but limits how many tasks can run at once, while
    near-limit means the next larger input gets OOM-killed. Report which one you
    found rather than just saying resources were "wrong".

    Verdicts of `too_few_tasks` or `no_memory_data` mean no judgement was
    possible -- not that the process is fine. Cached tasks are excluded
    throughout, since they carry no timings of their own.
    """
    requested_bytes: dict[str, int] = {}
    for process, text in (requested_memory or {}).items():
        parsed = parse_memory_bytes(text)
        if parsed is None:
            raise TriageError(
                f"Could not read {text!r} as a memory size for process {process!r}.",
                recover="Use a Nextflow-style size such as '64 GB', '8.5 GB' or '512 MB'.",
                detail={"process": process, "given": text},
            )
        requested_bytes[process] = parsed

    return assess_resources(load_run(_run_dir(run_dir)), requested_bytes)
