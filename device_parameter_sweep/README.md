# 设备寻优参数批量对比

这套代码沿用你提供的 `cpfr_test + qtbot + ConstantManager` 方法，在真实 CPQPBOP 环境依次执行：全部方案预检 → 备份原常数 → 每轮重新读取 → 一次写入本组完整参数 → 服务回读校验 → Start → 等待结果 → 采集结果 → Reset → 下一轮。批次结束或发生可恢复异常后，仅恢复本批修改过的字段，并回读校验。旧目录和旧测试不变。

**这里交付的是设备批测程序和离线模拟验证，不是设备已经执行完成的测试报告。** 当前没有设备的 CPFR/Qt/常数服务及实际结果对象，不能从离线测试推断寻优效果。

## 1. 先看参数对应关系

以下前缀全部是你提供的 proto 原始字段名：

- `S = PGEC_CPQPBOP_SEARCH_SCENARIO.search_config`
- `C = PGEC_CPQPBOP_SEARCH_SCENARIO.search_calib_config`
- `I = PGEC_CPQPBOP_SEARCH_SCENARIO.input`

| 想比较的参数 | 常数路径 | proto 默认值 | proto 范围 | 本次候选值 |
| --- | --- | --- | --- | --- |
| 优化器重新启动次数 | `S.n_restarts_optimizer` | 10 | 0～20，整数 | 0、10、20 |
| EI 初始值 | `S.xi_init` | 0.1 | 0.01～0.5 | 0.01、0.1、0.3 |
| EI 最终值 | `S.xi_final` | 0.001 | 0.001～0.01 | 0.001、0.005、0.01 |
| sigma3_rel | **当前 proto 没有** | — | — | 明确标记未测试 |
| global probability | **当前 proto 没有** | — | — | 明确标记未测试 |
| ls bounds lower limit | **当前 proto 没有** | — | — | 明确标记未测试 |
| ls bounds upper limit | **当前 proto 没有** | — | — | 明确标记未测试 |
| 收敛度评估点数量 | `C.convergence_point_num` | 5 | 2～20，整数 | 3、5、10 |
| 收敛度 | `C.convergence_tol` | 0.1 | 0～1 | 0.05、0.1、0.2 |
| 采样点最小距离约束 | `S.min_dis` | 0.5 | 0～100 | 0、0.5、2 |
| 平台区 ED 波动范围 | `S.platform_error` | 0.02 | 0～0.1 | 平台模式下 0.01、0.02、0.05 |

这些候选值用于比较敏感性，不是已经在设备上证明最优的推荐值。所有写入值都受上表约束；例如不能把 `xi_final` 配成 0.0001，也不能把 `initial_sampling_num` 配成 10（你当前 proto 下限是 20）。

特别注意：

1. `I.convergence_tol_threshold` 也是收敛相关字段，默认 0.1；本程序先固定它，只改变 `C.convergence_tol`。仅凭 proto 不能确定部署算法使用哪个字段、是否同时使用。若算法实际只读 input 的阈值，必须先调整参数映射/固定条件，不能把本组结果解释为标定字段的影响。默认显式设 `I.convergence_enable=true`。
2. `n_restarts_optimizer` 不等于 EI 候选点多起点优化的次数；它最终传给哪一个优化器，要以部署算法调用为准。proto 无法证明具体接线。
3. proto 没给出 `xi` 的缩放规则、`min_dis` 的距离定义或 `platform_error` 的计算公式；程序保持原值写入，不擅自乘 ED 范围或替换成百分数。

## 2. 每个文件做什么

| 文件 | 作用 | 通常需要修改吗 |
| --- | --- | --- |
| `sweep_config.json` | 候选值、固定条件、重复次数、指标来源 | **主要改这个** |
| `test_parameter_sweep.py` | 设备 pytest 入口，沿用 App、Presenter、subsystem | 项目类名/导入路径改变时修改 |
| `parameters.py` | 7 个参数的路径、类型、允许范围及 4 个缺失项 | proto 或算法映射改变时修改 |
| `plan.py` | 展开单因素计划并预览 | 通常不用 |
| `runner.py` | 写入、回读、Start、采集、Reset、恢复 | 通常不用 |
| `constant_manager.py` | 继承仓库原 manager；增加副本预检和按字段恢复 | 通常不用 |
| `tests/test_sweep.py`、`tests/test_constant_manager.py` | 不连接设备的回归验证 | 通常不用 |

