# 计划书 · 增量刷新（原建议 4）+ 事实级跨 run 对比（原建议 5）

> 建议 1/2 已落地（`app/trust/sanitize.py`、`app/trust/completeness.py`），DCF P0 一并修复。
> 本计划只做 4 和 5，不碰 3（交易日时间戳）。
> 不重录 `baseline.json`、不修改既有产物、不提交 git。

## 一、证据（全部来自现有产物实测）

### 建议 4：全量重算不可持续

| 现象 | 实测 |
|---|---|
| 同一标的两次全量跑的 `pipeline_time` | 600519：**355.34s → 221.10s**（AAPL 产物同量级） |
| 产物体量 | 66–119 KB，单标的已积累 2–6 份 |
| 私人 agent 的真实用法 | 每天看一眼 → 每次等 3–4 分钟，结果就是「跑一次就不动了」 |

跑一次的成本几乎全在 LLM 章节（新闻/会议/SWOT/竞争/地缘/供应链），而这些**一天之内不会变**；
每天真正变的是行情、技术指标、以及由现价决定的估值结论。

### 建议 5：现有 diff 只在文本层

- `ui/report_diff.py` 只做 Markdown 章节级文本 diff，**不认数字**（其 docstring 明说「不重算任何数值」）。
- 事实级对比目前不存在。实测把两份 AAPL 产物按 path 硬对：

| 对齐方式 | 共有键 | 「改值」 | 结论 |
|---|---:|---:|---|
| 按 path | 2707 | **1704** | 无用——行情序列按 Index 命名，多一天全表错位 |
| 按 path + as_of（仅归一末尾下标） | 2706 | 1380（noise 1180 / minor 16 / material 184） | 可用 |

按归一后的键跑两份 600519 相邻产物，**30 条实质变化**，其中：

| 变化 | 含义 |
|---|---|
| `sotp_analysis.segments[2].value_high` 1.646e10 → 1.235e10（−25%） | 分部估值两次跑差四分之一——印证「SOTP 分部数据靠模型现编」 |
| `news_analysis.sentiment_score` 0.65 → 0.45（−31%） | 舆情结论翻转 |
| `market_analysis...competitor_data@latest.market_cap` 1.53e11 → 3.59e12（+2247%） | 竞品数据源异常，肉眼看不出来 |
| `valuation_analysis.dcf.upside` 45.06 → 50.21 | 估值结论变化 |

这就是建议 5 的价值：**「这次和上次哪里不一样」必须能回答，且要能定位到字段。**

0700 相邻两份里还有 `nan -> 397.2` 的迁移（先 `NaN` 后真实值），对比必须容得下非有限值，不能崩。

## 二、范围

| Step | 内容 | 新增/改动 |
|---|---|---|
| D | 增量刷新：`--refresh price`，只重取行情/技术面/估值结论，**不调 LLM** | 新增 `app/analysis/refresh.py`、`app/analysis/price_series.py`、`scripts/refresh_artifact.py`；`pipeline.py` 抽函数；`api/routes_analysis.py` 加端点；`ui/pages/research.py` 加按钮与横幅 |
| E | 事实级跨 run 对比：新增/消失/改值/换源/降级反转 | 新增 `app/trust/factdiff.py`、`scripts/diff_artifacts.py`；`api/routes_analysis.py` 加端点；`ui/pages/research.py` 加对比面板 |

**明确不做**：不做定时任务、不做告警推送、不做多标的批量刷新、不改台账/评测、不碰建议 3、不重录基线。

## 三、Step D · 增量刷新

### D1 抽出行情序列构造（纯重构，行为不变）

`pipeline.py:703-724` 的 `price_history` 构造逻辑搬到 `app/analysis/price_series.py::build_price_history(price_data) -> dict`，
`pipeline.py` 改为调用它。验收：同一份 `price_data` 两边输出**逐字节相等**。

### D2 `app/analysis/refresh.py`

```python
def refresh_price(prev: dict, *, price_data=None, market=None) -> dict
```

只做四件确定性的事：

