"""Read and check the service policy."""

import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, DecimalException
from fractions import Fraction
from os import PathLike
from pathlib import Path
from typing import Any, Literal, TypeVar

from slo_eval.errors import InputError, describe

T = TypeVar("T")

SLOName = Literal["availability", "latency"]
SLO_NAMES: tuple[SLOName, ...] = ("availability", "latency")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DURATION = re.compile(r"([1-9][0-9]*)([smhd])")
_UNIT_SECONDS = {"d": 86_400, "h": 3_600, "m": 60, "s": 1}
_MAX_EXPONENT = 100


@dataclass(frozen=True)
class SLO:
    name: SLOName
    objective: Fraction
    impact: str
    runbook: str
    threshold_ms: int | Decimal | None = None


@dataclass(frozen=True)
class BurnRateRule:
    burn_rate: Fraction
    long_window: timedelta
    short_window: timedelta


@dataclass(frozen=True)
class AlertPolicy:
    critical: BurnRateRule
    warning: BurnRateRule
    budget_warning_remaining: Fraction


@dataclass(frozen=True)
class Policy:
    service: str
    environment: str
    window: timedelta
    min_requests: int
    slos: tuple[SLO, ...]
    alerts: AlertPolicy


def load_policy(path: str | PathLike[str]) -> Policy:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read policy {path}: {exc.strerror or exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(f"invalid policy {path}", [f"not valid UTF-8: {exc}"]) from exc
    return parse_policy(text, source=str(path))


def parse_policy(text: str, source: str = "<string>") -> Policy:
    try:
        data = tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        raise InputError(f"invalid policy {source}", [f"not valid TOML: {exc}"]) from exc
    except (ValueError, DecimalException) as exc:
        raise InputError(
            f"invalid policy {source}", ["not valid TOML: a number is too large"]
        ) from exc
    return validate_policy(data, source)


def validate_policy(data: Mapping[str, Any], source: str = "<string>") -> Policy:
    problems: list[str] = []
    root = _Fields(data, "", problems)
    service = root.required("service", _identifier)
    environment = root.required("environment", _identifier)
    window = root.required("window", parse_duration)
    min_requests = root.required("min_requests", _min_requests)
    slo_items = root.required("slo", _slo_array)
    alerts_table = root.required("alerts", _table)
    root.reject_unexpected()

    slos = _parse_slos(slo_items, problems) if slo_items is not None else ()
    alerts = _parse_alerts(alerts_table, window, problems) if alerts_table is not None else None

    if problems:
        raise InputError(f"invalid policy {source}", problems)
    return Policy(service, environment, window, min_requests, slos, alerts)


def parse_duration(value: object) -> timedelta:
    match = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(
            'must be a whole number above 0 followed by s, m, h or d (e.g. "30d"), '
            f"got {describe(value)}"
        )
    amount, unit = match.groups()
    try:
        return timedelta(seconds=int(amount) * _UNIT_SECONDS[unit])
    except (OverflowError, ValueError):
        raise ValueError(f"is too long, got {describe(value)}") from None


def format_duration(duration: timedelta) -> str:
    seconds = duration // timedelta(seconds=1)
    unit, size = next((unit, size) for unit, size in _UNIT_SECONDS.items() if seconds % size == 0)
    return f"{seconds // size}{unit}"


class _Fields:
    def __init__(self, table: Mapping[str, Any], path: str, problems: list[str]) -> None:
        self._table = table
        self._path = path
        self._problems = problems
        self._expected: list[str] = []
        self.ok = True

    def problem(self, key: str, message: str) -> None:
        where = f"{self._path}.{key}" if self._path else key
        self._problems.append(f"{where}: {message}")
        self.ok = False

    def required(self, key: str, check: Callable[[Any], T]) -> T | None:
        self._expected.append(key)
        if key not in self._table:
            self.problem(key, "required field is missing")
            return None
        try:
            return check(self._table[key])
        except ValueError as exc:
            self.problem(key, str(exc))
            return None

    def skip(self, key: str) -> None:
        self._expected.append(key)

    def reject_unexpected(self) -> None:
        expected = ", ".join(sorted(self._expected))
        for key in self._table:
            if key not in self._expected:
                self.problem(key, f"unexpected field (expected: {expected})")


def _parse_slos(items: list[Any], problems: list[str]) -> tuple[SLO, ...]:
    slos: list[SLO] = []
    first_index: dict[str, int] = {}
    for index, item in enumerate(items):
        path = f"slo[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{path}: must be a table, got {describe(item)}")
            continue
        slo = _parse_slo(item, path, problems)
        if slo is not None:
            slos.append(slo)
        name = item.get("name")
        if name in SLO_NAMES:
            if name in first_index:
                problems.append(
                    f"{path}.name: {describe(name)} is already defined in slo[{first_index[name]}]"
                )
            else:
                first_index[name] = index
    return tuple(slos)


