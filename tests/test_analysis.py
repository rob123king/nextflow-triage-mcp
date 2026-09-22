"""Trace reading, work-directory resolution, and the verdict logic.

The analysis functions take a `Trace` and return a model, with no filesystem
access. That is deliberate and it is what these tests exploit: every interesting
case is built from `TaskRecord` values in a couple of lines, with no pipeline
run, no trace file, and no waiting.
"""

from pathlib import Path

import pytest

from nextflow_triage.analysis import assess_resources, group_failures, summarise
from nextflow_triage.errors import TriageError
from nextflow_triage.trace import Trace, find_trace_file, load_run, read_trace
from nextflow_triage.workdir import read_diagnostics, resolve_work_dir
from tests.factories import task, write_trace

GB = 1024**3


def make_trace(*tasks: object, path: str = "trace.txt") -> Trace:
    return Trace(path=Path(path), tasks=list(tasks))  # type: ignore[arg-type]


# ----------------------------------------------------------------- discovery


def test_an_nfcore_pipeline_info_trace_is_found(run_dir: Path) -> None:
    found = find_trace_file(run_dir)
    assert found.name.startswith("execution_trace_")


def test_a_plain_trace_txt_is_preferred(tmp_path: Path) -> None:
    write_trace(tmp_path / "trace.txt")
    write_trace(tmp_path / "pipeline_info" / "execution_trace_old.txt")
    assert find_trace_file(tmp_path).name == "trace.txt"


def test_the_newest_nfcore_trace_wins(tmp_path: Path) -> None:
    """An nf-core directory accumulates one trace per run.

    Picking the wrong one means reporting confidently on a run from last week,
    which is the worst kind of wrong: plausible and undetectable. The newest is
    the only defensible default, and every response states which file was used.
    """
    import os
    import time

    old = write_trace(tmp_path / "pipeline_info" / "execution_trace_2026-01-01.txt")
    new = write_trace(tmp_path / "pipeline_info" / "execution_trace_2026-09-01.txt")
    past = time.time() - 10_000
    os.utime(old, (past, past))
    assert find_trace_file(tmp_path) == new


def test_a_trace_file_path_is_accepted_directly(run_dir: Path) -> None:
    direct = next((run_dir / "pipeline_info").glob("*.txt"))
    assert find_trace_file(direct) == direct


def test_a_missing_trace_explains_how_to_enable_it(tmp_path: Path) -> None:
    with pytest.raises(TriageError) as caught:
        find_trace_file(tmp_path)
    assert "trace { enabled = true }" in str(caught.value)


def test_a_missing_directory_is_a_recoverable_error(tmp_path: Path) -> None:
    with pytest.raises(TriageError, match="No such directory"):
        find_trace_file(tmp_path / "nope")


def test_a_file_that_is_not_a_trace_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "trace.txt"
    other.write_text("colA\tcolB\n1\t2\n", encoding="utf-8")
    with pytest.raises(TriageError, match="no 'status' column"):
        read_trace(other)


# -------------------------------------------------------------------- parsing


def test_records_parse_with_units_converted(run_dir: Path) -> None:
    trace = load_run(run_dir)
    assert len(trace.tasks) == 11
    align = next(t for t in trace.tasks if t.task_id == "4")
    assert align.process == "ALIGN"
    assert align.tag == "sample_01"
    assert align.realtime_seconds == pytest.approx(2472.0)
    assert align.peak_rss_bytes == pytest.approx(12.4 * GB, rel=0.01)


def test_the_tag_is_extracted_from_the_task_name() -> None:
    """'ALIGN (sample_03)' -- the tag is the single most useful field in a
    failure list, so it is pulled out rather than left for the caller to parse."""
    assert task(tag="sample_03").tag == "sample_03"
    assert task(tag="").tag == ""


# ------------------------------------------------------------------ summary


def test_a_clean_run_says_so(tmp_path: Path) -> None:
    trace = make_trace(task("1"), task("2", process="SORT"))
    summary = summarise(trace)
    assert summary.failed_tasks == 0
    assert "All 2 tasks succeeded" in summary.headline


