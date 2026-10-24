"""Command-line interface."""

import argparse
from collections.abc import Sequence

from slo_eval import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slo-eval",
        description=(
            "Evaluate SLIs against SLO targets, compute the remaining error budget, "
            "and decide whether to alert."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the CLI and return the process exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    # Placeholder until the evaluator is wired in.
    parser.print_help()
    return 0