def _parse_slo(table: Mapping[str, Any], path: str, problems: list[str]) -> SLO | None:
    fields = _Fields(table, path, problems)
    name = fields.required("name", _slo_name)
    objective = fields.required("objective", _objective)
    impact = fields.required("impact", _text)
    runbook = fields.required("runbook", _text)
    threshold_ms = None
    if name == "latency":
        threshold_ms = fields.required("threshold_ms", _positive_number)
    elif name is None:
        fields.skip("threshold_ms")
    fields.reject_unexpected()
    if not fields.ok:
        return None
    return SLO(name, objective, impact, runbook, threshold_ms)


def _parse_alerts(
    table: Mapping[str, Any], window: timedelta | None, problems: list[str]
) -> AlertPolicy | None:
    fields = _Fields(table, "alerts", problems)
    critical_table = fields.required("critical", _table)
    warning_table = fields.required("warning", _table)
    budget_warning_remaining = fields.required("budget_warning_remaining", _budget_level)
    fields.reject_unexpected()
    critical = warning = None
    if critical_table is not None:
        critical = _parse_rule(critical_table, "alerts.critical", window, problems)
    if warning_table is not None:
        warning = _parse_rule(warning_table, "alerts.warning", window, problems)
    if not fields.ok or critical is None or warning is None:
        return None
    return AlertPolicy(critical, warning, budget_warning_remaining)


def _parse_rule(
    table: Mapping[str, Any], path: str, window: timedelta | None, problems: list[str]
) -> BurnRateRule | None:
    fields = _Fields(table, path, problems)
    burn_rate = fields.required("burn_rate", _burn_rate)
    long_window = fields.required("long_window", parse_duration)
    short_window = fields.required("short_window", parse_duration)
    if long_window is not None and short_window is not None and short_window >= long_window:
        fields.problem(
            "short_window",
            f"must be shorter than long_window ({format_duration(long_window)}), "
            f"got {format_duration(short_window)}",
        )
    if long_window is not None and window is not None and long_window > window:
        fields.problem(
            "long_window",
            f"must not be longer than window ({format_duration(window)}), "
            f"got {format_duration(long_window)}",
        )
    fields.reject_unexpected()
    if not fields.ok:
        return None
    return BurnRateRule(burn_rate, long_window, short_window)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            "must be ASCII letters, digits, '.', '_' or '-', starting with a letter or digit, "
            f"got {describe(value)}"
        )
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"must be a non-empty string, got {describe(value)}")
    return value


def _number(value: object) -> int | Decimal:
    if isinstance(value, float):
        raise TypeError("binary floats are not accepted; decode numbers with parse_float=Decimal")
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise ValueError(f"must be a number, got {describe(value)}")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"must be a finite number, got {describe(value)}")
    return value


def _positive_number(value: object) -> int | Decimal:
    number = _number(value)
    if number <= 0:
        raise ValueError(f"must be greater than 0, got {describe(value)}")
    return number


def _fraction(number: int | Decimal) -> Fraction:
    if isinstance(number, Decimal) and abs(number.as_tuple().exponent) > _MAX_EXPONENT:
        raise ValueError(f"is too large or too precise to use exactly, got {describe(number)}")
    return Fraction(number)


def _objective(value: object) -> Fraction:
    number = _number(value)
    if not 0 < number < 1:
        raise ValueError(f"must be strictly between 0 and 1, got {describe(value)}")
    return _fraction(number)


def _budget_level(value: object) -> Fraction:
    number = _number(value)
    if not 0 <= number < 1:
        raise ValueError(f"must be at least 0 and less than 1, got {describe(value)}")
    return _fraction(number)


def _burn_rate(value: object) -> Fraction:
    return _fraction(_positive_number(value))


def _min_requests(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"must be a whole number of at least 1, got {describe(value)}")
    return value


def _slo_name(value: object) -> SLOName:
    if value not in SLO_NAMES:
        raise ValueError(f'must be "availability" or "latency", got {describe(value)}')
    return value


def _slo_array(value: object) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"must be one or more [[slo]] tables, got {describe(value)}")
    return value


def _table(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"must be a table, got {describe(value)}")
    return value
