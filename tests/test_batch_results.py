"""离线验证采集、统计、回读和失败恢复，不启动 CPQPBOP。"""

import csv
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cpd_modules.pbop_batch import run_batch, load_config
from cpd_modules.pbop_results import RoundCapture, build_summary, numeric_summary

PATH = "PGEC_CPQPBOP_SCAN_SCENARIO.input.scan_retry_time"


class FakeManager:
    def __init__(self):
        self.data = {PATH: 1}

    def _load_data(self):
        return 0

    def write_test_constants(self, values):
        self.data.update(values)
        return 0

    def read_test_constant(self, name):
        return self.data.get(name)


class FakeButton:
    def __init__(self, callback):
        self.click = callback

    def isEnabled(self):
        return True


class FakeFixture:
    def __init__(self, fail_round=None, reset_fails=False, omit_round=None):
        self.app = SimpleNamespace(show=lambda: None, window=SimpleNamespace(setVisible=lambda _: None))
        self.state = "input"
        self.round = 0
        self.fail_round = fail_round
        self.omit_round = omit_round
        self.reset_fails = reset_fails
        self.result = {}
        self.operate_buttons = {"Start": FakeButton(self.start), "Reset": FakeButton(self.reset)}

    def at_input_state(self):
        return self.state == "input"

    def at_result_state(self):
        return self.state == "result"

    def start(self):
        self.round += 1
        self.state = "running"
        if self.fail_round == self.round:
            return
        self.result = {"x": self.round * 2}
        if self.omit_round != self.round:
            logging.getLogger("cpqpbop.test").info(
                'PBOP_RESULT {"best_value": %s}', self.round * 10
            )
        self.state = "result"

    def reset(self):
        self.result.clear()
        if not self.reset_fails:
            self.state = "input"


class FakeQtBot:
    @staticmethod
    def waitUntil(callback, timeout):
        if not callback():
            raise TimeoutError("模拟等待超时")


class BatchTests(unittest.TestCase):
    def config(self):
        return {
            "cases": [{"name": "retry_2", "repeat": 3, "constants": {PATH: 2}}],
            "metrics": {
                "best_value": {"json_key": "best_value", "required": True},
                "best_x": {"fixture_path": "result.x", "required": True},
            },
            "read_constants": [PATH],
        }

    def execute(self, directory, fixture, config=None):
        path = Path(directory) / "input.json"
        path.write_text(json.dumps(config or self.config()), encoding="utf-8")
        with patch.dict(os.environ, {"PBOP_CONFIG": str(path), "PBOP_OUTPUT_DIR": str(Path(directory) / "out")}):
            return run_batch(fixture, FakeQtBot(), path, directory, manager_factory=FakeManager)

    def records(self, directory):
        path = next((Path(directory) / "out").glob("*/runs.jsonl"))
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_collect_before_reset_and_persist_actual_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.execute(directory, FakeFixture())
            records = self.records(directory)
            self.assertEqual([r["best_value"] for r in records], [10, 20, 30])
            self.assertEqual([r["best_x"] for r in records], [2, 4, 6])
            self.assertTrue(all(r["actual_constants"][PATH] == 2 for r in records))
            with (output / "summary.csv").open(encoding="utf-8-sig", newline="") as stream:
                summary = next(csv.DictReader(stream))
            self.assertEqual(float(summary["best_value_mean"]), 20)
            self.assertEqual(float(summary["best_value_std"]), 10)
            self.assertEqual(int(summary["passed"]), 3)
            self.assertEqual(len(list((output / "logs").glob("*.log"))), 3)

    def test_timeout_is_saved_then_safe_next_round_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FakeFixture(fail_round=2)
            with self.assertRaisesRegex(AssertionError, "1 轮失败"):
                self.execute(directory, fixture)
            records = self.records(directory)
            self.assertEqual([r["status"] for r in records], ["passed", "failed", "passed"])
            self.assertFalse(records[1]["result_reached"])
            self.assertIn("TimeoutError", records[1]["error"])
            self.assertTrue(records[1]["reset_ok"])

    def test_missing_required_metric_does_not_reuse_previous_round(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AssertionError, "1 轮失败"):
                self.execute(directory, FakeFixture(omit_round=2))
            records = self.records(directory)
            self.assertNotIn("best_value", records[1])
            self.assertIn("best_value", records[1]["metric_errors"])
            summary = build_summary(records, self.config())[0]
            self.assertEqual(summary["best_value_count"], 2)
            self.assertEqual(summary["best_value_mean"], 20)

    def test_reset_failure_stops_remaining_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FakeFixture(reset_fails=True)
            with self.assertRaisesRegex(AssertionError, "1 轮失败"):
                self.execute(directory, fixture)
            records = self.records(directory)
            self.assertEqual(len(records), 1)
            self.assertFalse(records[0]["reset_ok"])
            self.assertEqual(fixture.round, 1)

    def test_readback_mismatch_prevents_start(self):
        class MismatchManager(FakeManager):
            def write_test_constants(self, values):
                return 0
        with tempfile.TemporaryDirectory() as directory:
            fixture = FakeFixture()
            with patch(__name__ + ".FakeManager", MismatchManager):
                with self.assertRaises(AssertionError):
                    self.execute(directory, fixture)
            self.assertEqual(fixture.round, 0)
            self.assertTrue(all("回读不一致" in r["error"] for r in self.records(directory)))

    def test_failed_values_are_excluded_from_summary(self):
        config = self.config()
        rows = [
            {"case_index": 1, "status": "passed", "duration_s": 2, "best_value": 0},
            {"case_index": 1, "status": "failed", "duration_s": 100, "best_value": 999},
        ]
        summary = build_summary(rows, config)[0]
        self.assertEqual(summary["pass_rate"], 0.5)
        self.assertEqual(summary["best_value_mean"], 0)
        self.assertEqual(summary["best_x_count"], 0)
        self.assertEqual(summary["best_x_mean"], "")
        self.assertEqual(numeric_summary([])["mean"], "")

    def test_regex_and_malformed_values_are_recorded(self):
        metrics = {"ed": {"log_pattern": r"ED=(?P<value>\S+)", "group": "value", "required": True}}
        with tempfile.TemporaryDirectory() as directory:
            capture = RoundCapture(Path(directory) / "log.txt", metrics)
            try:
                capture.emit(logging.makeLogRecord({"msg": "ED=1.2e-3", "name": "cpqpbop"}))
                self.assertEqual(capture.collect(None)[0]["ed"], 0.0012)
                capture.emit(logging.makeLogRecord({"msg": "ED=nan", "name": "cpqpbop"}))
                values, errors, missing = capture.collect(None)
                self.assertEqual(values, {})
                self.assertIn("ed", errors)
                self.assertEqual(missing, ["ed"])
            finally:
                capture.close()

    def test_invalid_regex_is_rejected_before_hardware_work(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config()
            config["metrics"] = {"ed": {"log_pattern": "ED=([0-9]+)", "group": "missing"}}
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "捕获组"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
