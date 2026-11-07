"""调查工具集 —— 封闭工具集 + 诚实降级。

约定完全照抄 app/agent/fixed_income.py（那里已验证过的形状）：
  - OpenAI 格式声明 INVESTIGATION_TOOLS，调用前由 to_anthropic_tool 转换
  - Toolbox.execute(name, args) -> dict；Toolbox.tool_executor(name, args) -> JSON 串
  - 未知工具 -> {"error": ...}；工具抛错 -> {"ok": false, "reason": "<Type>: <msg>"}
  - 每个工具自带"如实说明"路径，从不抛异常，绝不用猜测补齐缺失数据

**明确建不出来的（代码里直接拒，绝不回退到编造）**
  * 供应商 / 客户关系查询 —— 全仓库没有任何真实数据源（search_competitors() 在
    两个 source 里都是返回空列表的桩；supply_chain.py 是硬编码行业配置，从不命名
    任何供应商；计划书里记录产业链模块当初被明确排除）。上下游只能作为对真实
    新闻 / 语料 / A股概念板块文本的**假设**存在，见 hypotheses.py。
  * 华尔街一致预期 / 目标价 / 评级 / 超预期基线 —— 完全不存在，因此
    「业绩不及预期」这一假设族根本不设。
  * point-in-time 财报 —— 免费源做不到，router.py 已如实标注。
  * 北向资金 / 龙虎榜 / 大宗交易 —— 不在 router 能力内。

用于去重的 args 规范化在 loop.py；本模块只负责取数与如实降级。
"""
from __future__ import annotations

import json

import pandas as pd

from app.investigation.signals import (
    METRIC_LABELS, METRIC_STATEMENT, lookup_series,
)
from app.utils.logger import logger

# ── 工具元数据 ────────────────────────────────────────────────
# llm_cost: 该工具内部会消耗的 LLM 调用次数（预算必须单独计数，否则"预算"是假的）
# once_per_run: 每次调查最多执行一次（控成本）
# est_seconds: 粗估耗时，供 UI 与 min_round_seconds 参考
TOOL_META: dict[str, dict] = {
    "get_statement_summary": {"llm_cost": 0, "once_per_run": False, "est_seconds": 5},
    "get_statement_line": {"llm_cost": 0, "once_per_run": False, "est_seconds": 3},
    "get_news": {"llm_cost": 0, "once_per_run": False, "est_seconds": 6},
    "get_announcements": {"llm_cost": 0, "once_per_run": False, "est_seconds": 6},
    "search_knowledge": {"llm_cost": 0, "once_per_run": False, "est_seconds": 3},
    "compare_peers": {"llm_cost": 1, "once_per_run": True, "est_seconds": 45},
    "get_company_profile": {"llm_cost": 0, "once_per_run": False, "est_seconds": 4},
    "get_price_history": {"llm_cost": 0, "once_per_run": False, "est_seconds": 5},
    "get_quarterly_income": {"llm_cost": 0, "once_per_run": False, "est_seconds": 6},
    # 证据登记不算"取数"，不占 tool_calls 预算（由 max_rounds 与
    # max_evidence_per_hypothesis 另行约束）
    "record_evidence": {"llm_cost": 0, "once_per_run": False, "est_seconds": 0,
                        "no_tool_budget": True},
}

TOOL_NAMES: tuple[str, ...] = tuple(TOOL_META)

_SNIPPET_CHARS = 300


# ── 小工具 ────────────────────────────────────────────────────

def _ticker(args: dict) -> str:
    return str((args or {}).get("ticker") or "").strip()


def _int(v, default: int) -> int:
    try:
        if isinstance(v, bool):
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _snip(s, n: int = _SNIPPET_CHARS) -> str:
    s = "" if s is None else str(s)
    return s[:n] + ("…" if len(s) > n else "")


def _empty_frames(frames) -> bool:
    if not frames:
        return True
    return not any(
        df is not None and not getattr(df, "empty", True) for df in frames.values()
    )


