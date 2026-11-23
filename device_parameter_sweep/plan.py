"""生成单因素计划；python -m device_parameter_sweep.plan 仅预览，不连接设备。"""

import argparse
import copy
import json
import random
from pathlib import Path

from cpd_modules.pbop_batch import load_config as validate_batch_config
from device_parameter_sweep.log_capture import OBSERVATION_KEYS
from device_parameter_sweep.parameters import (
    INPUT, PARAMETERS, UNAVAILABLE, validate_common, validate_parameter, validate_scalar,
)

DEFAULT_CONFIG = Path(__file__).with_name("sweep_config.json")


def load_settings(path):
    settings = json.loads(Path(path).read_text(encoding="utf-8"))
    build_plan(settings)
    return settings


def build_plan(settings):
    allowed = {
        "repeat", "baseline", "sweeps", "common_constants", "plateau", "context",
        "metrics", "metric_logger_prefixes", "input_timeout_ms", "run_timeout_ms",
        "output_dir", "continue_on_error", "randomize_case_order", "seed", "hooks",
        "require_runtime_verification", "targets",
    }
    unknown = set(settings) - allowed
    if unknown:
        raise ValueError("未知配置项: " + ", ".join(sorted(unknown)))
    repeat = settings.get("repeat", 3)
    validate_scalar("repeat", repeat, int, 1, 100000)
    baseline = settings.get("baseline", {})
    if set(baseline) != set(PARAMETERS):
        raise ValueError("baseline 必须显式给出全部 7 个已支持参数: " + ", ".join(PARAMETERS))
    for name, value in baseline.items():
        validate_parameter(name, value)
    if baseline["xi_final"] > baseline["xi_init"]:
        raise ValueError("基准 xi_final 不能大于 xi_init")
    common = settings.get("common_constants", {})
    validate_common(common)
    if any(INPUT + name in common for name in ("search_select_ed", "search_select_pf")):
        raise ValueError("目标选择由 targets 显式生成，请勿在 common_constants 重复配置")
    if INPUT + "search_objective" not in common:
        raise ValueError("必须显式指定本批 search_objective，避免沿用上一批模式")
    if common[INPUT + "search_objective"] != "Maximum":
        raise ValueError("主批次使用 Maximum；平台模式请在 plateau 中配置")
    targets = settings.get("targets", ["ed", "ed_pf_ratio"])
    if not isinstance(targets, list) or not targets or any(target not in ("ed", "ed_pf_ratio") for target in targets):
        raise ValueError("targets 只支持 ed、ed_pf_ratio，且必须是非空列表")
    if len(set(targets)) != len(targets):
        raise ValueError("targets 不能重复")
    sweeps = settings.get("sweeps", {})
    if not isinstance(sweeps, dict):
        raise ValueError("sweeps 必须是参数到候选值列表的映射")
    base_constants = {PARAMETERS[name].path: value for name, value in baseline.items()}
    cases = []

    def add_case(name, parameter, value, constants, mode="maximum"):
        merged = dict(base_constants)
        merged.update(constants)
        xi0 = merged[PARAMETERS["xi_init"].path]
        xi1 = merged[PARAMETERS["xi_final"].path]
        if xi1 > xi0:
            raise ValueError(name + ": xi_final 不能大于 xi_init")
        cases.append({
            "name": name, "repeat": repeat, "constants": merged,
            "changed_parameter": parameter, "parameter_value": value, "mode": mode,
        })

    add_case("maximum__baseline", "baseline", "", {})
    for name, values in sweeps.items():
        # 先检查参数名，缺失的 4 个参数即使值列表为空也必须明确报错。
        if name not in PARAMETERS:
            validate_parameter(name, 0)
        if name == "platform_error":
            raise ValueError("platform_error 请在 plateau 中配置，避免误在 Maximum 模式评估")
        if not isinstance(values, list) or not values:
            raise ValueError(name + " 候选值必须是非空列表")
        seen = []
        for value in values:
            validate_parameter(name, value)
            if value in seen:
                raise ValueError(name + " 候选值重复")
            seen.append(value)
            if value != baseline[name]:
                add_case("maximum__{}__{}".format(name, value), name, value, {PARAMETERS[name].path: value})
    if any(name.startswith("convergence_") for name in sweeps):
        if common.get(INPUT + "convergence_enable") is not True:
            raise ValueError("扫描收敛参数时须显式设置 convergence_enable=true")
    plateau = settings.get("plateau", {})
    if set(plateau) - {"enabled", "search_objective", "values"}:
        raise ValueError("plateau 中存在未知配置项")
    validate_scalar("plateau.enabled", plateau.get("enabled", False), bool)
    if plateau.get("enabled", False):
        objective = plateau.get("search_objective")
        validate_scalar("plateau.search_objective", objective, str)
        if objective == common[INPUT + "search_objective"]:
            raise ValueError("平台测试必须填写实际平台模式名，不能与主寻优模式相同")
        values = plateau.get("values", [])
        if not isinstance(values, list) or not values:
            raise ValueError("plateau.values 必须非空")
        if len(set(values)) != len(values):
            raise ValueError("plateau.values 重复")
        for value in values:
            validate_parameter("platform_error", value)
            add_case("plateau__platform_error__{}".format(value), "platform_error", value, {
                PARAMETERS["platform_error"].path: value,
                INPUT + "search_objective": objective,
            }, mode="plateau")
    hooks = settings.get("hooks", {})
    if set(hooks) - {"before_start", "verify_runtime"}:
        raise ValueError("hooks 只支持 before_start 和 verify_runtime")
    for name, hook in hooks.items():
        if hook is not None and (not isinstance(hook, str) or hook.count(":") != 1):
            raise ValueError("hooks.{} 须为 module:function 或 null".format(name))
    if settings.get("require_runtime_verification", False) and not hooks.get("verify_runtime"):
        raise ValueError("要求运行时参数验证时，必须配置 hooks.verify_runtime")
    for name in ("continue_on_error", "randomize_case_order", "require_runtime_verification"):
        validate_scalar(name, settings.get(name, False), bool)
    for name, default in (("input_timeout_ms", 20000), ("run_timeout_ms", 3900000)):
        validate_scalar(name, settings.get(name, default), int, 1, 86400000)
    maximum_time = common.get(INPUT + "max_search_time")
    if maximum_time is not None and settings.get("run_timeout_ms", 3900000) < (maximum_time + 60) * 1000:
        raise ValueError("run_timeout_ms 至少为 (max_search_time + 60) * 1000")
    seed = settings.get("seed", 42)
    validate_scalar("seed", seed, int)
    target_cases = []
    for target in targets:
        for case in cases:
            item = copy.deepcopy(case)
            item["name"] = target + "__" + case["name"]
            item["target"] = target
            item["constants"].update({
                INPUT + "search_select_ed": True,
                INPUT + "search_select_pf": target == "ed_pf_ratio",
            })
            target_cases.append(item)
    cases = target_cases
    if settings.get("randomize_case_order", False):
        random.Random(seed).shuffle(cases)
    config = copy.deepcopy(settings)
    config["common_constants"] = common
    config["cases"] = cases
    config["metrics"] = settings.get("metrics", {})
    config["read_constants"] = list(base_constants) + [
        INPUT + "convergence_tol_threshold", INPUT + "search_select_ed", INPUT + "search_select_pf",
    ]
    config["unavailable_parameters"] = dict(UNAVAILABLE)
    config["deferred_parameters"] = {} if plateau.get("enabled") else {
        "platform_error": "平台模式名尚未配置；Maximum 测试中仅固定该值，不做平台效果结论。",
    }
    return config


