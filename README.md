# 超级投研 Agent (Super-Investment-Agent)

统一投资研究系统 —— 整合 **Research-Agent**（深度研究）、**Agent-for-Investment / Violetz**（对话 Agent）、**Stock_Assistant / Violet Z**（A 股行情 + 四大师评分）三大能力，前端为 Streamlit 看板，后端为 FastAPI 统一服务。

> 本系统**不提供任何虚拟数据**：行情、财报、基金净值均来自真实数据源（Sina / akshare / yfinance / 天天基金）；估值、评分、风险值均由代码按公式真实计算；缺失字段显式标注为 `None` / “—”，绝不编造。

---

## 一、功能总览

| 看板模块 | 能力 |
|---|---|
| 📊 行情与评分 | 搜索联想、实时报价、Plotly K 线、Violetz 推荐评分 + 四大师（巴菲特/芒格/段永平/达利欧）确定性评分、财务概览、公司信息 |
| 📈 深度研究 | 后台分析管线（财务/估值 DCF·SOTP/竞品/SWOT/地缘/供应链/事实核查）、进度条、PDF/HTML/Markdown 研报下载 |
| 💬 AI 对话 | 股票 / 基金双模式、意图→规划→执行→报告、SSE 逐 token 流式、多市场歧义澄清 |
| 📦 基金分析 | 基金搜索、实时净值、对话式分析 |
| ⭐ 自选股 | 股票 + 基金自选、实时数据、一键跳转行情 |
| 🧱 固收研究 | ①主体信用评级 ②利率与情景引擎 ③AI 固收对话 ④行动·风险·合规（JD 三职责） |
| 🧪 评测看板 | 检索质量 / 分层明细 / 基线对比 / 历史趋势 / 🌀 飞轮健康度（只读 `data/eval/*.json`） |

---

## 二、环境要求

- Windows 11（本机已验证环境）
- Python：`D:\python\python.exe`（已安装全部依赖）
- LLM：DeepSeek V4 Flash（Anthropic 兼容接口 `https://api.deepseek.com/anthropic`），密钥写入 `.env`
- 无需 NodeJS / Java / Redis / Neo4j

---

## 三、快速启动

### 方式一：一键启动（推荐）

1. 确保 `D:\Desktop\ClaudeProjects\Super-Investment-Agent\.env` 中已配置 `LLM_API_KEY`（从原项目配置读取，不硬编码）。
2. 双击 **`start.bat`** —— 自动弹出两个窗口：后端（`127.0.0.1:8000`）与看板（`http://localhost:8501`）。
3. 浏览器打开 **http://localhost:8501** 即可使用。
4. 需要停止时双击 **`stop.bat`**，或直接关闭两个服务窗口。

> `start.bat` 会自动检测 8000 / 8501 端口：若已被占用则跳过启动（防止重复实例）。

### 方式二：命令行手动启动

```bash
# 1. 启动后端（保持此窗口运行）
PYTHONIOENCODING=utf-8 D:\python\python.exe api/main.py

# 2. 另开一个窗口，启动看板
PYTHONIOENCODING=utf-8 D:\python\python.exe -m streamlit run ui/dashboard.py --server.port 8501
```

---

## 四、配置（`.env`）

复制 `.env.example` 为 `.env` 并填写：

| 变量 | 说明 | 默认 |
|---|---|---|
| `LLM_API_KEY` | DeepSeek API 密钥（必填，否则 LLM 功能不可用） | — |
| `LLM_API_BASE` | Anthropic 兼容接口 | `https://api.deepseek.com/anthropic` |
| `LLM_MODEL` | 模型名 | `deepseek-v4-flash` |
| `SERVER_PORT` | 后端端口 | `8000` |
| `SOURCE_*` | 数据源开关（yfinance/akshare/sina/fund） | `true` |
| `RAG_ENABLED` | RAG 增强（可选，默认关闭，避免拉取 torch 重依赖） | `false` |
| `CACHE_DB_PATH` / `WATCHLIST_DB_PATH` | SQLite 缓存 / 自选股库 | `data/*.db` |

