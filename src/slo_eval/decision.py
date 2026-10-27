"""Turn evaluation results into severity tags, diagnostics and alerts."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from fractions import Fraction

from slo_eval.evaluator import Counts, Evaluation, InsufficientRequests, SLOResult, WindowResult
from slo_eval.policy import SLO, BurnRateRule, format_duration


class Severity(StrEnum):
    OK = "ok"
    UNKNOWN = "unknown"
    WARNING = "warning"
    CRITICAL = "critical"


class Rule(StrEnum):
    FAST_BURN = "fast_burn"
    SLOW_BURN = "slow_burn"
    LOW_BUDGET = "low_budget"


class Outcome(StrEnum):
    FIRING = "firing"
    NOT_FIRING = "not_firing"
    CANNOT_EVALUATE = "cannot_evaluate"


_RANK = (Severity.OK, Severity.UNKNOWN, Severity.WARNING, Severity.CRITICAL)
_ALERTING = (Severity.WARNING, Severity.CRITICAL)
_LABELS = {
    Rule.FAST_BURN: "fast burn",
    Rule.SLOW_BURN: "slow burn",
    Rule.LOW_BUDGET: "low error budget",
}
_NEXT_ACTIONS = {
    Rule.FAST_BURN: (
        "Investigate immediately: elevated budget consumption was observed in the recent "
        "evaluation windows. Follow the runbook."
    ),
    Rule.SLOW_BURN: (
        "Investigate soon: sustained elevated budget consumption was observed in the evaluation "
        "windows. Follow the runbook."
    ),
    Rule.LOW_BUDGET: (
        "Review recent incidents and budget consumption; follow the team's release/error-budget "
        "policy."
    ),
}
_CHECK_TELEMETRY = "Check whether requests are arriving and telemetry is flowing."


@dataclass(frozen=True)
class Measurement:
    window: timedelta
    counts: Counts
    value: Fraction | None
    reason: InsufficientRequests | None


@dataclass(frozen=True)
class RuleResult:
    rule: Rule
    raises: Severity
    outcome: Outcome
    threshold: Fraction
    measurements: tuple[Measurement, ...]


@dataclass(frozen=True)
class SLODecision:
    service: str
    environment: str
    slo: str
    evaluated_at: datetime
    dedup_key: str
    severity: Severity
    triggered_rules: tuple[Rule, ...]
    summary: str
    impact: str | None
    runbook: str
    next_action: str | None
    rules: tuple[RuleResult, ...]
    diagnostics: tuple[str, ...]
    result: SLOResult


@dataclass(frozen=True)
class Decision:
    evaluation: Evaluation
    severity: Severity
    slos: tuple[SLODecision, ...]

    @property
    def alerts(self) -> tuple[SLODecision, ...]:
        return tuple(slo for slo in self.slos if slo.severity in _ALERTING)


def decide(evaluation: Evaluation) -> Decision:
    slos = tuple(_decide_slo(evaluation, result) for result in evaluation.slos)
    return Decision(evaluation, max((slo.severity for slo in slos), key=_RANK.index), slos)


def _decide_slo(evaluation: Evaluation, result: SLOResult) -> SLODecision:
    policy = evaluation.policy
    slo = result.slo
    rules = (
        _burn_rule(Rule.FAST_BURN, Severity.CRITICAL, policy.alerts.critical, result),
        _burn_rule(Rule.SLOW_BURN, Severity.WARNING, policy.alerts.warning, result),
        _budget_rule(policy.alerts.budget_warning_remaining, result),
    )
    triggered = tuple(rule for rule in rules if rule.outcome is Outcome.FIRING)
    primary = triggered[0] if triggered else None
    severity = _severity(rules)
    return SLODecision(
        service=policy.service,
        environment=policy.environment,
        slo=slo.name,
        evaluated_at=evaluation.end,
        dedup_key=f"{policy.service}:{policy.environment}:{slo.name}",
        severity=severity,
        triggered_rules=tuple(rule.rule for rule in triggered),
        summary=f"{policy.service} ({policy.environment}) {slo.name}: {_label(primary, severity)}",
        impact=None if primary is None else _impact(slo, primary),
        runbook=slo.runbook,
        next_action=_next_action(primary, severity),
        rules=rules,
        diagnostics=_diagnostics(rules),
        result=result,
    )


def _burn_rule(rule: Rule, raises: Severity, config: BurnRateRule, result: SLOResult) -> RuleResult:
    measurements = tuple(
        _measurement(result.alert_windows[duration], result.alert_windows[duration].burn_rate)
        for duration in (config.long_window, config.short_window)
    )
    checks = [None if m.value is None else m.value >= config.burn_rate for m in measurements]
    return RuleResult(rule, raises, _outcome(checks), config.burn_rate, measurements)


def _budget_rule(level: Fraction, result: SLOResult) -> RuleResult:
    remaining = None if result.budget is None else result.budget.remaining
    check = None if remaining is None else remaining <= level
    measurement = _measurement(result.window, remaining)
    return RuleResult(Rule.LOW_BUDGET, Severity.WARNING, _outcome([check]), level, (measurement,))


def _measurement(window: WindowResult, value: Fraction | None) -> Measurement:
    return Measurement(window.duration, window.counts, value, window.reason)


def _outcome(checks: Sequence[bool | None]) -> Outcome:
    if any(check is False for check in checks):
        return Outcome.NOT_FIRING
    if any(check is None for check in checks):
        return Outcome.CANNOT_EVALUATE
    return Outcome.FIRING


def _severity(rules: Sequence[RuleResult]) -> Severity:
    firing = [rule.raises for rule in rules if rule.outcome is Outcome.FIRING]
    if firing:
        return max(firing, key=_RANK.index)
    if any(rule.outcome is Outcome.CANNOT_EVALUATE for rule in rules):
        return Severity.UNKNOWN
    return Severity.OK


def _label(primary: RuleResult | None, severity: Severity) -> str:
    if primary is None:
        return "cannot be evaluated" if severity is Severity.UNKNOWN else "no alert condition"
    windows = " and ".join(format_duration(m.window) for m in primary.measurements)
    return f"{_LABELS[primary.rule]} over {windows}"


def _impact(slo: SLO, primary: RuleResult) -> str:
    measurement = primary.measurements[0]
    counts = measurement.counts
    window = format_duration(measurement.window)
    if slo.name == "availability":
        detail = f"{counts.bad:,} of {counts.valid:,} requests failed in the {window} window."
    else:
        detail = (
            f"{counts.bad:,} of {counts.valid:,} successful requests exceeded "
            f"{slo.threshold_ms} ms in the {window} window."
        )
    return f"{slo.impact} {detail}"


def _next_action(primary: RuleResult | None, severity: Severity) -> str | None:
    if primary is not None:
        return _NEXT_ACTIONS[primary.rule]
    if severity is Severity.UNKNOWN:
        return _CHECK_TELEMETRY
    return None


def _diagnostics(rules: Sequence[RuleResult]) -> tuple[str, ...]:
    return tuple(
        f"{rule.rule} {format_duration(m.window)} window: {m.reason}"
        for rule in rules
        for m in rule.measurements
        if m.reason is not None
    )
