"""Render a decision as a JSON or a text report."""

import json
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any

from slo_eval import __version__
from slo_eval.decision import Decision, Measurement, Rule, RuleResult, SLODecision
from slo_eval.evaluator import Counts
from slo_eval.policy import format_duration

SCHEMA_VERSION = 1
_EXTRA_DIGITS = 4
_MAX_EXACT_DIGITS = 100


def render_json(decision: Decision) -> str:
    report = _report(decision)
    return json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def render_text(decision: Decision) -> str:
    evaluation = decision.evaluation
    policy = evaluation.policy
    lines = [
        f"{policy.service} ({policy.environment}): {decision.severity.upper()} "
        f"at {_time(evaluation.end)}",
        f"window {format_duration(policy.window)}: {evaluation.records_included:,} records in it, "
        f"{evaluation.records_before:,} before it, {evaluation.records_after:,} after its end",
        f"within objective: {_within_objective(evaluation.within_objective)}",
    ]
    for slo in decision.slos:
        lines += ["", *_slo_lines(slo)]
    return "\n".join(lines) + "\n"


def _report(decision: Decision) -> dict[str, Any]:
    evaluation = decision.evaluation
    policy = evaluation.policy
    alerts = decision.alerts
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": "slo-eval", "version": __version__},
        "service": policy.service,
        "environment": policy.environment,
        "evaluated_at": _time(evaluation.end),
        "window": {
            "duration": format_duration(policy.window),
            "start": None if evaluation.start is None else _time(evaluation.start),
            "end": _time(evaluation.end),
        },
        "records": {
            "before_window": evaluation.records_before,
            "in_window": evaluation.records_included,
            "after_end": evaluation.records_after,
        },
        "severity": decision.severity,
        "within_objective": evaluation.within_objective,
        "alert": {
            "alerting": bool(alerts),
            "severity": decision.severity if alerts else None,
            "dedup_keys": [alert.dedup_key for alert in alerts],
        },
        "slos": [_slo(slo) for slo in decision.slos],
    }


def _slo(slo: SLODecision) -> dict[str, Any]:
    result = slo.result
    budget = result.budget
    return {
        "service": slo.service,
        "environment": slo.environment,
        "slo": slo.slo,
        "evaluated_at": _time(slo.evaluated_at),
        "dedup_key": slo.dedup_key,
        "severity": slo.severity,
        "objective": _number(result.slo.objective),
        "threshold_ms": _number(result.slo.threshold_ms),
        "met": result.met,
        "sli": _number(result.window.sli),
        "counts": _counts(result.window.counts),
        "reason": _reason(result.window.reason),
        "budget": None
        if budget is None
        else {
            "allowed_bad": _number(budget.allowed_bad),
            "bad": budget.bad,
            "remaining": _number(budget.remaining),
        },
        "triggered_rules": list(slo.triggered_rules),
        "rules": [_rule(rule) for rule in slo.rules],
        "summary": slo.summary,
        "impact": slo.impact,
        "runbook": slo.runbook,
        "next_action": slo.next_action,
        "diagnostics": list(slo.diagnostics),
    }


def _rule(rule: RuleResult) -> dict[str, Any]:
    return {
        "rule": rule.rule,
        "outcome": rule.outcome,
        "raises": rule.raises,
        "threshold": _number(rule.threshold),
        "measurements": [
            {
                "window": format_duration(measurement.window),
                "value": _number(measurement.value),
                **_counts(measurement.counts),
                "reason": _reason(measurement.reason),
            }
            for measurement in rule.measurements
        ],
    }


def _counts(counts: Counts) -> dict[str, int]:
    return {"valid": counts.valid, "good": counts.good, "bad": counts.bad}


def _reason(reason: object) -> str | None:
    return None if reason is None else str(reason)


def _number(value: Fraction | Decimal | int | None) -> float | int | None:
    if value is None or isinstance(value, int):
        return value
    return float(value)


def _time(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _within_objective(value: bool | None) -> str:
    if value is None:
        return "cannot tell (not enough data)"
    return "yes" if value else "no"


def _slo_lines(slo: SLODecision) -> list[str]:
    lines = [f"{slo.slo}: {slo.severity.upper()}", f"  {_sli_text(slo)}"]
    lines += [f"  {rule.rule:<10}  {rule.outcome:<15}  {_rule_text(rule)}" for rule in slo.rules]
    if slo.impact is not None:
        lines.append(f"  impact: {slo.impact}")
    if slo.next_action is not None:
        lines.append(f"  next action: {slo.next_action}")
    lines.append(f"  runbook: {slo.runbook}")
    lines.append(f"  dedup key: {slo.dedup_key}")
    lines += [f"  gap: {diagnostic}" for diagnostic in slo.diagnostics]
    return lines


def _sli_text(slo: SLODecision) -> str:
    result = slo.result
    window = result.window
    if window.sli is None:
        return f"SLI not judged: {window.reason}"
    requests = "requests" if result.slo.name == "availability" else "successful requests"
    sli = _compared(window.sli, result.slo.objective, 3, scale=100, unit="%", against="objective")
    objective = _exact(result.slo.objective * 100)
    verdict = "met" if result.met else "missed"
    return f"SLI {sli} of {window.counts.valid:,} {requests} (objective {objective}%, {verdict})"


def _rule_text(rule: RuleResult) -> str:
    if rule.rule is Rule.LOW_BUDGET:
        threshold = f"{_exact(rule.threshold * 100)}%"
    else:
        threshold = f"{_exact(rule.threshold)}×"
    parts = ", ".join(_measurement_text(rule, measurement) for measurement in rule.measurements)
    return f"{parts} (threshold {threshold})"


def _measurement_text(rule: RuleResult, measurement: Measurement) -> str:
    window = format_duration(measurement.window)
    if measurement.value is None:
        return f"{window} no data ({measurement.reason})"
    if rule.rule is Rule.LOW_BUDGET:
        left = _compared(measurement.value, rule.threshold, 1, scale=100, unit="%")
        return f"{window} budget left {left}"
    return f"{window} burn {_compared(measurement.value, rule.threshold, 2, unit='×')}"


def _compared(
    value: Fraction,
    threshold: Fraction,
    places: int,
    *,
    scale: int = 1,
    unit: str = "",
    against: str = "threshold",
) -> str:
    scaled, limit = value * scale, threshold * scale
    for digits in range(places, places + _EXTRA_DIGITS + 1):
        shown = Fraction(round(scaled * 10**digits), 10**digits)
        if _side(shown, limit) == _side(scaled, limit):
            return f"{_decimal(shown, digits)}{unit}"
    side = "below" if scaled < limit else "above"
    return f"≈ {_decimal(scaled, places)}{unit} ({side} {against})"


def _side(value: Fraction, limit: Fraction) -> int:
    return (value > limit) - (value < limit)


def _exact(value: Fraction) -> str:
    digits = next(
        (d for d in range(_MAX_EXACT_DIGITS + 1) if (value * 10**d).denominator == 1),
        _MAX_EXACT_DIGITS,
    )
    return _decimal(value, digits)


def _decimal(value: Fraction, digits: int) -> str:
    scaled = round(value * 10**digits)
    sign = "-" if scaled < 0 else ""
    whole, fraction = divmod(abs(scaled), 10**digits)
    if digits == 0:
        return f"{sign}{whole:,}"
    return f"{sign}{whole:,}.{fraction:0{digits}d}"
