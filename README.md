# slo-eval

A small CLI that evaluates service reliability data against SLO targets: it calculates
availability and latency SLIs, the remaining error budget, and whether to raise an alert.

Built for the EOC SRE take-home exercise, **Option 3: SLO, Error-Budget, and Alert Evaluator**.
How AI tools were used, and how the result was verified: [AI_PROCESS.md](AI_PROCESS.md).

## Project layout

The tool is a pipeline of small steps: `load_policy` and `load_records` → `evaluate` → `decide` →
`render_json` or `render_text`. Only the two ends touch files, the clock or the process; the
evaluation and the decision are pure functions of their inputs.

| Path | Contents |
|---|---|
| `src/slo_eval/policy.py` | Reads and checks the service policy (TOML) |
| `src/slo_eval/records.py` | Reads and checks the request records (JSON Lines) |
| `src/slo_eval/evaluator.py` | SLIs, error budgets and burn rates at one evaluation time |
| `src/slo_eval/decision.py` | Rule outcomes, severity tags, diagnostics and alerts |
| `src/slo_eval/report.py` | JSON and text reports |
| `src/slo_eval/cli.py` | Command line, evaluation time, output delivery and exit codes |
| `src/slo_eval/errors.py` | The input error type, and how bad values are shown in messages |
| `scripts/generate_examples.py` | Generates the seven example scenarios; not part of the artifact |
| `examples/` | The example policy and a 12-line sample; generated scenarios go to `examples/scenarios/` |
| `tests/` | One test module per source module, plus `test_examples.py` for the scenarios end to end |

## Requirements

- Python 3.11+ (the tool itself has no third-party runtime dependencies)
- `make`

## Quick start

```bash
make check                      # creates .venv with pinned dev tools, lints, runs the tests
make build                      # builds dist/slo-eval.pyz and smoke-tests it
make run-example                # generates the seven example scenarios and evaluates each
./dist/slo-eval.pyz --policy examples/policy.toml --records examples/sample.jsonl \
    --at 2026-10-23T14:00:57Z --format text   # exits 2: critical
```

The artifact is a single file that runs anywhere with Python 3.11+, as `./dist/slo-eval.pyz` or
`python3 dist/slo-eval.pyz`. `pip install .` installs the same tool as a `slo-eval` command.

## Usage

```
slo-eval --policy PATH --records PATH [--at TIME] [--format json|text] [--output PATH]
```

| Option | Meaning |
|---|---|
| `--policy PATH` | Service policy (TOML) |
| `--records PATH` | Request records (JSON Lines), or `-` to read them from stdin |
| `--at TIME` | Evaluation time: ISO 8601 with a timezone, same rules as record timestamps. Default: now |
| `--format json\|text` | Report format. Default: `json` |
| `--output PATH` | Write the report to this file instead of stdout |

Without `--at`, the evaluation time is the current time, so the result depends on when the tool
runs; pass `--at` for reproducible runs, as the tests and examples do.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Completed report: `ok` |
| `1` | Completed report: `warning` |
| `2` | Completed report: `critical` |
| `3` | Completed report: `unknown` |
| `4` | Cannot evaluate as requested: invalid policy, records or arguments, an output path that is an input, or a report that cannot be delivered |
| `5` | Internal error, i.e. a bug; details on stderr |

Codes 1 to 3 are completed reports, not failures, and are written exactly like `0`; only 4 and 5
mean the run failed. Command-line mistakes exit 4 rather than Python's default 2, which would read
as `critical`, and crashes exit 5 rather than the default 1, which would read as `warning`.

**Output guarantees**

- No report is emitted before validation, evaluation, decision and rendering have all completed,
  so a failed run never produces a partial report.
- `--output` is replaced atomically: the report is written to a temporary file in the same
  directory and renamed over the target, so readers see either the previous report or the new one.
  This is not crash-durable storage; a power loss around the rename is not covered.
- A replaced file keeps its permission bits, and a new one gets the usual permissions from the
  umask, so a dashboard running as another user can still read it.