> 真实密钥只存在于 `.env`（已被 `.gitignore` 排除），源码与文档不展示明文密钥。

---

## 五、后端 API 一览（`/api/v1`）

| 分组 | 端点 | 说明 |
|---|---|---|
| 基础 | `GET /health` | 健康检查 + LLM 就绪状态 |
| 行情 | `GET /stock/search?q=` · `GET /stock/{ticker}` · `GET /stock/{ticker}/history?period=` · `GET /stock/{ticker}/financials` · `GET /stock/{ticker}/info` | 搜索 / 报价 / K线 / 财务 / 公司信息 |
| 评分 | `GET /stock/{ticker}/score` | Violetz 推荐评分 + 四大师评分 |
| 深度研究 | `POST /analysis/run/{ticker}` · `GET /analysis/status/{ticker}` · `GET /analysis/result/{ticker}` · `POST /analysis/report/{ticker}` | 后台管线（异步任务） |
| 对话 | `POST /chat` · `POST /chat/stream` · `POST /chat/fund/stream` | 非流式 / 股票 SSE / 基金 SSE |
| 报告 | `GET /reports/list?kind=` · `GET /reports/download/{name}` · `POST /report/export` | 列表 / 下载 / 导出 |
| 自选股 | `GET /watchlist` · `POST /watchlist/add` · `DELETE /watchlist/remove` · `GET /watchlist/data` · `POST /watchlist/fund/add` · `DELETE /watchlist/fund/remove` · `GET /watchlist/fund/data` · `GET /watchlist/fund/quote/{code}` · `GET /watchlist/fund/suggest?q=` | 股票 / 基金自选与实时数据 |
| 固收·信用 | `GET /credit/issuer/{ticker}` · `GET /credit/action/{ticker}` · `GET /credit/status` | 单主体结构信用风险 / 行动方案 / 信用侧数据源状态 |
| 固收·利率 | `GET /rates/curve` · `GET /rates/forecast?horizon=` · `GET /rates/scenario` · `GET /rates/policy` · `GET /rates/stress` · `GET /rates/montecarlo` | 曲线 NSS 因子 / VAR 预测 / 情景库 / FOMC 鸽鹰 / 压力重演 / Monte Carlo |
| 固收·总览 | `GET /fixed-income/status` | 固收各服务进程内成败/时延指标 + 数据源开关 |

---

## 五·B、固收研究模块（扩展）

面向固收研究员岗位 JD 的三大职责，全部基于**零 key 官方数据源 + 真实计算**，严格遵循「不编造数据、缺失显式标注」红线（见计划书 `plan/计划书_固收研究扩展.md`）。

| JD 职责 | 模块 | 数据源 | 方法 |
|---|---|---|---|
| ① 主体信用评级 | `app/credit/` | SEC EDGAR（公司facts，费 filed 日期防前视）+ yfinance（市值/波动） | Altman Z'' / Ohlson O / Merton-KMV + LightGBM/MLP 混合 |
| ② 利率与情景引擎 | `app/rates/` | US Treasury 每日曲线（treasury.gov）+ FOMC 官方声明 | NSS 拟合（level/slope/curvature）+ VAR/walk-forward + 情景库 + Monte Carlo + 压力重演 |
| ③ 固收投研平台 | `app/agent`/`app/action` + `ui/pages/fixed_income.py` | 全部 + RAG 语料 | NL→工具路由→LLM 综合 + 确定性动作引擎（久期/曲线层面 + 风险信号 + 合规复核队列） |

**诚实边界**：① 无商业评级/真实违约 feed → 信用输出为结构模型**代理标签**，非评级机构评级；② 无债券价格/久期报价 feed → 动作只到「久期/曲线」层面，**不给价格级**止损止盈；③ 政策鸽鹰分为词典法启发式，非美联储官方口径。以上缺口一律在结果里显式标注并进合规复核队列。

