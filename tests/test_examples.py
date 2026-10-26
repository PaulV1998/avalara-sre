"""End-to-end tests for the generated example scenarios."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

import pytest

from slo_eval.cli import main
from slo_eval.decision import Decision, decide
from slo_eval.evaluator import evaluate
from slo_eval.policy import load_policy
from slo_eval.records import load_records

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_examples.py"
POLICY = ROOT / "examples" / "policy.toml"
END = datetime(2026, 3, 29, 1, 7, 20, tzinfo=UTC)
AT = "2026-03-29T01:07:20Z"
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
NO_REQUESTS = "insufficient_requests: observed 0, required 10"

CENSUS = {
    "1-healthy": (129_600, 26, 324, END),
    "2-outage": (129_600, 161, 324, END),
    "3-elevated-errors": (129_600, 42, 324, END),
    "4-latency-regression": (129_600, 26, 1_257, END),
    "5-past-incident": (129_600, 206, 324, END),
    "6-telemetry-stopped-20m": (129_540, 26, 324, END - 20 * MINUTE),
    "7-telemetry-stopped-2h": (129_240, 26, 324, END - 2 * HOUR),
}
OUTCOMES = {
    "1-healthy": (0, "ok", True, ("ok", []), ("ok", [])),
    "2-outage": (
        2,
        "critical",
        False,
        ("critical", ["fast_burn", "slow_burn", "low_budget"]),
        ("ok", []),
    ),
    "3-elevated-errors": (1, "warning", True, ("warning", ["slow_burn"]), ("ok", [])),
    "4-latency-regression": (1, "warning", True, ("ok", []), ("warning", ["low_budget"])),
    "5-past-incident": (1, "warning", False, ("warning", ["low_budget"]), ("ok", [])),
    "6-telemetry-stopped-20m": (0, "ok", True, ("ok", []), ("ok", [])),
    "7-telemetry-stopped-2h": (3, "unknown", True, ("unknown", []), ("unknown", [])),
}
EMPTY_WINDOWS = {
    "2-outage": ((), ("fast_burn 5m", "slow_burn 30m")),
    "6-telemetry-stopped-20m": (("fast_burn 5m",), ("fast_burn 5m",)),
    "7-telemetry-stopped-2h": (
        ("fast_burn 1h", "fast_burn 5m", "slow_burn 30m"),
        ("fast_burn 1h", "fast_burn 5m", "slow_burn 30m"),
    ),
}


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("scenarios")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    subprocess.run(
        [sys.executable, str(GENERATOR), "--end", AT, "--out", str(directory)],
        cwd=directory,
        env=environment,
        capture_output=True,
        timeout=120,
        check=True,
    )
    return {path.stem: path for path in sorted(directory.glob("*.jsonl"))}


@pytest.fixture(scope="module")
def decisions(scenarios: dict[str, Path]) -> dict[str, Decision]:
    policy = load_policy(POLICY)
    return {
        name: decide(evaluate(policy, load_records(path), END)) for name, path in scenarios.items()
    }


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


# --- generated records --------------------------------------------------------------------------


def test_the_generator_runs_standalone_and_writes_the_seven_scenarios(
    scenarios: dict[str, Path],
) -> None:
    assert list(scenarios) == list(CENSUS)


@pytest.mark.parametrize("name", CENSUS)
def test_each_scenario_has_its_documented_records(name: str, scenarios: dict[str, Path]) -> None:
    lines = scenarios[name].read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    count, failures, slow, last = CENSUS[name]
    times = [datetime.fromisoformat(record["timestamp"]) for record in records]

    assert len(records) == count
    assert sum(not record["success"] for record in records) == failures
    assert sum(record["success"] and record["latency_ms"] > 300 for record in records) == slow
    assert all(("latency_ms" in record) is record["success"] for record in records)
    assert records[0]["timestamp"] == iso(END - timedelta(days=30) + 20 * SECOND)
    assert records[-1]["timestamp"] == iso(last)
    assert {later - earlier for earlier, later in pairwise(times)} == {20 * SECOND}


# --- decisions ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", OUTCOMES)
def test_each_scenario_exits_with_its_documented_decision(
    name: str, scenarios: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, severity, within_objective, availability, latency = OUTCOMES[name]

    assert main(["--policy", str(POLICY), "--records", str(scenarios[name]), "--at", AT]) == code

    report = json.loads(capsys.readouterr().out)
    assert (report["severity"], report["within_objective"]) == (severity, within_objective)
    assert [(slo["severity"], slo["triggered_rules"]) for slo in report["slos"]] == [
        availability,
        latency,
    ]


@pytest.mark.parametrize("name", CENSUS)
def test_each_scenario_reports_exactly_its_empty_windows(
    name: str, decisions: dict[str, Decision]
) -> None:
    expected = EMPTY_WINDOWS.get(name, ((), ()))

    for slo, windows in zip(decisions[name].slos, expected, strict=True):
        assert slo.diagnostics == tuple(f"{window} window: {NO_REQUESTS}" for window in windows)


# --- measurements -------------------------------------------------------------------------------


def test_healthy_traffic_uses_about_a_fifth_and_a_quarter_of_the_budgets(
    decisions: dict[str, Decision],
) -> None:
    availability, latency = decisions["1-healthy"].evaluation.slos

    assert (availability.window.counts.valid, availability.window.counts.bad) == (129_600, 26)
    assert availability.budget.remaining == 1 - Fraction(26_000, 129_600)
    assert (latency.window.counts.valid, latency.window.counts.bad) == (129_574, 324)
    assert latency.budget.remaining == 1 - Fraction(32_400, 129_574)


def test_the_outage_burns_750_times_over_1h_and_1000_times_over_5m(
    decisions: dict[str, Decision],
) -> None:
    availability, latency = decisions["2-outage"].evaluation.slos

    assert availability.alert_windows[HOUR].counts.bad == 135
    assert availability.alert_windows[HOUR].burn_rate == 750
    assert availability.alert_windows[5 * MINUTE].burn_rate == 1000
    assert availability.window.counts.bad == 161
    assert latency.alert_windows[HOUR].counts.valid == 45


def test_elevated_errors_burn_exactly_100_ninths_without_reaching_the_fast_threshold(
    decisions: dict[str, Decision],
) -> None:
    availability = decisions["3-elevated-errors"].evaluation.slos[0]
    windows = availability.alert_windows

    assert [windows[d].burn_rate for d in (30 * MINUTE, HOUR, 6 * HOUR)] == [Fraction(100, 9)] * 3
    assert windows[5 * MINUTE].burn_rate == 0
    assert availability.window.counts.bad == 42


def test_the_latency_regression_nearly_exhausts_the_budget_while_meeting_the_objective(
    decisions: dict[str, Decision],
) -> None:
    latency = decisions["4-latency-regression"].evaluation.slos[1]
    windows = latency.alert_windows

    assert (latency.window.counts.valid, latency.window.counts.bad) == (129_574, 1_257)
    assert latency.budget.remaining == 1 - Fraction(125_700, 129_574)
    assert latency.budget.remaining > 0
    assert latency.met is True
    assert [windows[d].burn_rate for d in (30 * MINUTE, HOUR, 6 * HOUR)] == [Fraction(10, 3)] * 3
    assert windows[5 * MINUTE].burn_rate == 0


def test_the_past_incident_overspends_the_budget_while_recent_windows_are_clean(
    decisions: dict[str, Decision],
) -> None:
    availability = decisions["5-past-incident"].evaluation.slos[0]

    assert availability.window.counts.bad == 206
    assert availability.budget.remaining == 1 - Fraction(206_000, 129_600)
    assert all(window.burn_rate == 0 for window in availability.alert_windows.values())


@pytest.mark.parametrize(
    ("name", "records"),
    [("6-telemetry-stopped-20m", 129_540), ("7-telemetry-stopped-2h", 129_240)],
)
def test_telemetry_gaps_remove_exactly_the_newest_records(
    name: str, records: int, decisions: dict[str, Decision]
) -> None:
    evaluation = decisions[name].evaluation

    assert (evaluation.records_before, evaluation.records_included) == (0, records)
