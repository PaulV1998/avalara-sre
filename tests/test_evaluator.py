"""Tests for calculating SLIs, error budgets and burn rates."""

import random
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from slo_eval.evaluator import Budget, Counts, SLOResult, evaluate
from slo_eval.policy import SLO, AlertPolicy, BurnRateRule, Policy, load_policy
from slo_eval.records import Record, load_records

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
END = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
MICROSECOND = timedelta(microseconds=1)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)


class LatencyThatMustNotBeConverted(Decimal):
    def as_integer_ratio(self) -> tuple[int, int]:
        raise AssertionError("a latency was converted to an exact fraction")


def make_policy(
    *,
    window: timedelta = HOUR,
    min_requests: int = 1,
    objective: Fraction = Fraction(9, 10),
    critical: tuple[timedelta, timedelta] = (10 * MINUTE, MINUTE),
    warning: tuple[timedelta, timedelta] = (30 * MINUTE, 5 * MINUTE),
) -> Policy:
    return Policy(
        service="checkout-api",
        environment="prod",
        window=window,
        min_requests=min_requests,
        slos=(
            SLO("availability", objective, "Customers see errors.", "runbook#availability"),
            SLO("latency", objective, "Checkout is slow.", "runbook#latency", 100),
        ),
        alerts=AlertPolicy(
            critical=BurnRateRule(Fraction(10), *critical),
            warning=BurnRateRule(Fraction(2), *warning),
            budget_warning_remaining=Fraction(1, 4),
        ),
    )


def ok(age: timedelta = 30 * SECOND, latency_ms: int | Decimal = 50) -> Record:
    return Record(END - age, True, latency_ms)


def failed(age: timedelta = 30 * SECOND) -> Record:
    return Record(END - age, False, None)


def slo_results(policy: Policy, records: list[Record]) -> tuple[SLOResult, SLOResult]:
    availability, latency = evaluate(policy, records, END).slos
    return availability, latency


# --- SLIs, objectives and error budgets ---------------------------------------------------------


def test_a_passing_slo_is_met_with_budget_left() -> None:
    policy = make_policy(objective=Fraction(8, 10))

    availability, _ = slo_results(policy, [ok()] * 9 + [failed()])

    assert availability.window.counts == Counts(valid=10, good=9)
    assert availability.window.sli == Fraction(9, 10)
    assert availability.met is True
    assert availability.budget == Budget(allowed_bad=Fraction(2), bad=1, remaining=Fraction(1, 2))


def test_a_failing_slo_overspends_its_budget_without_clamping() -> None:
    availability, _ = slo_results(make_policy(), [ok()] * 7 + [failed()] * 3)

    assert availability.window.sli == Fraction(7, 10)
    assert availability.met is False
    assert availability.budget == Budget(allowed_bad=Fraction(1), bad=3, remaining=Fraction(-2))


def test_an_sli_exactly_at_the_objective_is_met_with_no_budget_left() -> None:
    policy = make_policy(objective=Fraction("0.999"))

    availability, _ = slo_results(policy, [ok()] * 999 + [failed()])

    assert availability.window.sli == Fraction(999, 1000)
    assert availability.met is True
    assert availability.budget.remaining == 0


@pytest.mark.parametrize(
    ("latency_ms", "good"),
    [(100, 1), (Decimal("100.0"), 1), (Decimal("100.000001"), 0), (101, 0)],
)
def test_a_latency_exactly_at_the_threshold_is_good(latency_ms: int | Decimal, good: int) -> None:
    _, latency = slo_results(make_policy(), [ok(latency_ms=latency_ms)])

    assert latency.window.counts == Counts(valid=1, good=good)


def test_failed_requests_are_left_out_of_the_latency_sli() -> None:
    records = [ok(latency_ms=50), ok(latency_ms=500), failed(), Record(END, False, 20)]

    _, latency = slo_results(make_policy(), records)

    assert latency.window.counts == Counts(valid=2, good=1)


def test_latencies_are_compared_without_converting_them() -> None:
    huge = LatencyThatMustNotBeConverted("1e999999999")

    _, latency = slo_results(make_policy(), [ok(latency_ms=huge)])

    assert latency.window.counts == Counts(valid=1, good=0)


