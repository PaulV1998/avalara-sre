# slo-eval

A small CLI that evaluates service reliability data against SLO targets: it calculates
availability and latency SLIs, the remaining error budget, and whether to raise an alert.

Built for the EOC SRE take-home exercise, **Option 3: SLO, Error-Budget, and Alert Evaluator**.

> Work in progress: the [design](#design) below is the specification; the evaluator is being
> implemented step by step. Usage, example output and known limitations follow with the CLI.

## Requirements

- Python 3.11+ (the tool itself has no third-party runtime dependencies)
- `make`

## Quick start

```bash
make check                      # creates .venv with pinned dev tools, lints, runs the tests
make build                      # builds dist/slo-eval.pyz and smoke-tests it
./dist/slo-eval.pyz --version
```

## Make targets

| Target         | Description                                                           |
|----------------|-----------------------------------------------------------------------|
| `make help`    | List available targets (default)                                      |
| `make install` | Create `.venv` with the pinned dev tools from `requirements-dev.txt`  |
| `make fmt`     | Auto-format code and apply safe lint fixes                            |
| `make lint`    | Check lint and formatting without changing files                      |
| `make test`    | Run the test suite                                                    |
| `make check`   | `lint` + `test` (what CI runs)                                        |
| `make build`   | Build the single-file artifact `dist/slo-eval.pyz` and smoke-test it  |
| `make clean`   | Remove build outputs and caches (keeps `.venv`)                       |

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `make check` and `make build` on
Python 3.11 and 3.13 for pushes to `main`/`develop` and pull requests into `main`, then uploads
`dist/slo-eval.pyz` as a build artifact.

## Design

### Problem interpretation

An on-call engineer needs a fast, trustworthy answer to three questions about a service: is it
**within its SLO**, how much **error budget** is left, and does anything need **attention now**?
`slo-eval` answers them from a batch of request records and a per-service policy, evaluated over a
fixed time window, and returns a report that a human can read and automation can act on.

### Inputs

**Service policy:** TOML, one file per service and environment
([`examples/policy.toml`](examples/policy.toml)). Python reads TOML with its standard library
(`tomllib`, the reason for Python 3.11+), and TOML allows comments, so every target is documented
next to the reason it was chosen. Like the records, the file is UTF-8 without a byte order mark
(BOM).

| Field | Meaning |
|---|---|
| `service`, `environment` | What the policy covers, e.g. `checkout-api` in `prod`. Must follow the naming convention below |
| `window` | SLO window, e.g. `30d`. A duration is a whole number above 0 and one unit: `s`, `m`, `h` or `d` |
| `min_requests` | Fewest valid requests a window needs before it is judged; a whole number, at least 1 |
| `[[slo]]` `name` | `availability` or `latency`, each at most once; at least one SLO is required |
| `[[slo]]` `objective` | Target share of good requests, strictly between 0 and 1, e.g. `0.999` |
| `[[slo]]` `threshold_ms` | Latency SLO only, above 0: a request is fast if `latency_ms <= threshold_ms` |
| `[[slo]]` `impact`, `runbook` | Customer-impact text and runbook link, copied into the alert |
| `[alerts]` `critical`, `warning` | Burn-rate rules, each with `burn_rate` (above 0), `long_window`, and a shorter `short_window`. `long_window` may not exceed `window` |
| `[alerts]` `budget_warning_remaining` | Warn when this share of the budget or less is left, e.g. `0.25`; at least 0 and below 1 |

Every field is required except `threshold_ms`, which only the latency SLO takes, and unexpected
fields are rejected, so a typo cannot pass silently. Two rules are deliberate choices:

- **Naming convention.** `service` and `environment` use ASCII letters, digits, `.`, `_` and `-`,
  starting with a letter or digit. Strictly, only `:` has to be excluded, to keep the `dedup_key`
  (`service:environment:slo`) unambiguous; the rest is a convention that keeps names predictable in
  keys, labels, file paths and command lines. Supporting existing names outside it would call for
  encoding each part of the key rather than forcing renames, which this exercise does not need.
- **Budget warning level.** At `0`, the tool warns whenever the budget is exactly exhausted or
  overspent. `1` is excluded because every evaluable window has `remaining <= 1`, so even a
  perfectly healthy service would warn.

**Request records:** JSON Lines, one request per line
([`examples/sample.jsonl`](examples/sample.jsonl)):

```json
{"timestamp": "2026-10-23T14:00:01Z", "success": true, "latency_ms": 112}
```

| Field | Rules |
|---|---|
| `timestamp` | When the request completed. ISO 8601 with a timezone (`Z` or an offset such as `+02:00`) and at most 6 fractional-second digits; converted to UTC |
| `success` | `true` or `false`, decided upstream (see below) |
| `latency_ms` | Number ≥ 0. Required when `success` is `true`; may be `null` or missing when it is `false`, because a timeout often has no latency |

`success` is classified by whatever collects the telemetry, not by this tool. Whether a 404 or a 429
counts as a failure differs per service, so the evaluator does not hard-code HTTP rules; this also
lets it work for non-HTTP traffic such as queue consumers or batch jobs. Records may be in any order,
and unknown fields are ignored.

The file is UTF-8 without a byte order mark (BOM), one JSON object per line. Accepting a BOM would be
a legitimate compatibility choice; rejecting it simply follows that format contract. A line holding
only JSON whitespace (spaces, tabs, line endings) is blank and skipped. `NaN` and `Infinity` are not
JSON numbers and are rejected. Python's `datetime.fromisoformat` silently truncates fractions of a
second beyond microseconds, so a timestamp with more than 6 fractional digits is rejected rather than
quietly changed.

In the sample, line 4 is a failure that has a latency, line 5 sits exactly on the 300 ms threshold
(it counts as fast), line 7 is slow, and line 9 is a failure without a latency.

### Evaluation

1. **Validate** both files. One invalid record rejects the whole batch, with line numbers in the
   error: silently skipping bad records could hide exactly the failures being measured.
2. **Pick the window.** It ends at the evaluation time `end`: `--at` if given, otherwise the
   current time. The evaluator itself never reads the clock, so tests and examples always pass an
   explicit time. `end` is converted to UTC before any window is subtracted, so a daylight-saving
   change cannot stretch or shrink a window. The SLO window is `(end - window, end]`, and every
   record falls into exactly one group, all three counted in the report: before the window
   (including exactly at `end - window`), inside it, or after `end`. A window too long to subtract
   from `end` (it would reach before year 1) has no lower bound.
3. **Count** valid and good requests for the SLO window and for each distinct alert window in the
   policy (5m, 30m, 1h and 6h in the example), all ending at `end`. All later maths uses only these
   counts, so the same core could take pre-aggregated counts from a monitoring system instead of
   raw records.
4. **Calculate** for each SLO:

| | Availability | Latency |
|---|---|---|
| Valid requests | All requests | Successful requests |
| Good requests | Successful | `latency_ms <= threshold_ms` |
| SLI | good ÷ valid | good ÷ valid |

- **SLO met** when `SLI >= objective`.
- **Error budget:** allowed bad requests = `(1 - objective) × valid`, and budget left =
  `1 - bad ÷ allowed`. Below zero means the budget is overspent; it is never clamped to zero.
- **Burn rate** of a window = `(bad ÷ valid) ÷ (1 - objective)`. At 1.0 the budget lasts exactly
  the SLO window; at 14.4 a 30-day budget is gone in about two days.
- **Too little data.** A window with fewer than `min_requests` valid requests, checked per SLO and
  per window, gets no SLI, budget or burn rate, and the report says why, e.g.
  `insufficient_requests: observed 3, required 10`.

The latency SLI only counts successful requests: a fast error should not make latency look better,
and errors already count against availability. During an outage, latency can therefore lack data
while availability does not.

### Severity

Each SLO is checked by three rules (values from the example policy):

| Rule | Fires when | Raises |
|---|---|---|
| `fast_burn` | Burn rate ≥ 14.4 over **both** the 1h and the 5m window | `critical` |
| `slow_burn` | Burn rate ≥ 6 over **both** the 6h and the 30m window | `warning` |
| `low_budget` | 25% or less of the error budget left in the SLO window | `warning` |

Each rule fires, does not fire, or cannot be evaluated. A burn-rate rule needs both of its windows
at or above the threshold, so one window known to be below it is enough to say the rule does not
fire, even if the other window has too few requests; the rule cannot be evaluated only when no window
rules it out and at least one lacks data. Every window that lacks data is reported as a diagnostic,
e.g. `fast_burn 5m window: insufficient_requests: observed 4, required 10`, whatever the outcome of
its rule.

An SLO's tag comes from its rules, and the overall tag is the worst SLO tag, in the order
`critical` > `warning` > `unknown` > `ok`. Stale data, for example, leaves the recent windows empty
while the SLO window can still show an overspent budget: the result is then `warning`, with the empty
windows still listed as diagnostics.

| Tag | When | Typical handling, outside this tool |
|---|---|---|
| `critical` | `fast_burn` fires | Page on-call |
| `warning` | `slow_burn` or `low_budget` fires, and `fast_burn` does not | Ticket or team channel |
| `unknown` | No rule fires, and at least one rule cannot be evaluated | Check whether requests are arriving and telemetry is flowing |
| `ok` | Every rule is known not to fire | Nothing |

- `ok` means that no alert condition is established. It does not guarantee complete telemetry, and
  the rules are not a telemetry-freshness check: if telemetry stops after healthy traffic, a long
  window can stay below its threshold while the short window empties, so the result can stay `ok`
  for a while. The diagnostics still show the gap.
- The burn-rate rules are the multiwindow, multi-burn-rate alerts from the Google SRE Workbook. The
  long window ignores short blips and the short window lets the alert clear soon after recovery,
  which is what stops the tool from paging on every transient spike.
- A low budget never escalates to `critical`: it describes the past, and would keep paging after
  recovery until the incident leaves the window.
- The tool decides **how bad** things are; deciding **who gets notified** belongs to the paging
  system.

The report also answers "is the service within its objective?" with `within_objective`: `false` if
any SLO is not met, `null` if any SLO lacks the data to judge, and `true` otherwise. It depends only
on each SLO's own window, never on whether the shorter alert windows have enough data.

### Alerts and integrations

Every SLO gets a decision with the same identifying fields, whether it alerts or not; an SLO tagged
`warning` or `critical` is an alert. A decision carries:

- `service`, `environment`, `slo`, and `evaluated_at`, which is the evaluation time, never the clock
- `severity`, `dedup_key` and `triggered_rules`
- `summary`, `impact`, `runbook` and `next_action`
- every rule's outcome with its exact measurements and configured threshold, per window (the 1h and
  5m burn rates separately, for example), and the diagnostics for windows that lack data

