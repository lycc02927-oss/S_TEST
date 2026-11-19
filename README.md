# CPQPBOP 自动化测试

将提供的三段代码整理为一个常数管理模块和两个测试入口，并增加配置驱动、逐轮结果保存和分组统计。运行过程为：重新读取常数 → 修改 → 回读校验 → Start → 等待结果 → 采集数据 → Reset → 等待输入状态。

## 三段代码的位置

| 原代码 | 仓库文件 | 默认行为 |
| --- | --- | --- |
| 测试常数 manager | `cpd_modules/cpqpbop_test_constant_manager.py` | 保留原读写接口，增加多个字段一次写入 |
| 修改常数后 Start/Reset | `testcases/test_cpd_process/test_pbop_start_reset.py` | `scan_retry_time` 依次取 1、2、3，各运行一次 |
| 重复寻优测试 | `testcases/test_cpd_process/test_pbop_optimization.py` | 沿用当前常数，重复运行 10 次 |

共用循环在 `cpd_modules/pbop_batch.py`；日志采集、CSV 和统计在 `cpd_modules/pbop_results.py`。

## 1. 更改配置参数

编辑 `config/retry_scan.json` 或 `config/optimization.json`，不用再改测试函数。例如比较三个参数值、每个值运行 10 次：

```json
{
  "output_dir": "results",
  "input_timeout_ms": 20000,
  "run_timeout_ms": 1800000,
  "continue_on_error": true,
  "common_constants": {},
  "read_constants": ["PGEC_CPQPBOP_SCAN_SCENARIO.input.scan_retry_time"],
  "metrics": {},
  "cases": [
    {
      "name": "retry_1",
      "repeat": 10,
      "constants": {"PGEC_CPQPBOP_SCAN_SCENARIO.input.scan_retry_time": 1}
    },
    {
      "name": "retry_2",
      "repeat": 10,
      "constants": {"PGEC_CPQPBOP_SCAN_SCENARIO.input.scan_retry_time": 2}
    },
    {
      "name": "retry_3",
      "repeat": 10,
      "constants": {"PGEC_CPQPBOP_SCAN_SCENARIO.input.scan_retry_time": 3}
    }
  ]
}
```

总运行次数是所有 `repeat` 之和，上例为 30 次。

| 配置项 | 用途 |
| --- | --- |
| `cases[].name` | 配置组名称，必须唯一；统计按该名称分组 |
| `cases[].repeat` | 本组重复次数 |
| `cases[].constants` | 本组要写入的 proto 字段路径及标量值 |
| `common_constants` | 每轮都写入的公共常数；本组同名字段覆盖公共值 |
| `read_constants` | 额外回读并记录的字段；不修改这些字段 |
| `metrics` | 需要采集的数值结果及采集方式 |
| `continue_on_error` | 本轮失败且已恢复输入状态时，是否继续；Reset 失败始终停止 |
| `input_timeout_ms` / `run_timeout_ms` | 输入/按钮等待和寻优等待超时，单位毫秒 |
| `output_dir` | 相对仓库根目录的结果目录，也可用绝对路径 |

`constants` 的路径必须是项目 proto 的真实字段名，不能直接放 UI 控件名。JSON 的数值、布尔和字符串类型也必须与字段类型对应。每轮只改公共配置和本组指定的字段；其他字段沿用当时设备值。比较多组参数时，请明确填写所有参与比较的字段，避免依赖上一组遗留的值。测试结束后保留最后一轮写入的常数，不自动恢复。

## 2. 如何读取参数

独立调用 manager 时：

```python
from sw.cm.ConstantManager import OK
from cpd_modules.cpqpbop_test_constant_manager import TestConstantManager

manager = TestConstantManager()
path = "PGEC_CPQPBOP_SCAN_SCENARIO.input.scan_retry_time"

assert manager._load_data() == OK
old_value = manager.read_test_constant(path)

assert manager.write_test_constant_data(path, 2) == OK
assert manager._load_data() == OK
actual_value = manager.read_test_constant(path)
assert actual_value == 2
```

`read_test_constant()` 读取已加载对象；要读取服务中的最新值，先调用 `_load_data()`。多个字段可通过 `write_test_constants({字段路径: 值, ...})` 在副本上修改并一次写入。

