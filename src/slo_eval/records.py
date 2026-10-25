"""Read and check the request records."""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from os import PathLike
from typing import Any, NoReturn, TypeVar

from slo_eval.errors import BOM_PROBLEM, InputError, describe

T = TypeVar("T")

_MAX_REPORTED_LINES = 10
_FRACTION = re.compile(r"[.,]([0-9]+)")


@dataclass(frozen=True, slots=True)
class Record:
    timestamp: datetime
    success: bool
    latency_ms: int | Decimal | None


class _InvalidLine(Exception):
    pass


def load_records(path: str | PathLike[str]) -> list[Record]:
    try:
        with open(path, "rb") as file:
            return parse_records(file, source=str(path))
    except OSError as exc:
        raise InputError(f"cannot read records {path}: {exc.strerror or exc}") from exc


def parse_records(lines: Iterable[bytes | str], source: str = "<string>") -> list[Record]:
    records: list[Record] = []
    reported: list[str] = []
    non_blank = invalid = 0
    for number, line in enumerate(lines, start=1):
        if _is_blank(line):
            continue
        non_blank += 1
        try:
            records.append(_parse_line(line))
        except _InvalidLine as exc:
            invalid += 1
            if len(reported) < _MAX_REPORTED_LINES:
                reported.append(f"line {number}: {exc}")
    if invalid:
        counts = f"{invalid:,} of {non_blank:,} non-blank lines invalid"
        if invalid > len(reported):
            counts += f", first {len(reported)} shown"
        raise InputError(f"invalid records {source} ({counts})", reported)
    return records


def _is_blank(line: bytes | str) -> bool:
    if isinstance(line, bytes):
        return not line.strip(b" \t\r\n")
    return not line.strip(" \t\r\n")


def _parse_line(line: bytes | str) -> Record:
    try:
        text = line.decode("utf-8") if isinstance(line, bytes) else line
    except UnicodeDecodeError as exc:
        raise _InvalidLine(f"not valid UTF-8: {exc}") from None
    if text.startswith("\ufeff"):
        raise _InvalidLine(BOM_PROBLEM)
    try:
        data = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise _InvalidLine(f"not valid JSON: {exc.msg} (column {exc.colno})") from None
    except (ValueError, DecimalException):
        raise _InvalidLine("a number is too large") from None
    if not isinstance(data, dict):
        raise _InvalidLine(f"must be a JSON object, got {describe(data)}")
    return _check_fields(data)


def _reject_constant(name: str) -> NoReturn:
    raise _InvalidLine(f"not valid JSON: {name} is not a JSON number")


def _check_fields(data: Mapping[str, Any]) -> Record:
    problems: list[str] = []
    timestamp = _field(data, "timestamp", _timestamp, problems)
    success = _field(data, "success", _success, problems)
    latency_ms = data.get("latency_ms")
    if latency_ms is not None:
        try:
            latency_ms = _latency(latency_ms)
        except ValueError as exc:
            problems.append(f"latency_ms: {exc}")
    elif success is True:
        problems.append("latency_ms: required when success is true")
    if problems:
        raise _InvalidLine("; ".join(problems))
    return Record(timestamp, success, latency_ms)


def _field(
    data: Mapping[str, Any], key: str, check: Callable[[Any], T], problems: list[str]
) -> T | None:
    if key not in data:
        problems.append(f"{key}: required field is missing")
        return None
    try:
        return check(data[key])
    except ValueError as exc:
        problems.append(f"{key}: {exc}")
        return None


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"must be an ISO 8601 timestamp, got {describe(value)}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"must be an ISO 8601 timestamp, got {describe(value)}") from None
    if any(len(digits) > 6 for digits in _FRACTION.findall(value)):
        raise ValueError(f"must have at most 6 fractional-second digits, got {describe(value)}")
    if parsed.tzinfo is None:
        raise ValueError(
            f"must include a timezone (Z or an offset such as +02:00), got {describe(value)}"
        )
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        raise ValueError(f"is out of range once converted to UTC, got {describe(value)}") from None


def _success(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"must be true or false, got {describe(value)}")
    return value


def _latency(value: object) -> int | Decimal:
    if isinstance(value, bool) or not isinstance(value, int | Decimal) or value < 0:
        raise ValueError(f"must be a number of at least 0, got {describe(value)}")
    return value