When several rules fire, the first in the order `fast_burn`, `slow_burn`, `low_budget` is the
primary rule. It picks the summary, the next action, and the window whose counts the impact quotes:
the long window of a burn-rate rule, or the SLO window for `low_budget`. All firing rules and
diagnostics are kept. The impact is the policy's text plus counts that match the signal: for
availability "1,240 of 59,800 requests failed in the 1h window", for latency "N of M successful
requests exceeded 300 ms in the 6h window".

| Primary rule | Next action |
|---|---|
| `fast_burn` | Investigate immediately: elevated budget consumption was observed in the recent evaluation windows. Follow the runbook. |
| `slow_burn` | Investigate soon: sustained elevated budget consumption was observed in the evaluation windows. Follow the runbook. |
| `low_budget` | Review recent incidents and budget consumption; follow the team's release/error-budget policy. |
| none (`unknown`) | Check whether requests are arriving and telemetry is flowing. |

The next actions avoid claims the data cannot support: a windowed measurement, possibly evaluated at
a past `--at`, does not prove what customers see right now, and the input does not define a release
policy.

**Integrations.** The tool is stateless: each run reports the current decisions, and the paging or
incident system owns opening, updating and resolving incidents. An integration should read the tags
as follows:

- `warning` or `critical`: a firing state for the SLO's `dedup_key`.
- `ok`: known clear, which can support resolving that key's incident.
- `unknown`: insufficient evidence, which must not be read as recovery.