# ── 工具箱（一次运行一个实例，持有运行内记忆）──────────────────

class Toolbox:
    """一次调查运行的工具执行器。

    market 可注入（离线测试用替身）；max_peers 来自预算。
    运行内记忆避免同一轮里重复清洗三表/重复拉画像。
    """

    def __init__(self, *, market=None, max_peers: int = 3):
        self._market = market
        self.max_peers = max(1, int(max_peers))
        self._frames_memo: dict[tuple[str, str], dict] = {}
        self._profile_memo: dict[str, dict] = {}
        self._once_memo: dict[str, dict] = {}
        self._orchestrator = None

    # ── 依赖注入点 ──
    @property
    def market(self):
        if self._market is None:
            from app.data.router import get_market_data
            self._market = get_market_data()
        return self._market

    @property
    def orchestrator(self):
        if self._orchestrator is None:
            from app.analysis.pipeline import AnalysisOrchestrator
            self._orchestrator = AnalysisOrchestrator()
        return self._orchestrator

    # ── 取数 ──

    def frames(self, ticker: str, period: str = "annual") -> dict:
        """清洗后的三表（index=报告期、columns=标准科目代码）。取不到返回 {}。"""
        key = (ticker.upper(), period)
        if key in self._frames_memo:
            return self._frames_memo[key]
        out: dict = {}
        try:
            from app.data.cleaning.pipeline import CleaningPipeline
            from app.data.router import is_ashare
            raw = self.market.fetch_financial_statements(ticker, period=period)
            if not _empty_frames(raw):
                src = "akshare" if is_ashare(ticker) else "yfinance"
                out = CleaningPipeline(source=src).run(raw)
        except Exception as e:
            logger.warning("调查取三表失败 %s(%s): %s", ticker, period, e)
            out = {}
        self._frames_memo[key] = out
        return out

    def profile(self, ticker: str) -> dict:
        key = ticker.upper()
        if key not in self._profile_memo:
            try:
                d = self.market.fetch_company_info(ticker) or {}
            except Exception as e:
                logger.warning("调查取公司画像失败 %s: %s", ticker, e)
                d = {}
            self._profile_memo[key] = dict(d)
        return self._profile_memo[key]

    # ── 各工具实现 ──

    def _get_statement_summary(self, args: dict) -> dict:
        ticker = _ticker(args)
        if not ticker:
            return {"ok": False, "reason": "缺少 ticker"}
        period = str(args.get("period") or "annual").lower()
        if period not in ("annual", "quarterly"):
            period = "annual"
        fr = self.frames(ticker, period)
        if _empty_frames(fr):
            return {"ok": False, "available": False, "ticker": ticker, "period": period,
                    "reason": "三表为空（该标的无披露或数据源不可用）"}
        lines: dict[str, list] = {}
        periods: list[str] = []
        for code in sorted(METRIC_CODES_ORDERED):
            s = lookup_series(fr, code)
            if s is None:
                continue
            if not periods:
                periods = [str(i) for i in s.index]
            lines[code] = [_num(x) for x in s.tolist()]
        vintage = None
        try:
            vintage = (self.market.get_financials(ticker) or {}).get("vintage")
        except Exception:
            vintage = None
        return {
            "ok": True, "ticker": ticker, "period": period,
            "periods": periods,
            "lines": lines,
            "line_labels": {k: METRIC_LABELS.get(k, k) for k in lines},
            "vintage": vintage,
            "note": ("vintage 原样透传：免费数据源无法回退历史财务快照，"
                     "is_point_in_time 不为真时不可当作当期历史值使用。"),
        }

    def _get_statement_line(self, args: dict) -> dict:
        ticker = _ticker(args)
        if not ticker:
            return {"ok": False, "reason": "缺少 ticker"}
        code = str(args.get("line_code") or "").strip().upper()
        if not code:
            return {"ok": False, "reason": "缺少 line_code",
                    "allowed": sorted(METRIC_CODES_ORDERED)}
        if code not in METRIC_STATEMENT:
            return {"ok": False, "reason": f"未知科目代码 {code}（不接受模型自造科目名）",
                    "allowed": sorted(METRIC_CODES_ORDERED)}
        period = str(args.get("period") or "annual").lower()
        if period not in ("annual", "quarterly"):
            period = "annual"
        n = _clamp(_int(args.get("n_periods"), 6), 2, 12)
        fr = self.frames(ticker, period)
        s = lookup_series(fr, code)
        if s is None:
            return {"ok": False, "available": False, "ticker": ticker, "line_code": code,
                    "reason": f"未取得该科目数据（{METRIC_LABELS.get(code, code)}）"}
        tail = s.iloc[-n:]
        yoy, yoy_period, note = _same_period_yoy(s, period)
        return {
            "ok": True, "ticker": ticker, "line_code": code,
            "line_label": METRIC_LABELS.get(code, code),
            "period": period,
            "periods": [str(i) for i in tail.index],
            "values": [_num(x) for x in tail.tolist()],
            "latest": _num(tail.iloc[-1]),
            "yoy_percent": yoy,
            "yoy_period": yoy_period,
            "note": note,
        }

    def _get_news(self, args: dict) -> dict:
        ticker = _ticker(args)
        if not ticker:
            return {"ok": False, "reason": "缺少 ticker"}
        limit = _clamp(_int(args.get("limit"), 20), 1, 40)
        data = self.market.get_news(ticker, limit=limit) or {}
        arts = data.get("articles") or []
        items = [{
            "title": _snip(a.get("title"), 160),
            "summary": _snip(a.get("summary")),
            "published": a.get("published"),
            "source": a.get("source"),
            "url": a.get("url"),
        } for a in arts][:limit]
        if not items:
            return {"ok": True, "available": False, "ticker": ticker, "article_count": 0,
                    "articles": [], "reason": "未获取到新闻"}
        return {"ok": True, "available": True, "ticker": ticker,
                "source": "东财新闻" if _is_ashare(ticker) else "yfinance 新闻",
                "article_count": len(items), "articles": items}

    def _get_announcements(self, args: dict) -> dict:
        ticker = _ticker(args)
        if not ticker:
            return {"ok": False, "reason": "缺少 ticker"}
        limit = _clamp(_int(args.get("limit"), 10), 1, 30)
        data = self.market.get_news(ticker, limit=limit) or {}
        bri = (data.get("briefings") or [])[:limit]
        if not bri:
            return {"ok": True, "available": False, "ticker": ticker, "briefings": [],
                    "reason": ("美股无公告源（巨潮公告仅覆盖 A 股）"
                               if not _is_ashare(ticker) else "未获取到公告")}
        return {"ok": True, "available": True, "ticker": ticker,
                "source": "巨潮资讯", "briefing_count": len(bri),
                "briefings": [{
                    "title": _snip(b.get("title"), 160),
                    "type": b.get("type"),
                    "published": b.get("published"),
                    "source": b.get("source"),
                    "url": b.get("url"),
                } for b in bri]}

    def _search_knowledge(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "reason": "缺少 query"}
        top_k = _clamp(_int(args.get("top_k"), 5), 1, 8)
        ticker = _ticker(args) or None
        where = {"ticker": ticker.upper()} if ticker else None
        try:
            from app.rag.retriever import get_knowledge_retriever
            r = get_knowledge_retriever()
            if not getattr(r, "available", False):
                return {"ok": False, "reason": "RAG 嵌入/向量库不可用（已静默降级）"}
            docs = r.invoke(query, where=where, top_k=top_k)
        except Exception as e:
            return {"ok": False, "reason": f"RAG 不可用：{type(e).__name__}: {e}"}
        if not docs:
            return {"ok": True, "found": 0, "docs": [],
                    "note": "相关语料未索引，无检索结果。"}
        return {"ok": True, "found": len(docs), "docs": [{
            "content": _snip(getattr(d, "page_content", ""), 400),
            "source": (getattr(d, "metadata", {}) or {}).get("source"),
            "date": (getattr(d, "metadata", {}) or {}).get("date"),
            "score": (getattr(d, "metadata", {}) or {}).get("score"),
        } for d in docs]}

    def _get_company_profile(self, args: dict) -> dict:
        ticker = _ticker(args)
        if not ticker:
            return {"ok": False, "reason": "缺少 ticker"}
        p = self.profile(ticker)
        if not p:
            return {"ok": False, "available": False, "ticker": ticker,
                    "reason": "公司画像取不到（数据源不可用）"}
        concepts = list(p.get("concepts") or [])
        note = "" if concepts else (
            "A股才有真实概念板块字段；非 A 股恒为空，不编造概念。")
        return {
            "ok": True, "ticker": ticker,
            "name": p.get("name"), "industry": p.get("industry"),
            "sector": p.get("sector"), "country": p.get("country"),
            "market_cap": p.get("market_cap"), "currency": p.get("currency"),
            "website": p.get("website"), "employees": p.get("employees"),
            "summary": _snip(p.get("description") or p.get("summary"), 600),
            "concepts": concepts,
            "note": note,
        }

    def _get_price_history(self, args: dict) -> dict:
        ticker = _ticker(args)
        if not ticker:
            return {"ok": False, "reason": "缺少 ticker"}
        period = str(args.get("period") or "2y").strip() or "2y"
        try:
            df = self.market.get_history(ticker, period=period)
        except Exception as e:
            return {"ok": False, "available": False, "ticker": ticker,
                    "reason": f"历史行情不可用：{type(e).__name__}: {e}"}
        if df is None or getattr(df, "empty", True):
            return {"ok": False, "available": False, "ticker": ticker,
                    "reason": "历史行情为空"}
        out = {
            "ok": True, "ticker": ticker, "period": period,
            "start": str(df.index[0]), "end": str(df.index[-1]),
            "start_price": _num(df["Close"].iloc[0]),
            "end_price": _num(df["Close"].iloc[-1]),
            "high": _num(df["High"].max()),
            "low": _num(df["Low"].min()),
            "avg_volume": _num(df["Volume"].mean()) if "Volume" in df else None,
            "change_percent": _pct(df["Close"].iloc[0], df["Close"].iloc[-1]),
        }
        try:
            bdf = self.market.get_benchmark_history(ticker, period=period)
            if bdf is not None and not getattr(bdf, "empty", True):
                out["benchmark"] = {
                    "change_percent": _pct(bdf["Close"].iloc[0], bdf["Close"].iloc[-1]),
                    "start": str(bdf.index[0]), "end": str(bdf.index[-1]),
                }
            else:
                out["benchmark"] = None
        except Exception:
            out["benchmark"] = None
        return out

    def _get_quarterly_income(self, args: dict) -> dict:
        ticker = _ticker(args)
        if not ticker:
            return {"ok": False, "reason": "缺少 ticker"}
        try:
            df = self.market.fetch_quarterly_income(ticker)
        except Exception as e:
            return {"ok": False, "available": False, "ticker": ticker,
                    "reason": f"季度利润表不可用：{type(e).__name__}: {e}"}
        if df is None or getattr(df, "empty", True):
            return {"ok": True, "available": False, "ticker": ticker,
                    "reason": "未取得季度利润表（美股免费源通常无此数据）"}
        # 复用 signals 的科目归一（季度利润表未清洗，先过一遍）
        try:
            from app.data.cleaning.pipeline import CleaningPipeline
            from app.data.router import is_ashare
            src = "akshare" if is_ashare(ticker) else "yfinance"
            cleaned = CleaningPipeline(source=src).run(
                {"income": df, "balance_sheet": pd.DataFrame(),
                 "cash_flow": pd.DataFrame()}).get("income", df)
        except Exception:
            cleaned = df
        rev = lookup_series({"income": cleaned}, "REVENUE")
        ni = lookup_series({"income": cleaned}, "NET_INCOME")
        if rev is None and ni is None:
            return {"ok": True, "available": False, "ticker": ticker,
                    "reason": "季度利润表未含可识别科目（营收/净利）"}
        return {
            "ok": True, "available": True, "ticker": ticker,
            "periods": [str(i) for i in (rev.index if rev is not None else ni.index)],
            "revenue": None if rev is None else [_num(x) for x in rev.tolist()],
            "net_income": None if ni is None else [_num(x) for x in ni.tolist()],
            "note": ("A股季报为累计口径（Q2=半年、Q4=全年），相邻期不可比，"
                     "同比须同期对同期。" if _is_ashare(ticker) else ""),
        }

    def _compare_peers(self, args: dict) -> dict:
        ticker = _ticker(args)
        if not ticker:
            return {"ok": False, "reason": "缺少 ticker"}
        n = _clamp(_int(args.get("max_peers"), self.max_peers), 1, self.max_peers)
        key = ticker.upper()
        if key in self._once_memo:                      # 每次调查只跑一次
            return {**self._once_memo[key], "duplicate": True,
                    "note_dup": "本工具每次调查最多执行一次，已复用首次结果。"}

        p = self.profile(ticker)
        country = str(p.get("country") or "").strip()
        try:
            from app.analysis.industry import get_market_config
            cfg = get_market_config(country) or [("global", "全球")]
            market_label = f"{cfg[0][1]}市场"
        except Exception:
            market_label = "全球市场"

        try:
            from app.analysis.competitive import CompetitiveAnalyzer
            comps = CompetitiveAnalyzer().identify_competitors(
                company_name=str(p.get("name") or ticker),
                industry=str(p.get("industry") or ""),
                business_summary=str(p.get("description") or ""),
                market=market_label,
            ) or []
        except Exception as e:
            return {"ok": False, "reason": f"竞品识别失败：{type(e).__name__}: {e}"}

        requested = len(comps)
        comps = comps[:n]
        peers: list[dict] = []
        if comps:
            try:
                peers = self.orchestrator._fetch_competitor_revenues(comps) or []
            except Exception as e:
                logger.warning("调查取竞品营收失败 %s: %s", ticker, e)
                peers = []
        peer_pe = None
        try:
            from app.analysis.estimates import estimate_peer_pe
            peer_pe = estimate_peer_pe(peers)
        except Exception as e:
            logger.warning("调查竞品 PE 中位数失败 %s: %s", ticker, e)

        out = {
            "ok": True, "ticker": ticker,
            "requested": requested,          # LLM 提出的竞品名个数
            "resolved": len(peers),          # 真实取到财务数据的家数
            "peers": [{
                "name": x.get("name"), "ticker": x.get("ticker"),
                "revenue_usd": _num(x.get("revenue")),
                "currency_original": x.get("currency_original"),
                "pe": _num(x.get("pe")), "market_cap": _num(x.get("market_cap")),
            } for x in peers],
            "peer_pe": peer_pe,
            "note": ("竞品名由 LLM 提出、财务数据为真实取数；requested 与 resolved "
                     "不等表示有竞品未能解析出真实数据，未解析出的不计入。"),
        }
        self._once_memo[key] = out
        return out

    # ── 执行入口 ──

    def execute(self, name: str, args: dict) -> dict:
        """按名字执行工具，返回 dict。未知工具/异常都降级为 {"ok": false}，从不抛。"""
        fn = _IMPL.get(name)
        if fn is None:
            return {"error": f"未知工具 {name}", "ok": False,
                    "allowed": list(TOOL_NAMES)}
        try:
            return fn(self, args or {})
        except Exception as e:                      # noqa: BLE001
            logger.warning("调查工具 %s 执行失败: %s", name, e)
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    def tool_executor(self, name: str, args: dict) -> str:
        """引擎契约：返回 JSON 字符串，供模型读取真实数值。"""
        return json.dumps(self.execute(name, args), ensure_ascii=False, default=str)


