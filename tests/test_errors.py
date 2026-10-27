"""Tests for input errors and how values are shown in them."""

from decimal import Decimal

import pytest

from slo_eval.errors import InputError, describe


def test_error_lists_each_problem_on_its_own_line() -> None:
    error = InputError("invalid policy p.toml", ["service: x", "window: y"])

    assert str(error) == "invalid policy p.toml:\n  - service: x\n  - window: y"


def test_error_without_problems_is_just_the_summary() -> None:
    error = InputError("cannot read policy p.toml: No such file or directory")

    assert str(error) == "cannot read policy p.toml: No such file or directory"
    assert error.problems == []


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (True, "true"),
        (False, "false"),
        (None, "null"),
        (5, "5"),
        (Decimal("0.999"), "0.999"),
        (Decimal("NaN"), "NaN"),
        ("prod", '"prod"'),
        ("api\n", '"api\\n"'),
        ({}, "{}"),
        ({"name": "latency"}, "{...}"),
        ([], "[]"),
        ([1, 2], "[...]"),
    ],
)
def test_describe_shows_values_readably(value: object, shown: str) -> None:
    assert describe(value) == shown


def test_describe_shortens_long_values() -> None:
    assert describe("x" * 100) == '"' + "x" * 36 + "..."
