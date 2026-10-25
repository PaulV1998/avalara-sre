"""Tests for reading and checking the service policy."""

import copy
import tomllib
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from slo_eval.errors import InputError
from slo_eval.policy import (
    SLO,
    AlertPolicy,
    BurnRateRule,
    Policy,
    format_duration,
    load_policy,
    parse_duration,
    parse_policy,
    validate_policy,
)

EXAMPLE_POLICY = Path(__file__).resolve().parents[1] / "examples" / "policy.toml"
RUNBOOK = "https://runbooks.example.com/checkout-api"
VALID_POLICY = f"""\
service = "checkout-api"
environment = "prod"
window = "30d"
min_requests = 10

[[slo]]
name = "availability"
objective = 0.999
impact = "Customers see errors."
runbook = "{RUNBOOK}#availability"

[[slo]]
name = "latency"
objective = 0.99
threshold_ms = 300
impact = "Checkout is slow."
runbook = "{RUNBOOK}#latency"

[alerts]
budget_warning_remaining = 0.25

[alerts.critical]
burn_rate = 14.4
long_window = "1h"
short_window = "5m"

[alerts.warning]
burn_rate = 6
long_window = "6h"
short_window = "30m"
"""

NAME_RULE = "must be ASCII letters, digits, '.', '_' or '-', starting with a letter or digit"
EXACT_RULE = "is too large or too precise to use exactly"
DURATION_RULE = 'must be a whole number above 0 followed by s, m, h or d (e.g. "30d")'
BOM_PROBLEM = "starts with a byte order mark (BOM); the format is UTF-8 without a BOM"


def edit(*changes: tuple[str, str]) -> str:
    lines = VALID_POLICY.splitlines()
    for old, new in changes:
        matches = [index for index, line in enumerate(lines) if line == old]
        assert len(matches) == 1, f"{old!r} must match exactly one line of VALID_POLICY"
        lines[matches[0]] = new
    return "\n".join(lines) + "\n"


def problems_in(text: str) -> list[str]:
    with pytest.raises(InputError) as excinfo:
        parse_policy(text)
    return excinfo.value.problems


def decoded() -> dict[str, Any]:
    return tomllib.loads(VALID_POLICY, parse_float=Decimal)


def validation_problems(data: dict[str, Any]) -> list[str]:
    with pytest.raises(InputError) as excinfo:
        validate_policy(data)
    return excinfo.value.problems


# --- valid policies -----------------------------------------------------------------------------


def test_valid_policy_is_parsed() -> None:
    assert parse_policy(VALID_POLICY) == Policy(
        service="checkout-api",
        environment="prod",
        window=timedelta(days=30),
        min_requests=10,
        slos=(
            SLO(
                "availability",
                Fraction("0.999"),
                "Customers see errors.",
                f"{RUNBOOK}#availability",
            ),
            SLO("latency", Fraction("0.99"), "Checkout is slow.", f"{RUNBOOK}#latency", 300),
        ),
        alerts=AlertPolicy(
            critical=BurnRateRule(Fraction("14.4"), timedelta(hours=1), timedelta(minutes=5)),
            warning=BurnRateRule(Fraction(6), timedelta(hours=6), timedelta(minutes=30)),
            budget_warning_remaining=Fraction("0.25"),
        ),
    )


def test_numbers_are_exact_and_never_binary_floats() -> None:
    policy = parse_policy(edit(("threshold_ms = 300", "threshold_ms = 250.5")))
    availability, latency = policy.slos

    assert type(availability.objective) is Fraction
    assert type(policy.alerts.critical.burn_rate) is Fraction
    assert type(policy.alerts.budget_warning_remaining) is Fraction
    assert type(latency.threshold_ms) is Decimal
    assert latency.threshold_ms == Decimal("250.5")
    assert type(parse_policy(VALID_POLICY).slos[1].threshold_ms) is int
    assert type(policy.min_requests) is int


def test_example_policy_loads_and_matches_the_baseline() -> None:
    example = load_policy(EXAMPLE_POLICY)
    baseline = parse_policy(VALID_POLICY)

    assert (example.service, example.environment, example.window, example.min_requests) == (
        baseline.service,
        baseline.environment,
        baseline.window,
        baseline.min_requests,
    )
    assert [(slo.name, slo.objective, slo.threshold_ms) for slo in example.slos] == [
        (slo.name, slo.objective, slo.threshold_ms) for slo in baseline.slos
    ]
    assert example.alerts == baseline.alerts


def test_a_policy_may_define_a_single_slo() -> None:
    data = decoded()
    del data["slo"][1]

    assert [slo.name for slo in validate_policy(data).slos] == ["availability"]


