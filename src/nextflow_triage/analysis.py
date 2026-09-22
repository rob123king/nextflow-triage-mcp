"""Turning task records into the three answers this server exists to give.

All three functions are pure: they take a `Trace` and return a Pydantic model,
with no filesystem access and no I/O. That is what lets every interesting case
-- a run where everything cached, a process that always fails on one sample, a
job asking for 64 GB and using 2 -- be tested against constructed records
instead of a real pipeline run.
"""

from collections import Counter, defaultdict
from statistics import mean

from pydantic import BaseModel, Field

from nextflow_triage.trace import SUCCESS_STATUSES, TaskRecord, Trace
from nextflow_triage.units import format_bytes, format_duration

MAX_SAMPLE = 5
"""Failures or processes listed per response. The rest are counted, not dropped
silently: every response says how many it is showing out of how many."""

WASTE_RATIO = 0.4
"""Peak RSS below this fraction of the request counts as over-allocation.

0.4 rather than something tighter because memory requests should carry genuine
headroom -- a job that peaks at 90% of its request is one unlucky input away
from being killed. Flagging anything under 40% catches the 64 GB request that
uses 2 GB without nagging about sensible margins.
"""

MIN_TASKS_FOR_RESOURCE_ADVICE = 3
"""Below this, a per-process average is one task wearing a disguise."""


class ProcessSummary(BaseModel):
    """Per-process rollup: where the time went and what failed."""

    process: str
    tasks: int
    failed: int
    cached: int
    total_cpu_seconds: float = Field(
        description="Summed task runtime. This is compute consumed, not wall-clock elapsed."
    )
    total_runtime_human: str


class RunSummary(BaseModel):
    """What happened in this run, at a glance."""

    trace_file: str = Field(
        description=(
            "The trace this answer came from. An nf-core directory holds one per "
            "run, so check this matches the run you meant before trusting the numbers."
        )
    )
    total_tasks: int
    status_counts: dict[str, int]
    failed_tasks: int
    cached_tasks: int
    succeeded_tasks: int
    total_cpu_time_human: str = Field(
        description="Summed task runtime across the run, not wall-clock duration."
    )
    processes: list[ProcessSummary] = Field(
        description="Heaviest processes first, by total runtime. Truncated; see sample_note."
    )
    sample_note: str
    headline: str = Field(description="The one sentence to read first.")


class FailureGroup(BaseModel):
    """Failures that share a process and an exit code, which usually share a cause."""

    process: str
    exit_code: str
    count: int
    example_task_id: str = Field(
        description="Pass this to explain_task_failure for the command and the stderr."
    )
    example_tag: str = Field(description="The sample or tag on the example task, if any.")
    affected_tags: list[str] = Field(
        description="Tags seen in this group, truncated. Useful for spotting one bad input."
    )
    interpretation: str


class FailureReport(BaseModel):
    """Every failure in the run, grouped by likely shared cause."""

    trace_file: str
    total_failures: int
    groups: list[FailureGroup]
    sample_note: str
    headline: str


class ProcessResources(BaseModel):
    """How one process used memory and CPU, against what it asked for."""

    process: str
    tasks: int
    mean_peak_rss_human: str
    max_peak_rss_human: str
    mean_percent_cpu: float | None = Field(
        description="Above 100 means multiple cores were busy. Below ~90 on a multi-core "
        "request usually means the tool did not parallelise."
    )
    verdict: str
    suggestion: str


class ResourceReport(BaseModel):
    """Where this run over- or under-asked for resources."""

    trace_file: str
    processes: list[ProcessResources]
    note: str
    headline: str


def _ran(tasks: list[TaskRecord]) -> list[TaskRecord]:
    """Tasks that actually executed. Cached tasks have no meaningful timings."""
    return [task for task in tasks if task.status != "CACHED"]