def test_failures_point_at_the_next_tool(run_dir: Path) -> None:
    """The headline naming the next tool is the calling-sequence design made
    machine-readable: the model does not have to infer the workflow."""
    summary = summarise(load_run(run_dir))
    assert summary.failed_tasks == 3
    assert "explain_run_failures" in summary.headline


def test_a_fully_cached_run_is_called_out(tmp_path: Path) -> None:
    """A run that did no work looks like a successful run in every count.
    Saying so explicitly is the difference between -resume working and a
    pipeline that silently skipped everything you wanted."""
    trace = make_trace(task("1", status="CACHED"), task("2", status="CACHED"))
    assert "did no work" in summarise(trace).headline


def test_processes_are_ranked_by_total_runtime(run_dir: Path) -> None:
    summary = summarise(load_run(run_dir))
    assert summary.processes[0].process == "ALIGN"
    assert summary.processes[0].tasks == 3


def test_the_summary_is_bounded_and_says_so() -> None:
    trace = make_trace(*[task(str(i), process=f"P{i}") for i in range(20)])
    summary = summarise(trace)
    assert len(summary.processes) == 5
    assert "of 20 processes" in summary.sample_note


# ----------------------------------------------------------------- failures


def test_failures_group_by_process_and_exit_code(run_dir: Path) -> None:
    report = group_failures(load_run(run_dir))
    assert report.total_failures == 3
    groups = {(g.process, g.exit_code): g for g in report.groups}
    assert groups[("SORT_BAM", "137")].count == 2
    assert groups[("ALIGN", "1")].count == 1


def test_exit_137_is_interpreted_as_the_oom_killer(run_dir: Path) -> None:
    report = group_failures(load_run(run_dir))
    sort_group = next(g for g in report.groups if g.process == "SORT_BAM")
    assert "out-of-memory" in sort_group.interpretation
    assert "raise the memory directive" in sort_group.interpretation


def test_one_bad_sample_is_distinguished_from_a_broken_process() -> None:
    """The distinction that decides what to fix.

    Failures confined to one tag point at the input; failures spread across tags
    point at the process configuration. A count alone tells you neither.
    """
    one_sample = make_trace(
        task("1", status="FAILED", exit_code="1", tag="sample_03"),
        task("2", status="FAILED", exit_code="1", tag="sample_03"),
    )
    many_samples = make_trace(
        task("1", status="FAILED", exit_code="1", tag="sample_01"),
        task("2", status="FAILED", exit_code="1", tag="sample_02"),
    )
    assert "suspect that input" in group_failures(one_sample).groups[0].interpretation
    assert "suspect the process" in group_failures(many_samples).groups[0].interpretation


def test_an_unknown_exit_code_does_not_invent_a_cause() -> None:
    trace = make_trace(task("1", status="FAILED", exit_code="42"))
    interpretation = group_failures(trace).groups[0].interpretation
    assert "tool-specific" in interpretation
    assert "read the stderr" in interpretation


def test_a_clean_run_has_no_failure_groups() -> None:
    assert group_failures(make_trace(task("1"))).headline == "No failed tasks in this run."


def test_every_group_names_a_task_to_drill_into(run_dir: Path) -> None:
    report = group_failures(load_run(run_dir))
    assert all(group.example_task_id for group in report.groups)
    assert "explain_task_failure" in report.sample_note


# ---------------------------------------------------------------- resources


def test_over_allocation_is_flagged() -> None:
    """The 64 GB request that peaks at 900 MB."""
    trace = make_trace(*[task(str(i), process="INDEX", peak_rss=900 * 1024**2) for i in range(3)])
    report = assess_resources(trace, {"INDEX": 64 * GB})
    assert report.processes[0].verdict == "over_allocated"
    assert "more tasks run concurrently" in report.processes[0].suggestion


def test_running_near_the_limit_is_flagged_too() -> None:
    """The opposite failure, and the more dangerous one: the next larger input
    gets OOM-killed. Reporting only over-allocation would miss it entirely."""
    trace = make_trace(*[task(str(i), process="SORT", peak_rss=int(7.8 * GB)) for i in range(3)])
    report = assess_resources(trace, {"SORT": 8 * GB})
    assert report.processes[0].verdict == "near_limit"
    assert "OOM-killed" in report.processes[0].suggestion