def test_budget_warning_level_zero_is_allowed() -> None:
    text = edit(("budget_warning_remaining = 0.25", "budget_warning_remaining = 0"))

    assert parse_policy(text).alerts.budget_warning_remaining == 0


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('long_window = "6h"', 'long_window = "30d"'),
        ('short_window = "5m"', 'short_window = "59m"'),
        ("objective = 0.999", "objective = 0.99999"),
        ("min_requests = 10", "min_requests = 1"),
        ("threshold_ms = 300", "threshold_ms = 0.5"),
        ("objective = 0.999", "objective = 1e-100"),
        ("burn_rate = 14.4", "burn_rate = 1e100"),
    ],
)
def test_boundary_values_that_are_allowed(old: str, new: str) -> None:
    parse_policy(edit((old, new)))


# --- durations ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "duration"),
    [
        ("30s", timedelta(seconds=30)),
        ("5m", timedelta(minutes=5)),
        ("90m", timedelta(minutes=90)),
        ("6h", timedelta(hours=6)),
        ("30d", timedelta(days=30)),
    ],
)
def test_parse_duration_reads_a_whole_number_and_one_unit(text: str, duration: timedelta) -> None:
    assert parse_duration(text) == duration


@pytest.mark.parametrize(
    "value",
    [
        "",
        "30",
        "d",
        "0m",
        "05m",
        "-5m",
        "+5m",
        "1.5h",
        "1h30m",
        "5M",
        "5 m",
        " 5m",
        "5m\n",
        "\u0663\u0660d",
        5,
        None,
    ],
)
def test_parse_duration_rejects_other_formats(value: object) -> None:
    with pytest.raises(ValueError, match="must be a whole number above 0"):
        parse_duration(value)


@pytest.mark.parametrize("text", ["1000000000d", "9" * 5000 + "s"], ids=["days", "5000 digits"])
def test_parse_duration_rejects_durations_too_long_to_represent(text: str) -> None:
    with pytest.raises(ValueError, match="is too long"):
        parse_duration(text)


@pytest.mark.parametrize(
    ("duration", "text"),
    [
        (timedelta(days=30), "30d"),
        (timedelta(hours=36), "36h"),
        (timedelta(minutes=90), "90m"),
        (timedelta(seconds=45), "45s"),
    ],
)
def test_format_duration_uses_the_largest_exact_unit(duration: timedelta, text: str) -> None:
    assert format_duration(duration) == text
    assert parse_duration(text) == duration