def summarise(trace: Trace) -> RunSummary:
    """Roll a trace up into counts and the heaviest processes."""
    statuses = Counter(task.status for task in trace.tasks)
    failed = [task for task in trace.tasks if task.failed]
    cached = statuses.get("CACHED", 0)
    succeeded = sum(count for status, count in statuses.items() if status in SUCCESS_STATUSES)

    by_process: dict[str, list[TaskRecord]] = defaultdict(list)
    for task in trace.tasks:
        by_process[task.process].append(task)

    summaries: list[ProcessSummary] = []
    for process, tasks in by_process.items():
        runtime = sum(task.realtime_seconds or task.duration_seconds or 0.0 for task in tasks)
        summaries.append(
            ProcessSummary(
                process=process,
                tasks=len(tasks),
                failed=sum(1 for task in tasks if task.failed),
                cached=sum(1 for task in tasks if task.status == "CACHED"),
                total_cpu_seconds=round(runtime, 1),
                total_runtime_human=format_duration(runtime),
            )
        )
    summaries.sort(key=lambda s: (-s.total_cpu_seconds, s.process))

    total_runtime = sum(s.total_cpu_seconds for s in summaries)
    if failed:
        headline = (
            f"{len(failed)} of {len(trace.tasks)} tasks failed. Call "
            f"explain_run_failures for the grouped causes."
        )
    elif cached == len(trace.tasks):
        headline = (
            f"All {len(trace.tasks)} tasks were cached: this run did no work. "
            f"If you expected it to, check whether -resume was intended."
        )
    else:
        headline = (
            f"All {len(trace.tasks)} tasks succeeded, using "
            f"{format_duration(total_runtime)} of compute."
        )

    return RunSummary(
        trace_file=str(trace.path),
        total_tasks=len(trace.tasks),
        status_counts=dict(sorted(statuses.items())),
        failed_tasks=len(failed),
        cached_tasks=cached,
        succeeded_tasks=succeeded,
        total_cpu_time_human=format_duration(total_runtime),
        processes=summaries[:MAX_SAMPLE],
        sample_note=(
            f"Showing the {min(MAX_SAMPLE, len(summaries))} heaviest of "
            f"{len(summaries)} processes, by total runtime."
        ),
        headline=headline,
    )


def _interpret_exit_code(exit_code: str, count: int, distinct_tags: int) -> str:
    """Say what an exit code usually means, without pretending to be certain.

    These are conventions, not guarantees -- 137 is only SIGKILL by convention,
    and a tool is free to return it for anything. The wording stays hedged for
    that reason, and every group points at the stderr that would settle it.
    """
    base = {
        "137": (
            "Exit 137 is 128+9, conventionally SIGKILL. On a cluster or in a "
            "container this is almost always the out-of-memory killer: raise the "
            "memory directive for this process."
        ),
        "143": (
            "Exit 143 is 128+15, conventionally SIGTERM. Usually a scheduler "
            "walltime limit or a cancelled job rather than a fault in the task."
        ),
        "140": "Exit 140 is often a scheduler runtime limit on SGE/PBS.",
        "127": "Exit 127 is 'command not found': the tool is missing from the container or PATH.",
        "126": "Exit 126 means the command was found but not executable.",
        "1": "Exit 1 is a generic tool error. The stderr is the only way to tell what happened.",
    }.get(exit_code, f"Exit {exit_code or 'unknown'} is tool-specific; read the stderr.")

    if count > 1 and distinct_tags == 1:
        base += (
            " All failures here are the same sample, so suspect that input rather than the process."
        )
    elif count > 1 and distinct_tags > 1:
        base += (
            " This affects several samples, so suspect the process configuration "
            "rather than any one input."
        )
    return base


def group_failures(trace: Trace) -> FailureReport:
    """Group failures by process and exit code, which usually share a cause."""
    failures = [task for task in trace.tasks if task.failed]
    grouped: dict[tuple[str, str], list[TaskRecord]] = defaultdict(list)
    for task in failures:
        grouped[(task.process, task.exit_code)].append(task)

    groups: list[FailureGroup] = []
    for (process, exit_code), tasks in grouped.items():
        tags = [task.tag for task in tasks if task.tag]
        groups.append(
            FailureGroup(
                process=process,
                exit_code=exit_code or "unknown",
                count=len(tasks),
                example_task_id=tasks[0].task_id,
                example_tag=tasks[0].tag,
                affected_tags=sorted(set(tags))[:MAX_SAMPLE],
                interpretation=_interpret_exit_code(exit_code, len(tasks), len(set(tags))),
            )
        )
    groups.sort(key=lambda g: (-g.count, g.process))

    if not failures:
        headline = "No failed tasks in this run."
    else:
        worst = groups[0]
        headline = (
            f"{len(failures)} failures in {len(groups)} distinct "
            f"{'group' if len(groups) == 1 else 'groups'}. The largest is "
            f"{worst.process} exiting {worst.exit_code} ({worst.count} tasks)."
        )

    return FailureReport(
        trace_file=str(trace.path),
        total_failures=len(failures),
        groups=groups[:MAX_SAMPLE],
        sample_note=(
            f"Showing {min(MAX_SAMPLE, len(groups))} of {len(groups)} failure groups. "
            f"Each names one example_task_id -- pass it to explain_task_failure for "
            f"the command and the stderr."
        ),
        headline=headline,
    )


