"""Tests for rendering decisions as JSON and text reports."""

import dataclasses
import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from slo_eval import __version__
from slo_eval.decision import Decision, decide
from slo_eval.evaluator import Budget, Counts, Evaluation, SLOResult, WindowResult, evaluate
from slo_eval.policy import load_policy, parse_policy
from slo_eval.records import load_records, parse_records
from slo_eval.report import SCHEMA_VERSION, render_json, render_text

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
POLICY = load_policy(EXAMPLES / "policy.toml")
SAMPLE = load_records(EXAMPLES / "sample.jsonl")
LAST_RECORD = datetime(2026, 10, 23, 14, 0, 57, tzinfo=UTC)
END = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
EXTREME_POLICY = f"""\
service = "edge"
environment = "prod"
window = "30d"
min_requests = 1

[[slo]]
name = "availability"
objective = 0.{"9" * 100}
impact = "Requests fail."
runbook = "https://runbooks.example.com/edge#availability"

[[slo]]
name = "latency"
objective = 1e-100
threshold_ms = 1e100
impact = "Requests are slow."
runbook = "https://runbooks.example.com/edge#latency"

[alerts]
critical = {{ burn_rate = 1e100, long_window = "1h", short_window = "5m" }}
warning = {{ burn_rate = 1e-100, long_window = "6h", short_window = "30m" }}
budget_warning_remaining = 1e-100
"""


def strict_json(text: str) -> dict[str, Any]:
    def reject(constant: str) -> None:
        raise AssertionError(f"non-standard JSON constant {constant}")

    return json.loads(text, parse_constant=reject)


def numbers_in(value: object) -> Iterator[float]:
    if isinstance(value, dict):
        for item in value.values():
            yield from numbers_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from numbers_in(item)
    elif isinstance(value, float):
        yield value


def sample_report(end: datetime = LAST_RECORD) -> dict[str, Any]:
    return strict_json(render_json(decide(evaluate(POLICY, SAMPLE, end))))


def hand_decision(
    *,
    burn_rate: Fraction = Fraction(0),
    remaining: Fraction = Fraction(1, 2),
    sli: Fraction = Fraction(1),
) -> Decision:
    availability = POLICY.slos[0]
    windows = {
        duration: WindowResult(duration, Counts(1000, 990), Fraction(99, 100), burn_rate, None)
        for duration in (5 * MINUTE, 30 * MINUTE, HOUR, 6 * HOUR)
    }
    slo_window = WindowResult(30 * DAY, Counts(1000, 999), sli, Fraction(1), None)
    met = sli >= availability.objective
    result = SLOResult(availability, slo_window, met, Budget(Fraction(1), 1, remaining), windows)
    return decide(Evaluation(POLICY, END, END - 30 * DAY, 0, 1000, 0, (result,), met))


# --- JSON ---------------------------------------------------------------------------------------


def test_json_report_header_and_alert_block() -> None:
    report = sample_report()

    assert report["schema_version"] == SCHEMA_VERSION == 1
    assert report["generator"] == {"name": "slo-eval", "version": __version__}
    assert (report["service"], report["environment"]) == ("checkout-api", "prod")
    assert report["evaluated_at"] == "2026-10-23T14:00:57Z"
    assert report["window"] == {
        "duration": "30d",
        "start": "2026-09-23T14:00:57Z",
        "end": "2026-10-23T14:00:57Z",
    }
    assert report["records"] == {"before_window": 0, "in_window": 12, "after_end": 0}
    assert (report["severity"], report["within_objective"]) == ("critical", False)
    assert report["alert"] == {
        "alerting": True,
        "severity": "critical",
        "dedup_keys": ["checkout-api:prod:availability", "checkout-api:prod:latency"],
    }


def test_json_slo_entries_are_self_contained_and_show_each_window() -> None:
    availability = sample_report()["slos"][0]

    identity = ("service", "environment", "slo", "evaluated_at", "dedup_key", "severity")
    assert [availability[key] for key in identity] == [
        "checkout-api",
        "prod",
        "availability",
        "2026-10-23T14:00:57Z",
        "checkout-api:prod:availability",
        "critical",
    ]
    assert (availability["objective"], availability["met"], availability["sli"]) == (
        0.999,
        False,
        10 / 12,
    )
    assert availability["counts"] == {"valid": 12, "good": 10, "bad": 2}
    assert availability["budget"] == {"allowed_bad": 0.012, "bad": 2, "remaining": -497 / 3}
    assert availability["triggered_rules"] == ["fast_burn", "slow_burn", "low_budget"]
    fast_burn = availability["rules"][0]
    assert [fast_burn[key] for key in ("rule", "outcome", "raises", "threshold")] == [
        "fast_burn",
        "firing",
        "critical",
        14.4,
    ]
    assert [(m["window"], m["value"]) for m in fast_burn["measurements"]] == [
        ("1h", 500 / 3),
        ("5m", 500 / 3),
    ]
    assert availability["impact"].endswith("2 of 12 requests failed in the 1h window.")
    assert availability["diagnostics"] == []


