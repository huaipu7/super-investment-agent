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

> 部分测试耗时较长：A 股专家评分约 30-60s、A 股深度研究管线约 140-160s。全部跑完约 15-20 分钟。

---

## 七、目录结构

```
Super-Investment-Agent/
├── start.bat / stop.bat      # 一键启动 / 停止
├── app/
│   ├── config.py             # 统一配置（.env）
│   ├── llm/client.py         # 统一 DeepSeek V4 Flash 客户端（Anthropic 兼容）
│   ├── data/                 # 数据源（yfinance/akshare/sina/fund）+ 缓存
│   ├── analysis/             # 深度研究管线（财务/估值/竞品/SWOT/地缘/供应链/专家评分）
│   ├── agent/                # 对话 Agent（意图/规划/执行/表达/评分 + 基金模块）
│   ├── rag/                  # RAG（可选，默认关闭）
│   ├── report/               # 报告生成（PDF/HTML/Markdown + 图表）
│   └── watchlist/            # 自选股（SQLite）
├── api/                      # FastAPI 后端（routes_stock/analysis/chat/report/watchlist）
├── ui/                       # Streamlit 看板（dashboard.py 入口 + 5 个页面）
├── scripts/                  # 各 Phase 冒烟测试 + 端到端集成测试
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

---

## 九、常见问题

| 问题 | 处理 |
|---|---|
| 看板报“无法连接后端” | 先启动后端（`api/main.py`），确认 8000 端口在线 |
| LLM 功能无输出 | 检查 `.env` 中 `LLM_API_KEY` 是否配置、`/api/v1/health` 的 `llm_ready` 是否为 `true` |
| 端口被占用 | 运行 `stop.bat` 或手动关闭占用 8000/8501 的进程后重启 |
| A 股评分/深度研究较慢 | 正常现象：专家评分约 30-60s，深度研究约 140-160s，页面有进度提示 |
| 报告中文乱码 | 报告使用系统字体（微软雅黑）注册；PDF 需本机安装该字体 |
