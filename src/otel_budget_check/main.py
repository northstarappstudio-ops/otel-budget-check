"""CLI: run tests with an OTLP capture, compare to baseline, gate the PR.

Usage:
  otel-budget-check run --test-command "pytest" [options]
  otel-budget-check analyze --baseline capture.json --current capture.json [options]
  otel-budget-check serve --port 4318 --out capture.json
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

from . import analyzer, receiver

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


def _env_with_otlp(endpoint):
    env = dict(os.environ)
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
    env.setdefault("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    env.setdefault("OTEL_METRIC_EXPORT_INTERVAL", "1000")
    env.setdefault("OTEL_BLRP_SCHEDULE_DELAY", "1000")
    env.setdefault("OTEL_TRACES_EXPORTER", "otlp")
    env.setdefault("OTEL_METRICS_EXPORTER", "otlp")
    return env


def cmd_run(args):
    recv = receiver.OTLPReceiver(port=args.port).start()
    try:
        print("otel-budget-check: OTLP receiver on 127.0.0.1:%d" % recv.port, flush=True)
        print("otel-budget-check: running: %s" % args.test_command, flush=True)
        proc = subprocess.run(args.test_command, shell=True,
                              env=_env_with_otlp(recv.endpoint("")),
                              cwd=args.cwd or None)
        # Give the SDK a moment to flush pending exports.
        import time
        time.sleep(args.settle_seconds)
        snap = recv.capture.snapshot()
        if proc.returncode != 0:
            print("otel-budget-check: test command failed (rc=%d); "
                  "capture saved but gate not evaluated" % proc.returncode, flush=True)
            _write_capture(args.current, snap)
            return EXIT_ERROR
    finally:
        recv.stop()

    _write_capture(args.current, snap)
    if snap["counts"]["metrics"] == 0 and snap["counts"]["spans"] == 0:
        print("otel-budget-check: WARNING: no telemetry captured. Is your app "
              "configured to export OTLP during tests?", flush=True)

    if not args.baseline or not os.path.exists(args.baseline):
        print("otel-budget-check: no baseline found; recording baseline from this run.",
              flush=True)
        if args.baseline:
            _write_capture(args.baseline, snap)
        return EXIT_PASS

    with open(args.baseline, encoding="utf-8") as fh:
        baseline = json.load(fh)
    report = analyzer.analyze(
        baseline, snap,
        traffic_multiplier=args.traffic_multiplier,
        unit_cost=args.unit_cost,
        budget=args.budget,
        cardinality_limit=args.cardinality_limit,
        sample_interval=args.sample_interval,
    )
    print(analyzer.format_report(report, verbose=args.verbose), flush=True)
    if args.report_file:
        with open(args.report_file, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print("otel-budget-check: report written to %s" % args.report_file, flush=True)
    return EXIT_FAIL if report["gate"]["status"] == "FAIL" else EXIT_PASS


def cmd_analyze(args):
    with open(args.baseline, encoding="utf-8") as fh:
        baseline = json.load(fh)
    with open(args.current, encoding="utf-8") as fh:
        current = json.load(fh)
    report = analyzer.analyze(
        baseline, current,
        traffic_multiplier=args.traffic_multiplier,
        unit_cost=args.unit_cost,
        budget=args.budget,
        cardinality_limit=args.cardinality_limit,
        sample_interval=args.sample_interval,
    )
    print(analyzer.format_report(report, verbose=args.verbose), flush=True)
    if args.report_file:
        with open(args.report_file, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return EXIT_FAIL if report["gate"]["status"] == "FAIL" else EXIT_PASS


def cmd_serve(args):
    recv = receiver.OTLPReceiver(port=args.port).start()
    print("OTLP receiver listening on 127.0.0.1:%d" % recv.port, flush=True)
    try:
        while True:
            import time
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        snap = recv.capture.snapshot()
        recv.stop()
        if args.out:
            _write_capture(args.out, snap)
            print("wrote capture to %s" % args.out, flush=True)


def _write_capture(path, snap):
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(snap, fh)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="otel-budget-check")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run tests with OTLP capture + gate")
    run.add_argument("--test-command", required=True)
    run.add_argument("--baseline", default=os.environ.get("OBC_BASELINE", ""))
    run.add_argument("--current", default=os.environ.get("OBC_CURRENT", ""))
    run.add_argument("--port", type=int, default=4318)
    run.add_argument("--cwd", default="")
    run.add_argument("--settle-seconds", type=float, default=2.0)
    run.add_argument("--traffic-multiplier", type=float,
                     default=analyzer.DEFAULT_TRAFFIC_MULTIPLIER)
    run.add_argument("--unit-cost", type=float, default=analyzer.DEFAULT_UNIT_COST)
    run.add_argument("--budget", type=float, default=analyzer.DEFAULT_BUDGET)
    run.add_argument("--cardinality-limit", type=int,
                     default=analyzer.DEFAULT_CARDINALITY_LIMIT)
    run.add_argument("--sample-interval", type=float,
                     default=analyzer.DEFAULT_SAMPLE_INTERVAL)
    run.add_argument("--report-file", default="")
    run.add_argument("--verbose", action="store_true")
    run.set_defaults(func=cmd_run)

    an = sub.add_parser("analyze", help="compare two captures")
    an.add_argument("--baseline", required=True)
    an.add_argument("--current", required=True)
    an.add_argument("--traffic-multiplier", type=float,
                    default=analyzer.DEFAULT_TRAFFIC_MULTIPLIER)
    an.add_argument("--unit-cost", type=float, default=analyzer.DEFAULT_UNIT_COST)
    an.add_argument("--budget", type=float, default=analyzer.DEFAULT_BUDGET)
    an.add_argument("--cardinality-limit", type=int,
                    default=analyzer.DEFAULT_CARDINALITY_LIMIT)
    an.add_argument("--sample-interval", type=float,
                    default=analyzer.DEFAULT_SAMPLE_INTERVAL)
    an.add_argument("--report-file", default="")
    an.add_argument("--verbose", action="store_true")
    an.set_defaults(func=cmd_analyze)

    sv = sub.add_parser("serve", help="run capture receiver only")
    sv.add_argument("--port", type=int, default=4318)
    sv.add_argument("--out", default="")
    sv.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())