def test_slos_without_alerts_stay_identifiable() -> None:
    report = strict_json(render_json(decide(evaluate(POLICY, [], LAST_RECORD))))

    assert report["severity"] == "unknown"
    assert report["alert"] == {"alerting": False, "severity": None, "dedup_keys": []}
    assert [(slo["slo"], slo["severity"], slo["dedup_key"]) for slo in report["slos"]] == [
        ("availability", "unknown", "checkout-api:prod:availability"),
        ("latency", "unknown", "checkout-api:prod:latency"),
    ]
    assert report["slos"][0]["reason"] == "insufficient_requests: observed 0, required 10"


def test_a_window_without_a_lower_bound_has_a_null_start() -> None:
    policy = dataclasses.replace(POLICY, window=timedelta(days=999_999_999))

    report = strict_json(render_json(decide(evaluate(policy, SAMPLE, LAST_RECORD))))

    assert report["window"]["start"] is None


def test_json_refuses_non_finite_numbers_instead_of_emitting_infinity() -> None:
    latency = dataclasses.replace(POLICY.slos[1], threshold_ms=Decimal("1e999999999"))
    policy = dataclasses.replace(POLICY, slos=(POLICY.slos[0], latency))
    decision = decide(evaluate(policy, SAMPLE, LAST_RECORD))

    with pytest.raises(ValueError, match="not JSON compliant"):
        render_json(decision)


def test_accepted_extreme_values_never_produce_nan_or_infinity() -> None:
    policy = parse_policy(EXTREME_POLICY)
    records = parse_records(
        [
            '{"timestamp": "2026-10-23T12:00:00Z", "success": false}',
            '{"timestamp": "2026-10-23T12:00:00Z", "success": true, "latency_ms": 1e999999999}',
        ]
    )
    decision = decide(evaluate(policy, records, END))

    numbers = list(numbers_in(strict_json(render_json(decision))))

    assert numbers
    assert all(math.isfinite(number) for number in numbers)
    assert render_text(decision)


def test_float_collapse_does_not_change_the_decision_or_mislead_the_text() -> None:
    just_below = Fraction("14.4") - Fraction(1, 10**20)
    decision = hand_decision(burn_rate=just_below)

    fast_burn = strict_json(render_json(decision))["slos"][0]["rules"][0]

    assert float(just_below) == 14.4
    assert fast_burn["outcome"] == "not_firing"
    assert [m["value"] for m in fast_burn["measurements"]] == [14.4, 14.4]
    assert (
        "1h burn ≈ 14.40× (below threshold), 5m burn ≈ 14.40× (below threshold) (threshold 14.4×)"
    ) in render_text(decision)


# --- text ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("burn_rate", "shown"),
    [
        (Fraction("14.4"), "1h burn 14.40×"),
        (Fraction("14.399"), "1h burn 14.399×"),
        (Fraction("14.40001"), "1h burn 14.40001×"),
        (Fraction("14.4") + Fraction(1, 10**9), "1h burn ≈ 14.40× (above threshold)"),
        (Fraction(500, 3), "1h burn 166.67×"),
    ],
)
def test_a_burn_rate_keeps_its_side_of_the_threshold_in_text(
    burn_rate: Fraction, shown: str
) -> None:
    assert shown in render_text(hand_decision(burn_rate=burn_rate))


@pytest.mark.parametrize(
    ("remaining", "shown"),
    [
        (Fraction(1, 4), "30d budget left 25.0%"),
        (Fraction("0.2504"), "30d budget left 25.04%"),
        (Fraction("0.2496"), "30d budget left 24.96%"),
        (Fraction(1, 4) - Fraction(1, 10**12), "30d budget left ≈ 25.0% (below threshold)"),
        (Fraction(-497, 3), "30d budget left -16,566.7%"),
    ],
)
def test_the_remaining_budget_keeps_its_side_of_the_threshold_in_text(
    remaining: Fraction, shown: str
) -> None:
    assert shown in render_text(hand_decision(remaining=remaining))


@pytest.mark.parametrize(
    ("sli", "shown"),
    [
        (Fraction("0.999"), "SLI 99.900% of 1,000 requests (objective 99.9%, met)"),
        (Fraction("0.9989999"), "SLI 99.89999% of 1,000 requests (objective 99.9%, missed)"),
        (
            Fraction("0.999") - Fraction(1, 10**15),
            "SLI ≈ 99.900% (below objective) of 1,000 requests (objective 99.9%, missed)",
        ),
    ],
)
def test_the_sli_keeps_its_side_of_the_objective_in_text(sli: Fraction, shown: str) -> None:
    assert shown in render_text(hand_decision(sli=sli))


def test_text_report_for_the_sample() -> None:
    text = render_text(decide(evaluate(POLICY, SAMPLE, LAST_RECORD)))

    assert text.startswith(
        "checkout-api (prod): CRITICAL at 2026-10-23T14:00:57Z\n"
        "window 30d: 12 records in it, 0 before it, 0 after its end\n"
        "within objective: no\n"
    )
    assert "availability: CRITICAL\n  SLI 83.333% of 12 requests (objective 99.9%, missed)" in text
    assert "1h burn 166.67×, 5m burn 166.67× (threshold 14.4×)" in text
    assert "latency: WARNING\n  SLI 90.000% of 10 successful requests" in text
    assert "  dedup key: checkout-api:prod:latency" in text