- An `--output` that is the policy or the records file, even through a link, is refused.
- When a run fails, a previous `--output` file is left untouched; consumers should check its
  `evaluated_at` to notice a stale report.
- Delivery to stdout cannot be atomic: if the reader goes away part way, e.g. a closed pipe, the
  run exits 4.

## Examples

```bash
make examples                        # writes examples/scenarios/*.jsonl, ~10 MB each, not committed
make run-example                     # evaluates all seven with the built artifact
make run-example SCENARIO=2-outage   # evaluates one
```

`make run-example` evaluates at a fixed time, `2026-10-23T14:00:00Z`, prints each text report with
its exit code, accepts the completed-report codes 0 to 3, and fails on 4 or 5 or on an unknown
scenario name. [`scripts/generate_examples.py`](scripts/generate_examples.py) writes the scenarios;
it uses only the standard library and is not part of the artifact.

**Shared traffic.** One request every 20 seconds covers exactly the 30-day window: the first at
`end - 30d + 20s`, the last at `end`, 129,600 in total, so every 5-minute window holds 15 requests,
every 30-minute window 90 and every hour 180. Everything is positioned relative to `end`, so a
different `--end` shifts the timestamps but never the counts.

- Background failures: requests 0, 5,000, 10,000, … counted from the oldest, 26 in total, using
  about 20.06% of the availability budget.
- Background slow requests: every 400th successful request, starting with the first, 324 in total,
  using about 25.005% of the latency budget. Failures take precedence: a failed request never
  counts toward the slow cadence.
- A scenario's own pattern replaces the background pattern of the same kind during its period.
- Latency values come from a seeded generator using only `random()`, whose sequence Python keeps
  stable across versions: 40 to 250 ms for fast requests, 301 to 2,000 ms for slow ones. Failures
  and slow requests follow fixed patterns, so every count, and every result below, is exact.

| Scenario | What happens | Result | Exit |
|---|---|---|---|
| `1-healthy` | Background traffic only | Both SLOs `ok`, within objective | 0 |
| `2-outage` | Every request fails for the last 45 minutes: 135 failures | Availability `critical`: 750× burn over 1h, 1,000× over 5m. Latency `ok`: its 1h window still holds 45 successes, which rules the fast burn out, and its empty 5m and 30m windows are reported as gaps | 2 |
| `3-elevated-errors` | One failure every 30 minutes for the last 8 hours, about 1.1% | Availability `warning` from the slow burn, exactly 100/9 ≈ 11.11× over 6h and 30m. The fast burn is ruled out because the 1h burn is also ≈11.11×, below 14.4×. Still within objective | 1 |
| `4-latency-regression` | A release 7 days ago makes every 30th successful request slow, replacing the background pattern | Latency `warning` from the low budget alone: 1,257 slow requests leave about 2.99% of the budget. Burn is about 3.33× over 30m, 1h and 6h and 0× over 5m, where no slow request falls (one would make it about 6.67×), all below both burn thresholds. SLI about 99.03%, still within objective | 1 |
| `5-past-incident` | 2 hours at 50% failures starting 10 days ago, 180 failures, since recovered | Availability `warning`: 206 failures against 129.6 allowed overspend the budget, but the recent windows are clean, so nothing pages | 1 |
| `6-telemetry-stopped-20m` | The newest 60 records, 20 minutes, are missing | Both SLOs `ok`, each listing an empty 5m window | 0 |
| `7-telemetry-stopped-2h` | The newest 360 records, 2 hours, are missing | Both SLOs `unknown`: both fast-burn windows are empty | 3 |

**Telemetry gaps.** Scenarios 6 and 7 show what happens when records stop arriving. After 20
minutes, the 1h window still holds healthy data, which rules the fast burn out, so both SLOs stay
`ok` and the empty 5m window is listed as a gap. After 2 hours, both fast-burn windows are empty,
the rule cannot be evaluated, and the result is `unknown` with exit code 3. `ok` therefore means
that no alert condition is established, not that telemetry is complete: detecting stale telemetry
quickly belongs to a separate freshness check, which could use these gap diagnostics.