| 目标 | 做法 |
|---|---|
| `price_history` | 重取 5y 行情 → `build_price_history` |
| `financial_analysis.current_price` | 取最新有效收盘（与 `financial.py:183` 同一规则） |
| `financial_analysis.valuation_context.current_pe` | 同 `financial.py:551` 的公式：`price × shares ÷ 最新 NET_INCOME`（股本取 `company_info`，净利取 `raw_metrics.NET_INCOME[-1]`）；缺任一项则置 `None` |
| `valuation_analysis.dcf.upside` | `(既有 fair_value − 新价) / 新价`；`fair_value` 为 `None` 则保持 `None` |
| `trade_plan` | 复用 `build_trade_plan(price_data=new, valuation_result, swot_rating, company_info, financial_result)` |
| 其余章节 | 深拷贝沿用 |

**`pe_percentile` 不重算**——它需要年度均价序列与对齐后的财报 DataFrame，产物里没有；
强行半算只会产生混血数据。它进 `carried_over` 清单，明确标出「这是 YYYY-MM-DD 的值」。

### D3 产物新增 `refresh` 块（**只放字符串**）

```json
"refresh": {
  "mode": "price",
  "refreshed_at": "2026-11-06T22:30:11",
  "base_artifact": "data/artifacts/600519_SS/20261105T001635.json",
  "base_generated_at": "2026-11-05T00:16:35",
  "effective_price_date": "2026-11-06",
  "recomputed": ["price_history", "financial_analysis.current_price",
                 "valuation_analysis.dcf.upside", "trade_plan"],
  "carried_over": [
    {"section": "financial_analysis", "as_of": "2026-11-05",
     "note": "财报与比率沿用，未重取三表"},
    {"section": "financial_analysis.valuation_context.pe_percentile", "as_of": "2026-11-05",
     "note": "历史分位需年报均价序列，增量刷新不重算"},
    {"section": "swot_analysis", "as_of": "2026-11-05", "note": "LLM 章节，沿用"},
    {"section": "news_analysis", "as_of": "2026-11-05", "note": "LLM 章节，沿用"},
    {"section": "...", "as_of": "...", "note": "..."}
  ]
}
```

**只有字符串/列表/字典**，不产生数值叶子 → 台账 `fact_total` 不变、不新增 UNKNOWN。
沿用章节内的数值仍保持原有 provenance 与 as_of（它们是那个日期的真实事实，不是新事实）。

### D4 落盘链路

`refresh_price` → `sanitize_result` → `check_completeness` → 写 `data/artifacts/<TICKER>/<stamp>.json`（`allow_nan=False`）
→ `build_ledger`。与全量管线**同一条净化/完整性链路**，不另开旁路。

### D5 入口

- **CLI**：`scripts/refresh_artifact.py --ticker 600519.SS [--from PATH] [--dry-run] [--no-write]`
  - 默认取该 ticker 最新产物为基线；`--dry-run` 只打印将重算/沿用的清单
  - 结束打印「相对上一期 X 项实质变化」（复用 Step E）
- **API**：`POST /api/v1/analysis/refresh/{ticker}` → 返回 `{artifact_ref, refresh, summary}`，同时 `set_cached_result`
- **UI**：research 页 `status == completed` 区域加按钮「⚡ 行情刷新（不调 LLM）」；完成后顶部横幅

  > ⚡ 本次为行情增量刷新（2026-11-06）。财务 / 新闻 / SWOT / 竞争 / 地缘 / 供应链 / 会议 沿用 2026-11-05 计算结果。

  横幅只读产物里的 `refresh.carried_over`，不在 UI 侧重算。

### D6 验收

| 项 | 判据 |
|---|---|
| 纯重构无回归 | `build_price_history` 与 `pipeline` 原逻辑同输入**逐字节相等**（用 600519 产物反推不行，直接比对函数输出） |
| 不调 LLM | 刷新全程 LLM 调用计数 = 0（打桩计数） |
| 未刷新章节零改动 | `prev` 与 `refreshed` 除 `refresh`/`price_history`/`trade_plan`/上述三个字段外，**深度相等** |
| `upside` 自洽 | `upside == (fair_value − price) / price`，误差 < 1e-12 |
| 台账 | `fact_total` 只随价格字段变化；`unknown_count == 0` |
| 耗时 | 联网取行情 < 10s（不含网络抖动） |
| 守卫 | `verify_evals` / `verify_phase3` / `verify_trade_plan` 全绿 |

**需要联网**：D6 的真实刷新要拉一次 5y 行情（腾讯/东财/yfinance 之一）。若你不希望联网，我就只用打桩 `price_data` 验证，并如实说明「未做真实拉取」。

## 四、Step E · 事实级跨 run 对比

