"""设备循环：预检全部方案 -> 备份 -> 写入/回读 -> Start/采集/Reset -> 恢复。"""

import importlib
import json
import logging
import math
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from cpd_modules.pbop_results import ResultWriter, RoundCapture
from device_parameter_sweep.plan import DEFAULT_CONFIG, load_settings, prepare_config

logger = logging.getLogger(__name__)


def equal_value(actual, expected):
    if type(expected) in (bool, str):
        return type(actual) is type(expected) and actual == expected
    return type(actual) in (int, float) and math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-9)


def check_return(code, ok_code, action):
    if code != ok_code:
        raise RuntimeError("{}失败，err_code={}".format(action, code))


def load_hook(path):
    if not path:
        return None
    module, name = path.split(":")
    hook = getattr(importlib.import_module(module), name)
    if not callable(hook):
        raise TypeError("hook 必须可调用: " + path)
    return hook


class SweepResultWriter(ResultWriter):
    def __init__(self, output_root, config):
        super().__init__(output_root, config)
        self.columns.extend(["runtime_verified", "runtime_constants"])

    def _csv(self, filename, rows, columns=None):
        if filename in ("summary.csv", "comparison.csv"):
            for index, row in enumerate(rows, 1):
                case = self.config["cases"][index - 1]
                row.update({
                    "target": case["target"],
                    "mode": case["mode"],
                    "changed_parameter": case["changed_parameter"],
                    "parameter_value": case["parameter_value"],
                    "context": self.config.get("context", {}),
                    "runtime_verified_passed": sum(
                        record["case_index"] == index and record["status"] == "passed"
                        and record.get("runtime_verified", False) for record in self.records
                    ),
                })
        super()._csv(filename, rows, columns)
        if filename == "summary.csv":
            super()._csv("comparison.csv", rows)


