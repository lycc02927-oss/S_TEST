"""离线回归：不导入 cpfr、Qt、sw.cm，也不连接设备。"""

import csv
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from device_parameter_sweep.parameters import INPUT, PARAMETERS, UNAVAILABLE
from device_parameter_sweep.plan import DEFAULT_CONFIG, build_plan
from device_parameter_sweep.runner import run_sweep


class FakeManager:
    def __init__(self):
        self.data = {spec.path: spec.default for spec in PARAMETERS.values()}
        self.data.update({
            INPUT + "search_objective": "Maximum", INPUT + "max_iteration": 70,
            INPUT + "initial_sampling_num": 30, INPUT + "max_search_time": 3600,
            INPUT + "convergence_enable": False, INPUT + "convergence_tol_threshold": 0.1,
            INPUT + "search_select_ed": True, INPUT + "search_select_pf": False,
        })
        self.data[PARAMETERS["xi_init"].path] = 0.2  # 原值不同于测试基准，验证真正恢复。
        self.writes = []
        self.restores = 0

    def _load_data(self):
        return 0

    def validate_values(self, values):
        for path in values:
            if path not in self.data:
                raise AttributeError("不存在: " + path)

    def snapshot_fields(self, paths):
        return {path: {"present": True, "value": self.data[path]} for path in paths}

    def write_test_constants(self, values):
        self.writes.append(dict(values))
        self.data.update(values)
        return 0

    def read_test_constant(self, path):
        return self.data.get(path)

    def restore_fields(self, original):
        self.restores += 1
        self.data.update({path: state["value"] for path, state in original.items()})
        return 0


class FakeButton:
    def __init__(self, click):
        self.click = click

    def isEnabled(self):
        return True


class FakeFixture:
    def __init__(self, omit=None, timeout=None, reset_fails=False):
        self.state, self.round = "input", 0
        self.omit, self.timeout, self.reset_fails = omit, timeout, reset_fails
        self.result = {}
        self.app = SimpleNamespace(show=lambda: None, window=SimpleNamespace(setVisible=lambda _: None))
        self.operate_buttons = {"Start": FakeButton(self.start), "Reset": FakeButton(self.reset)}

    def at_input_state(self):
        return self.state == "input"

    def at_result_state(self):
        return self.state == "result"

    def start(self):
        self.round += 1
        self.state = "running"
        if self.timeout == self.round:
            return
        self.result = {"x": 2 * self.round}
        if self.omit != self.round:
            logging.getLogger("cpqpbop.test").info('PBOP_RESULT {"best_value": %s}', self.round * 10)
        self.state = "result"

    def reset(self):
        self.result.clear()
        if not self.reset_fails:
            self.state = "input"


class FakeQtBot:
    @staticmethod
    def waitUntil(predicate, timeout):
        if not predicate():
            raise TimeoutError("模拟等待超时")


def runtime_ok(fixture, expected):
    return dict(expected)


def runtime_stale(fixture, expected):
    values = dict(expected)
    values[PARAMETERS["xi_init"].path] = 0.5
    return values


def refresh_with_old_result(fixture, expected):
    logging.getLogger("cpqpbop.test").info('PBOP_RESULT {"best_value": 9999}')


