"""Finding and reading a Nextflow execution trace.

A trace file is a TSV with one row per task attempt. Where it lives depends on
how the pipeline was configured:

* ``trace.txt`` in the run directory, from a plain ``trace { enabled = true }``
* ``pipeline_info/execution_trace_<timestamp>.txt``, which is what nf-core
  pipelines write, one file per run

The second case is the reason discovery is a named, tested function rather than
a path join: an nf-core directory accumulates trace files, and picking the wrong
one means reporting on a run from last Tuesday with total confidence. This
module picks the newest and *says which file it used*, on every response.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

from nextflow_triage.errors import TriageError
from nextflow_triage.units import parse_duration_seconds, parse_memory_bytes, parse_percent

TRACE_NAMES = ("trace.txt", "trace.tsv", "execution_trace.txt")
PIPELINE_INFO_GLOB = "execution_trace*.txt"

# Statuses Nextflow writes. CACHED tasks did not run this time, so they are kept
# separate from COMPLETED everywhere rather than folded in -- a run that is 90%
# cached is a different event from one that did the work.
FAILED_STATUSES = frozenset({"FAILED", "ABORTED"})
SUCCESS_STATUSES = frozenset({"COMPLETED", "CACHED"})


@dataclass(frozen=True)
class TaskRecord:
    """One task attempt, with units already converted to numbers."""

    task_id: str
    name: str
    process: str
    status: str
    exit_code: str
    work_hash: str
    duration_seconds: float | None
    realtime_seconds: float | None
    peak_rss_bytes: int | None
    percent_cpu: float | None
    attempt: int

    @property
    def failed(self) -> bool:
        return self.status.upper() in FAILED_STATUSES

    @property
    def tag(self) -> str:
        """The per-sample label Nextflow puts in parentheses, when there is one.

        `name` looks like 'ALIGN (sample_03)'. The tag is usually the sample, so
        it is the single most useful field for a human reading a failure list,
        and worth pulling out rather than making the caller parse it.
        """
        if "(" in self.name and self.name.endswith(")"):
            return self.name[self.name.index("(") + 1 : -1]
        return ""


@dataclass(frozen=True)
class Trace:
    """Every task attempt from one trace file, plus where it came from."""

    path: Path
    tasks: list[TaskRecord]

    @property
    def provenance(self) -> str:
        return f"Read {len(self.tasks)} task records from {self.path}."


def find_trace_file(run_dir: Path) -> Path:
    """Locate the trace file for a run, preferring the newest nf-core one.

    Raises rather than guessing when there is nothing to read. Nextflow only
    writes a trace when it is switched on, so its absence is a configuration
    fact the caller needs told, not an empty result to work around.
    """
    if run_dir.is_file():
        return run_dir
    if not run_dir.is_dir():
        raise TriageError(
            f"No such directory: {run_dir}",
            recover="Pass the absolute path of the directory the pipeline was launched from.",
        )

    for name in TRACE_NAMES:
        candidate = run_dir / name
        if candidate.is_file():
            return candidate

    for parent in (run_dir / "pipeline_info", run_dir):
        if parent.is_dir():
            matches = sorted(
                parent.glob(PIPELINE_INFO_GLOB),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                return matches[0]

    raise TriageError(
        f"No Nextflow trace file found under {run_dir}.",
        recover=(
            "Re-run with tracing enabled (add `trace { enabled = true }` to your "
            "config, or -with-trace on the command line), or pass the path of an "
            "existing trace file directly."
        ),
        detail={"looked_for": [*TRACE_NAMES, f"pipeline_info/{PIPELINE_INFO_GLOB}"]},
    )


def _column(row: dict[str, str], *names: str) -> str:
    """Read the first column present under any of its known spellings.

    Nextflow's trace columns have changed spelling across versions ('%cpu' vs
    'pcpu', 'hash' vs 'work_dir'), and which columns appear at all depends on
    the `trace.fields` setting. Tolerating the variants here means one place to
    fix when a new one turns up.
    """
    for name in names:
        if name in row and row[name] is not None:
            return str(row[name]).strip()
    return ""


def read_trace(path: Path) -> Trace:
    """Parse a trace TSV into records, converting units as it goes."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TriageError(
            f"Could not read {path}: {exc}",
            recover="Check the file exists and is readable.",
        ) from exc

    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    if not reader.fieldnames or "status" not in [f.strip() for f in reader.fieldnames]:
        raise TriageError(
            f"{path.name} does not look like a Nextflow trace file: it has no 'status' column.",
            recover=(
                "Point at the trace file Nextflow wrote, usually trace.txt or "
                "pipeline_info/execution_trace_*.txt."
            ),
            detail={"columns_found": list(reader.fieldnames or [])},
        )

    tasks: list[TaskRecord] = []
    for raw in reader:
        row = {(k or "").strip(): (v or "") for k, v in raw.items()}
        name = _column(row, "name")
        if not name:
            continue
        tasks.append(
            TaskRecord(
                task_id=_column(row, "task_id", "id"),
                name=name,
                # 'ALIGN (sample_03)' -> 'ALIGN'. The process is what you group
                # by; the tag is what you look at.
                process=_column(row, "process") or name.split(" (")[0],
                status=_column(row, "status").upper(),
                exit_code=_column(row, "exit"),
                work_hash=_column(row, "hash", "workdir", "work_dir"),
                duration_seconds=parse_duration_seconds(_column(row, "duration")),
                realtime_seconds=parse_duration_seconds(_column(row, "realtime")),
                peak_rss_bytes=parse_memory_bytes(_column(row, "peak_rss", "rss", "vmem")),
                percent_cpu=parse_percent(_column(row, "%cpu", "pcpu", "cpu")),
                attempt=int(_column(row, "attempt") or 1),
            )
        )

    if not tasks:
        raise TriageError(
            f"{path.name} has a valid header but no task rows.",
            recover=(
                "The run may have failed before any task was submitted. Check "
                "the Nextflow log for a configuration or syntax error."
            ),
        )
    return Trace(path=path, tasks=tasks)


def load_run(run_dir: Path) -> Trace:
    """Find and read the trace for a run in one call."""
    return read_trace(find_trace_file(run_dir))