批量测试自动完成每轮重新读取、写入和回读核对，分别记录 `requested_constants` 与 `actual_constants`，并将实际值展开为 CSV 的 `param.<字段路径>` 列。浮点常数回读允许 `rel_tol=1e-6, abs_tol=1e-9` 的精度差。

**回读成功证明常数服务写入成功。CPD 是否在每次 Start 时重新加载常数，需要在实际项目确认。若应用只在初始化时缓存常数，还需要接入项目已有的刷新或重新创建应用流程；不能仅凭回读认定本轮算法已使用新参数。**

## 3. 当前只有日志，如何得到结果数据

默认 `metrics` 为空，先记录成功/失败、耗时、参数和逐轮原始日志。由于尚未提供项目的实际结果日志或 presenter 结构，默认不猜测最佳值、ED、PF 或 X/Y/Z。配置采集后，这些指标才会进入明细和统计。

每个指标只能选择下面一种方式，允许不同指标混用。所有指标必须能转换为有限数值。

### A. 保留现有日志，通过正则提取

假设真实日志包含以下格式（这是演示，不是对项目日志格式的假定）：

```text
最终最佳值=123.4
最终最佳位置: X=-1.2, Y=3.4, Z=5.6
```

把以下内容放入配置的 `metrics`，并按实际日志文字调整：

```json
"metrics": {
  "best_value": {
    "log_pattern": "最终最佳值\\s*=\\s*(?P<value>[-+0-9.eE]+)",
    "group": "value",
    "required": true
  },
  "best_x": {
    "log_pattern": "最终最佳位置:.*?X=(?P<value>[-+0-9.eE]+)",
    "group": "value",
    "required": true
  },
  "best_y": {
    "log_pattern": "最终最佳位置:.*?Y=(?P<value>[-+0-9.eE]+)",
    "group": "value",
    "required": true
  },
  "best_z": {
    "log_pattern": "最终最佳位置:.*?Z=(?P<value>[-+0-9.eE]+)",
    "group": "value",
    "required": true
  }
}
```

每轮重复匹配时取最后一条匹配记录；正则最好锚定“最终结果”，避免误取迭代过程中的其他数值。三个方向单独保存。可以用 `metric_logger_prefixes: ["cpqpbop"]` 限定指标来源 logger；空列表表示不限定。原始日志仍全部保存。

这里采集的是**当前 Python 进程中传播到 root logger 的 logging 记录**。`print()`、子进程日志、设备独立日志文件，以及 `propagate=False` 的 logger 需要另行接入，不能自动捕获。源 logger 也必须允许对应日志级别。

### B. 在项目最终结果处增加一条结构化日志

如果能修改最终结果的打印位置，建议同步记录一条 JSON 日志，减少文字格式变化导致的解析失败：

```python
import json

# 以下 best_value、best_x 等应替换为该位置实际已有的结果变量。
logger.info("PBOP_RESULT %s", json.dumps({
    "best_value": float(best_value),
    "best_x": float(best_x),
    "best_y": float(best_y),
    "best_z": float(best_z),
    "ed": float(ed),
    "pf": float(pf),
    "ed_pf_ratio": float(ed_pf_ratio),
    "iterations": int(iterations),
}))
```

配置对应键名：

```json
"metrics": {
  "best_value": {"json_key": "best_value", "required": true},
  "best_x": {"json_key": "best_x", "required": true},
  "best_y": {"json_key": "best_y", "required": true},
  "best_z": {"json_key": "best_z", "required": true},
  "ed": {"json_key": "ed"},
  "pf": {"json_key": "pf"},
  "ed_pf_ratio": {"json_key": "ed_pf_ratio"},
  "iterations": {"json_key": "iterations"}
}
```

消息正文必须以 `PBOP_RESULT ` 开头，后面是 JSON 对象，在到达结果状态前同步记录。代码取本轮最后一条结构化结果。尚未为实际 CPQPBOP 安装这条日志；上面是需要在项目结果位置接入的示例。

### C. 直接读取已存在的结果对象