复用仓库中的 `cpd_modules/pbop_results.py` 保存日志和统计，不需要复制、替换已有工具代码。

## 3. 默认怎么比较

默认只运行已知的 `search_objective="Maximum"`。所有 7 个可写参数每轮都回到显式基准，只有本组的一个参数改变。与基准相同的候选值共用一组 baseline，避免重复运行完全相同的配置。

六个 Maximum 参数各有两个非基准值，加一个 baseline，每个目标各 13 组。默认分别测试 ED、ED/PF：**26 组 × 每组 3 次 = 78 次完整寻优**，统计按目标分开。顺序按 `seed=42` 打乱，以减弱固定顺序与设备时间漂移的混淆；该 seed 只控制批测组顺序，不是设备算法或数据模型的随机种子。组内仍连续重复，长时间测试可自行分批复测。

`platform_error` 在 Maximum 批次中固定为基准值；平台模式的真实名称未提供，因此它的对比默认暂缓，清楚记录在 `parameter_support.json`。不要把 Maximum 测试中平台参数的固定值当成已验证其作用。

要比较每组 100 次，把 JSON 顶层改为：

```json
"repeat": 100
```

此时是 **26 组共 2600 次**，不是所有配置合计 100 次。`best_value_mean` 是该组成功且采集到有效结果的实验最终最佳值平均数；同时看 `best_value_count`，不要把少于 100 个样本当成 100 次平均。先减少 `sweeps` 或 `repeat` 调通一小批，再扩展实验规模。只测 ED 可将 `targets` 改为 `["ed"]`，只测 ED/PF 则用 `["ed_pf_ratio"]`。

默认 `max_search_time=3600` 秒，`run_timeout_ms=3900000` 毫秒，留有结束处理时间。78 次若每次耗满软件时限，仅寻优就约 78 小时，另外还有准备和 Reset。不要在同一台设备并发启动多个批次。

## 4. 在设备上使用

先将仓库放到能导入 `cpfr`、`cpqpbop`、`sw.cm` 和生成的 `CPQPBOPTC_pb2` 的项目环境，安装项目对应版本的 PySide2、pytest、pytest-qt。保留项目原来的启动环境、Python 搜索路径及 RPC 服务配置。

在 **S_TEST 根目录**执行计划预览；这条命令不导入 CPFR/Qt、不写设备：

```bash
python -m device_parameter_sweep.plan
```

编辑 `sweep_config.json`，按第 5 节接好真实结果采集后，显式运行设备入口：

```bash
python -m pytest device_parameter_sweep/test_parameter_sweep.py -s -v --log-cli-level=INFO
```

也可以直接执行：

```bash
python device_parameter_sweep/test_parameter_sweep.py
```

选择独立配置、输出目录：

```bash
PBOP_SWEEP_CONFIG=/absolute/path/my_sweep.json PBOP_SWEEP_OUTPUT_DIR=/absolute/path/results python -m pytest device_parameter_sweep/test_parameter_sweep.py -s -v --log-cli-level=INFO
```

`PBOP_SWEEP_CONFIG` / `PBOP_SWEEP_OUTPUT_DIR` 独立于旧入口的 `PBOP_CONFIG` / `PBOP_OUTPUT_DIR`，不会误用旧批测配置。

## 5. 必须接入实际结果来源

**proto 定义的是输入/配置，不包含最终最佳值和结果对象路径。当前没有提供真实结果日志，所以仓库不能预先知道如何从你的部署程序中提取这些数值。** 默认约定读取本进程的 `PBOP_RESULT` JSON 日志，`best_value` 是必需指标；如果实际程序未输出该日志，测试会在第一轮明确报“缺少必需结果指标”，不会用 0 或上一轮结果填充。

三种方式任选其一，均在 Reset 前采集：

**A. 使用真实结果对象。** 已确认 `cpfr_test` 下的路径后，将配置替换为实际路径。例如下面仅在实际有 `cpfr_test.result["best_value"]` 时适用：

