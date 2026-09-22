"""Unit-string parsing, over every format Nextflow actually emits.

This is the most systematic thing that could break: every duration, memory and
CPU number in every response passes through here, so an error would be wrong
everywhere at once rather than in one place. Hence the exhaustive parametrise
rather than a handful of spot checks.
"""

import pytest

from nextflow_triage.units import (
    format_bytes,
    format_duration,
    is_missing,
    parse_duration_seconds,
    parse_memory_bytes,
    parse_percent,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1s", 1.0),
        ("1.5s", 1.5),
        ("30s", 30.0),
        ("2m", 120.0),
        ("2m 30s", 150.0),
        ("1h", 3600.0),
        ("1h 2m 3s", 3723.0),
        ("2d", 172800.0),
        ("154ms", 0.154),
        ("1.2s", 1.2),
        ("  3m 4s  ", 184.0),
        ("41m 12s", 2472.0),
    ],
)
def test_durations_parse(text: str, expected: float) -> None:
    assert parse_duration_seconds(text) == pytest.approx(expected)


def test_milliseconds_are_not_read_as_minutes() -> None:
    """'ms' has to match before 'm'. If the regex alternation is ever reordered,
    154ms silently becomes 154 minutes and every runtime figure is wrong."""
    assert parse_duration_seconds("154ms") == pytest.approx(0.154)
    assert parse_duration_seconds("154m") == pytest.approx(9240.0)


@pytest.mark.parametrize("text", ["-", "", "   ", "na", "N/A"])
def test_a_missing_duration_is_none_not_zero(text: str) -> None:
    """None and 0.0 are different claims. A task with no recorded runtime is not
    a task that ran instantly, and averaging the two together understates every
    figure derived from it."""
    assert parse_duration_seconds(text) is None


def test_a_bare_number_is_read_as_milliseconds() -> None:
    """Some executors write raw millisecond counts rather than a formatted string."""
    assert parse_duration_seconds("1500") == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 GB", 1024**3),
        ("1GB", 1024**3),
        ("1.5 GB", int(1.5 * 1024**3)),
        ("800 MB", 800 * 1024**2),
        ("512 KB", 512 * 1024),
        ("2 TB", 2 * 1024**4),
        ("1 GiB", 1024**3),
        ("1024", 1024),
        ("7.9 GB", int(7.9 * 1024**3)),
    ],
)
def test_memory_parses_in_binary_multiples(text: str, expected: int) -> None:
    """Nextflow prints 'GB' but means GiB, matching how schedulers account for it."""
    assert parse_memory_bytes(text) == expected


@pytest.mark.parametrize("text", ["-", "", "not a size", "GB"])
def test_unparseable_memory_is_none(text: str) -> None:
    assert parse_memory_bytes(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("98.1%", 98.1), ("780.2%", 780.2), ("100", 100.0), ("0%", 0.0)],
)
def test_percentages_parse(text: str, expected: float) -> None:
    assert parse_percent(text) == pytest.approx(expected)


def test_cpu_above_one_hundred_percent_is_kept() -> None:
    """780% means roughly eight cores busy. Clamping it to 100 would destroy the
    one signal that tells you whether a multi-core request was used."""
    assert parse_percent("780.2%") == pytest.approx(780.2)


@pytest.mark.parametrize("text", ["-", "", "n/a"])
def test_missing_is_recognised(text: str) -> None:
    assert is_missing(text) is True


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.15, "150ms"), (5.0, "5.0s"), (90.0, "1m 30s"), (3723.0, "1h 2m")],
)
def test_durations_format_for_humans(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1024**3, "1.0 GB"),
        (int(1.5 * 1024**3), "1.5 GB"),
        (800 * 1024**2, "800.0 MB"),
        (512, "512 B"),
    ],
)
def test_bytes_format_for_humans(value: int, expected: str) -> None:
    assert format_bytes(value) == expected


@pytest.mark.parametrize("text", ["2m 30s", "1h 2m 3s", "154ms", "1.5s"])
def test_formatting_round_trips_approximately(text: str) -> None:
    """Parse then format then parse should land in the same place.

    Deliberately not exact. `format_duration` drops the smallest unit as the
    value grows -- an hour-plus duration renders as '1h 2m' with the seconds
    gone -- because that is what is readable. So the tolerance is one unit of
    whatever the smallest *displayed* unit is: a minute above an hour, a second
    below it.

    Getting this wrong in the other direction is the point of the test: a drift
    larger than that would mean one of the two conversions has a unit wrong,
    which is the bug that would silently corrupt every runtime figure.
    """
    seconds = parse_duration_seconds(text)
    assert seconds is not None
    reparsed = parse_duration_seconds(format_duration(seconds))
    assert reparsed is not None
    tolerance = 60.0 if seconds >= 3600 else 1.0
    assert abs(reparsed - seconds) < tolerance