def assess_resources(trace: Trace, requested_memory_bytes: dict[str, int]) -> ResourceReport:
    """Compare observed peak memory and CPU against what each process asked for.

    `requested_memory_bytes` is supplied by the caller rather than parsed from a
    config: Nextflow's memory directives are Groovy expressions that can scale
    with attempt and input size, so the only honest way to know the request is
    to be told. Processes with no stated request still get their observed usage
    reported, with the verdict saying that nothing was claimed.
    """
    by_process: dict[str, list[TaskRecord]] = defaultdict(list)
    for task in _ran(trace.tasks):
        by_process[task.process].append(task)

    rows: list[ProcessResources] = []
    flagged = 0
    for process, tasks in sorted(by_process.items()):
        peaks = [task.peak_rss_bytes for task in tasks if task.peak_rss_bytes is not None]
        cpus = [task.percent_cpu for task in tasks if task.percent_cpu is not None]
        if not peaks:
            rows.append(
                ProcessResources(
                    process=process,
                    tasks=len(tasks),
                    mean_peak_rss_human="not recorded",
                    max_peak_rss_human="not recorded",
                    mean_percent_cpu=round(mean(cpus), 1) if cpus else None,
                    verdict="no_memory_data",
                    suggestion=(
                        "The trace has no peak_rss for this process. Add peak_rss to "
                        "trace.fields in your config to get memory advice here."
                    ),
                )
            )
            continue

        mean_peak, max_peak = mean(peaks), max(peaks)
        requested = requested_memory_bytes.get(process)
        mean_cpu = round(mean(cpus), 1) if cpus else None

        if requested is None:
            verdict, suggestion = (
                "no_request_stated",
                f"Observed peak was {format_bytes(max_peak)}. Pass this process in "
                f"requested_memory to get an over- or under-allocation verdict.",
            )
        elif len(tasks) < MIN_TASKS_FOR_RESOURCE_ADVICE:
            verdict, suggestion = (
                "too_few_tasks",
                f"Only {len(tasks)} task(s) ran, so the average is not evidence of "
                f"anything. Observed peak {format_bytes(max_peak)} against a "
                f"{format_bytes(requested)} request.",
            )
        elif max_peak < requested * WASTE_RATIO:
            flagged += 1
            verdict, suggestion = (
                "over_allocated",
                f"Peak was {format_bytes(max_peak)} against a "
                f"{format_bytes(requested)} request -- under "
                f"{WASTE_RATIO:.0%} of it. Lowering the directive would let more "
                f"tasks run concurrently.",
            )
        elif max_peak > requested * 0.9:
            flagged += 1
            verdict, suggestion = (
                "near_limit",
                f"Peak was {format_bytes(max_peak)} against a "
                f"{format_bytes(requested)} request. That is little headroom; a "
                f"larger input would likely be OOM-killed.",
            )
        else:
            verdict, suggestion = (
                "reasonable",
                f"Peak {format_bytes(max_peak)} against {format_bytes(requested)} "
                f"leaves sensible headroom.",
            )

        rows.append(
            ProcessResources(
                process=process,
                tasks=len(tasks),
                mean_peak_rss_human=format_bytes(mean_peak),
                max_peak_rss_human=format_bytes(max_peak),
                mean_percent_cpu=mean_cpu,
                verdict=verdict,
                suggestion=suggestion,
            )
        )

    if not rows:
        headline = "No tasks in this run executed, so there is nothing to measure."
    elif flagged:
        headline = (
            f"{flagged} of {len(rows)} processes are mis-provisioned. "
            f"Over-allocation limits concurrency; near-limit risks OOM kills."
        )
    else:
        headline = f"No mis-provisioned processes found across {len(rows)} processes."

    return ResourceReport(
        trace_file=str(trace.path),
        processes=rows[:MAX_SAMPLE],
        note=(
            f"Showing {min(MAX_SAMPLE, len(rows))} of {len(rows)} processes. Cached "
            f"tasks are excluded: they have no timings of their own."
        ),
        headline=headline,
    )