**看板**：`ui/pages/fixed_income.py`（Streamlit，4 个 tab）。入口已接入 `ui/dashboard.py`。

**关键脚本**（在 `D:\Desktop\ClaudeProjects\Super-Investment-Agent` 下用 `D:\python\python.exe` 运行）：
- `scripts/verify_fi_data.py` · `verify_rates.py` · `verify_credit.py`：三个阶段引擎独立验证（真实数值 + 诚实降级）。
- `scripts/verify_fi_agent.py` · `verify_fi_action.py` · `verify_fi_dashboard.py`：Agent / 行动化 / 看板验证。
- `scripts/eval_fixed_income.py`：**评测集**——信用覆盖率/分区分布/结构分一致性 + 利率 VAR vs naive 诚实对照（无真标签不报 AUC）。
- `scripts/run_regression.py`：一键回归，独立跑上述 6 个 verify 脚本并汇总（任一失败退出码非零）。

---

## 五·C、可信层、五层评测与数据飞轮

产品需求文档：**[`docs/PRD_SIA_v2.md`](docs/PRD_SIA_v2.md)** —— 每条功能需求都挂着一条可执行的评测断言；工程实施计划见 `plan/计划书_可信层与数据飞轮_v2.md`。

### 可信层：每个数字都带出处

每次管线运行产出一份**事实台账**：`computed`（代码算）/ `source`（数据源取）/ `llm`（模型给）/ `imputed`（插补）/ `degraded`（降级占位），**没有第六种**。匹配不上任何归类的路径记为 `unknown`，评测直接判失败——映射表因此不会腐化。

### 五层评测

| 层 | 测什么 | 成本 | 通过门槛 | 当前值 |
|---|---|---|---|---|
| **L0** 台账闭合 | 每个数值是否登记在案、出处与期间是否齐全 | 零 | `unknown` = 0，全部断言过 | 131/132，24877 条事实（11 份产物；唯一失败是一份历史产物的 NaN，见 PRD FR-1） |
| **L1** 数值接地 | 模型文字里的**数值断言**（带单位/小数/千分位/货币）能否归因到台账；派生值须文本锚定后复算；含伪造 99.99% 反向控制 | 零 | 接地率 ≥ 0.98 且控制被检出 | **0.9876（21/22，1/4 产物未达标）** |
| **L2** 检索质量 | nDCG@k / Recall@k / MRR（人工标注集，固定跑 v1 只读快照） | 零 | nDCG@4 ≥ 0.5 | nDCG@4 0.9461 / Recall@4 0.8813 / MRR 0.96 |
| **L3** 生成质量 | 7 条确定性规则（期间一致、竞品真实、敏感性单调、分部占比、措辞红线、空章节、情景标注） | 零 | 全部硬项通过 | 77/77（11 份产物） |
| **L4** 端到端 | 3 支真实标的跑完整管线：成功/降级/token/耗时 | 真实 LLM | 3/3，降级均可解释 | 3/3 成功；221/261/85s；降级 4 条全部带原因（`data/eval/l4_report.json`，一次性基线） |

评测**只跑在落盘产物上**（`data/artifacts/`），不现跑管线：可复现、零 token。L4 例外，它要真跑，因此**不进日常门禁**。

```bash
# 离线四层（日常门禁，约 47s）
D:\python\python.exe scripts/run_eval.py --tier l0,l1,l2,l3
D:\python\python.exe scripts/run_eval.py --baseline          # 与基线比对，指标退化即失败

# 采集新产物（真实跑管线，然后四层评测会自动纳入）
D:\python\python.exe scripts/collect_artifacts.py

# L4 一次性端到端基线（真实 LLM，5–10 分钟，需显式确认）
D:\python\python.exe scripts/eval_l4_e2e.py --yes
```

### 数据飞轮

