# 对标 Qovest：A / B / C 三个改进 · 实施计划 v1

**日期**：2026-11-06
**范围**：个人自用，不做多租户 / 权限 / 计费 / 团队协作。

## 用户已定的边界

| 决定 | 内容 |
|---|---|
| 做 | A 论点台账、B 统一编排+红队、C 检索升级 |
| **不做 D** | 无法接入真实持仓，组合与归因无数据基础 |
| **A 砍掉定时监控** | 不要「每天替我看一眼」；台账是手动记录 + 手动检查，不设调度器、不设推送 |
| 不做 | RBAC、配额、券商对接、下单 |

## 不可破坏的既有约束

1. `data/eval/baseline.json` 不重录。
2. L2 只跑 `data/knowledge_db_v1`（2041 切片），**任何新检索特性都不得改变 v1 上的 L2 数字**。
3. `data/artifacts/0700_HK/20261105T014111.json` 保留，L0 维持 131/132。
4. 新增功能默认关闭或纯增量，不改已评测路径：`run_eval.py` l0–l4 与 44 个 `verify_*.py` 必须保持绿。
5. 不提交 git。

---

## A. 论点台账（Thesis Ledger）

### 为什么这个先做

Qovest 卖的是「投研空间沉淀」，但它的沉淀是给用户自己翻的。个人投研真正的缺口是**论点失效没人发现**——不是缺一个 DCF，是三个月后没人记得「当初我为什么买、什么情况下该认错」。台账把这件事做成结构化数据，而不是随笔。

### 数据模型（`app/thesis/db.py`，SQLite，路径 `data/thesis.db`）

```
thesis
  id TEXT PK            -- uuid4 hex
  ticker TEXT
  statement TEXT        -- 一句话论点
  stance TEXT           -- bullish | bearish | neutral
  horizon TEXT          -- 自由文本，如 "2026Q4"、"12-18 个月"
  key_assumptions TEXT  -- JSON: [str]  关键假设
  falsifiers TEXT       -- JSON: [{metric, op, value, note}]  证伪条件
  status TEXT           -- active | confirmed | falsified | abandoned
  conviction INTEGER    -- 1-5
  artifact_ref TEXT     -- 关联产物相对路径（可选）
  created_at / updated_at TEXT

thesis_evidence         -- 一个论点挂多条证据
  id INTEGER PK
  thesis_id TEXT
  ref TEXT              -- 台账路径（valuation_analysis.dcf.fair_value）、KB chunk id、或 URL
  kind TEXT             -- fact | snippet | artifact | doc
  direction TEXT        -- supports | contradicts
  note TEXT
  added_at TEXT

thesis_review           -- 每次复盘留痕，不改历史
  id INTEGER PK
  thesis_id TEXT
  verdict TEXT          -- holds | weakens | broken
  snapshot TEXT         -- JSON: 复盘时读到的指标快照
  note TEXT
  reviewed_at TEXT
```

### 证伪条件的执行（`app/thesis/checks.py`）

不做 LLM 判断。`falsifiers` 里每条是 `{metric, op, value}`，`metric` 直接走 `app/trust/ledger.py::Ledger.find(path)` 取真实值：

- 命中 → `breached`，附 `as_of` 与 `provenance`
- 台账里找不到该 path → `unknown`，**显式标注**，不当作未触发（"查不到" ≠ "没出事"）
- 值非有限数（NaN）→ `unknown`

这是整个 A 方案的核心：**结论可被机器检验，且检验失败会显形**。

### 步骤

| Step | 内容 | 验收 |
|---|---|---|
| A1 | `app/thesis/`（db + checks）、`config.THESIS_DB_PATH`、`scripts/verify_thesis.py` | 守卫脚本全绿；含「NaN→unknown」「缺失 path→unknown」「命中→breached」三类边界用例 |
| A2 | `api/routes_thesis.py`（CRUD + `/check` 证伪检查 + 从 artifact 一键生成草稿）+ `main.py` 注册 | 起后端后 CRUD 全通；`/check` 返回逐条判定 |
| A3 | `ui/pages/thesis.py` + `dashboard.py` 注册 | 列表/详情/新建/复盘可用；触发证伪的条目在列表顶部红标 |

**A 明确不做**：调度器、定时任务、钉钉/邮件/webhook 推送、自选股自动关联。

---

## B. 统一研究编排 + 强制红队

### 现状问题

三套栈互不相通：`app/agent`（chat，确定性模板 planner + 5 个工具）、`app/analysis/pipeline.py`（固定管线）、`app/investigation/loop.py`（受限 ReAct）。用户无法表达「这个问题值得深挖」，只能靠猜哪个入口。

### B1 · 编排入口（`app/agent/orchestrator.py`）

