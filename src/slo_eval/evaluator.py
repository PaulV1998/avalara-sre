"""Calculate SLIs, error budgets and burn rates at one evaluation time."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction

from slo_eval.policy import SLO, Policy
from slo_eval.records import Record


@dataclass(frozen=True)
class Counts:
    valid: int
    good: int

    @property
    def bad(self) -> int:
        return self.valid - self.good


@dataclass(frozen=True)
class InsufficientRequests:
    observed: int
    required: int

    def __str__(self) -> str:
        return f"insufficient_requests: observed {self.observed}, required {self.required}"


@dataclass(frozen=True)
class WindowResult:
    duration: timedelta
    counts: Counts
    sli: Fraction | None
    burn_rate: Fraction | None
    reason: InsufficientRequests | None


@dataclass(frozen=True)
class Budget:
    allowed_bad: Fraction
    bad: int
    remaining: Fraction


@dataclass(frozen=True)
class SLOResult:
    slo: SLO
    window: WindowResult
    met: bool | None
    budget: Budget | None
    alert_windows: dict[timedelta, WindowResult]


@dataclass(frozen=True)
class Evaluation:
    policy: Policy
    end: datetime
    start: datetime | None
    records_before: int
    records_included: int
    records_after: int
    slos: tuple[SLOResult, ...]
    within_objective: bool | None


def evaluate(policy: Policy, records: Iterable[Record], end: datetime) -> Evaluation:
    end = _to_utc(end)
    start = _window_start(end, policy.window)
    before = after = 0
    included: list[Record] = []
    for record in records:
        if record.timestamp > end:
            after += 1
        elif start is not None and record.timestamp <= start:
            before += 1
        else:
            included.append(record)
    durations = sorted(
        {
            duration
            for rule in (policy.alerts.critical, policy.alerts.warning)
            for duration in (rule.long_window, rule.short_window)
        }
    )
    slos = tuple(_evaluate_slo(slo, policy, included, end, durations) for slo in policy.slos)
    return Evaluation(
        policy, end, start, before, len(included), after, slos, _within_objective(slos)
    )


def _to_utc(end: datetime) -> datetime:
    if end.utcoffset() is None:
        raise ValueError("end must be timezone-aware")
    try:
        return end.astimezone(UTC)
    except OverflowError:
        raise ValueError(f"end is out of range once converted to UTC: {end.isoformat()}") from None


def _window_start(end: datetime, duration: timedelta) -> datetime | None:
    try:
        return end - duration
    except OverflowError:
        return None


def _evaluate_slo(
    slo: SLO,
    policy: Policy,
    records: Sequence[Record],
    end: datetime,
    durations: Sequence[timedelta],
) -> SLOResult:
    window = _evaluate_window(slo, policy.window, records, end, policy.min_requests)
    alert_windows = {
        duration: _evaluate_window(slo, duration, records, end, policy.min_requests)
        for duration in durations
    }
    if window.sli is None:
        return SLOResult(slo, window, None, None, alert_windows)
    allowed_bad = (1 - slo.objective) * window.counts.valid
    budget = Budget(allowed_bad, window.counts.bad, 1 - window.counts.bad / allowed_bad)
    return SLOResult(slo, window, window.sli >= slo.objective, budget, alert_windows)


def _evaluate_window(
    slo: SLO,
    duration: timedelta,
    records: Iterable[Record],
    end: datetime,
    min_requests: int,
) -> WindowResult:
    counts = _count(slo, records, _window_start(end, duration))
    if counts.valid < min_requests:
        reason = InsufficientRequests(counts.valid, min_requests)
        return WindowResult(duration, counts, None, None, reason)
    sli = Fraction(counts.good, counts.valid)
    return WindowResult(duration, counts, sli, (1 - sli) / (1 - slo.objective), None)


def _count(slo: SLO, records: Iterable[Record], start: datetime | None) -> Counts:
    valid = good = 0
    for record in records:
        if start is not None and record.timestamp <= start:
            continue
        if slo.name == "availability":
            valid += 1
            if record.success:
                good += 1
        elif record.success:
            valid += 1
            if record.latency_ms <= slo.threshold_ms:
                good += 1
    return Counts(valid, good)


def _within_objective(slos: Sequence[SLOResult]) -> bool | None:
    if any(slo.met is False for slo in slos):
        return False
    if any(slo.met is None for slo in slos):
        return None
    return True
