# AskData Studio

AskData Studio 是一个本地运行的自然语言问数项目。前端使用 Vue 3，后端使用 FastAPI 和 LangGraph；系统能够检索字段级 Schema、生成并执行 SQL，并以表格和文字形式返回结果。

## 环境要求

- Python 3.11
- Node.js 20.19 或更高版本
- 可用的大模型、Embedding 和 Rerank API Key

以下命令均从项目根目录开始执行，不依赖固定的本机路径。

## 安装环境

### 后端

Windows PowerShell：

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

macOS 或 Linux：

```bash
cd backend
python3.11 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

编辑 `backend/.env`，至少填写 API Key。默认配置使用 `text-embedding-v4` 和 `qwen3-rerank`：

```env
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3.7-plus
EMBEDDING_MODEL=text-embedding-v4
RERANK_MODEL=qwen3-rerank
```

首次克隆仓库后，在 `backend` 目录生成固定的示例 CSV 和电商合成数据（不需要模型）：

```bash
python seed_demo_data.py
python scripts/generate_ecommerce_data.py
python scripts/generate_ecommerce_schema.py
```

生成文件保存在 `backend/data/`，不提交到 Git。离线演示与离线回归门槛无需填写 API Key。

### 前端

```powershell
cd frontend
npm install
```

## 启动服务

分别打开两个终端。

后端（Windows）：

```powershell
cd backend
.\.venv\Scripts\python.exe run.py
```

后端（macOS 或 Linux）：

```bash
cd backend
./.venv/bin/python run.py
```

前端：

```powershell
cd frontend
npm run dev
```

启动后访问：

- 前端页面：`http://127.0.0.1:5173`
- API 文档：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/api/health`

## 使用方法

使用内置账号登录：

- 管理员：`admin` / `admin123`
- 电商运营用户：`sales` / `sales123`（访问 `ecommerce_ops`）

登录后可直接输入问题，例如：

- 查询本月各地区销售额
- 按客户等级统计本月销售额
- 对比本月各地区销售额和销售目标

右侧的“查询字段”用于明确本次 SQL 必须使用的字段；保存的结果表用于后续问答和综合分析。查询完成后可以查看 SQL、翻页、导出 Excel，并按需保存字段或结果。

## Agent 评测台

使用管理员账号登录后，在左侧点击“Agent 评测台”。选择题目及运行模式：

- 离线演示：无需模型或 API Key，运行固定样例并展示任务、判分、轨迹、历史和重放流程。分数不代表真实模型能力。
- 实时评测：调用 `backend/.env` 中配置的远程 Chat、Embedding 和 Rerank API；每次最多 5 题，顺序执行，可能产生费用。无需下载本地模型。

任务集 v2 增加固定时间范围、澄清恢复与受限账号权限探针。可选 1 或 3 次试验；三次实时试验最多选 3 题。Pass³ 仅用于三次模型任务均通过的稳定性观察，权限探针单独运行一次；离线三次试验仍只是流程演示。详见[第二阶段决策记录](docs/evaluation-v2-decisions.md)。

任务集 v3 加入实付金额与订单原价口径、单日边界及历史月份。评测从 UI 启动后在后台顺序运行，页面每约 1.5 秒刷新进度；离开页面后可从历史记录重新打开。当前本地服务一次只接受一组后台评测，服务重启会将未完成运行标为中断。详见[第三阶段决策记录](docs/evaluation-v3-decisions.md)。

评测台底部的“回归对比”可选择两次已完成运行，查看共同题中的退化、改善及判分项变化。对比要求任务版本、数据指纹、模式和试验次数一致；只在一侧出现的题会单独列出。对比不会调用模型。详见[第四阶段决策记录](docs/evaluation-v4-decisions.md)。

评测报告还会显示试验耗时、Chat/Embedding/Rerank 调用次数及供应商返回的 token 用量覆盖率。若接口不返回用量，会显示“未知”；不会由此推算费用。两次运行的共同题也可查看耗时和用量变化。详见[第五阶段决策记录](docs/evaluation-v5-decisions.md)。

任务集 v4 将标准 SQL 放在独立的 `backend/evals/oracles.json`，目录接口仅展示题目与判分要求。运行时可选原始数据及继承式 `edge_v1`、`edge_v2`、`edge_v3` 边界数据；边界行只在临时目录追加，原始 CSV 不会更改。不同 fixture 的运行不能直接比较。详见[第六阶段决策记录](docs/evaluation-v6-decisions.md)、[第九阶段决策记录](docs/evaluation-v9-decisions.md)与[第二十七阶段验收](docs/evaluation-v27-validation.md)。

评测台的“评测集区分力审计”会对固定错误 SQL 在四套数据上做本地对照，分别显示识别、漏检和无效样例。审计不调用模型；这些数字只代表已列出的错误样例。当前 20 条样例在 `base`、`edge_v1`、`edge_v2`、`edge_v3` 上分别识别 10、13、14、20 条；新增六条漏月份反例只在 v3 被识别。详见[第二十七阶段验收](docs/evaluation-v27-validation.md)。

后台评测运行时可点击“停止后续试验”。当前已开始的试验会等待返回，之后不再执行剩余试验；已完成的轨迹和调用指标仍可查看，部分完成的题不计入通过数。已取消运行不能重放或用于回归对比。详见[第十阶段决策记录](docs/evaluation-v10-decisions.md)。

实时运行还会保存提示词相关源码、工作流源码的短指纹，以及不含密钥和接口地址的模型配置快照。回归对比显示两次实现是相同、已变化还是旧记录信息不足；模型和 Prompt 不同仍允许对比。详见[第十一阶段决策记录](docs/evaluation-v11-decisions.md)。

任务集 v5 增加客户等级订单数、地区去重客户数和历史/当前跨月汇总，共 14 题。当前 v5.2 的目标对比题补齐订单月份条件，管理员评测台可逐题查看审核说明、标准 SQL 与四套数据预期结果，填写意见后标记通过或退回；题目或数据变化会使旧审核过期。首轮人工审核为通过 8、退回 6；新增 v3 后全部审核记录均需复审，历史意见仍保留。审核记录保存在本地 `backend/data/eval_reviews.json`，不随题库提交。详见[第二十六阶段决策记录](docs/evaluation-v26-decisions.md)。

实时评测现在记录请求预处理、Schema 检索、数据库 Agent、MCP 工具与结果整理等阶段的耗时和失败类型。运行报告可查看逐次阶段轨迹、逐题与整次运行汇总，回归对比可查看共同题的阶段变化。模型调用摘要标记所属阶段；离线演示与旧记录没有阶段数据时不显示虚构耗时。工具耗时包含在 Agent 阶段中，不能与父阶段相加。详见[第十三阶段决策记录](docs/evaluation-v13-decisions.md)。

P2 失败归因已完成：失败试验给出主要排查位置，区分工作流阶段异常、确定性判分未通过与评测执行异常；运行报告按失败试验聚合，回归对比显示归因变化。归因是排查起点，不将结果不符武断归因于某个模型或检索阶段。详见[第十五阶段决策记录](docs/evaluation-v15-decisions.md)。

已完成、已取消或远程请求预算耗尽的运行可在管理员评测台下载 JSON 摘要。文件包含任务和数据版本、实现来源、审核覆盖度、逐题判分与调用/阶段指标；不包含原始问题、SQL、结果行、模型正文或异常文本。导出只读取历史记录，不再次调用模型。详见[第十四阶段决策记录](docs/evaluation-v14-decisions.md)。

实时评测可在管理员页面选择已登记的 Chat 模型。`backend/.env` 中可设置 `EVAL_LLM_MODELS=qwen-plus,qwen-turbo`，当前 `LLM_MODEL` 自动包含在列表中；这些候选共用当前远程服务与密钥，不影响普通问数。每次运行默认最多发送 50 次远程 HTTP 请求，页面可选 20、50 或 100 次，包含重试。模型对比需要分别确认并启动两次运行；历史对比会标出模型和预算差异。详见[第十七阶段决策](docs/evaluation-v17-decisions.md)与[第十八阶段决策](docs/evaluation-v18-decisions.md)。

新运行记录判分器指纹；已知不同判分规则的两次运行不能直接比较。历史重放会显示原始与本次判分器指纹，旧运行缺失指纹时提示无法确认。详见[第十九阶段决策](docs/evaluation-v19-decisions.md)。

受控 A/B 流程：先完成一次实时基线运行，在其报告中选择候选 Chat 模型并点击“以此为基线运行候选”。系统固定原选题、数据集、试验次数和远程请求上限；只有任务、标准答案、判分器、提示词、工作流与非 Chat 配置仍一致才允许创建。候选完成后页面会填入回归对比。旧运行缺少完整实验条件时需重新建立基线。详见[第二十阶段决策](docs/evaluation-v20-decisions.md)与[评测结构和流程图](docs/evaluation-architecture.md)。

运行报告中的每次试验可由管理员标注完成度、安全、过程和回答质量，并写下判断依据。页面底部“人工标签与判分校准”只统计实时运行的有效标签，按判分器版本展示一致与分歧；离线演示标注仅用于练习。标签保存在本地 `backend/data/eval_labels.json`，不随 Git 提交。详见[第二十一阶段决策](docs/evaluation-v21-decisions.md)。

离线回归门槛不需要本地模型或远程 API 密钥。在 `backend` 目录运行 `python seed_demo_data.py` 准备固定 CSV，运行 `python -m unittest discover -s tests -q`，再运行 `python -m app.eval_gate`。门槛核对任务、标准答案、错误 SQL 和四套 fixture 的版本/指纹，并要求 `edge_v3` 识别全部已登记错误 SQL、所有 fixture 无无效 SQL。题目或数据有意变更时，先核对审计结果，再显式更新 `backend/evals/offline_gate.json`；门槛不会自动更新基线。GitHub Actions 同步执行这些步骤和前端构建。详见[第十六阶段决策记录](docs/evaluation-v16-decisions.md)与[第二十七阶段验收](docs/evaluation-v27-validation.md)。

运行记录保存在 `backend/data/eval_runs/`（被 Git 忽略）。从[首版工程阶段验收与两幅流程图](docs/evaluation-milestone-1.md)开始体验；详细结构见[评测结构与流程](docs/evaluation-architecture.md)，决策历程见[评测决策记录](docs/evaluation-decisions.md)。

管理员评测台的“批量候选 SQL 验证”可导入或粘贴 `{"fixture_id":"edge_v3","candidates":[{"task_id":"mock.region_sales","sql":"SELECT ..."}]}`，一次验证 1–20 道已审核的普通 SQL 题。它使用固定数据集和标准答案判分，不调用模型；页面展示逐题结果，下载的 JSON 汇总不含 SQL、结果行或错误正文。详见[第三十八阶段决策](docs/evaluation-v38-decisions.md)。

目标域 `ecommerce_ops` 已有 3 道开发题草稿，可在管理员评测台逐题查看标准 SQL 和 base 结果，并进入人工审核。它们尚不构成模型成绩；目标域当前只支持 base 数据集，同次运行只选同一数据库。数据指纹覆盖目标域 CSV 内容。详见[第三十九阶段决策](docs/evaluation-v39-decisions.md)。

自动化评测作业可在 `backend` 目录运行 `python -m app.eval_campaign --campaign-id demo00000001 --task-id mock.region_sales --task-id mock.customer_level`。重复相同命令会核对版本并跳过已完成分片；大量题目可用 `--task-file` 指向包含 `{"task_ids":["..."]}` 的 JSON 文件。作业按单次运行限制分片，记录存于本地 `data/eval_runs/_campaigns/`，管理员评测页可查看作业进度和关联运行。无模型作业用于验证编排，不代表 Agent 准确率；实时作业需显式传入 `--mode live --allow-remote --request-limit 20` 并配置远程密钥，重试中断片还需 `--retry-interrupted`。结构依据见[评测平台调研](docs/evaluation-platform-research.md)和[第四十阶段决策](docs/evaluation-v40-decisions.md)。

管理员评测页现在也可按当前选题创建分片作业并续跑未完成片；实时模式会再次确认整个作业的远程请求上限。逐题检查真实作业的运行轨迹后，可填写意见批准为基线，再选候选作业检查完整性、实验条件和逐题退化。基线与门槛记录只含安全摘要。未配置远程密钥时，页面不能产生新的真实模型成绩；无模型 CI 冒烟仍可运行。详见[第四十一阶段决策](docs/evaluation-v41-decisions.md)。

首次三题真实评测及修复记录见[第四十二阶段验收](docs/evaluation-v42-validation.md)：当前可审核作业 `stable000001` 在 `edge_v3` 上通过 1/3，SQL 执行 3/3，字段级 Schema 宏平均召回 100%；这只覆盖一轮三题样本。旧运行若没有逐请求预算记录，不能安全续跑；真实 MCP 工具轨迹的安全判分已按实际结构修复。

Prompt 受控对比与逐题人工审核材料见[第四十三阶段验收](docs/evaluation-v43-validation.md)：管理员可从已批准作业创建固定条件的 Prompt 候选；新 Prompt 尚未进行付费真实评测。

真实运行的阶段 Token 用量和 Schema 噪声分析见[第四十四阶段诊断](docs/evaluation-v44-validation.md)；管理员报告可查看各阶段已知用量及缺失覆盖。

紧凑 Schema 输入的可选评测版本及离线输入大小核对见[第四十五阶段验收](docs/evaluation-v45-validation.md)；真实模型效果仍待基线批准后验证。
