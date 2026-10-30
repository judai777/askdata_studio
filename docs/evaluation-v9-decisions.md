# 评测第九阶段决策记录

状态：已决策，2026-10-30。承接[第八阶段决策](evaluation-v8-decisions.md)。

## 目标与数据设计

关闭 `SUM(DISTINCT paid_amount)` 的已知漏检。`edge_v2` 继承 `edge_v1`，只追加一条华东已支付订单：使用新的订单 ID，实付金额与 `edge_v1` 的订单 `99001` 同为 700。正确的 `SUM(paid_amount)` 应计入两笔，错误的 `SUM(DISTINCT paid_amount)` 只计入一笔。

## 版本与运行规则

1. `edge_v2.json` 只描述相对于 `edge_v1` 的增量；运行时按 `base → edge_v1 → edge_v2` 在临时目录构建。原始 CSV 与 `edge_v1.json` 不修改。
2. `base` 和 `edge_v1` 的既有指纹保持原值；`edge_v2` 指纹包含 fixture ID、基础 CSV、两层增量清单。任何一层内容改变，`edge_v2` 旧运行停止重放。
3. 管理员可在运行 UI 选择 `edge_v2`。审计对三套 fixture 分别给出识别/漏检/无效计数，逐样例展示结果；总体“识别”仍表示至少一套 fixture 能识别。
4. `edge_v2` 是补足这 11 条固定错误样例的数据版本。全部识别只表示当前样例集被覆盖，不代表任务全集或任意模型错误都能被发现。
5. 错误样例清单更新为 v1.2，将原“新建 edge_v2”建议改为已实现的数据说明。

## 验收

- `base` 指纹仍为 `360b511959d6a4c4`，`edge_v1` 指纹仍为 `06d46c2bf6760fde`。
- 聚合去重错误在 `base`、`edge_v1` 漏检，在 `edge_v2` 被识别；11 条样例在 `edge_v2` 均能执行且被识别。
- `edge_v2` 离线评测可运行并重放，和旧 fixture 的运行不可直接比较。
- 后端测试、前端类型检查与生产构建通过。
