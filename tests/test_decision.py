"""Tests for turning evaluation results into severity tags, diagnostics and alerts."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pytest

from slo_eval.decision import Decision, Outcome, Rule, Severity, SLODecision, decide
from slo_eval.evaluator import (
    Budget,
    Counts,
    Evaluation,
    InsufficientRequests,
    SLOResult,
    WindowResult,
    evaluate,
)
from slo_eval.policy import SLO, AlertPolicy, BurnRateRule, Policy, load_policy
from slo_eval.records import load_records

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
RUNBOOK = "https://runbooks.example.com/checkout-api"
END = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
GAP = object()

AVAILABILITY = SLO(
    "availability", Fraction("0.999"), "Customers see errors.", f"{RUNBOOK}#availability"
)
LATENCY = SLO("latency", Fraction("0.99"), "Checkout is slow.", f"{RUNBOOK}#latency", 300)
POLICY = Policy(
    service="checkout-api",
    environment="prod",
    window=30 * DAY,
    min_requests=10,
    slos=(AVAILABILITY, LATENCY),
    alerts=AlertPolicy(
        critical=BurnRateRule(Fraction("14.4"), HOUR, 5 * MINUTE),
        warning=BurnRateRule(Fraction(6), 6 * HOUR, 30 * MINUTE),
        budget_warning_remaining=Fraction(1, 4),
    ),
)
CHECK_TELEMETRY = "Check whether requests are arriving and telemetry is flowing."
GAP_REASON = "insufficient_requests: observed 4, required 10"


def window(duration: timedelta, burn_rate: object) -> WindowResult:
    if burn_rate is GAP:
        return WindowResult(duration, Counts(4, 4), None, None, InsufficientRequests(4, 10))
    return WindowResult(duration, Counts(1000, 990), Fraction(99, 100), Fraction(burn_rate), None)


def slo_result(
    slo: SLO = AVAILABILITY,
    *,
    fast: tuple[object, object] = (0, 0),
    slow: tuple[object, object] = (0, 0),
    budget: object = Fraction(1, 2),
) -> SLOResult:
    alert_windows = {
        5 * MINUTE: window(5 * MINUTE, fast[1]),
        30 * MINUTE: window(30 * MINUTE, slow[1]),
        HOUR: window(HOUR, fast[0]),
        6 * HOUR: window(6 * HOUR, slow[0]),
    }
    if budget is GAP:
        return SLOResult(slo, window(30 * DAY, GAP), None, None, alert_windows)
    remaining = Fraction(budget)
    slo_window = window(30 * DAY, 0)
    return SLOResult(
        slo, slo_window, remaining >= 0, Budget(Fraction(1), 0, remaining), alert_windows
    )


def evaluation(*results: SLOResult) -> Evaluation:
    return Evaluation(POLICY, END, END - 30 * DAY, 0, 0, 0, results, None)


def one(**changes: object) -> SLODecision:
    [decision] = decide(evaluation(slo_result(**changes))).slos
    return decision


def sample_decision(end: datetime) -> Decision:
    policy = load_policy(EXAMPLES / "policy.toml")
    return decide(evaluate(policy, load_records(EXAMPLES / "sample.jsonl"), end))


# --- tags ---------------------------------------------------------------------------------------


def test_fast_burn_over_both_windows_is_critical() -> None:
    decision = one(fast=(20, 18))

    assert (decision.severity, decision.triggered_rules) == (Severity.CRITICAL, (Rule.FAST_BURN,))


def test_slow_burn_over_both_windows_is_a_warning() -> None:
    decision = one(slow=(7, 8))

    assert (decision.severity, decision.triggered_rules) == (Severity.WARNING, (Rule.SLOW_BURN,))


def test_low_budget_is_a_warning() -> None:
    decision = one(budget=Fraction(1, 10))

    assert (decision.severity, decision.triggered_rules) == (Severity.WARNING, (Rule.LOW_BUDGET,))


def test_a_rule_that_cannot_be_evaluated_makes_the_slo_unknown() -> None:
    decision = one(fast=(20, GAP))

    assert decision.severity is Severity.UNKNOWN
    assert decision.rules[0].outcome is Outcome.CANNOT_EVALUATE
    assert decision.triggered_rules == ()


def test_all_rules_clear_is_ok() -> None:
    decision = one()

    assert decision.severity is Severity.OK
    assert [rule.outcome for rule in decision.rules] == [Outcome.NOT_FIRING] * 3
    assert decision.diagnostics == ()


# --- thresholds ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("burn_rate", "outcome"),
    [("14.4", Outcome.FIRING), ("14.39", Outcome.NOT_FIRING), (100, Outcome.FIRING)],
)
def test_a_burn_rate_fires_at_or_above_its_threshold(burn_rate: object, outcome: Outcome) -> None:
    assert one(fast=(burn_rate, burn_rate)).rules[0].outcome is outcome


@pytest.mark.parametrize(
    ("remaining", "outcome"),
    [
        ("0.25", Outcome.FIRING),
        ("0.2499", Outcome.FIRING),
        ("0.2501", Outcome.NOT_FIRING),
        (-9, Outcome.FIRING),
    ],
)
def test_low_budget_fires_at_or_below_its_level(remaining: object, outcome: Outcome) -> None:
    assert one(budget=Fraction(remaining)).rules[2].outcome is outcome


@pytest.mark.parametrize(
    "fast", [(1, 100), (100, 1)], ids=["short window only", "long window only"]
)
def test_a_burn_in_only_one_window_does_not_fire(fast: tuple[object, object]) -> None:
    assert one(fast=fast).rules[0].outcome is Outcome.NOT_FIRING


# --- missing windows ----------------------------------------------------------------------------


def test_a_window_below_threshold_disproves_the_rule_while_the_gap_is_still_reported() -> None:
    decision = one(fast=("0.3", GAP))

    assert decision.rules[0].outcome is Outcome.NOT_FIRING
    assert decision.severity is Severity.OK
    assert decision.diagnostics == (f"fast_burn 5m window: {GAP_REASON}",)


def test_a_window_at_threshold_with_the_other_lacking_data_cannot_be_evaluated() -> None:
    decision = one(fast=(20, GAP))

    assert decision.rules[0].outcome is Outcome.CANNOT_EVALUATE
    assert decision.diagnostics == (f"fast_burn 5m window: {GAP_REASON}",)


def test_stale_data_gives_a_warning_that_keeps_every_gap() -> None:
    decision = one(fast=(GAP, GAP), slow=(GAP, GAP), budget=-9)

    assert (decision.severity, decision.triggered_rules) == (Severity.WARNING, (Rule.LOW_BUDGET,))
    assert decision.diagnostics == (
        f"fast_burn 1h window: {GAP_REASON}",
        f"fast_burn 5m window: {GAP_REASON}",
        f"slow_burn 6h window: {GAP_REASON}",
        f"slow_burn 30m window: {GAP_REASON}",
    )


def test_no_data_at_all_is_unknown_and_reports_every_gap() -> None:
    decision = one(fast=(GAP, GAP), slow=(GAP, GAP), budget=GAP)

    assert decision.severity is Severity.UNKNOWN
    assert decision.diagnostics[-1] == f"low_budget 30d window: {GAP_REASON}"
    assert len(decision.diagnostics) == 5
    assert decision.next_action == CHECK_TELEMETRY


# --- precedence ---------------------------------------------------------------------------------


def test_fast_burn_outranks_low_budget_and_every_triggered_rule_is_kept() -> None:
    decision = one(fast=(20, 20), budget=-1)

    assert decision.severity is Severity.CRITICAL
    assert decision.triggered_rules == (Rule.FAST_BURN, Rule.LOW_BUDGET)


def test_an_overspent_budget_alone_never_pages() -> None:
    assert one(budget=-100).severity is Severity.WARNING


@pytest.mark.parametrize(
    ("availability", "latency", "overall"),
    [
        ({"fast": (20, 20)}, {"slow": (7, 7)}, Severity.CRITICAL),
        ({"slow": (7, 7)}, {"fast": (20, GAP)}, Severity.WARNING),
        ({"fast": (20, GAP)}, {}, Severity.UNKNOWN),
        ({}, {}, Severity.OK),
    ],
)
def test_the_overall_severity_is_the_worst_slo(
    availability: dict, latency: dict, overall: Severity
) -> None:
    results = slo_result(AVAILABILITY, **availability), slo_result(LATENCY, **latency)

    assert decide(evaluation(*results)).severity is overall


# --- alerts -------------------------------------------------------------------------------------


def test_only_warning_and_critical_are_alerts_but_every_slo_stays_identifiable() -> None:
    results = slo_result(AVAILABILITY, fast=(20, 20)), slo_result(LATENCY, fast=(20, GAP))

    decision = decide(evaluation(*results))
    _, latency = decision.slos

    assert [alert.slo for alert in decision.alerts] == ["availability"]
    assert latency.severity is Severity.UNKNOWN
    assert latency.dedup_key == "checkout-api:prod:latency"
    assert latency.summary == "checkout-api (prod) latency: cannot be evaluated"
    assert (latency.impact, latency.next_action) == (None, CHECK_TELEMETRY)


def test_an_ok_slo_has_no_impact_or_next_action() -> None:
    decision = one()

    assert decision.summary == "checkout-api (prod) availability: no alert condition"
    assert (decision.impact, decision.next_action) == (None, None)


def test_an_alert_carries_structured_context_with_exact_measurements() -> None:
    decision = one(fast=(20, 18))

    assert (decision.service, decision.environment, decision.slo) == (
        "checkout-api",
        "prod",
        "availability",
    )
    assert decision.evaluated_at == END
    assert decision.dedup_key == "checkout-api:prod:availability"
    assert decision.summary == "checkout-api (prod) availability: fast burn over 1h and 5m"
    assert decision.impact == "Customers see errors. 10 of 1,000 requests failed in the 1h window."
    assert decision.runbook == f"{RUNBOOK}#availability"
    assert decision.next_action == (
        "Investigate immediately: elevated budget consumption was observed in the recent "
        "evaluation windows. Follow the runbook."
    )
    fast_burn = decision.rules[0]
    assert fast_burn.threshold == Fraction("14.4")
    assert [(m.window, m.value) for m in fast_burn.measurements] == [(HOUR, 20), (5 * MINUTE, 18)]


def test_latency_impact_counts_slow_successful_requests() -> None:
    [decision] = decide(evaluation(slo_result(LATENCY, slow=(7, 8)))).slos

    assert decision.impact == (
        "Checkout is slow. 10 of 1,000 successful requests exceeded 300 ms in the 6h window."
    )


def test_the_primary_rule_follows_a_fixed_order() -> None:
    decision = one(slow=(7, 8), budget=-1)

    assert decision.triggered_rules == (Rule.SLOW_BURN, Rule.LOW_BUDGET)
    assert decision.summary == "checkout-api (prod) availability: slow burn over 6h and 30m"
    assert decision.impact == "Customers see errors. 10 of 1,000 requests failed in the 6h window."
    assert decision.next_action == (
        "Investigate soon: sustained elevated budget consumption was observed in the evaluation "
        "windows. Follow the runbook."
    )


def test_low_budget_alone_uses_the_slo_window_and_defers_to_the_team_policy() -> None:
    decision = one(budget=-1)

    assert decision.summary == "checkout-api (prod) availability: low error budget over 30d"
    assert decision.impact == "Customers see errors. 10 of 1,000 requests failed in the 30d window."
    assert decision.next_action == (
        "Review recent incidents and budget consumption; follow the team's release/error-budget "
        "policy."
    )


def test_the_dedup_key_stays_the_same_when_severity_changes() -> None:
    warning = one(slow=(7, 7))
    critical = one(fast=(20, 20), slow=(7, 7))

    assert (warning.severity, critical.severity) == (Severity.WARNING, Severity.CRITICAL)
    assert warning.dedup_key == critical.dedup_key == "checkout-api:prod:availability"


# --- example files ------------------------------------------------------------------------------


def test_stale_sample_warns_on_both_budgets_and_explains_every_gap() -> None:
    decision = sample_decision(datetime(2026, 10, 25, 12, 0, tzinfo=UTC))

    assert decision.severity is Severity.WARNING
    assert [(slo.slo, slo.severity, slo.triggered_rules) for slo in decision.slos] == [
        ("availability", Severity.WARNING, (Rule.LOW_BUDGET,)),
        ("latency", Severity.WARNING, (Rule.LOW_BUDGET,)),
    ]
    for slo in decision.slos:
        assert len(slo.diagnostics) == 4
        assert all(
            diagnostic.endswith("insufficient_requests: observed 0, required 10")
            for diagnostic in slo.diagnostics
        )


def test_sample_at_its_last_record_is_critical_for_availability_and_warning_for_latency() -> None:
    decision = sample_decision(datetime(2026, 10, 23, 14, 0, 57, tzinfo=UTC))
    availability, latency = decision.slos

    assert decision.severity is Severity.CRITICAL
    assert availability.triggered_rules == (Rule.FAST_BURN, Rule.SLOW_BURN, Rule.LOW_BUDGET)
    assert availability.impact.endswith("2 of 12 requests failed in the 1h window.")
    assert latency.severity is Severity.WARNING
    assert latency.triggered_rules == (Rule.SLOW_BURN, Rule.LOW_BUDGET)
    assert latency.impact.endswith("1 of 10 successful requests exceeded 300 ms in the 6h window.")
