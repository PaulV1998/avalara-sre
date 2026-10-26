"""Tests for the command-line interface and its entry points."""

import io
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import slo_eval.__main__ as entry_point
from slo_eval import __version__, cli
from slo_eval.cli import main

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
EXAMPLES = ROOT / "examples"
END = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
AT = "2026-10-23T12:00:00Z"
POLICY = """\
service = "checkout-api"
environment = "prod"
window = "1h"
min_requests = 1

[[slo]]
name = "availability"
objective = 0.9
impact = "Customers see errors."
runbook = "https://runbooks.example.com/checkout-api#availability"

[alerts]
critical = { burn_rate = 5, long_window = "10m", short_window = "1m" }
warning = { burn_rate = 2, long_window = "30m", short_window = "5m" }
budget_warning_remaining = 0.25
"""
CASES = {
    "ok": ({"failures": 0}, 0),
    "warning": ({"failures": 3}, 1),
    "critical": ({"failures": 6}, 2),
    "unknown": ({"failures": 0, "age": timedelta(minutes=45)}, 3),
}


@pytest.fixture
def policy(tmp_path: Path) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text(POLICY, encoding="utf-8")
    return path


def write_records(path: Path, *, failures: int, age: timedelta = timedelta(seconds=30)) -> Path:
    moment = (END - age).isoformat().replace("+00:00", "Z")
    lines = [
        json.dumps({"timestamp": moment, "success": n >= failures, "latency_ms": 50})
        for n in range(10)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(policy: Path, records: Path | str, *extra: str) -> int:
    return main(["--policy", str(policy), "--records", str(records), "--at", AT, *extra])


def leftovers(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name.endswith(".tmp"))


def raise_runtime_error(*args: object) -> None:
    raise RuntimeError("boom")


# --- exit codes ---------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_each_severity_is_a_completed_report_with_its_own_exit_code(
    case: str, policy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changes, code = CASES[case]
    records = write_records(tmp_path / "records.jsonl", **changes)

    assert run(policy, records) == code
    assert json.loads(capsys.readouterr().out)["severity"] == case


@pytest.mark.parametrize(
    "argv",
    [[], ["--policy", "policy.toml"], ["--bogus"], ["--format", "yaml"]],
    ids=["no arguments", "missing --records", "unknown option", "unknown format"],
)
def test_usage_errors_exit_4_instead_of_the_critical_code(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 4
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("at", "message"),
    [
        ("2026-10-23T12:00:00", "must include a timezone"),
        ("2026-10-23T12:00:00.1234567Z", "must have at most 6 fractional-second digits"),
        ("yesterday", "must be an ISO 8601 timestamp"),
    ],
)
def test_an_invalid_evaluation_time_is_a_usage_error(
    at: str, message: str, policy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = write_records(tmp_path / "records.jsonl", failures=0)

    code = main(["--policy", str(policy), "--records", str(records), "--at", at])

    captured = capsys.readouterr()
    assert code == 4
    assert message in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_help_and_version_need_no_input_flags(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([flag]) == 0
    assert capsys.readouterr().out


def test_version_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"slo-eval {__version__}"


def test_invalid_records_exit_4_and_print_no_report(
    policy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = tmp_path / "records.jsonl"
    records.write_text('{"timestamp": "2026-10-23T12:00:00", "success": true}\n', encoding="utf-8")

    code = run(policy, records)

    captured = capsys.readouterr()
    assert code == 4
    assert captured.out == ""
    assert "line 1: timestamp: must include a timezone" in captured.err


def test_a_missing_policy_exits_4(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    records = write_records(tmp_path / "records.jsonl", failures=0)

    assert run(tmp_path / "missing.toml", records) == 4
    assert "cannot read policy" in capsys.readouterr().err


def test_an_internal_error_exits_5_instead_of_the_warning_code(
    policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "evaluate", raise_runtime_error)
    records = write_records(tmp_path / "records.jsonl", failures=0)

    code = run(policy, records)

    captured = capsys.readouterr()
    assert code == 5
    assert captured.out == ""
    assert "internal error" in captured.err
    assert "RuntimeError: boom" in captured.err


# --- inputs -------------------------------------------------------------------------------------


def test_the_evaluation_time_defaults_to_the_clock(
    policy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = write_records(tmp_path / "records.jsonl", failures=0)

    code = main(["--policy", str(policy), "--records", str(records)], clock=lambda: END)

    assert code == 0
    assert json.loads(capsys.readouterr().out)["evaluated_at"] == AT


def test_records_can_come_from_stdin(
    policy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = write_records(tmp_path / "records.jsonl", failures=6)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(records.read_bytes())))

    assert run(policy, "-") == 2


def test_the_text_format_shows_each_window_separately(
    policy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = write_records(tmp_path / "records.jsonl", failures=6)

    assert run(policy, records, "--format", "text") == 2

    text = capsys.readouterr().out
    assert text.startswith("checkout-api (prod): CRITICAL at 2026-10-23T12:00:00Z\n")
    assert "10m burn 6.00×, 1m burn 6.00× (threshold 5×)" in text


# --- output -------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_every_completed_report_replaces_the_output_file(
    case: str, policy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changes, code = CASES[case]
    records = write_records(tmp_path / "records.jsonl", **changes)
    output = tmp_path / "report.json"
    output.write_text("previous report\n", encoding="utf-8")

    assert run(policy, records, "--output", str(output)) == code
    assert json.loads(output.read_text(encoding="utf-8"))["severity"] == case
    assert capsys.readouterr().out == ""
    assert leftovers(tmp_path) == []


def break_records(records: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records.write_text("not json\n", encoding="utf-8")


def break_evaluation(records: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "evaluate", raise_runtime_error)


def break_serialization(records: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def serialize_infinity(decision: object) -> str:
        return json.dumps(float("inf"), allow_nan=False)

    monkeypatch.setattr(cli, "render_json", serialize_infinity)


@pytest.mark.parametrize(
    ("break_run", "code"),
    [(break_records, 4), (break_evaluation, 5), (break_serialization, 5)],
    ids=["invalid input", "internal error", "serialization error"],
)
def test_a_failed_run_leaves_the_output_file_untouched(
    break_run: Callable[[Path, pytest.MonkeyPatch], None],
    code: int,
    policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = write_records(tmp_path / "records.jsonl", failures=6)
    output = tmp_path / "report.json"
    output.write_text("previous report\n", encoding="utf-8")
    break_run(records, monkeypatch)

    assert run(policy, records, "--output", str(output)) == code
    assert output.read_text(encoding="utf-8") == "previous report\n"
    assert leftovers(tmp_path) == []


def test_a_failed_replacement_keeps_the_old_file_and_removes_the_temporary_file(
    policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(source: object, destination: object) -> None:
        raise PermissionError(13, "Permission denied")

    records = write_records(tmp_path / "records.jsonl", failures=6)
    output = tmp_path / "report.json"
    output.write_text("previous report\n", encoding="utf-8")
    monkeypatch.setattr(os, "replace", refuse)

    assert run(policy, records, "--output", str(output)) == 4
    assert output.read_text(encoding="utf-8") == "previous report\n"
    assert leftovers(tmp_path) == []
    assert "cannot write the report" in capsys.readouterr().err


def test_replacing_keeps_the_existing_file_permissions(policy: Path, tmp_path: Path) -> None:
    records = write_records(tmp_path / "records.jsonl", failures=0)
    output = tmp_path / "report.json"
    output.write_text("previous report\n", encoding="utf-8")
    output.chmod(0o640)

    assert run(policy, records, "--output", str(output)) == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o640


def test_a_new_output_file_gets_ordinary_permissions(policy: Path, tmp_path: Path) -> None:
    records = write_records(tmp_path / "records.jsonl", failures=0)
    output = tmp_path / "report.json"
    previous_umask = os.umask(0o022)
    try:
        assert run(policy, records, "--output", str(output)) == 0
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(output.stat().st_mode) == 0o644


@pytest.mark.parametrize("target", ["policy", "records", "link to records"])
def test_the_output_cannot_be_an_input(
    target: str, policy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = write_records(tmp_path / "records.jsonl", failures=0)
    inputs = {path: path.read_bytes() for path in (policy, records)}
    link = tmp_path / "link.jsonl"
    link.symlink_to(records)
    output = {"policy": policy, "records": records, "link to records": link}[target]

    assert run(policy, records, "--output", str(output)) == 4
    assert {path: path.read_bytes() for path in inputs} == inputs
    assert "refusing to overwrite an input" in capsys.readouterr().err


class ClosedPipe(io.StringIO):
    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_closed_stdout_pipe_is_an_execution_failure(
    policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    records = write_records(tmp_path / "records.jsonl", failures=6)
    monkeypatch.setattr(sys, "stdout", ClosedPipe())

    assert run(policy, records) == 4
    assert "cannot write the report" in capsys.readouterr().err


# --- entry points -------------------------------------------------------------------------------


def test_run_propagates_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entry_point, "main", lambda: 3)

    with pytest.raises(SystemExit) as excinfo:
        entry_point.run()

    assert excinfo.value.code == 3


def run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "slo_eval", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        timeout=30,
        check=False,
    )


def test_module_entry_point_prints_version() -> None:
    result = run_module("--version")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"slo-eval {__version__}"


def test_module_entry_point_exits_with_the_decision_code() -> None:
    result = run_module(
        "--policy",
        str(EXAMPLES / "policy.toml"),
        "--records",
        str(EXAMPLES / "sample.jsonl"),
        "--at",
        "2026-10-23T14:00:57Z",
    )

    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["severity"] == "critical"
