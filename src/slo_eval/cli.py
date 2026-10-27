"""Command-line interface."""

import argparse
import contextlib
import os
import secrets
import stat
import sys
import traceback
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from slo_eval import __version__
from slo_eval.decision import Severity, decide
from slo_eval.errors import InputError
from slo_eval.evaluator import evaluate
from slo_eval.policy import load_policy
from slo_eval.records import load_records, parse_records, parse_timestamp
from slo_eval.report import render_json, render_text

EXIT_CODES = {Severity.OK: 0, Severity.WARNING: 1, Severity.CRITICAL: 2, Severity.UNKNOWN: 3}
EXIT_CANNOT_EVALUATE = 4
EXIT_INTERNAL_ERROR = 5


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_CANNOT_EVALUATE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="slo-eval",
        description=(
            "Evaluate SLIs against SLO targets, compute the remaining error budget, "
            "and decide whether to alert."
        ),
        epilog=(
            "exit codes: 0 ok, 1 warning, 2 critical, 3 unknown (each a completed report); "
            "4 cannot evaluate as requested; 5 internal error"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--policy", required=True, metavar="PATH", help="service policy (TOML)")
    parser.add_argument(
        "--records",
        required=True,
        metavar="PATH",
        help="request records (JSON Lines), or - to read them from stdin",
    )
    parser.add_argument(
        "--at",
        type=_evaluation_time,
        metavar="TIME",
        help="evaluation time, ISO 8601 with a timezone (default: now)",
    )
    parser.add_argument(
        "--format", choices=("json", "text"), default="json", help="report format (default: json)"
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="write the report to this file, replacing it atomically (default: stdout)",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, clock: Callable[[], datetime] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_CANNOT_EVALUATE
    try:
        report, severity = _evaluate(args, clock or _now)
    except InputError as exc:
        print(f"slo-eval: {exc}", file=sys.stderr)
        return EXIT_CANNOT_EVALUATE
    except Exception:
        print("slo-eval: internal error; please report it with these details:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    try:
        _deliver(report, args.output)
    except OSError as exc:
        print(f"slo-eval: cannot write the report: {exc}", file=sys.stderr)
        return EXIT_CANNOT_EVALUATE
    return EXIT_CODES[severity]


def _now() -> datetime:
    return datetime.now(UTC)


def _evaluation_time(text: str) -> datetime:
    try:
        return parse_timestamp(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _evaluate(args: argparse.Namespace, clock: Callable[[], datetime]) -> tuple[str, Severity]:
    _refuse_to_overwrite_an_input(args)
    policy = load_policy(args.policy)
    if args.records == "-":
        records = parse_records(sys.stdin.buffer, source="<stdin>")
    else:
        records = load_records(args.records)
    end = clock() if args.at is None else args.at
    decision = decide(evaluate(policy, records, end))
    render = render_text if args.format == "text" else render_json
    return render(decision), decision.severity


def _refuse_to_overwrite_an_input(args: argparse.Namespace) -> None:
    if args.output is None or not os.path.exists(args.output):
        return
    for flag, path in (("--policy", args.policy), ("--records", args.records)):
        if path != "-" and os.path.exists(path) and os.path.samefile(args.output, path):
            raise InputError(
                f"--output {args.output} is the same file as {flag} {path}; "
                "refusing to overwrite an input"
            )


def _deliver(report: str, output: str | None) -> None:
    if output is not None:
        _replace(Path(output), report)
        return
    try:
        sys.stdout.write(report)
        sys.stdout.flush()
    except BrokenPipeError:
        _discard_unsent_output()
        raise


def _replace(path: Path, report: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else None
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8") as file:
            file.write(report)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _discard_unsent_output() -> None:
    with contextlib.suppress(OSError, ValueError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