# --- one problem at a time ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "path"),
    [
        ('service = "checkout-api"', "service"),
        ('environment = "prod"', "environment"),
        ('window = "30d"', "window"),
        ("min_requests = 10", "min_requests"),
        ('name = "availability"', "slo[0].name"),
        ("objective = 0.999", "slo[0].objective"),
        ('impact = "Customers see errors."', "slo[0].impact"),
        (f'runbook = "{RUNBOOK}#availability"', "slo[0].runbook"),
        ("threshold_ms = 300", "slo[1].threshold_ms"),
        ("budget_warning_remaining = 0.25", "alerts.budget_warning_remaining"),
        ("burn_rate = 14.4", "alerts.critical.burn_rate"),
        ('long_window = "1h"', "alerts.critical.long_window"),
        ('short_window = "30m"', "alerts.warning.short_window"),
    ],
)
def test_each_missing_field_is_one_problem(line: str, path: str) -> None:
    assert problems_in(edit((line, ""))) == [f"{path}: required field is missing"]


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (
            'service = "checkout-api"',
            'service = "checkout:api"',
            f'service: {NAME_RULE}, got "checkout:api"',
        ),
        (
            'service = "checkout-api"',
            'service = "checkout api"',
            f'service: {NAME_RULE}, got "checkout api"',
        ),
        (
            'service = "checkout-api"',
            'service = "-checkout"',
            f'service: {NAME_RULE}, got "-checkout"',
        ),
        ('service = "checkout-api"', 'service = ".."', f'service: {NAME_RULE}, got ".."'),
        ('service = "checkout-api"', 'service = ""', f'service: {NAME_RULE}, got ""'),
        ('service = "checkout-api"', 'service = "api\\n"', f'service: {NAME_RULE}, got "api\\n"'),
        (
            'environment = "prod"',
            'environment = "pr\u00f8d"',
            f'environment: {NAME_RULE}, got "pr\u00f8d"',
        ),
        ('environment = "prod"', "environment = 5", f"environment: {NAME_RULE}, got 5"),
        ('window = "30d"', 'window = "30"', f'window: {DURATION_RULE}, got "30"'),
        ('window = "30d"', "window = 30", f"window: {DURATION_RULE}, got 30"),
        ('window = "30d"', 'window = "1000000000d"', 'window: is too long, got "1000000000d"'),
        (
            "min_requests = 10",
            "min_requests = 0",
            "min_requests: must be a whole number of at least 1, got 0",
        ),
        (
            "min_requests = 10",
            "min_requests = 10.0",
            "min_requests: must be a whole number of at least 1, got 10.0",
        ),
        (
            "min_requests = 10",
            "min_requests = true",
            "min_requests: must be a whole number of at least 1, got true",
        ),
        (
            'name = "latency"',
            'name = "throughput"',
            'slo[1].name: must be "availability" or "latency", got "throughput"',
        ),
        (
            "objective = 0.999",
            "objective = 1",
            "slo[0].objective: must be strictly between 0 and 1, got 1",
        ),
        (
            "objective = 0.999",
            "objective = 0",
            "slo[0].objective: must be strictly between 0 and 1, got 0",
        ),
        (
            "objective = 0.999",
            "objective = -0.5",
            "slo[0].objective: must be strictly between 0 and 1, got -0.5",
        ),
        (
            "objective = 0.999",
            'objective = "0.999"',
            'slo[0].objective: must be a number, got "0.999"',
        ),
        ("objective = 0.999", "objective = true", "slo[0].objective: must be a number, got true"),
        (
            "objective = 0.999",
            "objective = nan",
            "slo[0].objective: must be a finite number, got NaN",
        ),
        (
            "objective = 0.999",
            "objective = inf",
            "slo[0].objective: must be a finite number, got Infinity",
        ),
        (
            'impact = "Customers see errors."',
            'impact = "  "',
            'slo[0].impact: must be a non-empty string, got "  "',
        ),
        (
            f'runbook = "{RUNBOOK}#availability"',
            "runbook = 42",
            "slo[0].runbook: must be a non-empty string, got 42",
        ),
        (
            "threshold_ms = 300",
            "threshold_ms = 0",
            "slo[1].threshold_ms: must be greater than 0, got 0",
        ),
        (
            "threshold_ms = 300",
            'threshold_ms = "300"',
            'slo[1].threshold_ms: must be a number, got "300"',
        ),
        (
            "budget_warning_remaining = 0.25",
            "budget_warning_remaining = 1",
            "alerts.budget_warning_remaining: must be at least 0 and less than 1, got 1",
        ),
        (
            "budget_warning_remaining = 0.25",
            "budget_warning_remaining = -0.1",
            "alerts.budget_warning_remaining: must be at least 0 and less than 1, got -0.1",
        ),
        (
            "burn_rate = 14.4",
            "burn_rate = 0",
            "alerts.critical.burn_rate: must be greater than 0, got 0",
        ),
        (
            "burn_rate = 6",
            'burn_rate = "fast"',
            'alerts.warning.burn_rate: must be a number, got "fast"',
        ),
        (
            'short_window = "5m"',
            'short_window = "1h"',
            "alerts.critical.short_window: must be shorter than long_window (1h), got 1h",
        ),
        (
            'long_window = "6h"',
            'long_window = "31d"',
            "alerts.warning.long_window: must not be longer than window (30d), got 31d",
        ),
        (
            'window = "30d"',
            'window = "1h"',
            "alerts.warning.long_window: must not be longer than window (1h), got 6h",
        ),
        ('window = "30d"', 'window = "30x"', f'window: {DURATION_RULE}, got "30x"'),
        (
            'long_window = "1h"',
            'long_window = "1x"',
            f'alerts.critical.long_window: {DURATION_RULE}, got "1x"',
        ),
        (
            "objective = 0.999",
            "objective = 1e-101",
            f"slo[0].objective: {EXACT_RULE}, got 1E-101",
        ),
        (
            "burn_rate = 14.4",
            "burn_rate = 1e101",
            f"alerts.critical.burn_rate: {EXACT_RULE}, got 1E+101",
        ),
        (
            "budget_warning_remaining = 0.25",
            "budget_warning_remaining = 1e-101",
            f"alerts.budget_warning_remaining: {EXACT_RULE}, got 1E-101",
        ),
    ],
)
def test_each_invalid_value_is_one_problem(old: str, new: str, problem: str) -> None:
    assert problems_in(edit((old, new))) == [problem]


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (
            'window = "30d"',
            'window = "30d"\nowner = "team-a"',
            "owner: unexpected field "
            "(expected: alerts, environment, min_requests, service, slo, window)",
        ),
        (
            "objective = 0.999",
            "objective = 0.999\nthreshold_ms = 300",
            "slo[0].threshold_ms: unexpected field (expected: impact, name, objective, runbook)",
        ),
        (
            "budget_warning_remaining = 0.25",
            "budget_warning_remaining = 0.25\npage = true",
            "alerts.page: unexpected field (expected: budget_warning_remaining, critical, warning)",
        ),
        (
            "burn_rate = 14.4",
            'burn_rate = 14.4\nfor = "5m"',
            "alerts.critical.for: unexpected field "
            "(expected: burn_rate, long_window, short_window)",
        ),
    ],
)
def test_unexpected_fields_are_named_with_the_expected_ones(
    old: str, new: str, problem: str
) -> None:
    assert problems_in(edit((old, new))) == [problem]