**Low traffic.** Scenario 3 originally used about 0.8% errors, but at 90 requests per 30 minutes
that expects only 0.72 failures per window, so many 30-minute windows would hold none and rule the
slow burn out. With little traffic, short windows are coarse.

Two of the reports, as `make run-example` prints them. The outage needs action now:

```
checkout-api (prod): CRITICAL at 2026-10-23T14:00:00Z
window 30d: 129,600 records in it, 0 before it, 0 after its end
within objective: no

availability: CRITICAL
  SLI 99.876% of 129,600 requests (objective 99.9%, missed)
  fast_burn   firing           1h burn 750.00×, 5m burn 1,000.00× (threshold 14.4×)
  slow_burn   firing           6h burn 125.00×, 30m burn 1,000.00× (threshold 6×)
  low_budget  firing           30d budget left -24.2% (threshold 25%)
  impact: Customers see errors when checking out and cannot complete their order. 135 of 180 requests failed in the 1h window.
  next action: Investigate immediately: elevated budget consumption was observed in the recent evaluation windows. Follow the runbook.
  runbook: https://runbooks.example.com/checkout-api#availability
  dedup key: checkout-api:prod:availability

latency: OK
  SLI 99.750% of 129,439 successful requests (objective 99%, met)
  fast_burn   not_firing       1h burn 0.00×, 5m no data (insufficient_requests: observed 0, required 10) (threshold 14.4×)
  slow_burn   not_firing       6h burn 0.21×, 30m no data (insufficient_requests: observed 0, required 10) (threshold 6×)
  low_budget  not_firing       30d budget left 75.0% (threshold 25%)
  runbook: https://runbooks.example.com/checkout-api#latency
  dedup key: checkout-api:prod:latency
  gap: fast_burn 5m window: insufficient_requests: observed 0, required 10
  gap: slow_burn 30m window: insufficient_requests: observed 0, required 10
exit code: 2
```

The latency regression is a warning while the SLO is still met:

```
checkout-api (prod): WARNING at 2026-10-23T14:00:00Z
window 30d: 129,600 records in it, 0 before it, 0 after its end
within objective: yes

availability: OK
  SLI 99.980% of 129,600 requests (objective 99.9%, met)
  fast_burn   not_firing       1h burn 0.00×, 5m burn 0.00× (threshold 14.4×)
  slow_burn   not_firing       6h burn 0.00×, 30m burn 0.00× (threshold 6×)
  low_budget  not_firing       30d budget left 79.9% (threshold 25%)
  runbook: https://runbooks.example.com/checkout-api#availability
  dedup key: checkout-api:prod:availability

latency: WARNING
  SLI 99.030% of 129,574 successful requests (objective 99%, met)
  fast_burn   not_firing       1h burn 3.33×, 5m burn 0.00× (threshold 14.4×)
  slow_burn   not_firing       6h burn 3.33×, 30m burn 3.33× (threshold 6×)
  low_budget  firing           30d budget left 3.0% (threshold 25%)
  impact: Checkout is slow; customers may abandon their cart. 1,257 of 129,574 successful requests exceeded 300 ms in the 30d window.
  next action: Review recent incidents and budget consumption; follow the team's release/error-budget policy.
  runbook: https://runbooks.example.com/checkout-api#latency
  dedup key: checkout-api:prod:latency
exit code: 1
```

## Tests

```bash
make test                                            # the whole suite, about 15 seconds
.venv/bin/python -m pytest tests/test_evaluator.py   # one module
```

348 tests, none of which need a network or a live service. Most of the time goes to
`test_examples.py`, which generates the seven 30-day scenarios once per run, in pytest's temporary
directories, and evaluates them. Expected values are written out by hand, as exact fractions where
the maths is exact, rather than derived from the code under test.