如果已确认结果存在于 `cpfr_test` 下的某个对象，使用 `fixture_path`。点分路径支持对象属性、字典键和列表下标，不调用方法。例如，**仅当实际存在 `cpfr_test.result["best_value"]` 时**，才配置：

```json
"metrics": {
  "best_value": {"fixture_path": "result.best_value", "required": true}
}
```

项目的实际 presenter/data model 路径尚未知，所以仓库未内置未经核实的结果字段。读取时机在到达结果状态后、Reset 前。

`required: true` 表示缺失该指标将使本轮失败；未标记必需的指标缺失时，保留空值并记录 `metric_errors`。不会用上一轮数值或 0 补齐。保存的是每次完整实验的最终结果，不是逐次寻优迭代的完整历史。

## 4. 统一保存与统计

每次启动测试创建独立时间目录，避免覆盖：

```text
results/<时间戳>/config.json
results/<时间戳>/runs.jsonl
results/<时间戳>/runs.csv
results/<时间戳>/summary.csv
results/<时间戳>/logs/round_0001.log
```

| 文件 | 内容 |
| --- | --- |
| `config.json` | 本次实际使用的配置快照 |
| `runs.jsonl` | 每轮追加的原始记录，包括参数、错误和结构化结果 |
| `runs.csv` | 每轮一行；CSV 使用 UTF-8 BOM，便于 Excel 打开 |
| `summary.csv` | 每个配置组一行的汇总统计 |
| `logs/round_*.log` | 每轮独立的原始 logging 日志 |

明细包括配置组、轮次、状态、请求/实际常数、开始结束时间、寻优耗时、总耗时、Reset 状态、失败原因及配置的数值指标。

统计包括计划次数、已执行次数、成功/失败次数、成功率；成功轮次的寻优耗时及各指标的有效样本数、均值、样本标准差、最小值、最大值、中位数。只用 `status=passed` 的有效数值计算；样本数为 1 时标准差记 0；没有样本时留空。成功率分母是已执行次数，未执行次数可由 `planned - executed` 得到。

`duration_s` 从点击 Start 前计时到进入结果状态，不含参数准备和 Reset；`elapsed_s` 包含整轮。失败轮次的等待时间只保存在明细，不混入成功轮次的耗时统计。成功表示到达结果状态、满足必需采集要求并完成 Reset，不代表已经验证算法收敛或寻优质量。

每轮保存后重建 CSV；后续轮次超时不会丢失此前已保存的结果。能恢复输入状态时按配置继续，最终只要有失败轮次，pytest 仍返回失败。无法完成 Reset 时停止运行后续轮次。

## 5. 在 CPQPBOP 设备环境运行

需要已有项目环境：`cpfr`、`cpqpbop`、`sw.cm`、生成的 `CPQPBOPTC` proto、PySide2、pytest 和提供 `qtbot` 的 pytest-qt。请核对 proto 导入路径及消息类名。`QT_API` 已在测试文件导入 Qt 前设置为 `pyside2`。

在仓库根目录执行：

```bash
# 常数 1、2、3 各运行一轮
python -m pytest testcases/test_cpd_process/test_pbop_start_reset.py -s -v --log-cli-level=INFO

# 沿用当前常数，寻优 10 轮
python -m pytest testcases/test_cpd_process/test_pbop_optimization.py -s -v --log-cli-level=INFO

# 指定自定义配置和输出目录
PBOP_CONFIG=/absolute/path/config.json PBOP_OUTPUT_DIR=/absolute/path/results python -m pytest testcases/test_cpd_process/test_pbop_optimization.py -s -v --log-cli-level=INFO
```

也可直接 `python testcases/test_cpd_process/test_pbop_optimization.py`。不要对同一设备并行执行两个入口。界面隐藏行为沿用提供的代码。

## 6. 离线验证

```bash
python -m unittest discover -s tests -v
```

也可安装 pytest 后运行 `python -m pytest`。`pytest.ini` 默认只收集离线测试，设备测试通过上面的完整路径显式选择。

离线测试覆盖回读不一致阻止 Start、结果在 Reset 前采集、逐轮隔离、超时后的记录与恢复、Reset 失败停止、缺失指标及统计口径。它们使用模拟 fixture 和 manager，不验证现场设备、常数热加载或实际结果字段。