def test_a_misspelled_field_reports_both_sides_of_the_typo() -> None:
    assert problems_in(edit(("objective = 0.999", "objectve = 0.999"))) == [
        "slo[0].objective: required field is missing",
        "slo[0].objectve: unexpected field (expected: impact, name, objective, runbook)",
    ]


@pytest.mark.parametrize(
    ("path", "value", "problem"),
    [
        (("alerts",), 5, "alerts: must be a table, got 5"),
        (("alerts", "critical"), "fast", 'alerts.critical: must be a table, got "fast"'),
        (("slo",), [], "slo: must be one or more [[slo]] tables, got []"),
        (("slo",), {"name": "latency"}, "slo: must be one or more [[slo]] tables, got {...}"),
        (("slo", 0), 5, "slo[0]: must be a table, got 5"),
    ],
)
def test_a_wrong_parent_is_one_problem_not_a_cascade(
    path: tuple[str | int, ...], value: object, problem: str
) -> None:
    data = decoded()
    parent: Any = data
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value

    assert validation_problems(data) == [problem]


def test_a_duplicate_slo_name_is_reported_against_the_first_definition() -> None:
    data = decoded()
    data["slo"].append(copy.deepcopy(data["slo"][0]))

    assert validation_problems(data) == ['slo[2].name: "availability" is already defined in slo[0]']


# --- several problems ---------------------------------------------------------------------------


def test_independent_problems_are_all_reported_in_a_stable_order() -> None:
    text = edit(
        ('service = "checkout-api"', 'service = "checkout api"'),
        ("objective = 0.99", "objective = 99"),
        ('short_window = "30m"', 'short_window = "6h"'),
    )

    assert problems_in(text) == [
        f'service: {NAME_RULE}, got "checkout api"',
        "slo[1].objective: must be strictly between 0 and 1, got 99",
        "alerts.warning.short_window: must be shorter than long_window (6h), got 6h",
    ]


def test_binary_floats_are_refused_as_a_programming_error() -> None:
    data = decoded()
    data["slo"][0]["objective"] = 0.999

    with pytest.raises(TypeError, match="parse_float=Decimal"):
        validate_policy(data)


# --- files and syntax ---------------------------------------------------------------------------


def test_missing_file_cannot_be_read(tmp_path: Path) -> None:
    path = tmp_path / "missing.toml"

    with pytest.raises(InputError) as excinfo:
        load_policy(path)

    assert str(excinfo.value) == f"cannot read policy {path}: No such file or directory"


def test_directory_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(InputError, match=r"cannot read policy .*: Is a directory"):
        load_policy(tmp_path)


def test_file_that_is_not_utf8_is_one_problem(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_bytes(b'service = "caf\xe9"\n')

    with pytest.raises(InputError) as excinfo:
        load_policy(path)

    assert excinfo.value.summary == f"invalid policy {path}"
    [problem] = excinfo.value.problems
    assert problem.startswith("not valid UTF-8: ")


def test_toml_syntax_errors_report_only_the_parsers_first_error(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    text = edit(('window = "30d"', "window = 30d"), ("min_requests = 10", "min_requests = ten"))
    path.write_text(text, encoding="utf-8")

    with pytest.raises(InputError) as excinfo:
        load_policy(path)

    assert excinfo.value.summary == f"invalid policy {path}"
    [problem] = excinfo.value.problems
    assert problem.startswith("not valid TOML: ")
    assert "line 3" in problem


@pytest.mark.parametrize(
    "number", ["1e999999999999999999999", "1" * 5000], ids=["huge exponent", "5000 digits"]
)
def test_a_number_too_large_to_decode_is_one_problem(number: str) -> None:
    text = edit(("min_requests = 10", f"min_requests = {number}"))

    assert problems_in(text) == ["not valid TOML: a number is too large"]


def test_a_byte_order_mark_is_rejected() -> None:
    assert problems_in("\ufeff" + VALID_POLICY) == [BOM_PROBLEM]


def test_a_file_starting_with_a_byte_order_mark_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_bytes(b"\xef\xbb\xbf" + VALID_POLICY.encode())

    with pytest.raises(InputError) as excinfo:
        load_policy(path)

    assert excinfo.value.summary == f"invalid policy {path}"
    assert excinfo.value.problems == [BOM_PROBLEM]