用 → 日志 → 标注 → 评测 → 改进。本版本接上了第一环：

- **采集**：对话页每条回答下 👍/👎（👎 可多选原因 + 备注）；研究页报告底部可按「整份报告 / 某个数字」反馈。
- **归因**：`request_id` + `msg_id` 是唯一钥匙，缺任一个的反馈被拒绝入库（孤儿数据）；`model` / `prompt_version` / `git_sha` 由**服务端**打戳，客户端伪造无效。
- **待审**：👎 记录镜像到 `data/flywheel/inbox.jsonl`，状态 `pending_review`，**绝不自动进回归集**——一次误操作就能把错误答案固化成金标准。
- **护栏**：`/api/v1/metrics` 的 `guardrails` 暴露 `grounding_rate`、`degraded_rate`、`llm_error_rate`、`feedback_negative_rate`、`feedback_coverage`，评测看板展示同一份值；分母为 0 时显示「—」而不是 0%。

### 诚实边界

- **单用户、样本个位数量级**：飞轮能证明「闭环机制已接通」，不能支撑任何统计显著的改进结论；不声称「飞轮带来 X% 提升」。
- **L2 语料是项目自生成的研报**：固定跑只读快照 `data/knowledge_db_v1`（2,041 切片 = 1,907 研报 + 100 条东财快讯 + 12 条宏观数据 + 22 条个股新闻/公告），不是外部权威语料；外部语料（政策/披露文件/论文）只进正式库、不入该快照；召回率为池化下界估计（标注池 = top-4 + 盲抽）。
- **L1 门槛定 0.98 而非 1.0**：残差逐条列在报告里，不靠放宽阈值掩盖。
- **L1 分母只算「数值断言」**：带单位/百分号、带小数位、带千分位、带货币符号，或处在带单位的区间里。品牌名「国窖1573」、法条年份「《1974年贸易法》」、海关编码「HS 8517/8471」这类不计入，但仍有 49/53 被台账解释、4 条无出处逐条列在报告里；年份计入 `year_ref`，期间一致性由 L3 负责。
- **派生值只在文本锚定成立时才认**：同句须出现指标名 + 两个年度（「营业利润率2025年33.1%（2022年20.5%），三年扩张1260bp」），或同句两个已接地数字之差（「从34.0%骤降至19.4%（-1460bp）」）。纯按值复算已试过并否决——它把「CR3 98.2%」凑成流动比率差、把「600 元」凑成 ROE 差，等于给编造发通行证。
- **当前 L1 仍未达标（17/18，聚合 0.9876）**：四份产物中 000858.SZ、AAPL 为 1.0，600519.SS 0.988，**0700.HK 0.9444 未过 0.98**。残差是同一条「美国基准利率 4.25–4.50%」——外部宏观基准，台账里不可能有，已登记为已知残差，不靠放宽匹配规则换绿。
- **分母变小只会更严**：0700.HK 最新产物因 Yahoo 限流丢了行情与同业营收，断言数从 48 降到 36，同一残差占分母的比例从 2.1% 升到 5.6%，因此由 0.9792 变成 0.9444。降级如实记在产物的 `price_history.reason` 里，不隐瞒。
- **评测每标的只取最新一份产物**：历史产物留在磁盘上不删除，被排除的名单逐份列在报告的 `skipped_superseded` 中。
- **已知缺口**：① 分部识别失败导致 SOTP 不出数时，报告跳过 SOTP 区块且不显示原因（`sotp_analysis.reason` 未渲染）；② 插补事实在台账里可追溯，但报告正文没有「数据说明」区块，读者看不出某个比率是填出来的。

---

## 六、运行测试

全部测试脚本（后端需已启动，见上）：

```bash
cd D:\Desktop\ClaudeProjects\Super-Investment-Agent
for f in scripts/smoke_phase0.py scripts/smoke_phase1.py scripts/smoke_phase2.py \
         scripts/smoke_phase3.py scripts/smoke_phase4.py scripts/smoke_phase5.py \
         scripts/smoke_phase6.py scripts/smoke_phase7.py scripts/test_e2e.py; do
  PYTHONIOENCODING=utf-8 D:\python\python.exe "$f" || break
done
```

