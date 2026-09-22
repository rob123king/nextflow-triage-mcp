"""Reading what a failed task left behind in its work directory.

Nextflow puts each task in ``work/<first two hash chars>/<rest of hash>/`` and
drops the command it ran, its stdout, its stderr and its exit code in there.
That directory is where the actual reason for a failure lives -- the trace file
only says a task exited non-zero.

The trace records a shortened hash (``ab/cdef12``), so the directory has to be
found by prefix rather than exact match.
"""

from dataclasses import dataclass
from pathlib import Path

MAX_TAIL_LINES = 20
"""Lines kept from the end of a log. Errors are at the end, and the head of a
stderr stream is usually a tool's banner."""

MAX_LINE_CHARS = 300
"""A single line cap, for tools that emit a megabyte-long progress line."""

MAX_COMMAND_CHARS = 1500


@dataclass(frozen=True)
class TaskDiagnostics:
    """What the work directory says about one task."""

    work_dir: str | None
    exit_code: str | None
    command: str | None
    stderr_tail: list[str]
    stdout_tail: list[str]
    note: str


def resolve_work_dir(run_dir: Path, work_hash: str) -> Path | None:
    """Turn a trace hash like 'ab/cdef12' into a real directory, or None.

    The trace shortens the hash, so the second component is a prefix and the
    directory has to be found by scanning that one bucket. Scanning is cheap:
    a bucket holds at most a few hundred entries however large the run is.
    """
    cleaned = work_hash.strip()
    if not cleaned or "/" not in cleaned:
        return None
    bucket_name, prefix = cleaned.split("/", 1)
    bucket = run_dir / "work" / bucket_name
    if not bucket.is_dir():
        return None
    exact = bucket / prefix
    if exact.is_dir():
        return exact
    for entry in sorted(bucket.iterdir()):
        if entry.is_dir() and entry.name.startswith(prefix):
            return entry
    return None


def _tail(path: Path, limit: int = MAX_TAIL_LINES) -> list[str]:
    """Last few lines of a file, truncated per line and never unbounded.

    A task that failed in a loop can leave a gigabyte of stderr. Reading it all
    to take the last twenty lines would work and would also be a very bad idea
    on a login node, so this reads a bounded window from the end of the file.
    """
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            # 64 KiB is comfortably more than MAX_TAIL_LINES of ordinary log
            # output, and bounds the read regardless of file size.
            window = min(size, 64 * 1024)
            handle.seek(size - window)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return [line[:MAX_LINE_CHARS] for line in lines[-limit:]]


def read_diagnostics(run_dir: Path, work_hash: str) -> TaskDiagnostics:
    """Collect the command, exit code and log tails for one task.

    Returns a populated object with a `note` explaining any gap rather than
    raising. A missing work directory is ordinary -- runs get cleaned up, and
    `-resume` reuses directories from elsewhere -- and the rest of the answer is
    still worth having.
    """
    directory = resolve_work_dir(run_dir, work_hash)
    if directory is None:
        return TaskDiagnostics(
            work_dir=None,
            exit_code=None,
            command=None,
            stderr_tail=[],
            stdout_tail=[],
            note=(
                f"No work directory found for hash {work_hash!r} under "
                f"{run_dir / 'work'}. It was probably cleaned up, or this run "
                f"was resumed and the task's directory belongs to an earlier run. "
                f"The trace still tells you which task failed and with what exit code."
            ),
        )

    exit_file = directory / ".exitcode"
    command_file = directory / ".command.sh"
    command = None
    if command_file.is_file():
        command = command_file.read_text(encoding="utf-8", errors="replace")[:MAX_COMMAND_CHARS]

    return TaskDiagnostics(
        work_dir=str(directory),
        exit_code=(
            exit_file.read_text(encoding="utf-8", errors="replace").strip()
            if exit_file.is_file()
            else None
        ),
        command=command,
        stderr_tail=_tail(directory / ".command.err"),
        stdout_tail=_tail(directory / ".command.out"),
        note=(
            f"Showing the last {MAX_TAIL_LINES} non-empty lines of each stream. "
            f"The full logs are in {directory}."
        ),
    )