```json
"metrics": {
  "best_value": {"fixture_path": "result.best_value", "required": true},
  "best_x": {"fixture_path": "result.best_x", "required": true},
  "best_y": {"fixture_path": "result.best_y", "required": true},
  "best_z": {"fixture_path": "result.best_z", "required": true},
  "iterations": {"fixture_path": "result.iterations", "required": true}
}
```

**B. 使用现有最终结果日志。** 根据真实日志调整正则，不能把示例文字直接当成设备格式：

```json
"metrics": {
  "best_value": {
    "log_pattern": "最终最佳值\\s*=\\s*(?P<value>[-+0-9.eE]+)",
    "group": "value",
    "required": true
  }
}
```

**C. 在部署程序已有的最终结果位置补一条日志。** 将下列右侧变量替换成该位置真实存在的变量，并在进入结果状态之前同步输出：

```python
logger.info("PBOP_RESULT %s", json.dumps({
    "best_value": float(best_value),
    "best_x": float(best_x),
    "best_y": float(best_y),
    "best_z": float(best_z),
    "iterations": int(iterations),
}))
```

方式 C 要在部署程序中自行接入；本仓库未擅自修改它。日志方式仅捕获当前 Python 进程传播到 root logger 的 `logging` 消息，不包含 `print()`、子进程或远端日志文件。源 logger 须允许相应级别。每轮新建采集器，参数刷新阶段的旧日志不参与指标提取；仍应确保部署侧不把上轮延迟日志送进下一轮。按实际情况配置 `metric_logger_prefixes` 限定指标来源。

只想先检查 Start/Reset 和常数读写，可临时将 `metrics` 设为 `{}`；此时无最佳值统计，不能评估寻优质量。`passed` 只表示配置流程、必需指标和 Reset 成功，不代表已经检验算法结果页的业务成功标志、全局最优性或真实收敛。

## 6. 确认应用真的使用了新常数

服务回读正确，只证明常数服务保存了值。如果 App/Presenter 在构造时缓存常数，原代码在应用已创建后修改常数可能不影响下一次 Start。程序不会把这种情况冒充验证成功：默认每轮 `runtime_verified=false`。

可按项目真实接口提供两个 hook，配置为 `模块名:函数名`：

```json
"hooks": {
  "before_start": "my_project_hooks:refresh_constants",
  "verify_runtime": "my_project_hooks:read_runtime_constants"
},
"require_runtime_verification": true
```

`before_start(fixture, requested)` 在服务回读后、Start 前调用，应使用项目已有接口刷新配置；`verify_runtime(fixture, requested)` 返回**算法实际将使用的配置**，映射键与 `requested` 的完整 proto 路径一致。它必须从实际算法/Presenter 配置读取，不能直接 `return requested`，也不能只重读常数服务。任一实际值不符或缺失时禁止本轮 Start。不要把未经核实的刷新方法名填进去。

这些 hook 默认是 null，因为当前没有提供部署程序的热加载接口。若配置只在 Start 内加载，需先按实际调用时序调整验证位置；当前 hook 适用于可在 Start 前刷新并检查配置的项目。

## 7. 平台、ED/ED/PF 和六种模型

平台对比：在 `plateau.search_objective` 填入部署程序**实际识别的平台模式字符串**后，将 `enabled` 设为 true。每个目标追加 3 组平台参数实验，每组采用相同基准，其 `platform_error` 分别为 0.01、0.02、0.05。两个目标、默认重复数 3 时，Maximum 和平台合计 96 次。不内置猜测的 `Plateau` 字符串。

ED/ED/PF：已按本次确认的设备映射实现，下表的选择值每轮都显式写入并回读。通过顶层 `targets` 选择要测试的目标，不要在 `common_constants` 重复配置选择开关。

| `targets` 值 | `search_select_ed` | `search_select_pf` | 测试目标 |
| --- | --- | --- | --- |
| `ed` | true | false | ED |
| `ed_pf_ratio` | true | true | ED/PF |

六种数值模型：proto 里没有 `DATA_MODEL`、`MODEL_MODE` 或模型 seed 字段，所以不能通过本程序的常数管理自动切换六个模型。在各模型实际部署/切换完成后分别跑一批，修改 `context.model_label` 标记来源。`context` 只写入报告，不改变设备模型，也不构成模型身份校验。

