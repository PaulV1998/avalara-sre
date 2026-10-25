"""Tests for reading and checking the request records."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from slo_eval.errors import InputError
from slo_eval.records import Record, load_records, parse_records

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample.jsonl"
WHEN = datetime(2026, 10, 23, 14, 0, 1, tzinfo=UTC)
VALID = '{"timestamp": "2026-10-23T14:00:01Z", "success": true, "latency_ms": 112}'
DROP = object()

BOM_PROBLEM = "starts with a byte order mark (BOM); the format is UTF-8 without a BOM"
TIMESTAMP_RULE = "must be an ISO 8601 timestamp"
TIMEZONE_RULE = "must include a timezone (Z or an offset such as +02:00)"
PRECISION_RULE = "must have at most 6 fractional-second digits"
LATENCY_RULE = "must be a number of at least 0"


def line(**changes: object) -> str:
    fields = {"timestamp": "2026-10-23T14:00:01Z", "success": True, "latency_ms": 112, **changes}
    return json.dumps({key: value for key, value in fields.items() if value is not DROP})


def rejected(*lines: bytes | str) -> InputError:
    with pytest.raises(InputError) as excinfo:
        parse_records(lines)
    return excinfo.value


def problem(text: bytes | str) -> str:
    [reported] = rejected(text).problems
    assert reported.startswith("line 1: ")
    return reported.removeprefix("line 1: ")


def test_sample_file_loads_with_exact_types() -> None:
    records = load_records(SAMPLE)

    assert len(records) == 12
    assert records[0] == Record(WHEN, True, 112)
    assert type(records[0].latency_ms) is int
    assert records[2].latency_ms == Decimal("143.5")
    assert type(records[2].latency_ms) is Decimal
    assert records[3] == Record(datetime(2026, 10, 23, 14, 0, 12, tzinfo=UTC), False, 121)
    assert records[8].latency_ms is None
    assert sum(record.success for record in records) == 10


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-23T14:00:01Z", WHEN),
        ("2026-10-23T16:00:01+02:00", WHEN),
        ("2026-10-23T09:00:01-05:00", WHEN),
        ("2026-W43-5T14:00:01Z", WHEN),
        ("2026-10-23T14:00:01.5Z", WHEN.replace(microsecond=500_000)),
        ("2026-10-23T14:00:01,5Z", WHEN.replace(microsecond=500_000)),
        ("2026-10-23T14:00:01.123456Z", WHEN.replace(microsecond=123_456)),
    ],
)
def test_timestamps_are_read_as_utc(text: str, expected: datetime) -> None:
    [record] = parse_records([line(timestamp=text)])

    assert record.timestamp == expected
    assert record.timestamp.tzinfo is UTC


@pytest.mark.parametrize(
    ("fields", "latency"),
    [
        ({"latency_ms": 0}, 0),
        ({"latency_ms": 250.5}, Decimal("250.5")),
        ({"success": False, "latency_ms": DROP}, None),
        ({"success": False, "latency_ms": None}, None),
        ({"success": False, "latency_ms": 30000}, 30000),
    ],
)
def test_latency_is_kept_exactly_and_optional_on_failures(fields: dict, latency: object) -> None:
    [record] = parse_records([line(**fields)])

    assert record.latency_ms == latency
    assert type(record.latency_ms) is type(latency)


def test_huge_latency_stays_a_cheap_decimal() -> None:
    [record] = parse_records([VALID.replace("112", "1e999999999")])

    assert record.latency_ms == Decimal("1e999999999")


def test_unknown_fields_are_ignored() -> None:
    [record] = parse_records([line(region="eu-west-1", status=503)])

    assert record == Record(WHEN, True, 112)


def test_records_keep_file_order() -> None:
    later = line(timestamp="2026-10-23T15:00:00Z")

    records = parse_records([later, VALID])

    assert [record.timestamp.hour for record in records] == [15, 14]


@pytest.mark.parametrize("lines", [[], [""], ["\n", " \t\r\n"], [b"", b"\r\n"]])
def test_input_without_records_is_valid_and_empty(lines: list[bytes | str]) -> None:
    assert parse_records(lines) == []


@pytest.mark.parametrize("text", ["\x0c", "\u00a0"], ids=["form feed", "no-break space"])
def test_only_json_whitespace_makes_a_line_blank(text: str) -> None:
    assert problem(text) == "not valid JSON: Expecting value (column 1)"


def test_bytes_with_windows_line_endings() -> None:
    assert parse_records([VALID.encode() + b"\r\n"]) == [Record(WHEN, True, 112)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            '{"timestamp": "x" "success": true}',
            "not valid JSON: Expecting ',' delimiter (column 19)",
            id="syntax",
        ),
        pytest.param("[1, 2]", "must be a JSON object, got [...]", id="array"),
        pytest.param('"text"', 'must be a JSON object, got "text"', id="string"),
        pytest.param("42", "must be a JSON object, got 42", id="number"),
        pytest.param("null", "must be a JSON object, got null", id="null"),
        pytest.param(
            VALID.replace("112", "NaN"), "not valid JSON: NaN is not a JSON number", id="NaN"
        ),
        pytest.param(
            VALID.replace("112", "-Infinity"),
            "not valid JSON: -Infinity is not a JSON number",
            id="-Infinity",
        ),
        pytest.param(
            VALID.replace("112", "1e999999999999999999999"),
            "a number is too large",
            id="huge exponent",
        ),
        pytest.param(VALID.replace("112", "1" * 5000), "a number is too large", id="5000 digits"),
        pytest.param("\ufeff" + VALID, BOM_PROBLEM, id="BOM text"),
        pytest.param(b"\xef\xbb\xbf" + VALID.encode(), BOM_PROBLEM, id="BOM bytes"),
    ],
)
def test_a_line_that_is_not_a_json_object(text: bytes | str, expected: str) -> None:
    assert problem(text) == expected


def test_a_line_that_is_not_utf8() -> None:
    assert problem(b'{"timestamp": "caf\xe9"}').startswith("not valid UTF-8: ")


def test_a_duplicate_field_is_rejected_instead_of_keeping_the_last_value() -> None:
    text = (
        '{"timestamp": "2026-10-23T14:00:01Z", '
        '"success": false, "success": true, "latency_ms": 112}'
    )

    assert problem(text) == 'duplicate field "success"'


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"timestamp": DROP}, "timestamp: required field is missing"),
        ({"timestamp": 1792764001}, f"timestamp: {TIMESTAMP_RULE}, got 1792764001"),
        ({"timestamp": "yesterday"}, f'timestamp: {TIMESTAMP_RULE}, got "yesterday"'),
        (
            {"timestamp": "2026-10-23T14:00:01"},
            f'timestamp: {TIMEZONE_RULE}, got "2026-10-23T14:00:01"',
        ),
        ({"timestamp": "2026-10-23"}, f'timestamp: {TIMEZONE_RULE}, got "2026-10-23"'),
        (
            {"timestamp": "2026-10-23T14:00:01.1234567Z"},
            f'timestamp: {PRECISION_RULE}, got "2026-10-23T14:00:01.1234567Z"',
        ),
        (
            {"timestamp": "2026-10-23T140001.1234567Z"},
            f'timestamp: {PRECISION_RULE}, got "2026-10-23T140001.1234567Z"',
        ),
        (
            {"timestamp": "2026-10-23T14:00:01+02:00:00.1234567"},
            f'timestamp: {PRECISION_RULE}, got "2026-10-23T14:00:01+02:00:00.1234567"',
        ),
        (
            {"timestamp": "0001-01-01T00:30:00+01:00"},
            'timestamp: is out of range once converted to UTC, got "0001-01-01T00:30:00+01:00"',
        ),
        ({"success": DROP}, "success: required field is missing"),
        ({"success": "true"}, 'success: must be true or false, got "true"'),
        ({"success": 1}, "success: must be true or false, got 1"),
        ({"success": None}, "success: must be true or false, got null"),
        ({"latency_ms": DROP}, "latency_ms: required when success is true"),
        ({"latency_ms": None}, "latency_ms: required when success is true"),
        ({"latency_ms": -3}, f"latency_ms: {LATENCY_RULE}, got -3"),
        ({"latency_ms": "100"}, f'latency_ms: {LATENCY_RULE}, got "100"'),
        ({"latency_ms": True}, f"latency_ms: {LATENCY_RULE}, got true"),
        ({"success": False, "latency_ms": -3}, f"latency_ms: {LATENCY_RULE}, got -3"),
        ({"success": "yes", "latency_ms": DROP}, 'success: must be true or false, got "yes"'),
    ],
)
def test_each_field_problem(fields: dict, expected: str) -> None:
    assert problem(line(**fields)) == expected


def test_all_problems_on_one_line_are_one_diagnostic() -> None:
    text = line(timestamp="2026-10-23T14:00:01", success="yes", latency_ms=-3)

    assert problem(text) == (
        f'timestamp: {TIMEZONE_RULE}, got "2026-10-23T14:00:01"; '
        'success: must be true or false, got "yes"; '
        f"latency_ms: {LATENCY_RULE}, got -3"
    )


def test_one_invalid_line_rejects_the_whole_batch() -> None:
    error = rejected(VALID, line(success="yes"), VALID)

    assert error.summary == "invalid records <string> (1 of 3 non-blank lines invalid)"
    assert error.problems == ['line 2: success: must be true or false, got "yes"']


def test_line_numbers_count_blank_lines_but_totals_do_not() -> None:
    error = rejected("", VALID, "   ", line(success="yes"), "\n")

    assert error.summary == "invalid records <string> (1 of 2 non-blank lines invalid)"
    assert error.problems == ['line 4: success: must be true or false, got "yes"']


def test_an_invalid_line_counts_once_however_many_problems_it_has() -> None:
    error = rejected(line(timestamp="x", success="yes", latency_ms=-3), "not json", VALID)

    assert error.summary == "invalid records <string> (2 of 3 non-blank lines invalid)"
    assert [problem.split(":")[0] for problem in error.problems] == ["line 1", "line 2"]


def test_only_the_first_ten_invalid_lines_are_listed_but_all_are_counted() -> None:
    error = rejected(*[line(success=f"bad {n}") for n in range(15)], *[VALID] * 5)

    assert error.summary == (
        "invalid records <string> (15 of 20 non-blank lines invalid, first 10 shown)"
    )
    assert [problem.split(":")[0] for problem in error.problems] == [
        f"line {n}" for n in range(1, 11)
    ]


def test_a_line_that_is_not_utf8_does_not_stop_the_scan() -> None:
    error = rejected(VALID.encode(), b'{"timestamp": "caf\xe9"}', b"not json")

    assert error.summary == "invalid records <string> (2 of 3 non-blank lines invalid)"
    assert error.problems[0].startswith("line 2: not valid UTF-8: ")
    assert error.problems[1] == "line 3: not valid JSON: Expecting value (column 1)"


def test_file_with_windows_line_endings_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    failure = line(success=False, latency_ms=DROP)
    path.write_bytes(f"{VALID}\r\n\r\n{failure}\r\n".encode())

    assert [record.success for record in load_records(path)] == [True, False]


def test_bom_at_the_start_of_a_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    path.write_bytes(b"\xef\xbb\xbf" + VALID.encode() + b"\n")

    with pytest.raises(InputError) as excinfo:
        load_records(path)

    assert excinfo.value.summary == f"invalid records {path} (1 of 1 non-blank lines invalid)"
    assert excinfo.value.problems == [f"line 1: {BOM_PROBLEM}"]


def test_missing_file_cannot_be_read(tmp_path: Path) -> None:
    path = tmp_path / "missing.jsonl"

    with pytest.raises(InputError) as excinfo:
        load_records(path)

    assert str(excinfo.value) == f"cannot read records {path}: No such file or directory"


def test_directory_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(InputError, match=r"cannot read records .*: Is a directory"):
        load_records(tmp_path)