只做**分派**，不重写执行：

```python
Depth = quick | standard | deep
resolve_depth(intent) -> Depth     # 显式参数优先，其次按意图
dispatch(text, depth)  -> 路由到 chat / analysis / investigation
```

现有 URL 与调用方行为不变；orchestrator 是新增门面。

### B2 · planner 的 LLM 兜底

现状：`build_plan()` 对模板外需求无解。改法：

- 保留全部确定性分支（可复现）
- **仅当** `intent` 置信度低或无 ticker 时，用 LLM 出步骤，且步骤仍走 `runner.ACTION_MAP` 白名单
- 结果按 (意图, 文本 hash) 缓存，同一问题二次调用不再花 token
- LLM 不可用 → 维持现状返回 `error`

### B3 · 红队环节（`app/agent/redteam.py`）

只在 `depth == deep` 触发，输出三段，全部要求引用已有事实：

1. **反方论证**：至少 3 条，每条必须挂 `evidence_ref`
2. **假设脆弱点**：论点依赖但证据最弱的那条
3. **未解决问题**：明确写「不知道」，不许用推测填充

产物写入 artifact 的 `redteam` 字段，L3 新增断言：`redteam` 存在时每条反方论证都必须有 `evidence_ref`，否则失败。**没有引用就不许说话**——否则红队会变成第二个幻觉源。

### 步骤

| Step | 内容 | 验收 |
|---|---|---|
| B1 | `orchestrator.py` + `Depth` + 分派 | 现有 chat/analysis/investigation 结果逐字不变（A/B 对比同一请求） |
| B2 | planner LLM 兜底 + 缓存 | 模板外请求能出计划；同请求第二次 0 token |
| B3 | `redteam.py` + artifact `redteam` 字段 + L3 断言 | `verify_evals` 新增断言通过；`run_eval --tier l3` 仍全绿（默认不触发红队） |
| B4 | `ui` 研究页展示红队段落 | deep 模式结果可见 |

**B 的风险**：L4 e2e 走 standard，默认不触发红队，故 L4 数字不变。

---

## C. 检索升级

### 现状

`retriever.invoke()` = 稠密 + TF-IDF 分数融合，**没有 cross-encoder 重排**。22,943 切片下 top-50→top-5 的精度有明显提升空间。

### C0 · 先解决模型来源（需你拍板）

本机 HF 缓存里只有 `bge-small-zh-v1.5` / `Qwen1.5-0.5B` / `text2vec-base-chinese` / whisper / blenderbot，**没有任何 reranker 模型**，且 `app/rag/embed.py` 设了 `HF_HUB_OFFLINE=1`。

三个可选方案：

| 方案 | 体积 | 说明 |
|---|---|---|
| a. 下载 `BAAI/bge-reranker-base` | ~1.1 GB | 标准做法，中文效果好，一次性下载需联网 |
| b. 不下载，用现有 `bge-small` 做 chunk 级二段打分 | 0 | 只是再算一次余弦，**不是真重排**，收益有限，我不推荐 |
| c. 暂不做重排，C 只做检索页 | 0 | 最保守 |

### C1 · 重排器（`app/rag/rerank.py`）

- 配置开关 `RERANK_ENABLED`（**默认关**）
- 模型缺失时 `available()=False`，检索退回现状，**不报错、不改排序**
- 只在**正式库**路径启用；**v1 快照路径强制关闭**（`eval_retrieval.py --db knowledge_db_v1` 时），保证 L2 数字一个字节不变
- 开关状态写进检索结果的 `metadata.reranked`，让「这条排序是重排过的」可见

### C2 · 知识库检索页

`ui/pages/search.py` + `api/routes_search.py`：输入 query → 显示命中的切片、来源、日期、kind、分数（dense/lex/rerank 分列）。解决「22,943 切片进了库，用户摸不到」。

### 步骤

| Step | 内容 | 验收 |
|---|---|---|
| C0 | 你定重排方案 | — |
| C1 | `rerank.py` + 配置 + v1 强制旁路 | `verify_rag` 扩断言；**L2 仍 0.9461** |
| C2 | 检索页 API + UI | 页面能检索出外部语料（政策/论文/披露） |

---

## 影响评估

| 既有检查 | 影响 |
|---|---|
| L0 / L1 | 不动 |
| L2 | 不动（C1 在 v1 路径强制关闭） |
| L3 | 新增红队断言，默认不触发 |
| L4 | 不动（standard 档不跑红队） |
| 44 个 verify_* | 只增不减；新增 `verify_thesis.py` |

## 顺序

A1 → A2 → A3 → B1 → B2 → B3 → B4 → C0 → C1 → C2

每步落盘后汇报，经你确认再进下一步。
