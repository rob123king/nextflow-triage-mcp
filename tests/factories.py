"""Value factories and fixture data, importable by any test module.

Separate from `conftest.py` on purpose. `conftest.py` is auto-discovered by
pytest and exists to define *fixtures*; importing from it directly works only
because it happens to be on the path. Shared helpers that tests call as
ordinary functions belong in a normal module, which is this one.
"""

from collections.abc import Sequence
from pathlib import Path

from nextflow_triage.trace import TaskRecord

TRACE_HEADER = (
    "task_id\thash\tnative_id\tname\tstatus\texit\tsubmit\tduration\t"
    "realtime\t%cpu\tpeak_rss\tpeak_vmem\trchar\twchar\tattempt"
)

# The same three deliberate problems as scripts/make_fixture_run.py:
#   SORT_BAM  OOM-killed on every sample      -> a process fault
#   ALIGN     fails on one sample only        -> a bad input
#   INDEX     peaks far below a 64 GB request -> over-allocation
TRACE_ROWS = [
    ("1", "a1/111111", "FASTQC (sample_01)", "COMPLETED", "0", "2m 10s", "98.1%", "1.2 GB"),
    ("2", "a2/222222", "FASTQC (sample_02)", "COMPLETED", "0", "2m 4s", "97.4%", "1.1 GB"),
    ("3", "a3/333333", "FASTQC (sample_03)", "CACHED", "0", "-", "-", "-"),
    ("4", "b1/444444", "ALIGN (sample_01)", "COMPLETED", "0", "41m 12s", "780.2%", "12.4 GB"),
    ("5", "b2/555555", "ALIGN (sample_02)", "COMPLETED", "0", "38m 2s", "771.5%", "11.9 GB"),
    ("6", "b3/666666", "ALIGN (sample_03)", "FAILED", "1", "1m 2s", "102.0%", "800 MB"),
    ("7", "c1/777777", "SORT_BAM (sample_01)", "FAILED", "137", "12m 30s", "99.0%", "7.9 GB"),
    ("8", "c2/888888", "SORT_BAM (sample_02)", "FAILED", "137", "13m 1s", "99.2%", "7.9 GB"),
    ("9", "d1/999999", "INDEX (sample_01)", "COMPLETED", "0", "45s", "95.0%", "890 MB"),
    ("10", "d2/aaaaaa", "INDEX (sample_02)", "COMPLETED", "0", "47s", "94.1%", "900 MB"),
    ("11", "d3/bbbbbb", "INDEX (sample_03)", "COMPLETED", "0", "44s", "96.2%", "880 MB"),
]

WORK_CONTENT = {
    "b3/666666": (
        "1",
        "bwa mem ref.fa sample_03*.fq.gz > sample_03.sam\n",
        "[E::bwa_idx_load_from_disk] fail to locate the index files\n",
    ),
    "c1/777777": (
        "137",
        "samtools sort -@ 8 -m 4G -o sample_01.bam sample_01.sam\n",
        "samtools sort: couldn't allocate memory for bam_mem\nKilled\n",
    ),
    "c2/888888": (
        "137",
        "samtools sort -@ 8 -m 4G -o sample_02.bam sample_02.sam\n",
        "samtools sort: couldn't allocate memory for bam_mem\nKilled\n",
    ),
}


def write_trace(path: Path, rows: Sequence[tuple[str, ...]] = TRACE_ROWS) -> Path:
    """Write a trace TSV. Kept a plain function so a test can write its own rows."""
    lines = [TRACE_HEADER]
    for task_id, task_hash, name, status, exit_code, duration, cpu, rss in rows:
        lines.append(
            "\t".join(
                [
                    task_id,
                    task_hash,
                    "1000",
                    name,
                    status,
                    exit_code,
                    "2026-09-22 09:00:00.000",
                    duration,
                    duration,
                    cpu,
                    rss,
                    rss,
                    "1.0 GB",
                    "500 MB",
                    "1",
                ]
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def task(
    task_id: str = "1",
    *,
    process: str = "ALIGN",
    status: str = "COMPLETED",
    exit_code: str = "0",
    tag: str = "sample_01",
    realtime: float | None = 60.0,
    peak_rss: int | None = 1024**3,
    percent_cpu: float | None = 100.0,
) -> TaskRecord:
    """Build one TaskRecord directly, for testing analysis without any files.

    A plain factory function rather than a fixture: these are values, not
    resources, and a test often needs several with different fields. A fixture
    would force every variation through parametrisation for no benefit.
    """
    return TaskRecord(
        task_id=task_id,
        name=f"{process} ({tag})" if tag else process,
        process=process,
        status=status,
        exit_code=exit_code,
        work_hash=f"ab/{task_id}",
        duration_seconds=realtime,
        realtime_seconds=realtime,
        peak_rss_bytes=peak_rss,
        percent_cpu=percent_cpu,
        attempt=1,
    )