def prepare_config(settings, directory):
    """复用仓库已有的指标正则/路径校验；在写入任何设备常数前执行。"""
    config = build_plan(settings)
    validation = copy.deepcopy(config)
    for name, spec in validation["metrics"].items():
        if "observation_key" in spec:
            if spec["observation_key"] not in OBSERVATION_KEYS:
                raise ValueError("未知 observation_key: " + str(spec["observation_key"]))
            if any(key in spec for key in ("log_pattern", "fixture_path", "json_key")):
                raise ValueError("指标 {} 只能配置一种采集方式".format(name))
            # 复用旧校验器的名称冲突检查；此占位路径只用于校验，不用于设备采集。
            del spec["observation_key"]
            spec["fixture_path"] = "_observation_validation_only"
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(validation, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    validate_batch_config(path)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, help="可选：保存展开后的计划 JSON")
    args = parser.parse_args()
    plan = build_plan(load_settings(args.config))
    print("配置组={}；每组重复={}；总实验次数={}".format(
        len(plan["cases"]), plan.get("repeat", 3), sum(case["repeat"] for case in plan["cases"])))
    print("顺序随机种子={}（仅控制组顺序，不改变设备算法随机种子）".format(plan.get("seed", 42)))
    for case in plan["cases"]:
        print("{} x{}".format(case["name"], case["repeat"]))
    for name, reason in dict(plan["unavailable_parameters"], **plan["deferred_parameters"]).items():
        print("未测试 {}: {}".format(name, reason))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