### E1 `app/trust/factdiff.py`

```python
def align_key(fact) -> str
def diff_facts(old_result: dict, new_result: dict, *, base=?) -> dict
```

**对齐规则**（实测决定，不是拍脑袋）：

- 路径末尾是 `[N]` 或 `[N].xxx` 且该事实有 `as_of` → 把末尾下标换成 `@<as_of>`
  - `price_history.close[299]` → `price_history.close@2026-11-06`
  - `financial_analysis.ratios.ROE[3].value` → `...ROE@2025.value`
- 其余按原 path
  - `sotp_analysis.segments[2].revenue_share` 保持原样（分部顺序有语义）
  - `knowledge.snippets[2].metadata.lex_score` 保持原样（检索排序变了就是变了）

**五类变化**

| 类别 | 判定 |
|---|---|
| `added` | 键仅在新产物 |
| `removed` | 键仅在旧产物 |
| `value_changed` | 键两侧都有且值不同 |
| `reprovenanced` | 键两侧都有、值相同，但 `provenance` 不同（换源） |
| `degraded_flip` | `provenance` 在 `degraded` 与其它之间翻转（升/降级） |

**噪声分档**（`value_changed` 每条带 `rel_delta` 与 `class`）：

- `noise`：`|Δ| / max(|a|,|b|) < 1e-6`（float32 双精度往返，实测 AAPL 相邻两期 1180 条）
- `minor`：`1e-6 ≤ rel < 1e-2`
- `material`：`rel ≥ 1e-2`

**非有限值**：任一侧是 `NaN/Inf` 时不做算术，标 `class="undefined"`、`note="旧值非有限"`，不崩、不吞。

**默认忽略**：`pipeline_time`（跑得快慢不是数据变化）。其余一律列出，由 UI 过滤。

### E2 入口

- **CLI**：`scripts/diff_artifacts.py --ticker 600519.SS [--old REF] [--new REF] [--material-only] [--json] [--out md]`
- **API**：`POST /api/v1/analysis/diff` `{old_ref, new_ref | "current"}`；产物列举复用现有 `GET /api/v1/thesis/artifacts?ticker=`（`_list_artifacts` 已有路径穿越防护）
- **UI**：research 页新增「🔀 事实级对比」expander
  - 5 个指标卡：新增 / 消失 / 改值 / 换源 / 降级反转（改值卡副标题注明其中实质 N 条）
  - 默认只显示 `minor + material`；勾选框「显示噪声级变化（N 条）」
  - 表格列：路径 / 指标 / 旧值 → 新值 / 变化幅度 / 出处变化

### E3 验收（全部离线，可复现）

| 用例 | 实测（E1 已跑通） |
|---|---|
| AAPL `20261104T171711` vs `20261105T002222` | 对齐 2706/2712；新增 29 / 消失 23 / 改值 1380（noise 1180 · minor 16 · material 184） |
| 600519 `20261104T151305` vs `20261105T001635` | 新增 44 / 消失 0 / 改值 68（material 30）；含 SOTP 分部 `value_high` −25%、舆情分 0.65→0.45 |
| 0700 `20261105T014111` vs `20261105T015658` | 改值 80，其中 **undefined 2 条**——`current_price` `NaN → 397.20`、`current_pe` `NaN → 15.56`，不抛异常、不吞 |
| 同一份产物自比 | **九项计数全 0**（确定性） |
| 台账不受影响 | 对比纯只读，`build_ledger` 结果不变 |

> 说明：早期用「同键后者覆盖前者」的粗略探针会丢掉重复键（AAPL 少算 34 条）。正式实现保留重复键，
> 以 `#2`、`#3` 后缀区分，因此计数比探针略高，上表为正式实现的结果。

## 五、顺序与验证

```
D1 抽函数 → D2 refresh → D3 refresh 块 → D4 落盘 → D5 CLI/API/UI → D6 验收
E1 factdiff → E2 CLI/API/UI → E3 验收
```

D5 的「相对上一期变化」依赖 E，实现顺序上 **E1 先做**，D5 再接入。

```bash
D:/python/python.exe scripts/verify_evals.py
D:/python/python.exe scripts/verify_phase3.py
D:/python/python.exe scripts/verify_trade_plan.py
D:/python/python.exe scripts/eval_l0_ledger.py            # 预期仍 155/156（历史 NaN 产物保留）
D:/python/python.exe scripts/diff_artifacts.py --ticker 600519.SS --material-only
```