## 8. 看哪些输出

每批创建独立的 `results/device_parameter_sweep/<时间戳>/`：

| 文件 | 内容 |
| --- | --- |
| `config.json` | 展开后的全部组、候选值、指标、标签和固定条件 |
| `parameter_support.json` | 4 个 proto 缺失项，以及平台对比是否暂缓 |
| `preflight.json` | 全组路径/类型/配置预检结果 |
| `original_constants.json` | 开始前修改目标字段的原值和 proto2 presence |
| `runs.jsonl`、`runs.csv` | 每轮的参数、回读值、结果、耗时、错误和 Reset 状态 |
| `summary.csv`、`comparison.csv` | 相同的分组汇总，CSV 可直接用 Excel 打开 |
| `logs/round_*.log` | 每轮独立运行日志 |
| `restoration.json` | 原常数恢复是否成功以及失败原因 |

重点看 `comparison.csv` 中的 `target`、`changed_parameter`、`parameter_value`、`planned`、`executed`、`passed`、`failed`、`best_value_count`、`best_value_mean`、`best_value_std`、`iterations_mean`、`duration_s_mean` 和 `runtime_verified_passed`。只有配置了对应指标才会有该指标的统计列。成功率分母是已执行次数；未执行次数为 `planned-executed`。失败轮次不混入成功轮次的均值，缺失可选指标留空并记录错误，不能忽略样本数比较平均值。

默认任何一轮失败即结束当前批次；设置 `continue_on_error=true` 后，只有 Reset 回到输入状态才会继续。Reset 失败始终停止，且不会在仍运行/结果状态时写恢复常数。恢复失败也会使测试失败，查看 `restoration.json`，待设备回到输入状态后根据原值备份处理。断电或强制杀进程无法执行 Python finally，原值文件仍保留；恢复使用最新整对象，只修改本批触及字段，不整体覆盖 CPD 更新过的其他值。测试期间应避免另一操作者并发修改相同字段。

此版本采集每次完整寻优的最终指标，不从未知部署接口推测逐迭代历史或理论最优值。若需要 X/Y/Z 到理论最优位置的偏差，需额外提供对应模型的理论位置及实际结果来源，不能用常数的范围中心代替。

## 9. 缺失的 4 个参数怎样补齐

目前将这 4 个名字放入 `sweeps` 会在连接设备/写入常数前报错。不能把它们写成某个现有字段，或只保存在 JSON 中就声称算法采用了该值。

完整接入需同时完成：在实际 proto 为新字段分配未占用编号 → 重新生成 pb2 并部署 → 在实际算法调用处读取并传参 → 在本程序登记路径/范围 → 用运行时 hook 或部署端日志验证。

依据你提供的 `CPQPBOPSearchConfig` 当前最大编号 13，可讨论使用 14～17，但必须先核对设备分支是否已经占用；新增字段建议先采用 optional，避免让既有 proto2 消息因缺少 required 字段无法初始化。`global probability` 的变量名到底是 `global_prob` 还是其他名字，以实际算法为准。

长度尺度上下限应先确定含义：如果是比例字段，可定义 `ls_bounds_lower_ratio`、`ls_bounds_upper_ratio` 并在算法中乘各轴搜索跨度；如果实际接口接受绝对长度，就应使用绝对长度和对应单位。不能把 `0.01` 直接当作两种含义通用的下限。`sigma3_rel` 应与部署端采用的统计口径和目标值缩放一致。当前代码不修改设备 proto 或部署算法。

## 10. 离线检查

在仓库根目录执行，不需要设备依赖：

```bash
python -m unittest discover -s device_parameter_sweep/tests -v
python -m unittest discover -s tests -v
python -m device_parameter_sweep.plan
```

覆盖单因素隔离、范围/类型/缺失字段拒绝、全部方案预检、结果在 Reset 前采集、回读不符禁止 Start、失败样本排除、超时后恢复、Reset 失败停止、运行时缓存不符、原常数恢复及恢复失败。这里的 App、常数服务和结果是模拟对象，设备联调仍须在实际项目环境执行。