| Case | What is checked | Where |
|---|---|---|
| Passing | An SLO met with budget left; the healthy scenario | `test_evaluator.py`, `test_examples.py` |
| Failing | An SLO missed, with an overspent budget that is never clamped; the outage and past-incident scenarios | `test_evaluator.py`, `test_examples.py` |
| Boundaries | An SLI exactly at its objective; a latency exactly at the threshold; a burn rate of 14.4 fires and 14.39 does not; 25% and 24.99% budget left fire and 25.01% does not; a record exactly at `end` is in and one exactly at `end - window` is out; exactly `min_requests` is judged and one fewer is not; a daylight-saving change | `test_evaluator.py`, `test_decision.py` |
| Missing data and empty windows | No records, or only records outside the window; latency during a full outage; one burn-rate window empty, in each direction; stale data; telemetry stopping for 20 minutes and for 2 hours | `test_evaluator.py`, `test_decision.py`, `test_examples.py` |
| Invalid input | Every policy rule, with exact messages and no cascades; every record rule, with line numbers and totals; a BOM, duplicate keys, `NaN`, numbers too large or too precise; a bad `--at` | `test_policy.py`, `test_records.py`, `test_cli.py` |
| Decisions and alerts | Each tag and rule; rule precedence; summary, impact, next action and `dedup_key`; every SLO identifiable whether it alerts or not | `test_decision.py` |
| Reports | JSON fields; no `NaN` or `Infinity`; a value that becomes the threshold as a float keeping its exact decision; text that keeps each value on the correct side of its threshold | `test_report.py` |
| CLI and output | Exit codes 0 to 5, including usage errors (4, not 2) and crashes (5, not 1); `--output` replaced for every completed report and left untouched by a failed run; permissions; inputs never overwritten; a closed pipe; stdin | `test_cli.py` |
| End to end | Each scenario's record counts, exact measurements, gaps, decisions and exit codes | `test_examples.py` |
| Artifact | `make build` runs the packaged `.pyz` on the sample and requires exit code 2 | `Makefile` |

## Make targets

| Target         | Description                                                           |
|----------------|-----------------------------------------------------------------------|
| `make help`    | List available targets (default)                                      |
| `make install` | Create `.venv` with the pinned dev tools from `requirements-dev.txt`  |
| `make fmt`     | Auto-format code and apply safe lint fixes                            |
| `make lint`    | Check lint and formatting without changing files                      |
| `make test`    | Run the test suite                                                    |
| `make check`   | `lint` + `test` (what CI runs)                                        |
| `make build`   | Build the single-file artifact `dist/slo-eval.pyz` and smoke-test it; the sample must exit 2 (critical) |
| `make examples` | Generate the seven example scenarios into `examples/scenarios` (not committed) |
| `make run-example` | Evaluate every scenario with the artifact, or one with `SCENARIO=name`; fails on exit code 4 or 5 |
| `make clean`   | Remove build outputs, caches and generated examples (keeps `.venv`)   |

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
fields are rejected, so a typo cannot pass silently. Three rules are deliberate choices:

- **Naming convention.** `service` and `environment` use ASCII letters, digits, `.`, `_` and `-`,
  starting with a letter or digit. Strictly, only `:` has to be excluded, to keep the `dedup_key`
  (`service:environment:slo`) unambiguous; the rest is a convention that keeps names predictable in
  keys, labels, file paths and command lines. Supporting existing names outside it would call for
  encoding each part of the key rather than forcing renames, which this exercise does not need.
- **Budget warning level.** At `0`, the tool warns whenever the budget is exactly exhausted or
  overspent. `1` is excluded because every evaluable window has `remaining <= 1`, so even a
  perfectly healthy service would warn.