# 工具名 → 实现方法（与 fixed_income._DISPATCH 同构）
_IMPL = {
    "get_statement_summary": Toolbox._get_statement_summary,
    "get_statement_line": Toolbox._get_statement_line,
    "get_news": Toolbox._get_news,
    "get_announcements": Toolbox._get_announcements,
    "search_knowledge": Toolbox._search_knowledge,
    "compare_peers": Toolbox._compare_peers,
    "get_company_profile": Toolbox._get_company_profile,
    "get_price_history": Toolbox._get_price_history,
    "get_quarterly_income": Toolbox._get_quarterly_income,
}

# 报表科目的稳定顺序（供 summary 输出与 allowed 列表）
METRIC_CODES_ORDERED: tuple[str, ...] = tuple(sorted(METRIC_STATEMENT))


# ── 数值格式化 ────────────────────────────────────────────────

def _num(v):
    """None/NaN → None（显式标缺失，不编造）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:          # NaN
        return None
    return round(f, 4)


def _pct(a, b):
    try:
        a, b = float(a), float(b)
        if a == 0:
            return None
        return round((b - a) / abs(a) * 100.0, 4)
    except (TypeError, ValueError):
        return None


def _is_ashare(ticker: str) -> bool:
    try:
        from app.data.router import is_ashare
        return bool(is_ashare(ticker))
    except Exception:
        return False


def _same_period_yoy(series: pd.Series, period: str) -> tuple:
    """同期对同期同比 → (yoy_percent, 基期标签, note)。

    年度序列的上一期即去年同期。季度累计序列**必须**找去年同一报告期：
    相邻季度做差会静默编造出一个不存在的趋势，所以找不到就如实返回 None。
    """
    if series is None or len(series) < 2:
        return None, None, "序列不足两期，无法计算同比"
    last_idx = series.index[-1]
    last_ts = pd.to_datetime(last_idx, errors="coerce")
    last_val = float(series.iloc[-1])
    if not pd.isna(last_ts):
        target = last_ts - pd.DateOffset(years=1)
        best = None
        for idx in series.index[:-1]:
            ts = pd.to_datetime(idx, errors="coerce")
            if pd.isna(ts):
                continue
            diff = abs(int((ts - target).days))
            if diff <= 45 and (best is None or diff < best[0]):
                best = (diff, idx)
        if best is None:
            return (None, None,
                    "未找到去年同期报告期：季度累计口径不可用相邻期做差")
        prev_val = float(series.loc[best[1]])
        if prev_val == 0:
            return None, str(best[1]), "去年同期基数为 0，同比无意义"
        return (round((last_val - prev_val) / abs(prev_val) * 100.0, 4),
                str(best[1]), "")
    # 报告期不是日期（如 "Q2 2023"）：退化为上一期，并如实说明口径
    prev_val = float(series.iloc[-2])
    if prev_val == 0:
        return None, str(series.index[-2]), "上期基数为 0，同比无意义"
    note = "" if period == "annual" else "报告期标签非日期，退化为与上一期比较（口径可能不一致）"
    return (round((last_val - prev_val) / abs(prev_val) * 100.0, 4),
            str(series.index[-2]), note)


# ── 工具声明（OpenAI 格式，由 to_anthropic_tool 转换）────────────

def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": required}}}


_TICKER = {"type": "string", "description": "股票代码，如 AAPL / 0700.HK / 600519.SS"}
_PERIOD = {"type": "string", "enum": ["annual", "quarterly"],
           "description": "报表口径：annual=年报（默认）/ quarterly=季报"}

# ── 证据登记工具（由 loop 拦截处理，不走 Toolbox）────────────────
# 为什么把"指认证据"做成工具而不是让模型在文本里写 JSON：
# 实测模型会正常调用取数工具并用散文写它的思路，但**不会**在文本里输出约定的
# JSON —— 结果 8 次真实取数换不来 1 条证据，闸门 3 根本没机会生效。
# 工具调用的参数由 API 侧结构化约束，远比"求它在散文里带一段 JSON"可靠。
RECORD_EVIDENCE_TOOL: dict = _tool(
    "record_evidence",
    "把你已经读到的某个工具结果登记为某条假设的证据。ref_index 请用我每轮给出的"
    "「可引用的工具结果」清单里的 index（从 0 开始）。本调用不消耗工具调用预算。",
    {"hypothesis_id": {"type": "string", "description": "假设编号，如 H1"},
     "relation": {"type": "string", "enum": ["supports", "contradicts", "neutral"],
                  "description": "这条证据与该假设的关系"},
     "ref_index": {"type": "integer", "description": "工具结果编号（从 0 开始）"},
     "source_ref": {"type": "string",
                    "description": "可选：具体出处（URL / 公告标题 / 科目代码）。"
                                   "一旦填写就必须逐字出现在该工具结果里，否则不被采信"},
     "rationale": {"type": "string", "description": "为什么这条证据支持/反驳该假设"}},
    ["hypothesis_id", "relation", "ref_index"])

INVESTIGATION_TOOLS: list[dict] = [
    _tool("get_statement_summary",
          "取三张财务报表（标准化科目代码）。绝不含 point-in-time 保证，"
          "vintage 字段如实说明数据口径。",
          {"ticker": _TICKER, "period": _PERIOD}, ["ticker"]),
    _tool("get_statement_line",
          "取单个财务科目的多期数值与同期对同期同比。科目代码必须来自真实标准词表，"
          "不接受自造科目名。A股季报为累计口径。",
          {"ticker": _TICKER,
           "line_code": {"type": "string",
                         "description": "标准科目代码，如 REVENUE / INVENTORY / "
                                        "OPERATING_CASH_FLOW / RESEARCH_DEV"},
           "period": _PERIOD,
           "n_periods": {"type": "integer", "description": "返回最近 N 期，2~12"}},
          ["ticker", "line_code"]),
    _tool("get_news",
          "取该标的的真实新闻（A股走东财、美股走 yfinance），返回标题/摘要/日期/"
          "来源/URL。下游引用必须使用这里返回的真实 URL。",
          {"ticker": _TICKER, "limit": {"type": "integer", "description": "最多几条，1~40"}},
          ["ticker"]),
    _tool("get_announcements",
          "取公司公告标题（仅 A 股有源，走巨潮资讯）。美股恒为空并如实说明。",
          {"ticker": _TICKER, "limit": {"type": "integer", "description": "最多几条，1~30"}},
          ["ticker"]),
    _tool("search_knowledge",
          "在已入库的研报/宏观/新闻语料里做混合检索，返回带 source/date/score 的片段。",
          {"query": {"type": "string", "description": "检索问句"},
           "ticker": _TICKER,
           "top_k": {"type": "integer", "description": "返回条数，1~8"}},
          ["query"]),
    _tool("compare_peers",
          "同业真实财务对比：竞品名由模型提出，营收/PE/市值为真实取数，并给出竞品 "
          "PE 中位数。每次调查最多执行一次。",
          {"ticker": _TICKER,
           "max_peers": {"type": "integer",
                         "description": "最多取几家竞品的真实数据（受预算上限约束）"}},
          ["ticker"]),
    _tool("get_company_profile",
          "取公司画像：名称/行业/板块/国家/市值/业务描述，以及 A 股独有的真实"
          "概念板块(concepts)。",
          {"ticker": _TICKER}, ["ticker"]),
    _tool("get_price_history",
          "取历史行情与区间涨跌，并给基准指数同期涨跌，用于判断市场是否已 price in。",
          {"ticker": _TICKER,
           "period": {"type": "string",
                      "description": "如 1mo/3mo/6mo/1y/2y/5y，默认 2y"}},
          ["ticker"]),
    _tool("get_quarterly_income",
          "取季度利润表（营收/净利序列）。A股为累计口径；美股通常无此数据。",
          {"ticker": _TICKER}, ["ticker"]),
]