| 脚本 | 覆盖 |
|---|---|
| `smoke_phase0` | LLM 客户端连通性 |
| `smoke_phase1` | 统一数据层（美股/A股/基金抓取 + 缓存） |
| `smoke_phase2` | 深度研究分析管线（AAPL + 600519 全流程） |
| `smoke_phase3` | 四大师专家评分引擎 |
| `smoke_phase4` | 对话 Agent 层（与原 Violetz 逐项比对 + 三类端到端对话） |
| `smoke_phase5` | 统一报告生成（PDF/HTML/Markdown + 图表） |
| `smoke_phase6` | FastAPI 后端全端点（TestClient，进程内） |
| `smoke_phase7` | 看板渲染 + 完整用户流程（真实 HTTP） |
| `test_e2e.py` | **端到端集成**：行情→评分→深度研究→对话→基金→自选股→报告下载（真实 HTTP） |

**可信层 / 评测 / 飞轮脚本**（全部离线，除 `eval_l4_e2e.py`）：

| 脚本 | 覆盖 |
|---|---|
| `eval_l0_ledger.py` | L0 台账闭合（48 条断言 × 4 产物） |
| `eval_l1_grounding.py` | L1 数值接地 + 伪造 99.99% 反向控制 |
| `eval_retrieval.py` | L2 检索质量（人工标注 25 查询，nDCG/Recall/MRR） |
| `eval_l3_generation.py` | L3 生成质量 7 条规则 × 4 产物 |
| `eval_l4_e2e.py` | L4 端到端一次性基线（**真实 LLM**，需 `--yes`） |
| `run_eval.py` | 五层统一入口：`--tier` / `--json` / `--baseline` / `--save-baseline` |
| `collect_artifacts.py` | 真实跑管线并落盘产物（评测的事实来源） |
| `verify_trust.py` | 降级显式化 + 插补可追溯（37/37） |
| `verify_evals.py` | **评测必须会失败**：构造坏数据验证反向控制与基线漂移报警 |
| `verify_feedback.py` | 数据飞轮：落库/孤儿守卫/版本戳/护栏/防污染（61/61） |
| `verify_eval_dashboard.py` | 评测看板渲染与只读约束（29/29） |
| `verify_prd.py` | PRD 与评测咬合：FR↔断言双向映射、数字与报告一致 |

**固收子项目脚本**（同样用 `D:\python\python.exe` 运行）：

| 脚本 | 覆盖 |
|---|---|
| `verify_fi_data.py` | 零 key 数据接入层（Treasury/EDGAR/FOMC，as-of 防前视） |
| `verify_rates.py` | 利率引擎（NSS + walk-forward + 情景库 + 压力重演） |
| `verify_credit.py` | 信用引擎（Altman Z''/Ohlson O/Merton/LightGBM） |
| `verify_fi_agent.py` | 固收 Agent（NL→工具路由→LLM 综合） |
| `verify_fi_action.py` | 建议转行动 + 风控信号 + 合规复核队列 |
| `verify_fi_dashboard.py` | 固收看板渲染（Streamlit AppTest） |
| `eval_fixed_income.py` | **评测集**（信用覆盖/分布/一致性 + 利率 VAR vs naive 诚实对照） |
| `run_regression.py` | **一键回归**：独立跑 verify_fi_data/rates/credit/agent/action/dashboard 并汇总 |

> 部分测试耗时较长：A 股专家评分约 30-60s、A 股深度研究管线约 140-160s。全部跑完约 15-20 分钟。
> 固收回归中 `verify_fi_data` / `verify_rates` / `verify_fi_agent` 依赖实时外网（Treasury 曲线 / 在线 LLM），
> 当前环境不可用时会被 `run_regression.py` 如实标注为 **ENV-DEGRADED**（不计入引擎逻辑门禁），而非代码回归。

