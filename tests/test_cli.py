"""Smoke tests for the CLI entry points."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import slo_eval.__main__ as entry_point
from slo_eval import __version__
from slo_eval.cli import main

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def test_version_flag_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"slo-eval {__version__}"


def test_run_propagates_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entry_point, "main", lambda: 3)

    with pytest.raises(SystemExit) as excinfo:
        entry_point.run()

    assert excinfo.value.code == 3


def test_module_entry_point_prints_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "slo_eval", "--version"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"slo-eval {__version__}"
