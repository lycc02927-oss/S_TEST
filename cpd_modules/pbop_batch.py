"""配置驱动的 CPQPBOP Start/结果采集/Reset 循环。"""

import json
import logging
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path

from cpd_modules.pbop_results import ResultWriter, RoundCapture

logger = logging.getLogger(__name__)


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not config.get("cases"):
        raise ValueError("配置必须包含非空 cases")
    names = set()
    for case in config["cases"]:
        if not case.get("name") or case["name"] in names:
            raise ValueError("每组 case.name 必须非空且唯一")
        names.add(case["name"])
        case.setdefault("repeat", 1)
        if type(case["repeat"]) is not int or case["repeat"] < 1:
            raise ValueError("case.repeat 必须为正整数")
        if not isinstance(case.get("constants", {}), dict):
            raise ValueError("case.constants 必须是字段路径到标量值的映射")
    metrics = config.setdefault("metrics", {})
    reserved = {
        "run", "case_index", "case", "repeat", "status", "result_reached", "reset_ok",
        "started_at", "ended_at", "duration_s", "elapsed_s", "error", "metric_errors",
        "log_file", "requested_constants", "actual_constants", "structured_result",
    }
    for name, spec in metrics.items():
        if name in reserved or name.startswith("param."):
            raise ValueError("指标名与基础字段冲突: " + name)
        if sum(key in spec for key in ("log_pattern", "fixture_path", "json_key")) != 1:
            raise ValueError("指标 {} 必须配置且只配置一种采集方式".format(name))
        if "log_pattern" in spec:
            pattern = re.compile(spec["log_pattern"])
            group = spec.get("group", 1)
            if isinstance(group, str):
                if group not in pattern.groupindex:
                    raise ValueError("指标 {} 缺少正则命名捕获组 {}".format(name, group))
            elif type(group) is not int or not 0 <= group <= pattern.groups:
                raise ValueError("指标 {} 的正则捕获组无效".format(name))
    return config


def _same_value(actual, expected):
    if type(expected) is float and isinstance(actual, (int, float)):
        return math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-9)
    return actual == expected


def run_batch(fixture, qtbot, default_config, project_root, manager_factory=None):
    """返回输出目录；任何失败仍使 pytest 失败，已保存的记录保留。"""
    if manager_factory is None:
        from cpd_modules.cpqpbop_test_constant_manager import TestConstantManager
        from sw.cm.ConstantManager import OK
        manager_factory = TestConstantManager
    else:
        # 仅用于本仓库无设备单元测试，真实运行使用 ConstantManager.OK。
        OK = 0
    config_path = Path(os.environ.get("PBOP_CONFIG", str(default_config))).resolve()
    config = load_config(config_path)
    output_root = Path(os.environ.get("PBOP_OUTPUT_DIR", config.get("output_dir", "results")))
    if not output_root.is_absolute():
        output_root = Path(project_root) / output_root
    writer = ResultWriter(output_root, config)
    logger.info("PBOP 批量配置: %s；结果目录: %s", config_path, writer.directory)
    fixture.app.show()
    fixture.app.window.setVisible(False)
    input_timeout = config.get("input_timeout_ms", 20000)
    run_timeout = config.get("run_timeout_ms", 1800000)
    qtbot.waitUntil(fixture.at_input_state, timeout=input_timeout)
    manager = manager_factory()
    start = fixture.operate_buttons["Start"]
    reset = fixture.operate_buttons["Reset"]
    root_logger = logging.getLogger()
    original_level = root_logger.level
    root_logger.setLevel(min(original_level, logging.INFO))
    stop = False
    run_index = 0
    try:
        for case_index, case in enumerate(config["cases"], 1):
            for repeat in range(1, case["repeat"] + 1):
                run_index += 1
                requested = dict(config.get("common_constants", {}))
                requested.update(case.get("constants", {}))
                record = dict(
                    run=run_index, case_index=case_index, case=case["name"], repeat=repeat,
                    status="failed", result_reached=False, reset_ok=False,
                    started_at=datetime.now().astimezone().isoformat(),
                    requested_constants=requested, actual_constants={},
                    metric_errors={}, error="测试被中断或未完成", duration_s=None,
                    log_file="logs/round_{:04d}.log".format(run_index),
                )
                capture = RoundCapture(
                    writer.directory / record["log_file"], config["metrics"],
                    config.get("metric_logger_prefixes", []),
                )
                root_logger.addHandler(capture)
                round_start = time.monotonic()
                operation_start = None
                start_attempted = False
                try:
                    qtbot.waitUntil(fixture.at_input_state, timeout=input_timeout)
                    ret = manager._load_data()
                    assert ret == OK, "读取常数失败: {}".format(ret)
                    ret = manager.write_test_constants(requested)
                    assert ret == OK, "写入常数失败: {}".format(ret)
                    ret = manager._load_data()
                    assert ret == OK, "回读常数失败: {}".format(ret)
                    for path in sorted(set(requested) | set(config.get("read_constants", []))):
                        actual = manager.read_test_constant(path)
                        assert actual is not None, "读取字段失败: " + path
                        record["actual_constants"][path] = actual
                        record["param." + path] = actual
                        if path in requested:
                            assert _same_value(actual, requested[path]), (
                                "常数回读不一致 {}: 期望={!r}, 实际={!r}".format(path, requested[path], actual)
                            )
                    logger.info("第 %s 轮 / %s / 重复 %s：Start，实际常数=%s",
                                run_index, case["name"], repeat, record["actual_constants"])
                    qtbot.waitUntil(start.isEnabled, timeout=input_timeout)
                    operation_start = time.monotonic()
                    start_attempted = True
                    start.click()
                    qtbot.waitUntil(fixture.at_result_state, timeout=run_timeout)
                    record["duration_s"] = time.monotonic() - operation_start
                    record["result_reached"] = True
                    # 必须在 Reset 前读取。日志数值只来自本轮 handler。
                    values, errors, missing = capture.collect(fixture)
                    record.update(values)
                    record["metric_errors"] = errors
                    record["structured_result"] = dict(capture.structured_result)
                    if missing:
                        raise AssertionError("本轮缺少必需指标: " + ", ".join(missing))
                    record["status"] = "passed"
                    record["error"] = ""
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
                        record["error"] += " Reset 失败: {}: {}".format(type(exc).__name__, exc)
                        logger.exception("第 %s 轮无法回到输入状态，停止后续运行", run_index)
                    record["ended_at"] = datetime.now().astimezone().isoformat()
                    record["elapsed_s"] = time.monotonic() - round_start
                    logger.info("第 %s 轮记录：status=%s，reset_ok=%s", run_index,
                                record["status"], record["reset_ok"])
                    root_logger.removeHandler(capture)
                    capture.close()
                    writer.append(record)
                stop = not record["reset_ok"] or (
                    record["status"] != "passed" and not config.get("continue_on_error", True)
                )
                if stop:
                    break
            if stop:
                break
    finally:
        root_logger.setLevel(original_level)
        logger.info("PBOP 批量测试结果已保存: %s", writer.directory)
    failed = [record for record in writer.records if record["status"] != "passed"]
    assert not failed, "{} 轮失败；查看 {}".format(len(failed), writer.directory / "runs.csv")
    return writer.directory
