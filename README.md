# otel-budget-check

Catch observability cost and cardinality regressions **before merge**.

A GitHub Action that runs your normal test command with an OTLP capture
receiver attached, compares the telemetry your tests emit against a baseline
from your default branch, and gates the PR on your telemetry budget.

Built by [Northstar App Studio](https://github.com/northstarappstudio-ops).

## What it reports

- Added / removed metrics, spans, attribute keys
- Observed cardinality change (unique series)
- Obviously unbounded dimensions (e.g. `user_id`, `request_id` as attribute keys)
- Projected signal-volume delta (test rate × traffic multiplier × 30 days)
- Estimated monthly cost delta (volume × backend unit cost)
- **PASS / FAIL** budget gate

## Quick start

```yaml
# .github/workflows/otel-budget.yml
on:
  pull_request:
  push:
    branches: [main]

jobs:
  otel-budget-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5   # or node, etc. — whatever your tests need
        with:
          python-version: '3.11'

      - name: Cache baseline
        uses: actions/cache@v4
        with:
          path: .otel-budget-check/baseline.json
          key: otel-budget-baseline-${{ github.ref_name == 'main' && github.sha || 'pr' }}

      - uses: northstarappstudio-ops/otel-budget-check@v0.1.0
        with:
          test-command: 'pytest -q'          # your normal test command
          traffic-multiplier: '100'          # prod traffic vs test volume
          unit-cost: '1.0'                   # $ per 1M signals
          budget: '50'                       # $/mo allowed cost increase
```

On the default branch the action records the baseline. On PRs it compares and
gates: a PR that blows the budget or introduces unbounded dimensions fails the
check.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `test-command` | *(required)* | Your normal test command. |
| `baseline-path` | `.otel-budget-check/baseline.json` | Baseline capture location. |
| `current-path` | `.otel-budget-check/current.json` | Current capture (artifact). |
| `traffic-multiplier` | `100` | Prod traffic vs test volume. |
| `unit-cost` | `1.0` | $ per 1,000,000 signals (metric points + spans). |
| `budget` | `50` | $/mo allowed cost increase. |
| `cardinality-limit` | `100` | Unique values before an attribute is "unbounded". |
| `sample-interval` | `5` | Seconds of test telemetry assumed per run. |
| `settle-seconds` | `3` | Wait after tests for SDK export flush. |
| `report-path` | `.otel-budget-check/report.json` | JSON report (artifact). |

## How it works

1. The action starts a local OTLP receiver (HTTP, JSON + protobuf) on
   `127.0.0.1:4318` and injects standard OTLP env vars
   (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_METRICS_EXPORTER=otlp`,
   `OTEL_TRACES_EXPORTER=otlp`, …).
2. It runs your test command. Any OTel SDK in your stack exports to the
   receiver automatically — no code changes.
3. It compares the capture against the baseline and prints the report.
4. It sets the gate: FAIL if the projected monthly cost delta exceeds the
   budget, or if obviously unbounded dimensions appear.

## Privacy

- The OTLP receiver captures **only your own test telemetry, locally, inside
  your CI runner**. The action transmits nothing; no source code, secrets,
  customer telemetry, or repository identifiers are collected or sent.
- There is no telemetry collection in this action, disclosed or otherwise.

## License

MIT. Not affiliated with or endorsed by the OpenTelemetry project.

---

**Team — $199/month**: persistent baselines, org budgets, history, multi-repo
policies, code→signal lineage.
**Request access: northstarappstudio@gmail.com**

The free action includes the check itself; the Team plan adds the
org-level features above.