def test_sensible_headroom_is_not_nagged_about() -> None:
    trace = make_trace(*[task(str(i), process="SORT", peak_rss=5 * GB) for i in range(3)])
    assert assess_resources(trace, {"SORT": 8 * GB}).processes[0].verdict == "reasonable"


def test_too_few_tasks_is_its_own_verdict_not_a_pass() -> None:
    """Two tasks is not evidence. Saying 'reasonable' here would be a claim the
    data does not support, so the verdict names the gap instead."""
    trace = make_trace(task("1", process="ONCE", peak_rss=1 * GB))
    report = assess_resources(trace, {"ONCE": 64 * GB})
    assert report.processes[0].verdict == "too_few_tasks"


def test_without_a_stated_request_usage_is_still_reported() -> None:
    """No request means no verdict, but 'how much does this actually need' is
    still worth answering."""
    trace = make_trace(*[task(str(i), process="INDEX", peak_rss=900 * 1024**2) for i in range(3)])
    row = assess_resources(trace, {}).processes[0]
    assert row.verdict == "no_request_stated"
    assert "900.0 MB" in row.suggestion


def test_missing_memory_data_says_how_to_enable_it() -> None:
    trace = make_trace(*[task(str(i), process="P", peak_rss=None) for i in range(3)])
    row = assess_resources(trace, {"P": 8 * GB}).processes[0]
    assert row.verdict == "no_memory_data"
    assert "trace.fields" in row.suggestion


def test_cached_tasks_are_excluded_from_resource_figures() -> None:
    """A cached task has no timings of its own; averaging it in would drag every
    figure towards whatever the placeholder happened to be."""
    trace = make_trace(
        task("1", process="P", peak_rss=4 * GB),
        task("2", process="P", status="CACHED", peak_rss=None),
    )
    assert assess_resources(trace, {}).processes[0].tasks == 1


def test_high_cpu_percent_survives_into_the_report() -> None:
    trace = make_trace(*[task(str(i), process="ALIGN", percent_cpu=780.0) for i in range(3)])
    assert assess_resources(trace, {}).processes[0].mean_percent_cpu == pytest.approx(780.0)


# ------------------------------------------------------------- work directory


def test_a_short_hash_resolves_to_the_full_directory(run_dir: Path) -> None:
    """The trace shows 'c1/777777'; the directory is 'c1/777777cdef...'."""
    resolved = resolve_work_dir(run_dir, "c1/777777")
    assert resolved is not None
    assert resolved.name.startswith("777777")


def test_diagnostics_read_the_command_and_stderr(run_dir: Path) -> None:
    diagnostics = read_diagnostics(run_dir, "c1/777777")
    assert diagnostics.exit_code == "137"
    assert "samtools sort" in (diagnostics.command or "")
    assert any("couldn't allocate memory" in line for line in diagnostics.stderr_tail)


def test_a_cleaned_up_work_directory_is_explained_not_raised(run_dir: Path) -> None:
    """Cleaned-up and resumed runs are both ordinary. Raising here would turn a
    normal situation into an apparent failure of the tool."""
    diagnostics = read_diagnostics(run_dir, "zz/999999")
    assert diagnostics.work_dir is None
    assert "cleaned up" in diagnostics.note
    assert "resumed" in diagnostics.note


def test_a_huge_stderr_is_tailed_not_loaded(run_dir: Path) -> None:
    """A task failing in a loop can leave a gigabyte of stderr. Reading it all
    to take the last twenty lines would work and would also be a very bad idea
    on a login node."""
    directory = resolve_work_dir(run_dir, "c1/777777")
    assert directory is not None
    (directory / ".command.err").write_text("x" * 500 + "\n" + "line\n" * 5000, encoding="utf-8")

    diagnostics = read_diagnostics(run_dir, "c1/777777")
    assert len(diagnostics.stderr_tail) == 20
    assert all(len(line) <= 300 for line in diagnostics.stderr_tail)
