"""严格对应本次提供的 CPQPBOPTC.proto，不臆造缺失参数的路径。"""

import math
from collections import namedtuple

SCENARIO = "PGEC_CPQPBOP_SEARCH_SCENARIO"
CONFIG = SCENARIO + ".search_config."
CALIB = SCENARIO + ".search_calib_config."
INPUT = SCENARIO + ".input."
Parameter = namedtuple("Parameter", "path kind minimum maximum default description")

PARAMETERS = {
    "n_restarts_optimizer": Parameter(CONFIG + "n_restarts_optimizer", int, 0, 20, 10, "优化器重新启动次数"),
    "xi_init": Parameter(CONFIG + "xi_init", float, 0.01, 0.5, 0.1, "EI 初始值"),
    "xi_final": Parameter(CONFIG + "xi_final", float, 0.001, 0.01, 0.001, "EI 最终值"),
    "convergence_point_num": Parameter(CALIB + "convergence_point_num", int, 2, 20, 5, "收敛度评估点数量"),
    "convergence_tol": Parameter(CALIB + "convergence_tol", float, 0, 1, 0.1, "标定配置中的收敛度"),
    "min_dis": Parameter(CONFIG + "min_dis", float, 0, 100, 0.5, "采样点最小距离约束"),
    "platform_error": Parameter(CONFIG + "platform_error", float, 0, 0.1, 0.02, "平台区 ED 波动范围"),
}

UNAVAILABLE = {
    "sigma3_rel": "当前 proto 未定义；需新增字段并接入算法噪声标定。",
    "global_probability": "当前 proto 未定义；需新增字段并传给实际 EI 选点函数。",
    "ls_bounds_lower_limit": "当前 proto 未定义；需先明确绝对长度还是搜索跨度比例。",
    "ls_bounds_upper_limit": "当前 proto 未定义；需先明确绝对长度还是搜索跨度比例。",
}

# 这些字段可作为固定条件；不是新增的算法接口。
INPUT_SPECS = {
    "search_objective": (str, None, None),
    "max_iteration": (int, 50, 300),
    "initial_sampling_num": (int, 20, 80),
    "max_search_time": (int, 1000, 7200),
    "convergence_enable": (bool, None, None),
    "convergence_tol_threshold": (float, 0, 1),
    "search_select_pf": (bool, None, None),
    "search_select_ed": (bool, None, None),
}


def validate_scalar(name, value, kind, minimum=None, maximum=None):
    if kind is float:
        valid = type(value) in (int, float) and math.isfinite(value)
    else:
        valid = type(value) is kind
    if not valid:
        raise ValueError("{} 类型错误，需要 {}: {!r}".format(name, kind.__name__, value))
    if kind is str and not value.strip():
        raise ValueError(name + " 不能为空")
    if minimum is not None and not minimum <= value <= maximum:
        raise ValueError("{} 必须在 [{}, {}]，实际={!r}".format(name, minimum, maximum, value))


def validate_parameter(name, value):
    if name in UNAVAILABLE:
        raise ValueError(name + ": " + UNAVAILABLE[name])
    if name not in PARAMETERS:
        raise ValueError("未知参数（不允许静默忽略）: " + name)
    spec = PARAMETERS[name]
    validate_scalar(name, value, spec.kind, spec.minimum, spec.maximum)


def validate_common(values):
    for path, value in values.items():
        name = path[len(INPUT):] if path.startswith(INPUT) else ""
        if name not in INPUT_SPECS:
            raise ValueError("common_constants 仅接受已登记的 search input 路径: " + path)
        validate_scalar(path, value, *INPUT_SPECS[name])
