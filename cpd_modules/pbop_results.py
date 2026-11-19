"""每轮日志、数值采集与 CSV/JSONL 统计；不依赖设备或 Qt。"""

import csv
import json
import logging
import math
import re
import statistics
from datetime import datetime
from pathlib import Path


def read_path(root, path):
    """读取对象属性、字典键或列表下标，路径以点分隔，不调用方法。"""
    for part in path.split("."):
        if isinstance(root, dict):
            root = root[part]
        elif isinstance(root, (list, tuple)):
            root = root[int(part)]
        else:
            root = getattr(root, part)
    return root


def number(value):
    if isinstance(value, bool):
        raise ValueError("布尔值不能作为数值指标")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("指标必须是有限数值")
    return result


class RoundCapture(logging.Handler):
    """每轮一个日志文件；重复出现的数值取本轮最后一次有效值。"""

    def __init__(self, path, metrics, logger_prefixes=()):
        super().__init__()
        self.stream = Path(path).open("w", encoding="utf-8")
        self.specs = metrics
        self.prefixes = tuple(logger_prefixes)
        self.patterns = {
            name: re.compile(spec["log_pattern"])
            for name, spec in metrics.items() if "log_pattern" in spec
        }
        self.values = {}
        self.errors = {}
        self.structured_result = {}
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))

    def emit(self, record):
        # 保存原日志。指标解析失败不抛出到被测寻优代码。
        self.stream.write(self.format(record) + "\n")
        self.stream.flush()
        if self.prefixes and not any(
            record.name == prefix or record.name.startswith(prefix + ".")
            for prefix in self.prefixes
        ):
            return
        message = record.getMessage()
        if message.startswith("PBOP_RESULT "):
            try:
                payload = json.loads(message[len("PBOP_RESULT "):])
                if not isinstance(payload, dict):
                    raise ValueError("PBOP_RESULT 后必须是 JSON 对象")
                self.structured_result = payload
                self.errors.pop("PBOP_RESULT", None)
            except (TypeError, ValueError) as exc:
                self.structured_result = {}
                self.errors["PBOP_RESULT"] = str(exc)
        for name, pattern in self.patterns.items():
            match = pattern.search(message)
            if match:
                try:
                    self.values[name] = number(match.group(self.specs[name].get("group", 1)))
                    self.errors.pop(name, None)
                except (IndexError, TypeError, ValueError, OverflowError) as exc:
                    self.values.pop(name, None)
                    self.errors[name] = str(exc)

    def collect(self, fixture):
        values = dict(self.values)
        errors = dict(self.errors)
        for name, spec in self.specs.items():
            try:
                if "fixture_path" in spec:
                    values[name] = number(read_path(fixture, spec["fixture_path"]))
                elif "json_key" in spec:
                    values[name] = number(read_path(self.structured_result, spec["json_key"]))
                elif name not in values:
                    raise ValueError("本轮日志未匹配到指标")
            except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
                values.pop(name, None)
                errors[name] = str(exc)
        missing_required = [
            name for name, spec in self.specs.items()
            if spec.get("required", False) and name not in values
        ]
        return values, errors, missing_required

    def close(self):
        if not self.stream.closed:
            self.stream.close()
        super().close()


def numeric_summary(values):
    if not values:
        return {key: (0 if key == "count" else "")
                for key in ("count", "mean", "std", "min", "max", "median")}
    return {
        "count": len(values), "mean": statistics.mean(values),
        "std": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values), "max": max(values),
        "median": statistics.median(values),
    }


def build_summary(records, config):
    rows = []
    for case_index, case in enumerate(config["cases"], 1):
        selected = [r for r in records if r["case_index"] == case_index]
        passed = [r for r in selected if r["status"] == "passed"]
        row = {
            "case": case["name"], "planned": case["repeat"],
            "executed": len(selected), "passed": len(passed),
            "failed": len(selected) - len(passed),
            "pass_rate": len(passed) / len(selected) if selected else "",
            "configured_constants": json.dumps(
                dict(config.get("common_constants", {}), **case.get("constants", {})),
                ensure_ascii=False, sort_keys=True,
            ),
        }
        for name in ["duration_s"] + list(config.get("metrics", {})):
            values = [r[name] for r in passed if r.get(name) is not None]
            row.update({name + "_" + key: value
                        for key, value in numeric_summary(values).items()})
        rows.append(row)
    return rows


class ResultWriter:
    def __init__(self, output_root, config):
        self.config = config
        self.records = []
        self.directory = Path(output_root) / datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory / "logs").mkdir()
        (self.directory / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        constants = set(config.get("read_constants", [])) | set(config.get("common_constants", {}))
        for case in config["cases"]:
            constants.update(case.get("constants", {}))
        self.constant_paths = sorted(constants)
        self.columns = [
            "run", "case_index", "case", "repeat", "status", "result_reached",
            "reset_ok", "started_at", "ended_at", "duration_s", "elapsed_s",
            "error", "metric_errors", "log_file", "requested_constants", "actual_constants",
        ] + ["param." + path for path in self.constant_paths] + list(config.get("metrics", {}))
        self._csv("summary.csv", build_summary([], config))

    def append(self, record):
        # JSONL 是逐轮追加的原始记录；即使后续循环失败也已保存前轮结果。
        with (self.directory / "runs.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        self.records.append(record)
        self._csv("runs.csv", self.records, self.columns)
        self._csv("summary.csv", build_summary(self.records, self.config))

    def _csv(self, filename, rows, columns=None):
        columns = columns or list(rows[0])
        destination = self.directory / filename
        temporary = destination.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(value, ensure_ascii=False)
                                 if isinstance(value, (dict, list)) else value
                                 for key, value in row.items()})
        temporary.replace(destination)