- **Number range.** Every number has at most 100 digits after the decimal point and a magnitude
  below 10^101. This keeps exact conversion cheap (converting `1e-999999999` exactly would take
  hours) and keeps every value a finite JSON number (`threshold_ms = 1e999999999` would otherwise
  become `Infinity`).

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
    one unit; non-finite numbers (`nan`, `inf`); a number outside the number range (above), e.g.
    `objective = 1e-999999999` or `threshold_ms = 1e999999999`; an objective not strictly between
    0 and 1; a budget warning level outside `0 <= level < 1`; a zero or negative threshold or burn
    rate; a `min_requests` that is not a whole number of at least 1; an unknown or duplicate SLO
    name, or no SLO at all; a short window not shorter than its long window; a long window longer
    than the SLO window.
  - Records: a line that is not valid UTF-8, starts with a BOM, is not valid JSON or is not a JSON
    object; a duplicate field (rather than letting the last value silently win); `NaN` or
    `Infinity`; a number too large to decode; a missing `timestamp` or `success`;
    a timestamp that is not ISO 8601, has no timezone, has more than 6 fractional-second digits, or
    is out of range once converted to UTC; a `success` that is not `true` or `false`; a latency that
    is negative or not a number, or missing when `success` is `true`.

### Output

A JSON report by default, or a short text report with `--format text`. The JSON report starts with
`schema_version`, the version of the report format, kept separate from the tool's own version under
`generator`. Then come the evaluation window and record counts, the overall `severity`,
`within_objective`, an `alert` block, and one entry per SLO with its numbers and decision. Each SLO
entry repeats `service`, `environment` and `evaluated_at`, so it can be forwarded on its own.

Values stay exact until they are written here:

- **JSON** uses ordinary numbers without deliberate rounding, but a float cannot always hold the
  exact value: a burn rate just below 14.4 can become the same float as 14.4. Consumers must treat
  `met`, rule `outcome` and `severity` as authoritative, not recompute decisions from the numbers.
  JSON never contains `NaN` or `Infinity`: the number range keeps every value finite, and the
  writer refuses non-finite values as a last guard.
- **Text** rounds for reading, working from the exact values. When rounding would hide which side
  of a threshold a value is on, it shows up to four more digits; beyond that it says so instead,
  e.g. `1h burn ≈ 14.40× (below threshold)`. This works in both directions: a burn rate fires at
  or above its threshold, the remaining budget at or below its level, and an SLI meets its
  objective at or above it.