def test_burn_rates_are_calculated_for_each_alert_window() -> None:
    records = [ok(age=20 * MINUTE)] * 5 + [ok()] * 3 + [failed()] * 2

    availability, _ = slo_results(make_policy(), records)

    burn_rates = {
        duration: window.burn_rate for duration, window in availability.alert_windows.items()
    }
    assert burn_rates == {MINUTE: 4, 5 * MINUTE: 4, 10 * MINUTE: 4, 30 * MINUTE: 2}


def test_alert_windows_come_from_the_policy() -> None:
    policy = make_policy(critical=(HOUR, 7 * MINUTE), warning=(20 * MINUTE, 7 * MINUTE))

    availability, latency = slo_results(policy, [ok()])

    assert list(availability.alert_windows) == [7 * MINUTE, 20 * MINUTE, HOUR]
    assert list(latency.alert_windows) == [7 * MINUTE, 20 * MINUTE, HOUR]


# --- window edges and time ----------------------------------------------------------------------


def test_every_record_is_before_inside_or_after_the_window() -> None:
    after_end = Record(END + MICROSECOND, True, 50)
    at_end = Record(END, True, 50)
    just_inside = Record(END - HOUR + MICROSECOND, True, 50)
    at_start = Record(END - HOUR, True, 50)
    older = Record(END - 2 * HOUR, True, 50)
    records = [after_end, at_end, just_inside, at_start, older]

    evaluation = evaluate(make_policy(), records, END)

    groups = (evaluation.records_before, evaluation.records_included, evaluation.records_after)
    assert groups == (2, 2, 1)
    assert sum(groups) == len(records)
    assert evaluation.start == END - HOUR
    assert evaluation.slos[0].window.counts.valid == 2


def test_alert_windows_include_their_end_and_exclude_their_start() -> None:
    records = [ok(age=0 * MINUTE), ok(age=MINUTE), ok(age=MINUTE - MICROSECOND)]

    availability, _ = slo_results(make_policy(), records)

    assert availability.alert_windows[MINUTE].counts.valid == 2
    assert availability.alert_windows[5 * MINUTE].counts.valid == 3


def test_duplicate_timestamps_are_distinct_requests() -> None:
    availability, _ = slo_results(make_policy(), [Record(END, True, 50), Record(END, False, None)])

    assert availability.window.counts == Counts(valid=2, good=1)


def test_record_order_does_not_change_the_result_or_the_callers_list() -> None:
    records = [ok(age=n * MINUTE) for n in range(1, 60)]
    records += [failed(age=n * MINUTE) for n in range(2, 60, 7)]
    shuffled = records.copy()
    random.Random(7).shuffle(shuffled)
    snapshot = shuffled.copy()

    assert evaluate(make_policy(), shuffled, END) == evaluate(make_policy(), records, END)
    assert shuffled == snapshot


def test_end_is_converted_to_utc_before_windows_are_subtracted() -> None:
    end = datetime(2026, 10, 25, 4, 30, tzinfo=ZoneInfo("Europe/Bucharest"))
    policy = make_policy(window=2 * HOUR, critical=(HOUR, MINUTE), warning=(2 * HOUR, 5 * MINUTE))
    two_and_a_half_hours_earlier = Record(datetime(2026, 10, 25, 0, 0, tzinfo=UTC), True, 50)

    evaluation = evaluate(policy, [two_and_a_half_hours_earlier], end)

    assert evaluation.end == datetime(2026, 10, 25, 2, 30, tzinfo=UTC)
    assert evaluation.start == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert evaluation.records_before == 1


def test_a_window_too_long_to_subtract_has_no_lower_bound() -> None:
    longest = timedelta(days=999_999_999)
    policy = make_policy(window=longest, critical=(longest, MINUTE), warning=(longest, 5 * MINUTE))
    records = [Record(datetime(1, 1, 1, tzinfo=UTC), True, 50), ok()]

    evaluation = evaluate(policy, records, END)

    assert evaluation.start is None
    assert (evaluation.records_before, evaluation.records_included) == (0, 2)
    assert evaluation.slos[0].alert_windows[longest].counts.valid == 2