An SLO that is not an alert, or a run without any alerts, therefore does not mean "resolve every
open incident". The `dedup_key` is `service:environment:slo` and deliberately leaves out severity,
rule and time, so a `warning` that becomes `critical` updates the same incident. Availability and
latency keep separate keys, but one dependency outage can affect both, so a paging system may group
alerts by service and environment to avoid two pages for one incident.

### Edge cases

- **Boundary values.** Numbers are never binary floats: whole numbers stay integers and other
  numbers are read as `Decimal`. Reading decimals exactly is not enough on its own, because decimal
  division rounds too, so SLIs, burn rates and budgets are calculated as exact fractions (e.g.
  `Fraction(good, valid)`) and compared with exactly built targets; floats appear only when the
  report is displayed. An SLI of exactly 99.9% therefore meets a 99.9% objective. Every comparison
  includes its boundary: SLI ≥ objective, burn rate ≥ threshold, budget left ≤ warning level,
  latency ≤ threshold. A record exactly at `end` is inside the window; one exactly at
  `end - window` is not.
- **Missing data.** A window with fewer than `min_requests` valid requests is not judged, and its
  reason is always reported as a diagnostic. It makes its rule impossible to evaluate, and the tag
  `unknown`, only when no other window rules that rule out and no other rule fires (see
  [Severity](#severity)). In request records, "no traffic" and "telemetry stopped" look the same, so
  an empty window is never treated as proof of either: the tool says it cannot tell instead of
  guessing. A missing `latency_ms` is fine on a failed request and invalid on a successful one.
- **Empty window.** No records in the window, or an empty file, gives `unknown`. It is never
  reported as 100% healthy, and never as a breach.
- **Invalid input** is rejected with a clear message and an exit code separate from the four tags.
  Every independent problem is reported in one go, each with its location (e.g.
  `slo[1].objective`), so a file can be fixed in one pass. A check that depends on an invalid field
  is skipped, so one mistake does not cause a cascade of messages; a TOML syntax error reports only
  the parser's first error. For records, each invalid line gets one message listing all of its
  problems, numbered by its position in the file (blank lines included). The first 10 are listed,
  and the summary counts invalid lines out of all non-blank lines, e.g. `3 of 1,204 non-blank lines
  invalid`: a line with several problems counts once, and the display limit changes neither total.
  The policy is strict, because it is written by hand and a typo should
  not pass silently; records are lenient about extra fields, so telemetry can carry more data than
  the tool needs.
  - Policy: unreadable, not UTF-8, starting with a BOM, or malformed TOML; missing or unexpected
    fields; names outside the naming convention; a duration that is not a whole number above 0 with
    one unit; non-finite numbers (`nan`, `inf`); an objective, burn rate or budget warning level too
    large or too precise to convert exactly (an exponent beyond ±100, e.g. `1e-999999999`, whose
    exact conversion would run for hours); an objective not strictly between 0 and 1; a budget
    warning level outside `0 <= level < 1`; a zero or negative threshold or burn rate; a
    `min_requests` that is not a whole number of at least 1; an unknown or duplicate SLO name, or no
    SLO at all; a short window not shorter than its long window; a long window longer than the SLO
    window.
  - Records: a line that is not valid UTF-8, starts with a BOM, is not valid JSON or is not a JSON
    object; a duplicate field (rather than letting the last value silently win); `NaN` or
    `Infinity`; a number too large to decode; a missing `timestamp` or `success`;
    a timestamp that is not ISO 8601, has no timezone, has more than 6 fractional-second digits, or
    is out of range once converted to UTC; a `success` that is not `true` or `false`; a latency that
    is negative or not a number, or missing when `success` is `true`.

### Output

A JSON report by default, or a short text summary. It contains the evaluation window and record
counts, `within_objective`, and for each SLO the SLI, objective, whether it is met, the error
budget, the burn rates and the decision described above; an `alert` block lists the SLOs that are
alerts, with the overall severity. Values stay exact until this point and are only formatted here,
as decimals and percentages. The exit code follows the overall tag: `0` ok, `1` warning,
`2` critical, `3` unknown.

### Assumptions

- The records belong to the service and environment named in the policy; the telemetry query that
  produced them already filtered them.
- Each line is one distinct request. There is no request ID, so duplicates cannot be detected.
- `timestamp` is when the request completed, i.e. when its outcome was known.
- Windows roll and end at the evaluation time; they are not aligned to calendar months.
- The error budget is counted in requests, not in "bad minutes".
