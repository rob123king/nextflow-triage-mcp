"""Parsing the unit strings Nextflow writes into its trace file.

Nextflow does not write machine-friendly numbers. Durations come out as
``2h 30m 15s``, ``1.5s`` or ``154ms``; memory as ``1.2 GB`` or ``800 MB``; CPU
as ``123.4%``. A missing value is ``-``.

This module is pure string-to-number conversion with no file handling, which is
what lets it be tested exhaustively against the real formats without a Nextflow
run anywhere in sight. Everything else in the package builds on it, so an error
here would be systematic rather than local.
"""

import re

MISSING = {"-", "", "na", "n/a", "null"}

_DURATION_PART = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h|d)", re.IGNORECASE)

# Seconds per unit. Nextflow's 'm' is always minutes in a duration; it never
# means milliseconds, which is why 'ms' has to be matched before 's' and 'm'
# above -- the alternation order in the regex is load-bearing.
_DURATION_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

_MEMORY = re.compile(
    r"^(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTP]?i?B?)$",
    re.IGNORECASE,
)

# Nextflow reports memory in binary multiples even though it prints "GB", which
# matches how schedulers and the JVM actually account for it.
_MEMORY_BYTES = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "GIB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
    "TIB": 1024**4,
    "P": 1024**5,
    "PB": 1024**5,
    "PIB": 1024**5,
}


def is_missing(text: str) -> bool:
    return text.strip().lower() in MISSING


def parse_duration_seconds(text: str) -> float | None:
    """Convert a Nextflow duration to seconds, or None when it is absent.

    Returns None rather than raising or defaulting to zero. A task with no
    recorded runtime is a different thing from a task that ran for no time, and
    collapsing the two would quietly understate every average computed from it.
    """
    cleaned = text.strip()
    if is_missing(cleaned):
        return None

    total = 0.0
    matched_any = False
    for part in _DURATION_PART.finditer(cleaned):
        total += float(part.group("value")) * _DURATION_SECONDS[part.group("unit").lower()]
        matched_any = True
    if matched_any:
        return total

    # Some executors write a bare number of milliseconds.
    try:
        return float(cleaned) / 1000.0
    except ValueError:
        return None


def parse_memory_bytes(text: str) -> int | None:
    """Convert a Nextflow memory string to bytes, or None when it is absent."""
    cleaned = text.strip()
    if is_missing(cleaned):
        return None
    match = _MEMORY.match(cleaned)
    if not match:
        return None
    unit = match.group("unit").upper().replace("IB", "IB")
    multiplier = _MEMORY_BYTES.get(unit)
    if multiplier is None:
        return None
    return int(float(match.group("value")) * multiplier)


def parse_percent(text: str) -> float | None:
    """Convert a Nextflow percentage such as '123.4%' to a float, or None."""
    cleaned = text.strip().rstrip("%")
    if is_missing(cleaned):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_duration(seconds: float) -> str:
    """Render seconds back into something a human reads at a glance.

    Used only in response text. Numbers stay numbers in the structured output,
    so a caller that wants to compute with them never has to re-parse this.
    """
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}h {rest // 60}m"


def format_bytes(value: float) -> str:
    """Render bytes as the GB/MB string a pipeline author would recognise."""
    for unit, size in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if value >= size:
            return f"{value / size:.1f} {unit}"
    return f"{value:.0f} B"
