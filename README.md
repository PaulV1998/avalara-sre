# slo-eval

A small CLI that evaluates service reliability data against SLO targets: it calculates
availability and latency SLIs, the remaining error budget, and whether to raise an alert.

Built for the EOC SRE take-home exercise, **Option 3: SLO, Error-Budget, and Alert Evaluator**.

> Work in progress: design, usage, example output and known limitations will be documented
> as the tool is built.

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
