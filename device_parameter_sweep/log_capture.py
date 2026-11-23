"""解析用户实际提供的 MeasureResult/Update iterations 日志，保留观测来源。"""

import json
import re
from pathlib import Path

from cpd_modules.pbop_results import RoundCapture, number

POSITION = r"PlasmaPosition\(\s*x\s*=\s*(?P<{p}x>[^,]+),\s*y\s*=\s*(?P<{p}y>[^,]+),\s*z\s*=\s*(?P<{p}z>[^)]+)\)"
MEASURE = re.compile(
    r"Measure\s+result:\s*MeasureResult\(\s*plasma_pos_setpoint\s*=\s*"
    + POSITION.format(p="") + r".*?target_value\s*=\s*(?P<value>[^)]+)\)", re.DOTALL,
)
MEASURED_POSITION = re.compile(r"plasma_pos_mp\s*=\s*" + POSITION.format(p="mp_"), re.DOTALL)
UPDATE = re.compile(
    r"Update\s+iterations:\s*(?P<iteration>\d+)\s*,\s*plasma\s+position\s*:\s*"
    + POSITION.format(p="") + r"\s*,\s*ed\s*en\s*ergy\s*:\s*(?P<value>[^,]+?)\s*$", re.DOTALL,
)
OBSERVATION_KEYS = {
    "best_value", "best_x", "best_y", "best_z", "best_mp_x", "best_mp_y", "best_mp_z",
    "last_iteration", "measurement_log_count",
}


def log_number(value, trailing_period=False):
    # 兼容所提供日志中数字因终端换行/对齐而被插入空白的情况。
    compact = "".join(value.split())
    if trailing_period and compact.endswith("."):
        compact = compact[:-1]
    return number(compact)


class ObservationCapture(RoundCapture):
    def __init__(self, path, metrics, logger_prefixes=(), history_path=None):
        super().__init__(path, metrics, logger_prefixes)
        self.best = None
        self.measurement_count = 0
        self.last_iteration = None
        self.observation_errors = []
        self.history = Path(history_path).open("w", encoding="utf-8") if history_path else None

    def _event(self, event):
        if self.history:
            self.history.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            self.history.flush()

    def emit(self, record):
        super().emit(record)
        if self.prefixes and not any(record.name == prefix or record.name.startswith(prefix + ".")
                                     for prefix in self.prefixes):
            return
        message = record.getMessage()
        match = MEASURE.search(message)
        try:
            if match:
                sample = {axis: log_number(match.group(axis)) for axis in ("x", "y", "z", "value")}
                measured = MEASURED_POSITION.search(message)
                if measured:
                    sample.update({"mp_" + axis: log_number(measured.group("mp_" + axis)) for axis in ("x", "y", "z")})
                self.measurement_count += 1
                sample["measurement_log_index"] = self.measurement_count
                if self.best is None or sample["value"] > self.best["value"]:
                    self.best = dict(sample)
                self._event(dict(sample, event="measurement", logger=record.name))
                return
            match = UPDATE.search(message)
            if match:
                iteration = int(match.group("iteration"))
                self.last_iteration = iteration if self.last_iteration is None else max(self.last_iteration, iteration)
                sample = {axis: log_number(match.group(axis)) for axis in ("x", "y", "z")}
                sample["logged_ed_energy"] = log_number(match.group("value"), trailing_period=True)
                # Update 的 ed energy 标签不用于推断所选目标；目标值只取 MeasureResult.target_value。
                self._event(dict(sample, event="iteration", iteration=iteration, logger=record.name))
        except (TypeError, ValueError, OverflowError) as exc:
            self.observation_errors.append(str(exc))

    def collect(self, fixture):
        values, errors, _ = super().collect(fixture)
        derived = {}
        if self.best is not None:
            derived["best_value"] = self.best["value"]
            for axis in ("x", "y", "z", "mp_x", "mp_y", "mp_z"):
                if axis in self.best:
                    derived["best_" + axis] = self.best[axis]
            derived["measurement_log_count"] = self.measurement_count
        if self.last_iteration is not None:
            derived["last_iteration"] = self.last_iteration
        for name, spec in self.specs.items():
            if "observation_key" not in spec:
                continue
            key = spec["observation_key"]
            if key in derived:
                values[name] = derived[key]
                errors.pop(name, None)
            else:
                values.pop(name, None)
                errors[name] = "本轮未从真实量测日志取得 " + key
        if self.observation_errors:
            errors["observation_log"] = self.observation_errors[-1]
        missing = [name for name, spec in self.specs.items() if spec.get("required", False) and name not in values]
        return values, errors, missing

    def close(self):
        if getattr(self, "history", None) is not None and not self.history.closed:
            self.history.close()
        super().close()