新增守卫：`scripts/verify_refresh.py`（打桩行情 + 零 LLM 计数 + 深度相等断言）、
`scripts/verify_factdiff.py`（三组真实产物对 + 自比零变化）。

## 六、实施结果（2026-11-06 已完成）

### 交付物

| 文件 | 说明 |
|---|---|
| `app/trust/factdiff.py` | 新。对齐键 + 五类变化 + 幅度分档 |
| `app/analysis/price_series.py` | 新。`build_price_history`，全量与刷新共用 |
| `app/analysis/refresh.py` | 新。`refresh_price` / `summarize_refresh` |
| `scripts/refresh_artifact.py` | 新。刷新 CLI（`--dry-run` / `--no-write` / `--no-cache`） |
| `scripts/diff_artifacts.py` | 新。对比 CLI（`--material-only` / `--json` / `--out`） |
| `scripts/verify_refresh.py` | 新守卫，14 项 |
| `scripts/verify_factdiff.py` | 新守卫，31 项 |
| `api/routes_analysis.py` | 加 `POST /analysis/refresh/{ticker}`、`POST /analysis/diff` |
| `ui/api_client.py` / `ui/pages/research.py` | 刷新按钮、刷新横幅、事实级对比面板 |
| `app/analysis/valuation.py` | 抽出 `build_valuation_summary` / `band_degrade_reason`（口径共用） |

### 实测

| 项 | 结果 |
|---|---|
| 真实刷新 600519.SS | **3.3 秒**，价格取至 2026-11-06（1453.0），产物 `20261106T223715.json` |
| 沿用章节 | 与基线**逐字段一致**（`verify_refresh` 断言） |
| 基线 DCF 的 50.98 倍结果 | 刷新时被倍数闸拦下 → `fair_value=None`、`degraded=True`、完整性判为缺章 |
| `trade_plan` 修正 | `entry_zone[1]` / `take_profit` 从 74074.26 → 1480.60；`risk_reward_ratio` 1628.63 → 4.94 |
| 台账 | 刷新产物 2659 条事实，`unknown=0`，L0 逐项 PASS |
| 不调 LLM | `app.llm.client` 全程未加载（守卫断言） |
| 对比 CLI | 600519 相邻两期：实质 30 条，抓到 SOTP 分部 −25%、舆情分 0.65→0.45 |
| 对比 API | `POST /analysis/diff` 200；路径穿越请求 400 |

### 过程中被守卫抓到的问题

1. **对齐键正则写错**：最初用 `\[(\d+)\](\.\w+)?$`，导致 `sotp_analysis.segments[2].revenue_share`
   被归一到 `segments@2024.revenue_share`——分部顺序被打乱，对比结果是假的。改成只认
   `[N]` 与 `[N].value`，由 `verify_factdiff` 的对齐用例钉住。
2. **刷新会绕过 DCF 倍数闸**：基线里旧的 74074 被原样带过来，重算出 +4646% 的「低估」。
   已让 `refresh_price` 走同一道 `FAIR_VALUE_PRICE_BAND` 闸。

### 已知边界

- 刷新产物会被 `eval_l0_ledger.py` 纳入统计（产物集合 +1，断言总数 156 → 168，仍只有历史 NaN 那 1 条红）。
- `pe_percentile` 刷新后仍是旧值，靠 `refresh.carried_over` 与页面横幅说明。
- 刷新产物与全量产物同目录同格式，靠 `refresh.mode` 区分。
- 报告文件（md/pdf）**不会**随刷新重生成，仍停在基线那一期。

## 七、风险

| 风险 | 处理 |
|---|---|
| 刷新后「沿用章节」被当成本次结论 | `refresh.carried_over` 逐章写明日期；UI 顶部横幅；报告若生成需同样标注 |
| 刷新产物与全量产物混在同一目录 | 文件名不变（同格式时间戳），靠 `refresh.mode` 区分；对比时两侧 `refresh` 情况打印在页头 |
| 行情源取不到 | 刷新失败即报错并**不覆盖**旧产物，不产出半成品 |
| `current_pe` 口径与全量不一致 | 沿用同一条公式与同一取数位置；验收要求 `upside`/`current_pe` 与手算一致 |
| 事实对比被噪声淹没 | 三档 `class` + 默认只显示 minor/material + 明示噪声条数，不静默隐藏 |