def readback(manager, requested, paths):
    actual = {}
    for path in sorted(paths):
        value = manager.read_test_constant(path)
        if value is None:
            raise ValueError("读取字段失败: " + path)
        actual[path] = value
        if path in requested and not equal_value(value, requested[path]):
            raise ValueError("常数回读不一致 {}: 期望={!r}，实际={!r}".format(path, requested[path], value))
    return actual


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def run_sweep(fixture, qtbot, project_root, config_path=None, manager_factory=None, ok_code=0):
    """真实入口由 CPFR 提供 fixture/qtbot；factory 仅用于离线模拟。"""
    config_path = Path(config_path or os.environ.get("PBOP_SWEEP_CONFIG", str(DEFAULT_CONFIG))).resolve()
    settings = load_settings(config_path)
    with tempfile.TemporaryDirectory() as temporary:
        config = prepare_config(settings, temporary)
    for name in config["metrics"]:
        if name in {"runtime_verified", "runtime_constants"}:
            raise ValueError("指标名冲突: " + name)
    before_start = load_hook(config.get("hooks", {}).get("before_start"))
    verify_runtime = load_hook(config.get("hooks", {}).get("verify_runtime"))
    if manager_factory is None:
        from device_parameter_sweep.constant_manager import SweepConstantManager
        from sw.cm.ConstantManager import OK
        manager_factory, ok_code = SweepConstantManager, OK
    output = Path(os.environ.get("PBOP_SWEEP_OUTPUT_DIR", config.get("output_dir", "results/device_parameter_sweep")))
    if not output.is_absolute():
        output = Path(project_root) / output
    writer = SweepResultWriter(output, config)
    _write_json(writer.directory / "parameter_support.json", {
        "unavailable": config["unavailable_parameters"], "deferred": config["deferred_parameters"],
    })
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.setLevel(min(previous_level, logging.INFO))
    original = None
    write_attempted = False
    restoration = {"status": "not_needed", "error": ""}
    pending = None
    manager = None
    input_timeout = config.get("input_timeout_ms", 20000)
    run_timeout = config.get("run_timeout_ms", 3900000)
    try:
        fixture.app.show()
        fixture.app.window.setVisible(False)
        qtbot.waitUntil(fixture.at_input_state, timeout=input_timeout)
        manager = manager_factory()
        check_return(manager._load_data(), ok_code, "预检读取常数")
        touched_paths = set(config["common_constants"])
        for case in config["cases"]:
            values = dict(config["common_constants"], **case["constants"])
            manager.validate_values(values)
            touched_paths.update(values)
        # 预检所有只读字段，避免第二组才发现设备 proto 版本不匹配。
        readback(manager, {}, set(config["read_constants"]))
        original = manager.snapshot_fields(touched_paths)
        _write_json(writer.directory / "original_constants.json", original)
        _write_json(writer.directory / "preflight.json", {"status": "passed", "groups": len(config["cases"])})
        start = fixture.operate_buttons["Start"]
        reset = fixture.operate_buttons["Reset"]
        stop = False
        run_index = 0
        for case_index, case in enumerate(config["cases"], 1):
            for repeat in range(1, case["repeat"] + 1):
                run_index += 1
                requested = dict(config["common_constants"], **case["constants"])
                record = {
                    "run": run_index, "case_index": case_index, "case": case["name"], "repeat": repeat,
                    "status": "failed", "result_reached": False, "reset_ok": False,
                    "runtime_verified": False, "runtime_constants": {},
                    "started_at": datetime.now().astimezone().isoformat(),
                    "requested_constants": requested, "actual_constants": {},
                    "metric_errors": {}, "duration_s": None, "error": "本轮中断或未完成",
                    "log_file": "logs/round_{:04d}.log".format(run_index),
                }
                round_start = time.monotonic()
                operation_start = None
                start_attempted = False
                capture = RoundCapture(writer.directory / record["log_file"], config["metrics"],
                                       config.get("metric_logger_prefixes", []))
                capture_installed = False
                try:
                    qtbot.waitUntil(fixture.at_input_state, timeout=input_timeout)
                    check_return(manager._load_data(), ok_code, "读取常数")
                    write_attempted = True  # WriteData 报错时也可能已部分生效，仍需尝试恢复。
                    check_return(manager.write_test_constants(requested), ok_code, "写入常数")
                    check_return(manager._load_data(), ok_code, "回读常数")
                    actual = readback(manager, requested, set(requested) | set(config["read_constants"]))
                    record["actual_constants"] = actual
                    record.update({"param." + path: value for path, value in actual.items()})
                    if before_start:
                        before_start(fixture, dict(requested))
                    if verify_runtime:
                        runtime = verify_runtime(fixture, dict(requested))
                        if not isinstance(runtime, dict):
                            raise ValueError("verify_runtime 必须返回实际运行时常数的路径到值映射")
                        record["runtime_constants"] = runtime
                        for path, expected in requested.items():
                            if path not in runtime or not equal_value(runtime[path], expected):
                                raise ValueError("算法运行时常数未生效: " + path)
                        record["runtime_verified"] = True
                    qtbot.waitUntil(start.isEnabled, timeout=input_timeout)
                    logger.info("第 %s 轮 %s 重复 %s/%s，Start；runtime_verified=%s",
                                run_index, case["name"], repeat, case["repeat"], record["runtime_verified"])
                    # 写入、刷新阶段的旧结果日志不进入本轮指标采集。
                    root_logger.addHandler(capture)
                    capture_installed = True
                    operation_start = time.monotonic()
                    start_attempted = True
                    start.click()
                    qtbot.waitUntil(fixture.at_result_state, timeout=run_timeout)
                    record["duration_s"] = time.monotonic() - operation_start
                    record["result_reached"] = True
                    values, errors, missing = capture.collect(fixture)
                    record.update(values)
                    record["metric_errors"] = errors
                    record["structured_result"] = dict(capture.structured_result)
                    if missing:
                        raise ValueError("本轮缺少必需结果指标: " + ", ".join(missing))
                    record["status"], record["error"] = "passed", ""
                except Exception as exc:
                    record["error"] = "{}: {}".format(type(exc).__name__, exc)
                    logger.exception("第 %s 轮失败", run_index)
                finally:
                    if operation_start is not None and record["duration_s"] is None:
                        record["duration_s"] = time.monotonic() - operation_start
                    try:
                        if start_attempted or not fixture.at_input_state():
                            qtbot.waitUntil(reset.isEnabled, timeout=input_timeout)
                            reset.click()
                        qtbot.waitUntil(fixture.at_input_state, timeout=input_timeout)
                        record["reset_ok"] = True
                    except Exception as exc:
                        record["status"] = "failed"
                        record["error"] += "；Reset 失败: {}".format(exc)
                    record["ended_at"] = datetime.now().astimezone().isoformat()
                    record["elapsed_s"] = time.monotonic() - round_start
                    if capture_installed:
                        root_logger.removeHandler(capture)
                    capture.close()
                    writer.append(record)
                stop = not record["reset_ok"] or (
                    record["status"] != "passed" and not config.get("continue_on_error", False))
                if stop:
                    break
            if stop:
                break
    except BaseException as exc:
        pending = exc
        if original is None:
            _write_json(writer.directory / "preflight.json", {"status": "failed", "error": str(exc)})
    finally:
        if original is not None and write_attempted:
            try:
                if not fixture.at_input_state():
                    raise RuntimeError("尚未回到输入状态；未写恢复常数，请保留 original_constants.json")
                check_return(manager.restore_fields(original), ok_code, "恢复原常数")
                check_return(manager._load_data(), ok_code, "恢复后回读")
                restored = manager.snapshot_fields(set(original))
                for path, state in original.items():
                    actual = restored[path]
                    if actual["present"] != state["present"] or not equal_value(actual["value"], state["value"]):
                        raise ValueError("恢复回读不一致: " + path)
                restoration["status"] = "restored"
            except Exception as exc:
                restoration = {"status": "failed", "error": "{}: {}".format(type(exc).__name__, exc)}
                logger.exception("恢复常数失败；原值文件: %s", writer.directory / "original_constants.json")
        _write_json(writer.directory / "restoration.json", restoration)
        root_logger.setLevel(previous_level)
        logger.info("参数对比输出目录: %s；常数恢复: %s", writer.directory, restoration["status"])
    if pending is not None:
        if restoration["status"] == "failed":
            logger.error("此外恢复失败: %s", restoration["error"])
        raise pending
    if restoration["status"] == "failed":
        raise RuntimeError("恢复原常数失败: {}；结果={}".format(restoration["error"], writer.directory))
    failed = sum(record["status"] != "passed" for record in writer.records)
    if failed:
        raise AssertionError("{} 轮失败；结果={}".format(failed, writer.directory))
    return writer.directory