@pytest.mark.parametrize(
    "end",
    [datetime(2026, 10, 23, 12, 0), datetime(1, 1, 1, 0, 30, tzinfo=timezone(HOUR))],
    ids=["naive", "before year 1 in UTC"],
)
def test_end_must_be_a_timezone_aware_time_that_exists_in_utc(end: datetime) -> None:
    with pytest.raises(ValueError, match="end"):
        evaluate(make_policy(), [], end)


# --- too little data ----------------------------------------------------------------------------


def test_min_requests_applies_to_each_slo_and_each_window() -> None:
    records = [ok(), failed(), failed(), ok(age=20 * MINUTE)]

    availability, latency = slo_results(make_policy(min_requests=3), records)

    assert availability.window.reason is None
    assert availability.alert_windows[MINUTE].counts.valid == 3
    assert availability.alert_windows[MINUTE].burn_rate == Fraction(20, 3)
    assert str(latency.window.reason) == "insufficient_requests: observed 2, required 3"
    assert (latency.window.sli, latency.met, latency.budget) == (None, None, None)
    assert str(latency.alert_windows[MINUTE].reason) == (
        "insufficient_requests: observed 1, required 3"
    )


def test_no_records_leave_every_window_unjudged() -> None:
    evaluation = evaluate(make_policy(), [], END)

    assert evaluation.within_objective is None
    for slo in evaluation.slos:
        assert (slo.met, slo.budget) == (None, None)
        windows = [slo.window, *slo.alert_windows.values()]
        assert {str(window.reason) for window in windows} == {
            "insufficient_requests: observed 0, required 1"
        }


def test_records_only_outside_the_window_leave_it_empty() -> None:
    evaluation = evaluate(make_policy(), [ok(age=2 * HOUR), ok(age=-MINUTE)], END)

    groups = (evaluation.records_before, evaluation.records_included, evaluation.records_after)
    assert groups == (1, 0, 1)
    assert evaluation.within_objective is None


def test_a_full_outage_fails_availability_and_leaves_latency_unjudged() -> None:
    evaluation = evaluate(make_policy(), [failed()] * 5, END)
    availability, latency = evaluation.slos

    assert availability.met is False
    assert latency.met is None
    assert str(latency.window.reason) == "insufficient_requests: observed 0, required 1"
    assert evaluation.within_objective is False


@pytest.mark.parametrize(
    ("records", "expected"),
    [([ok()] * 3, True), ([ok(), ok(), failed()], None), ([failed()] * 3, False)],
    ids=["all met", "one unjudged", "one missed and one unjudged"],
)
def test_within_objective(records: list[Record], expected: bool | None) -> None:
    policy = make_policy(min_requests=3, objective=Fraction(1, 2))

    assert evaluate(policy, records, END).within_objective is expected


def test_within_objective_ignores_whether_alert_windows_have_enough_data() -> None:
    evaluation = evaluate(make_policy(), [ok(age=50 * MINUTE)] * 10, END)

    assert evaluation.within_objective is True
    for slo in evaluation.slos:
        assert all(window.sli is None for window in slo.alert_windows.values())


# --- example files ------------------------------------------------------------------------------


def test_stale_sample_has_overspent_budgets_and_unjudged_alert_windows() -> None:
    policy = load_policy(EXAMPLES / "policy.toml")
    records = load_records(EXAMPLES / "sample.jsonl")

    evaluation = evaluate(policy, records, datetime(2026, 10, 25, 12, 0, tzinfo=UTC))
    availability, latency = evaluation.slos

    assert (availability.met, latency.met) == (False, False)
    assert availability.budget.remaining == Fraction(-497, 3)
    assert latency.budget.remaining == -9
    assert evaluation.within_objective is False
    reasons = {str(w.reason) for slo in evaluation.slos for w in slo.alert_windows.values()}
    assert reasons == {"insufficient_requests: observed 0, required 10"}


def test_sample_evaluated_at_its_last_record_has_recent_burn_rates() -> None:
    policy = load_policy(EXAMPLES / "policy.toml")
    records = load_records(EXAMPLES / "sample.jsonl")

    availability, _ = evaluate(policy, records, records[-1].timestamp).slos

    assert availability.alert_windows[timedelta(minutes=5)].burn_rate == Fraction(500, 3)
