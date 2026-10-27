"""Generate the example scenarios: 30 days of request records ending at a given time."""

import argparse
import json
import random
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

INTERVAL = timedelta(seconds=20)
COUNT = timedelta(days=30) // INTERVAL
SEED = 20261023
FAILURE_EVERY = 5000
SLOW_EVERY = 400
REGRESSION_SLOW_EVERY = 30


def requests_in(duration: timedelta) -> int:
    return duration // INTERVAL


def _background(position: int) -> bool | None:
    return None


def _outage(position: int) -> bool | None:
    return True if position < requests_in(timedelta(minutes=45)) else None


def _elevated_errors(position: int) -> bool | None:
    if position >= requests_in(timedelta(hours=8)):
        return None
    return position % requests_in(timedelta(minutes=30)) == requests_in(timedelta(minutes=15))


def _past_incident(position: int) -> bool | None:
    start = requests_in(timedelta(days=10) - timedelta(hours=2))
    if not start <= position < requests_in(timedelta(days=10)):
        return None
    return (position - start) % 2 == 0


@dataclass(frozen=True)
class Scenario:
    name: str
    failing: Callable[[int], bool | None] = _background
    regression: int = 0
    missing: int = 0


SCENARIOS = (
    Scenario("1-healthy"),
    Scenario("2-outage", failing=_outage),
    Scenario("3-elevated-errors", failing=_elevated_errors),
    Scenario("4-latency-regression", regression=requests_in(timedelta(days=7))),
    Scenario("5-past-incident", failing=_past_incident),
    Scenario("6-telemetry-stopped-20m", missing=requests_in(timedelta(minutes=20))),
    Scenario("7-telemetry-stopped-2h", missing=requests_in(timedelta(hours=2))),
)


def generate(scenario: Scenario, end: datetime) -> Iterator[str]:
    rng = random.Random(SEED)
    successes = regression_successes = 0
    for index in range(COUNT):
        position = COUNT - 1 - index
        if position < scenario.missing:
            continue
        failed = scenario.failing(position)
        if failed is None:
            failed = index % FAILURE_EVERY == 0
        record: dict[str, object] = {
            "timestamp": _iso(end - position * INTERVAL),
            "success": not failed,
        }
        if not failed:
            if position < scenario.regression:
                slow = regression_successes % REGRESSION_SLOW_EVERY == 0
                regression_successes += 1
            else:
                slow = successes % SLOW_EVERY == 0
                successes += 1
            record["latency_ms"] = _latency(rng, slow)
        yield json.dumps(record)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the example request records.")
    parser.add_argument(
        "--end",
        required=True,
        type=_end,
        metavar="TIME",
        help="evaluation time the scenarios end at, ISO 8601 with a timezone",
    )
    parser.add_argument(
        "--out", required=True, type=Path, metavar="DIRECTORY", help="where to write the files"
    )
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for scenario in SCENARIOS:
        path = args.out / f"{scenario.name}.jsonl"
        with path.open("w", encoding="utf-8") as file:
            file.writelines(f"{line}\n" for line in generate(scenario, args.end))
        print(path)
    return 0


def _end(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.utcoffset() is None:
        raise argparse.ArgumentTypeError(f"must include a timezone, got {text!r}")
    return moment.astimezone(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _latency(rng: random.Random, slow: bool) -> float:
    low, high = (301, 2000) if slow else (40, 250)
    return round(low + (high - low) * rng.random(), 1)


if __name__ == "__main__":
    raise SystemExit(main())