---

## 七、目录结构

```
Super-Investment-Agent/
├── start.bat / stop.bat      # 一键启动 / 停止
├── app/
│   ├── config.py             # 统一配置（.env）
│   ├── llm/client.py         # 统一 DeepSeek V4 Flash 客户端（Anthropic 兼容）
│   ├── data/                 # 数据源（yfinance/akshare/sina/fund）+ 缓存 + fixed_income.py 固收门面
│   ├── analysis/             # 深度研究管线（财务/估值/竞品/SWOT/地缘/供应链/专家评分）
│   ├── agent/                # 对话 Agent（意图/规划/执行/表达/评分 + 基金模块 + fixed_income 固收 Agent）
│   ├── action/               # 固收动作化引擎（建议转行动 + 风险信号 + 合规复核队列）
│   ├── credit/               # 固收·主体信用评级（Altman Z''/Ohlson O/Merton + LightGBM/MLP）
│   ├── rates/                # 固收·利率与情景引擎（NSS + VAR + 情景库 + Monte Carlo + 压力重演）
│   ├── rag/                  # RAG（可选，默认关闭）
│   ├── report/               # 报告生成（PDF/HTML/Markdown + 图表）
│   ├── trust/                # 可信层：出处映射 + 事实台账 + 接地检查
│   ├── feedback/             # 数据飞轮：反馈落库（只追加）+ 待审队列
│   ├── observability/        # 版本戳 / 护栏指标 / 进程内 metrics
│   └── watchlist/            # 自选股（SQLite）
├── api/                      # FastAPI 后端（routes_* 含 feedback / metrics）
├── ui/                       # Streamlit 看板（dashboard.py 入口 + 7 个页面，含固收研究、评测看板）
├── scripts/                  # 冒烟 / 端到端 / 固收 verify / 五层评测 eval_* / 各层守卫
├── docs/PRD_SIA_v2.md        # 产品需求文档（每条需求挂钩一条评测断言）
├── data/eval/                # 评测报告（l0~l4、baseline、history）+ 人工标注集
├── data/artifacts/           # 管线产物（评测的唯一事实来源）
├── data/flywheel/            # 👎 待审队列（pending_review，不自动进回归集）
├── data/reports/             # 生成的研报（md/html/pdf）+ 图表
├── requirements.txt / .env.example
└── 计划书.md                 # 项目整合计划书
```

---

## 八、数据真实性说明

- 行情：Sina（A 股实时）/ yfinance（全球）；财务：akshare / yfinance；基金：天天基金 / 东方财富。
- 估值（DCF/SOTP）、财务比率、专家评分、风险值均由代码按公式真实计算。
- LLM 只负责文本生成、意图识别、结构化摘要；数值一律由后端真实计算后注入模板，LLM 不自由捏造数字。
- 抓取失败或字段缺失：返回 `None` / “—” / “暂无数据”，显式标注，绝不填充假值。
- 事实台账：每个进入报告的数字都登记出处与期间；`unknown`（无法归类）在 L0 评测中直接判失败；降级与插补必须带原因，否则同样判失败。

---

## 九、常见问题

| 问题 | 处理 |
|---|---|
| 看板报“无法连接后端” | 先启动后端（`api/main.py`），确认 8000 端口在线 |
| LLM 功能无输出 | 检查 `.env` 中 `LLM_API_KEY` 是否配置、`/api/v1/health` 的 `llm_ready` 是否为 `true` |
| 端口被占用 | 运行 `stop.bat` 或手动关闭占用 8000/8501 的进程后重启 |
| A 股评分/深度研究较慢 | 正常现象：专家评分约 30-60s，深度研究约 140-160s，页面有进度提示 |
| 报告中文乱码 | 报告使用系统字体（微软雅黑）注册；PDF 需本机安装该字体 |
