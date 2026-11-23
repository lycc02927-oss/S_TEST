"""用用户提供的实际日志格式验证最佳观测值、位置和迭代编号的提取。"""

import json
import logging
import tempfile
import unittest
from pathlib import Path

from device_parameter_sweep.log_capture import ObservationCapture


def measure(value, x=9.32):
    return ("Measure result: MeasureResult(plasma_pos_setpoint=PlasmaPosition(x={}, y=11.26, z=-9.08), "
            "plasma_pos_mp=PlasmaPosition(x={}, y=11.259999999999998, z=-9.079999999999998), "
            "target_value={})").format(x, x, value)


def iteration(index, value, x=9.32):
    return "Update iterations: {}, plasma position:PlasmaPosition(x={}, y=11.26, z=-9.08), ed energy: {}.".format(index, x, value)


class LogCaptureTests(unittest.TestCase):
    def capture(self, directory, prefixes=()):
        metrics = {key: {"observation_key": key, "required": key == "best_value"}
                   for key in ("best_value", "best_x", "best_y", "best_z", "best_mp_y", "last_iteration")}
        return ObservationCapture(Path(directory) / "raw.log", metrics, prefixes, Path(directory) / "history.jsonl")

    def emit(self, capture, message, name="CPQPBOP"):
        capture.emit(logging.makeLogRecord({"name": name, "msg": message}))

    def test_user_log_sample_extracts_target_setpoint_measured_position_and_iteration(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory)
            try:
                self.emit(capture, measure(0.5660453284463942))
                self.emit(capture, iteration(62, 0.5660453284463942))
                values, errors, missing = capture.collect(None)
                self.assertEqual(values["best_value"], 0.5660453284463942)
                self.assertEqual([values["best_x"], values["best_y"], values["best_z"]], [9.32, 11.26, -9.08])
                self.assertEqual(values["best_mp_y"], 11.259999999999998)
                self.assertEqual(values["last_iteration"], 62)
                self.assertEqual((errors, missing), ({}, []))
            finally:
                capture.close()
            events = [json.loads(line) for line in (Path(directory) / "history.jsonl").read_text().splitlines()]
            self.assertEqual([event["event"] for event in events], ["measurement", "iteration"])

    def test_best_is_maximum_over_measurements_not_last_sample_or_update_energy(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory)
            try:
                self.emit(capture, measure(0.8, x=1.0))
                self.emit(capture, measure(0.5, x=2.0))
                self.emit(capture, iteration(62, 999.0, x=2.0))
                values, _, _ = capture.collect(None)
                self.assertEqual((values["best_value"], values["best_x"]), (0.8, 1.0))
            finally:
                capture.close()

    def test_pasted_whitespace_inside_numeric_fields_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory)
            try:
                text = measure(0.5660453284463942).replace("-9.08)", "-9    .08)").replace("0.5660453284463942", "0.56604532844639     42")
                self.emit(capture, text)
                self.emit(capture, iteration(62, 0.5660453284463942).replace("ed energy", "ed en      ergy"))
                values, _, missing = capture.collect(None)
                self.assertEqual(values["best_value"], 0.5660453284463942)
                self.assertEqual(values["best_z"], -9.08)
                self.assertEqual(values["last_iteration"], 62)
                self.assertEqual(missing, [])
            finally:
                capture.close()

    def test_duplicate_logs_do_not_change_best_or_reported_iteration(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory)
            try:
                for _ in range(2):
                    self.emit(capture, measure(0.6))
                    self.emit(capture, iteration(62, 0.6))
                values, _, _ = capture.collect(None)
                self.assertEqual(values["best_value"], 0.6)
                self.assertEqual(values["last_iteration"], 62)
            finally:
                capture.close()

    def test_no_measurement_does_not_invent_best_from_update_line(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory)
            try:
                self.emit(capture, iteration(62, 0.7))
                values, errors, missing = capture.collect(None)
                self.assertNotIn("best_value", values)
                self.assertEqual(missing, ["best_value"])
                self.assertIn("best_value", errors)
            finally:
                capture.close()

    def test_logger_filter_and_nonfinite_measurement_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory, ("CPQPBOP",))
            try:
                self.emit(capture, measure(1234), name="other")
                self.emit(capture, measure("nan"))
                values, errors, missing = capture.collect(None)
                self.assertNotIn("best_value", values)
                self.assertIn("observation_log", errors)
                self.assertEqual(missing, ["best_value"])
            finally:
                capture.close()


if __name__ == "__main__":
    unittest.main()
