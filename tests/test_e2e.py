"""End-to-end tests for otel-budget-check.

Runs the real receiver + analyzer via the CLI with a fake OTel SDK that
exports OTLP/HTTP JSON and protobuf, and verifies gate behavior.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
sys.path.insert(0, SRC)  # allow direct imports in this test process
FAKE_SDK = os.path.join(REPO, "tests", "fake_sdk.py")
PORT = 14318  # fixed to avoid ephemeral-port flakiness in CI logs


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, "-m", "otel_budget_check.main"] + args,
                          capture_output=True, text=True, env=env)


def run_fake_sdk(port, scenario, count=1):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, FAKE_SDK, "--port", str(port),
                           "--scenario", scenario, "--count", str(count)],
                          capture_output=True, text=True, env=env)


def emit(scenario, count=1):
    """Start receiver, emit via fake SDK, return capture snapshot dict."""
    from otel_budget_check.receiver import OTLPReceiver

    recv = OTLPReceiver(port=0).start()
    try:
        proc = run_fake_sdk(recv.port, scenario, count)
        assert proc.returncode == 0, proc.stderr
        time.sleep(0.5)
        return recv.capture.snapshot()
    finally:
        recv.stop()


class ReceiverTests(unittest.TestCase):
    def test_healthz(self):
        from otel_budget_check.receiver import OTLPReceiver

        recv = OTLPReceiver(port=0).start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % recv.port) as r:
                self.assertEqual(r.status, 200)
        finally:
            recv.stop()

    def test_capture_json_metrics_and_spans(self):
        snap = emit("basic")
        self.assertGreater(snap["counts"]["metrics"], 0)
        self.assertGreater(snap["counts"]["spans"], 0)
        self.assertEqual(snap["errors"], [])

    def test_capture_protobuf(self):
        snap = emit("basic-proto")
        self.assertGreater(snap["counts"]["metrics"], 0)
        self.assertGreater(snap["counts"]["spans"], 0)
        self.assertEqual(snap["errors"], [])

    def test_high_cardinality_captured(self):
        snap = emit("highcard", count=300)
        # 300 distinct user_id values must all be captured.
        self.assertEqual(snap["counts"]["metrics"], 300)
        self.assertEqual(snap["errors"], [])


class AnalyzeTests(unittest.TestCase):
    def test_clean_pr_passes(self):
        base = emit("basic")
        cur = emit("basic")
        report = analyze(base, cur)
        self.assertEqual(report["gate"]["status"], "PASS")
        self.assertEqual(report["added_metrics"], [])
        self.assertEqual(report["added_spans"], [])

    def test_added_metric_fails_budget(self):
        base = emit("basic")
        cur = emit("verbose")
        report = analyze(base, cur, budget=1.0)
        self.assertEqual(report["gate"]["status"], "FAIL")
        self.assertTrue(any("cost delta" in f for f in report["gate"]["failures"]))
        self.assertTrue(report["added_metrics"])

    def test_unbounded_dimension_fails(self):
        base = emit("basic")
        cur = emit("highcard", count=300)
        report = analyze(base, cur, cardinality_limit=100)
        self.assertEqual(report["gate"]["status"], "FAIL")
        self.assertTrue(report["unbounded_dimensions"])
        self.assertEqual(report["unbounded_dimensions"][0]["attribute"], "user_id")

    def test_removed_signals_reported(self):
        base = emit("verbose")
        cur = emit("basic")
        report = analyze(base, cur)
        self.assertTrue(report["removed_metrics"])
        self.assertGreaterEqual(report["cardinality"]["delta"], -100000)

    def test_cost_math(self):
        base = emit("basic")
        cur = emit("verbose")
        report = analyze(base, cur, traffic_multiplier=100, unit_cost=1.0)
        vol = report["volume"]
        cost = report["cost"]
        self.assertAlmostEqual(cost["delta"], vol["delta"] / 1e6, places=3)


def analyze(base, cur, **kwargs):
    from otel_budget_check import analyzer

    return analyzer.analyze(base, cur, **kwargs)


class CliTests(unittest.TestCase):
    def test_analyze_command(self):
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, "base.json")
            cur = os.path.join(td, "cur.json")
            with open(base, "w") as fh:
                json.dump(emit("basic"), fh)
            with open(cur, "w") as fh:
                json.dump(emit("verbose"), fh)
            proc = run_cli(["analyze", "--baseline", base, "--current", cur,
                            "--budget", "1", "--report-file",
                            os.path.join(td, "report.json")])
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("Gate: FAIL", proc.stdout)
            self.assertIn("estimated monthly cost delta", proc.stdout)
            self.assertTrue(os.path.exists(os.path.join(td, "report.json")))

    def test_run_command_records_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, "baseline.json")
            cur = os.path.join(td, "current.json")
            cmd = ('"%s" "%s" --port 14319 --scenario basic'
                   % (sys.executable, FAKE_SDK))
            proc = run_cli(["run", "--test-command", cmd,
                            "--baseline", base, "--current", cur,
                            "--port", "14319", "--settle-seconds", "1"])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("no baseline found; recording baseline", proc.stdout)
            self.assertTrue(os.path.exists(base))

    def test_run_command_gates_pr(self):
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, "baseline.json")
            cur = os.path.join(td, "current.json")
            cmd = ('"%s" "%s" --port 14320 --scenario basic'
                   % (sys.executable, FAKE_SDK))
            proc = run_cli(["run", "--test-command", cmd,
                            "--baseline", base, "--current", cur,
                            "--port", "14320", "--settle-seconds", "1"])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            # Now a "PR" run with verbose telemetry and a tight budget.
            cmd2 = ('"%s" "%s" --port 14321 --scenario verbose'
                    % (sys.executable, FAKE_SDK))
            proc2 = run_cli(["run", "--test-command", cmd2,
                             "--baseline", base, "--current", cur,
                             "--port", "14321", "--settle-seconds", "1",
                             "--budget", "1"])
            self.assertEqual(proc2.returncode, 1, proc2.stdout + proc2.stderr)
            self.assertIn("Gate: FAIL", proc2.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)