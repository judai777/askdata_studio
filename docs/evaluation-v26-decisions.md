# 评测第二十六阶段决策记录：P0 首轮人工审核结果

状态：首轮审核已完成，2026-10-31。承接[首轮审核材料](evaluation-p0-review-round1.md)。审核记录原文保存在本地 `backend/data/eval_reviews.json`，该文件被 Git 忽略；本文只记录结论与后续工程顺序。

## 审核结论

项目使用者逐题作出判断，管理员页面按其决定记录了全部 14 题：通过 8、退回 6、待审核 0、过期 0。通过题为 `mock.region_sales`、`mock.customer_level`、`mock.date_range`、`time.single_day`、`time.history_july`、`route.greeting`、`mock.clarify_orders`、`guard.restricted_database`。

退回题为 `join.customer_orders`、`grain.unique_customers`、`metric.paid_vs_list`、`metric.list_amount`、`mock.target_comparison`、`time.july_august`。前四题在三套现有 fixture 上漏掉 2026 年 8 月过滤仍得相同结果；`time.july_august` 的 8 月当前表分支同样存在这个盲点。`mock.target_comparison` 除边界数据不足外，标准 SQL 还缺少订单侧的 8 月日期过滤，与修订后题面不一致。

## 修复顺序

1. 先为 `mock.target_comparison` 的订单侧补 8 月闭开日期过滤，重新核对三套现有结果，并升 oracle 版本指纹。
2. 增加继承现有边界数据的新版本 fixture，在 `orders_current` 中放入 8 月以外的已支付订单；旧三套 fixture 保持原样。对六道退回题分别构造漏月份过滤错误 SQL，要求新 fixture 可识别。该新增数据也会改变其他使用当前表任务的新 fixture 预期结果，须逐题核对。

   实施中审计发现：若跨月订单沿用 8 月已出现的客户，地区去重客户数仍不变。新 fixture 因此还需追加一名仅在跨月订单出现的测试客户，并按 `customer_id` 关联；否则无法识别漏月份过滤的去重统计错误。此增量只作用于新 fixture。
3. 更新离线门槛、前端数据集选项、测试和审计材料。现有题目审核指纹包含**全部 fixture 的哈希集合**，新增 fixture 后 14 题都会显示“审核已过期”；历史结论仍保留，管理员需按新数据重新审核，不能自动沿用通过状态。
4. 修复后再开展 49 题开发集的基准建设。真实模型指标仍须另行运行和人工复核。

本轮人工审核只确认题目及评测依据的可信度；通过 8 题不代表模型在这些题上的通过率。
