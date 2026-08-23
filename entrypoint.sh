#!/bin/sh
set -eu

TEST_COMMAND="$1"
BASELINE_PATH="$2"
CURRENT_PATH="$3"
TRAFFIC_MULTIPLIER="$4"
UNIT_COST="$5"
BUDGET="$6"
CARDINALITY_LIMIT="$7"
SAMPLE_INTERVAL="$8"
SETTLE_SECONDS="$9"
REPORT_PATH="${10}"

export OBC_BASELINE="$BASELINE_PATH"
export OBC_CURRENT="$CURRENT_PATH"

python -m otel_budget_check.main run \
  --test-command "$TEST_COMMAND" \
  --baseline "$BASELINE_PATH" \
  --current "$CURRENT_PATH" \
  --traffic-multiplier "$TRAFFIC_MULTIPLIER" \
  --unit-cost "$UNIT_COST" \
  --budget "$BUDGET" \
  --cardinality-limit "$CARDINALITY_LIMIT" \
  --sample-interval "$SAMPLE_INTERVAL" \
  --settle-seconds "$SETTLE_SECONDS" \
  --report-file "$REPORT_PATH"