The exit code follows the overall tag, `0` ok, `1` warning, `2` critical, `3` unknown; see
[Usage](#usage) for runs that fail.

### Assumptions

- The records belong to the service and environment named in the policy; the telemetry query that
  produced them already filtered them.
- Each line is one distinct request. There is no request ID, so duplicates cannot be detected.
- `timestamp` is when the request completed, i.e. when its outcome was known.
- Windows roll and end at the evaluation time; they are not aligned to calendar months.
- The error budget is counted in requests, not in "bad minutes".

## Running it in production

This is how the output could feed a dashboard, a paging system and an incident workflow without
paging on every transient data point.

**Scheduling.** A scheduler such as cron, a Kubernetes CronJob or a CI schedule runs the tool every
few minutes without `--at`, so each run evaluates the current time, and pipes in the records from a
telemetry query:

```bash
query-telemetry --service checkout-api --env prod --last 30d \
  | slo-eval --policy policies/checkout-api-prod.toml --records - \
      --output /var/lib/slo/checkout-api-prod.json
```

`query-telemetry` stands for whatever exports the records. Exit codes 0 to 3 are decisions and feed
alerting; 4 and 5 mean the job itself failed, which goes to whoever owns the SLO tooling rather than
to the service's on-call.

**Dashboards.** The `--output` file is replaced atomically, so a dashboard can read it at any time
and show the severity, budget, burn rates and gap diagnostics, using `evaluated_at` to flag a stale
report. For trends, each run can also publish its numbers as metrics labelled by service,
environment and SLO, such as the SLI, the budget left and the burn rate per window.

**Paging and incidents.** Each SLO decision becomes one event keyed by its `dedup_key`, read as
described in [Alerts and integrations](#alerts-and-integrations): `critical` pages, `warning` opens
or updates a ticket, `ok` can resolve, and `unknown` changes nothing. A severity change updates the
same incident, and the paging system can group a service's SLOs so that one outage sends one page.

**Not paging on every transient data point.**

- A burn-rate rule needs both its long and its short window at the threshold, so a 5-minute spike
  alone cannot fire it, and the alert clears soon after recovery.
- Only `fast_burn` pages; a slower burn or a low budget becomes a ticket.
- A window with fewer than `min_requests` requests has no burn rate, so a handful of requests cannot
  fire a rule.
- `unknown` never pages and never resolves.
- The tool is stateless, so anything that needs history, such as requiring two consecutive
  `critical` runs before paging, belongs in the paging system.

**Watching the watcher.** A broken evaluator looks like silence, so alert on the job itself: when
the report's `evaluated_at` is older than a few intervals, and when runs keep exiting with 4 or 5.
Because `ok` does not prove that telemetry is complete, a separate freshness check should alert
when records stop arriving.

## What a production version would add

- **Counts instead of raw records.** Holding 30 days of raw requests does not scale, and monitoring
  systems already store counts. The evaluation only needs valid and good counts per SLO and window,
  so the change is an adapter behind a small interface such as `counts(slo, start, end)`,
  implemented by today's records reader and by a monitoring-system client: for example, Prometheus
  queries over request counters and latency histogram buckets, where `threshold_ms` must match a
  bucket boundary. The rules, decisions and reports stay the same, and tests keep using in-memory
  fakes, so the CLI stays easy to test.
- **Time-series storage of results**, so budget trends, time to exhaustion and past decisions can be
  queried during and after an incident.
- **Many services per run**, from a directory of policies, where one bad policy fails only its own
  service.
- **Configuration validation in CI.** A `--check-policy` mode run on every change to the policies,
  so a mistake fails review instead of a 3 a.m. evaluation, and ownership and escalation details in
  each policy.
- **A telemetry-freshness alert**, separate from the SLO rules.
- **More rules and windows:** a third, long-term ticket rule such as 1× over 3 days, as in the Google
  SRE Workbook; calendar-month windows for reporting; per-endpoint or per-customer-tier SLOs.
- **Request IDs**, so duplicate records can be detected.
- **Packaging and operation:** a wheel or container image, and structured logs and metrics for the
  tool's own runs.

## Known limitations and shortcuts

- **Raw records in memory.** Every record is loaded, and each window is a separate pass over them.
  A 30-day scenario of 129,600 records takes about 0.7 seconds and 57 MB at peak, but a service at
  100 requests per second produces about 259 million records in 30 days, far more than this design
  should hold in memory. Counts from a monitoring system, above, remove the limit.
- **One service and environment per run**, with rolling windows only. Budgets are counted in
  requests, not in "bad minutes", so an outage during a quiet hour costs less budget than the same
  outage during a busy one.
- **Trusted inputs.** `success` is decided upstream, records are assumed to be filtered to the
  policy's service and environment, and duplicates cannot be detected without request IDs.
- **One `min_requests` for every window.** At low traffic, short windows are coarse or lack data,
  so a quiet night can leave the 5-minute window unevaluable. A minimum per window, or one scaled to
  the window's length, would fit better.
- **`ok` is not a freshness check.** It means that no alert condition is established; telemetry
  that stops can stay `ok` for a while, as scenario 6 shows.
- **Output.** `--output` is replaced atomically but not durably: there is no `fsync`, so a power
  loss around the rename is not covered. Delivery to stdout cannot be atomic. JSON numbers are
  floats, so consumers must use the decisions rather than recompute them.
- **Deliberate input limits.** Policy numbers have at most 100 decimal places and stay below
  10^101, timestamps have at most 6 fractional-second digits, and both files are UTF-8 without a
  BOM.
- **Platforms.** Developed on macOS; CI runs Ubuntu with Python 3.11 and 3.13 only. Windows is not
  tested, and its file permissions and symbolic links behave differently.
- **Synthetic examples.** Fixed failure and slow-request patterns keep every example exact, but they
  are not realistic traffic.

## Time spent

About 4 to 5 hours across 3 days. The work went in small, reviewed steps,
each its own commit on `develop`.