class SweepTests(unittest.TestCase):
    def settings(self):
        settings = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        settings.update({
            "repeat": 3, "sweeps": {}, "randomize_case_order": False, "targets": ["ed"],
            "metrics": {
                "best_value": {"json_key": "best_value", "required": True},
                "best_x": {"fixture_path": "result.x", "required": True},
            },
        })
        return settings

    def execute(self, directory, fixture=None, manager=None, settings=None):
        settings = settings or self.settings()
        settings["output_dir"] = str(Path(directory) / "out")
        config = Path(directory) / "settings.json"
        config.write_text(json.dumps(settings), encoding="utf-8")
        return run_sweep(fixture or FakeFixture(), FakeQtBot(), directory,
                         config_path=config, manager_factory=lambda: manager or FakeManager())

    def output(self, directory):
        return next((Path(directory) / "out").iterdir())

    def records(self, directory):
        return [json.loads(line) for line in (self.output(directory) / "runs.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_default_plan_counts_and_one_factor_isolation(self):
        settings = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        plan = build_plan(settings)
        self.assertEqual(len(plan["cases"]), 26)
        self.assertEqual(sum(case["repeat"] for case in plan["cases"]), 78)
        baseline = {spec.path: settings["baseline"][name] for name, spec in PARAMETERS.items()}
        for case in plan["cases"]:
            changed = [path for path, value in case["constants"].items() if path in baseline and value != baseline[path]]
            self.assertEqual(len(changed), 0 if case["changed_parameter"] == "baseline" else 1)
            self.assertTrue(case["constants"][INPUT + "search_select_ed"])
            self.assertEqual(case["constants"][INPUT + "search_select_pf"], case["target"] == "ed_pf_ratio")
        self.assertIn("platform_error", plan["deferred_parameters"])
        self.assertEqual([case["name"] for case in plan["cases"]],
                         [case["name"] for case in build_plan(settings)["cases"]])

    def test_unavailable_unknown_invalid_range_bool_and_nan_are_rejected(self):
        for name in list(UNAVAILABLE) + ["typo"]:
            settings = self.settings()
            settings["sweeps"] = {name: [0.1]}
            with self.subTest(name=name), self.assertRaises(ValueError):
                build_plan(settings)
        for name, value in [("xi_init", 0.001), ("n_restarts_optimizer", True), ("min_dis", float("nan")), ("xi_final", 0.02)]:
            settings = self.settings()
            settings["sweeps"] = {name: [value]}
            with self.subTest(name=name), self.assertRaises(ValueError):
                build_plan(settings)

    def test_plateau_is_separate_and_mode_name_is_required(self):
        settings = self.settings()
        settings["plateau"]["enabled"] = True
        with self.assertRaises(ValueError):
            build_plan(settings)
        settings["plateau"]["search_objective"] = "PROJECT_CONFIRMED_PLATEAU"
        plan = build_plan(settings)
        self.assertEqual(len(plan["cases"]), 4)
        self.assertEqual(plan["deferred_parameters"], {})
        for case in plan["cases"][1:]:
            self.assertEqual(case["constants"][INPUT + "search_objective"], "PROJECT_CONFIRMED_PLATEAU")

    def test_collect_before_reset_mean_and_restore_original(self):
        manager = FakeManager()
        original = dict(manager.data)
        with tempfile.TemporaryDirectory() as directory:
            output = self.execute(directory, manager=manager)
            records = self.records(directory)
            self.assertEqual([record["best_value"] for record in records], [10, 20, 30])
            self.assertEqual([record["best_x"] for record in records], [2, 4, 6])
            self.assertTrue(all(not record["runtime_verified"] for record in records))
            with (output / "comparison.csv").open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(float(row["best_value_mean"]), 20)
            self.assertEqual(int(row["best_value_count"]), 3)
            self.assertEqual(int(row["runtime_verified_passed"]), 0)
            self.assertEqual(json.loads((output / "restoration.json").read_text())["status"], "restored")
        self.assertEqual(manager.data, original)
        self.assertEqual(manager.restores, 1)

    def test_both_targets_are_applied_reported_separately_and_restored(self):
        settings, manager = self.settings(), FakeManager()
        settings.update({"targets": ["ed", "ed_pf_ratio"], "repeat": 1})
        with tempfile.TemporaryDirectory() as directory:
            output = self.execute(directory, manager=manager, settings=settings)
            flags = [(values[INPUT + "search_select_ed"], values[INPUT + "search_select_pf"])
                     for values in manager.writes]
            self.assertEqual(flags, [(True, False), (True, True)])
            with (output / "comparison.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row["target"] for row in rows], ["ed", "ed_pf_ratio"])
            self.assertEqual([float(row["best_value_mean"]) for row in rows], [10, 20])
        self.assertTrue(manager.data[INPUT + "search_select_ed"])
        self.assertFalse(manager.data[INPUT + "search_select_pf"])

    def test_readback_mismatch_prevents_start_and_restores(self):
        class Mismatch(FakeManager):
            def write_test_constants(self, values):
                return 0
        fixture, manager = FakeFixture(), Mismatch()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AssertionError, "1 轮失败"):
                self.execute(directory, fixture, manager)
            self.assertEqual(fixture.round, 0)
            self.assertEqual(manager.restores, 1)

    def test_missing_later_path_stops_all_before_first_write(self):
        manager, settings = FakeManager(), self.settings()
        settings["sweeps"] = {"min_dis": [1.0]}
        original_validate = manager.validate_values

        def validate(values):
            original_validate(values)
            if values[PARAMETERS["min_dis"].path] == 1.0:
                raise AttributeError("模拟后续组部署字段不匹配")
        manager.validate_values = validate
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AttributeError):
                self.execute(directory, manager=manager, settings=settings)
            self.assertEqual(manager.writes, [])
            self.assertEqual(manager.restores, 0)

    def test_timeout_then_continue_and_missing_metric_do_not_reuse_results(self):
        for fixture in (FakeFixture(timeout=2), FakeFixture(omit=2)):
            settings = self.settings()
            settings["continue_on_error"] = True
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(AssertionError, "1 轮失败"):
                    self.execute(directory, fixture, settings=settings)
                records = self.records(directory)
                self.assertEqual([record["status"] for record in records], ["passed", "failed", "passed"])
                self.assertNotIn("best_value", records[1])
                with (self.output(directory) / "comparison.csv").open(encoding="utf-8-sig") as stream:
                    row = next(csv.DictReader(stream))
                self.assertEqual(int(row["best_value_count"]), 2)
                self.assertEqual(float(row["best_value_mean"]), 20)

    def test_reset_failure_stops_and_does_not_write_restore_while_running(self):
        manager, fixture = FakeManager(), FakeFixture(reset_fails=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "恢复原常数失败"):
                self.execute(directory, fixture, manager)
            self.assertEqual(fixture.round, 1)
            self.assertEqual(manager.restores, 0)
            restoration = json.loads((self.output(directory) / "restoration.json").read_text())
            self.assertEqual(restoration["status"], "failed")
            self.assertTrue((self.output(directory) / "original_constants.json").exists())

    def test_runtime_hook_rejects_stale_cache_and_records_verified_case(self):
        for hook, passes in (("runtime_ok", True), ("runtime_stale", False)):
            settings = self.settings()
            settings["hooks"]["verify_runtime"] = __name__ + ":" + hook
            settings["require_runtime_verification"] = True
            fixture = FakeFixture()
            with tempfile.TemporaryDirectory() as directory:
                if passes:
                    self.execute(directory, fixture, settings=settings)
                    self.assertTrue(all(record["runtime_verified"] for record in self.records(directory)))
                else:
                    with self.assertRaises(AssertionError):
                        self.execute(directory, fixture, settings=settings)
                    self.assertEqual(fixture.round, 0)

    def test_refresh_logs_do_not_contaminate_round_metrics(self):
        settings = self.settings()
        settings["hooks"]["before_start"] = __name__ + ":refresh_with_old_result"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AssertionError):
                self.execute(directory, FakeFixture(omit=1), settings=settings)
            self.assertNotIn("best_value", self.records(directory)[0])

    def test_invalid_metric_config_and_short_timeout_rejected_before_manager(self):
        for change in ({"metrics": {"best_value": {"log_pattern": "("}}}, {"run_timeout_ms": 2000}):
            settings = self.settings()
            settings.update(change)
            manager = FakeManager()
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(Exception):
                    self.execute(directory, manager=manager, settings=settings)
                self.assertEqual(manager.writes, [])

    def test_restore_error_makes_successful_runs_fail(self):
        manager = FakeManager()
        manager.restore_fields = lambda states: 77
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "恢复原常数失败"):
                self.execute(directory, manager=manager)


if __name__ == "__main__":
    unittest.main()
