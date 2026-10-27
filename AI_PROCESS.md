# AI-assisted development process

I used AI tools throughout this exercise. This file covers what the brief asks for: the tools, how I
worked with them, what I accepted, changed or rejected, how the result was verified, and what I
would not ship without more work.

## Tools and models

| Tool | What I used it for |
|---|---|
| Claude Opus 5.5, as a coding agent in VS Code | Reading the brief, proposing plans and options, writing the code, tests and documentation, and running lint, tests and builds locally |
| GPT-6.1 Sol | A second reviewer, chosed to act as a "human" which potentionally could have been catch technical or operational procedures that I may have been missed: reviewing the agent's plans and changes, and checking facts before I accepted them |

The work involved only the brief, this repository and synthetic data. No internal systems,
customer data or credentials went into any prompt.

## How I worked

I chose Option 3, then agreed a seven-step roadmap with the agent: skeleton and
build pipeline, design, input validation, evaluator, decisions, CLI and examples, documentation.
Every step followed the same loop:

1. The agent proposed a plan for that step only, with options and a recommendation, and stopped.
2. I reviewed the plan, with GPT-6.1 Sol as a second reviewer, and replied with my decisions and
   corrections.
3. The agent implemented exactly that, ran lint, tests and the build, reported what it had checked,
   and stopped.
4. I reviewed the diff, edited it where I wanted to, and committed and pushed it myself.

The agent never committed or pushed. Each step is its own commit on `develop`, checked by CI, with
one pull request into `main`. 

## What I accepted, changed or rejected

**Design decisions that came from me**

- Per-request records with an explicit `success` field, instead of the agent's pre-aggregated
  5-minute counts, so the evaluator never decides whether an HTTP 4xx counts as a failure.
- Severity tags (`warning`, `critical`) instead of the tool paging directly, which also brought in
  a warning when the remaining budget runs low.
- Checking the design against every requirement in the brief before writing any code. That added
  `within_objective`, an explicit alert block, and an optional latency on failed requests.

**Corrections to the agent's proposals**

| Area | The agent proposed or claimed | What I changed |
|---|---|---|
| Validation | Report "all problems" in a bad file | Report every independent problem without cascading errors; for a TOML syntax error, only the parser's first error |
| Validation | Read every number as `Decimal` | Integers stay integers and ratios use `Fraction`, because decimal division also rounds |
| Validation | A space would make the `dedup_key` ambiguous | Only `:` does; the name rule stays as a documented convention |
| Records | The JSON parser already rejects a byte order mark | Only in text: from bytes it silently strips it, so each line is now decoded explicitly as UTF-8 |
| Records | Use Python's timestamp parsing as it is | Reject more than 6 fractional-second digits, which it silently truncates |
| Records | Not considered | Reject duplicate JSON keys, where the last value silently decided whether a request succeeded |
| Evaluator | Stale data always ends as `unknown` | `warning` outranks `unknown`, gap reasons are kept, and an empty window proves neither "no traffic" nor "broken telemetry" |
| Evaluator | Binary search over sorted records | Plain counting, which is clearer and fast enough at this scale |
| Decisions | Every run updates or resolves its alert | `unknown` is not recovery: the tool reports, and the paging system owns incident state |
| Decisions | "Customers are affected right now"; a deployment freeze at 25% budget left | Wording the data supports, deferring to the team's own release policy |
| Decisions | Tests where "just below 25% left does not fire" | The budget rule fires at or below its level, so just below also fires |
| Report | Ordinary JSON numbers are good enough | A burn rate just below 14.4 becomes the float 14.4, and `threshold_ms = 1e999999999` would have become `Infinity`: decisions are authoritative, policy numbers have a range, and `NaN` and `Infinity` are refused |
| CLI | A failed run leaves `--output` untouched | "Failed" means exit 4 or 5 only; `warning`, `critical` and `unknown` are completed reports and replace the file |
| CLI | Atomic `--output` | Atomic is not durable; keep the file's permissions; refuse an output that is an input; stdout cannot be atomic |
| Examples | A 7-day latency regression | It must replace the background slowdown, not add to it, or `within_objective` flips; I worked out both variants |
| Examples | "Every 5,000th request", "uses 20% of the budget" | Pin the starting phase (26 failures, 324 slow requests) and call rounded figures approximate |

**Problems the agent caught in its own work**

- A 24-hour example window that contradicted the burn-rate thresholds, which assume 30 days.
- Defaulting the evaluation time to the latest record, which would make stale data look healthy.
  It now defaults to the current time.
- An example error rate too low to fire reliably at the example traffic level.
- An unguarded case where a huge exponent in the policy would hang the tool. I wrote the fix from
  the agent's pointers, and it added the tests and the remaining wiring.
- A formatter comment in a test file that broke my no-comments rule.

**Accepted largely as proposed**: Python with no runtime dependencies and a single-file `.pyz`
artifact; pinned dev tools and SHA-pinned CI actions; exact arithmetic with `Fraction`; the module
split and the validation layering; the multiwindow burn-rate rules from the Google SRE Workbook;
exit codes 4 and 5 for failed runs; and the seven example scenarios.

## How I verified the result

- **Automated checks.** 348 tests that need no network, lint and format checks with ruff, and CI on
  Python 3.11 and 3.13 for every push and for the pull request. `make build` runs the packaged
  artifact on the sample and requires exit code 2.
- **My own checks.** I ran `make check`, `make build` and `make run-example` locally and ran the CI
  workflow. I compared the logs, generated reports and exit codes against the documented scenario
  expectations, checked the availability and latency calculations by hand, and reviewed every
  change before committing it.
- **Fact checks.** Before accepting the agent's claims about Python behaviour or the maths, I checked
  them myself and with GPT-6.1 Sol. For example: how `json.loads` treats a byte order mark, that
  duplicate JSON keys keep the last value, that an accepted window can be too long to subtract from
  a date, and that a burn rate just below 14.4 becomes the float 14.4.
- **The agent's checks**, which it reported at every step:
  - For two fixes, it wrote the tests first and showed them failing before the fix.
  - It planted about 70 likely bugs, one at a time, in throwaway copies of the code, such as
    off-by-one window edges, a budget clamped at zero, `unknown` treated as an alert or a
    non-atomic write, and confirmed that at least one test failed for each. These were one-off
    scripts and are not part of the repository.
  - It tried Python's actual behaviour before relying on it: timezone and daylight-saving
    arithmetic, file permissions, and the command-line parser's exit codes.
  - It checked the README against reality: the two example reports in it are identical to fresh
    output, and the quick start works from a clean state.

## Limitations and risks

What I would not ship without further work:

- **Shared blind spots.** The agent wrote most of the code and most of the tests. I reviewed every
  change, and the hand calculations and fact checks are independent of it, but a misunderstanding
  shared by the code and the tests could still pass both.
- **Text rounding** in `report.py`. The threshold-aware rounding is subtle and covered by targeted
  cases only; I would add property-based tests.
- **Output file handling** in `cli.py`. It relies on POSIX behaviour for permissions, symbolic links
  and renames, does not `fsync`, and has a small race between checking that the output is not an
  input and writing it.
- **Planted-bug checks** were ad hoc. A mutation-testing tool in CI would make them repeatable.
- **Thresholds.** 14.4×, 6×, 25% and `min_requests = 10` are textbook or illustrative values. They
  need tuning against each service's real traffic, which synthetic scenarios cannot provide.
- **Scale and scope.** Raw records in memory, one service per run and no telemetry-freshness check;
  see [Known limitations and shortcuts](README.md#known-limitations-and-shortcuts) in the README.
