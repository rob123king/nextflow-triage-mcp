#!/usr/bin/env python3
"""Write a realistic fake Nextflow run directory to poke the server at.

Real pipeline runs are large, slow to produce and often contain data you cannot
share. This builds a directory with the same *shape* -- an nf-core style
`pipeline_info/execution_trace_*.txt`, a `work/` tree with `.command.sh`,
`.command.err` and `.exitcode` -- holding three deliberate problems:

* SORT_BAM is OOM-killed (exit 137) on every sample: a process-level fault.
* ALIGN fails (exit 1) on one sample only: a bad input.
* INDEX asks for 64 GB and peaks at 900 MB: over-allocation.

    uv run python scripts/make_fixture_run.py [directory]

It prints the tool arguments to use afterwards.
"""

import sys
from pathlib import Path

TRACE_HEADER = (
    "task_id\thash\tnative_id\tname\tstatus\texit\tsubmit\tduration\t"
    "realtime\t%cpu\tpeak_rss\tpeak_vmem\trchar\twchar\tattempt"
)

# (task_id, hash, name, status, exit, duration, realtime, %cpu, peak_rss)
ROWS = [
    (
        "1",
        "a1/111111",
        "FASTQC (sample_01)",
        "COMPLETED",
        "0",
        "2m 10s",
        "2m 5s",
        "98.1%",
        "1.2 GB",
    ),
    (
        "2",
        "a2/222222",
        "FASTQC (sample_02)",
        "COMPLETED",
        "0",
        "2m 4s",
        "1m 59s",
        "97.4%",
        "1.1 GB",
    ),
    ("3", "a3/333333", "FASTQC (sample_03)", "CACHED", "0", "-", "-", "-", "-"),
    (
        "4",
        "b1/444444",
        "ALIGN (sample_01)",
        "COMPLETED",
        "0",
        "41m 12s",
        "40m 50s",
        "780.2%",
        "12.4 GB",
    ),
    (
        "5",
        "b2/555555",
        "ALIGN (sample_02)",
        "COMPLETED",
        "0",
        "38m 2s",
        "37m 40s",
        "771.5%",
        "11.9 GB",
    ),
    ("6", "b3/666666", "ALIGN (sample_03)", "FAILED", "1", "1m 2s", "58s", "102.0%", "800 MB"),
    (
        "7",
        "c1/777777",
        "SORT_BAM (sample_01)",
        "FAILED",
        "137",
        "12m 30s",
        "12m 20s",
        "99.0%",
        "7.9 GB",
    ),
    (
        "8",
        "c2/888888",
        "SORT_BAM (sample_02)",
        "FAILED",
        "137",
        "13m 1s",
        "12m 50s",
        "99.2%",
        "7.9 GB",
    ),
    ("9", "d1/999999", "INDEX (sample_01)", "COMPLETED", "0", "45s", "43s", "95.0%", "890 MB"),
    ("10", "d2/aaaaaa", "INDEX (sample_02)", "COMPLETED", "0", "47s", "44s", "94.1%", "900 MB"),
    ("11", "d3/bbbbbb", "INDEX (sample_03)", "COMPLETED", "0", "44s", "42s", "96.2%", "880 MB"),
]

# Only failed tasks get a work directory here, which also exercises the
# "directory was cleaned up" path for everything else.
WORK_CONTENT = {
    "b3/666666": (
        "1",
        "#!/bin/bash -ue\nbwa mem ref.fa sample_03_R1.fq.gz sample_03_R2.fq.gz > sample_03.sam\n",
        "[E::bwa_idx_load_from_disk] fail to locate the index files\n"
        "[main] ERROR: could not load reference index\n",
    ),
    "c1/777777": (
        "137",
        "#!/bin/bash -ue\nsamtools sort -@ 8 -m 4G -o sample_01.bam sample_01.sam\n",
        "samtools sort: couldn't allocate memory for bam_mem\n"
        "/bin/bash: line 1: 4823 Killed  samtools sort -@ 8 -m 4G -o sample_01.bam\n",
    ),
    "c2/888888": (
        "137",
        "#!/bin/bash -ue\nsamtools sort -@ 8 -m 4G -o sample_02.bam sample_02.sam\n",
        "samtools sort: couldn't allocate memory for bam_mem\n"
        "/bin/bash: line 1: 5011 Killed  samtools sort -@ 8 -m 4G -o sample_02.bam\n",
    ),
}


def main() -> int:
    root = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / "nf-fixture-run"
    (root / "pipeline_info").mkdir(parents=True, exist_ok=True)

    lines = [TRACE_HEADER]
    for task_id, task_hash, name, status, exit_code, duration, realtime, cpu, rss in ROWS:
        lines.append(
            "\t".join(
                [
                    task_id,
                    task_hash,
                    str(10000 + int(task_id)),
                    name,
                    status,
                    exit_code,
                    "2026-09-22 09:00:00.000",
                    duration,
                    realtime,
                    cpu,
                    rss,
                    rss,
                    "1.0 GB",
                    "500 MB",
                    "1",
                ]
            )
        )
    trace = root / "pipeline_info" / "execution_trace_2026-09-22_09-00-00.txt"
    trace.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for task_hash, (exit_code, command, stderr) in WORK_CONTENT.items():
        bucket, rest = task_hash.split("/")
        # Real work directories use the full 32-character hash; the trace shows
        # only a prefix. Padding here means the fixture exercises the
        # prefix-matching path in workdir.resolve_work_dir rather than an exact hit.
        directory = root / "work" / bucket / (rest + "cdef0123456789abcdef0123")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".exitcode").write_text(exit_code + "\n", encoding="utf-8")
        (directory / ".command.sh").write_text(command, encoding="utf-8")
        (directory / ".command.err").write_text(stderr, encoding="utf-8")
        (directory / ".command.out").write_text("", encoding="utf-8")

    print(f"Fixture run written to {root}\n")
    print("Try these, in order:\n")
    print("  summarise_run")
    print(f"    run_dir = {root}\n")
    print("  explain_run_failures")
    print(f"    run_dir = {root}\n")
    print("  explain_task_failure")
    print(f"    run_dir = {root}")
    print("    task_id = 7            # SORT_BAM, exit 137\n")
    print("  check_resource_usage")
    print(f"    run_dir          = {root}")
    print('    requested_memory = {"INDEX": "64 GB", "ALIGN": "16 GB", "SORT_BAM": "8 GB"}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
