"""事实台账：把一次管线产物的每个数值叶子落成一条带出处的事实。

台账是一次运行的可审计清单。它回答三个此前无人回答的问题：
  1. 这个数字是谁算的（computed / source / llm / imputed / degraded）
  2. 它是哪个期间的（as_of）
  3. 若有人质疑，能定位到哪个字段（path / fact_id）

用法：
    from app.trust import build_ledger
    ledger = build_ledger(result, ticker="600519.SS", model="deepseek-v4-flash")
    ledger.to_json("data/artifacts/600519.SS/20261104T120000.json")

本模块不做任何计算、不调用 LLM、不修改 result——纯只读派生。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from app.trust import provenance as prov

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 顶层 section → 负责模块
_MODULE_BY_SECTION = {
    "company_info": "data.profile",
    "financial_analysis": "analysis.financial",
    "valuation_analysis": "analysis.valuation",
    "sotp_analysis": "analysis.segment",
    "market_analysis": "analysis.market",
    "competitive_analysis": "analysis.competitive",
    "swot_analysis": "analysis.swot",
    "fact_checks": "analysis.validator",
    "geopolitical_analysis": "analysis.geopolitical",
    "supply_chain_analysis": "analysis.supply_chain",
    "news_analysis": "analysis.news",
    "meeting_analysis": "analysis.meeting",
    "knowledge": "rag",
    "trade_plan": "analysis.trade_plan",
    "price_history": "data.history",
    "sector_adapter": "analysis.sector_adapter",
}

# 显式声明单位的指标；未列出的保持 None（「未声明」，不猜）
_UNIT_RATIO = frozenset({
    "revenue_share", "net_income_share", "share", "market_share", "upside",
    "upside_low", "upside_high", "premium", "cr3", "cr5", "sentiment_score",
    "growth_assumption", "terminal_growth", "wacc_used", "gross_margin_impact",
    "china_exposure", "cost_ratio", "GROSS_MARGIN", "NET_MARGIN",
    "OPERATING_MARGIN", "OPERATING_CF_MARGIN", "RD_TO_REVENUE",
    "CAPEX_TO_REVENUE", "ROE", "ROA", "FCF_YIELD", "BUYBACK_YIELD",
    "CURRENT_RATIO", "QUICK_RATIO", "DEBT_TO_EQUITY", "NET_DEBT_TO_EQUITY",
    "ASSET_TURNOVER", "CASH_CONVERSION_RATIO", "EV_EBITDA", "EQUITY_MULTIPLIER",
})
_UNIT_CURRENCY = frozenset({
    "open", "high", "low", "close",  # 行情K线，计价币种随标的
    "market_cap", "revenue", "current_price", "fair_value", "fair_value_ev",
    "pv_projections", "pv_terminal", "terminal_value", "sotp_value_low",
    "sotp_value_high", "per_share_low", "per_share_high", "value_low",
    "value_high", "tam_estimate", "NET_INCOME", "REVENUE", "EBITDA",
    "TOTAL_ASSETS", "TOTAL_EQUITY", "OPERATING_INCOME", "INTEREST_EXPENSE",
    "TAX_PROVISION", "STOCK_BUYBACK", "FREE_CASH_FLOW", "net_change",
})
_UNIT_COUNT = frozenset({"employees", "shares_outstanding", "volume", "period_count"})
_UNIT_SECONDS = frozenset({"pipeline_time"})


def _unit_for(metric: str) -> str | None:
    if metric in _UNIT_RATIO:
        return "ratio"
    if metric in _UNIT_CURRENCY:
        return "currency"
    if metric in _UNIT_COUNT:
        return "count"
    if metric in _UNIT_SECONDS:
        return "seconds"
    return None


def _module_for(path: str) -> str | None:
    return _MODULE_BY_SECTION.get(path.split(".", 1)[0].split("[", 1)[0])


def content_hash(obj: object) -> str:
    """结构哈希：用于 run_id、基线对比与「产物是否真的变了」。"""
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


_GIT_SHA_CACHE: str | None = None


def git_sha() -> str | None:
    """当前 HEAD 短哈希；非 git 环境返回 None（不伪造）。"""
    global _GIT_SHA_CACHE
    if _GIT_SHA_CACHE is not None:
        return _GIT_SHA_CACHE or None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=5,
        )
        _GIT_SHA_CACHE = out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        _GIT_SHA_CACHE = ""
    return _GIT_SHA_CACHE or None


@dataclass(frozen=True)
class Fact:
    fact_id: str
    path: str
    metric: str
    value: float
    unit: str | None
    as_of: str | None
    provenance: str
    module: str | None
    evidence_ref: str | None

    @property
    def is_unknown(self) -> bool:
        return self.provenance == prov.UNKNOWN

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Ledger:
    facts: list[Fact] = field(default_factory=list)
    ticker: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    git_sha: str | None = None
    artifact_hash: str | None = None
    run_id: str | None = None

    # ── 查询 ──
    def by_provenance(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.facts:
            counts[f.provenance] = counts.get(f.provenance, 0) + 1
        return dict(sorted(counts.items()))

    def unknown(self) -> list[Fact]:
        return [f for f in self.facts if f.is_unknown]

    def find(self, path: str) -> Fact | None:
        for f in self.facts:
            if f.path == path:
                return f
        return None

    def of(self, provenance: str) -> list[Fact]:
        return [f for f in self.facts if f.provenance == provenance]

    @property
    def total(self) -> int:
        return len(self.facts)

    def provenance_rate(self, target: str) -> float:
        """某出处占比；空台账返回 0.0（不返回 NaN 以免污染基线）。"""
        return 0.0 if not self.facts else round(
            len(self.of(target)) / len(self.facts), 4)

    # ── 序列化 ──
    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "ticker": self.ticker,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "git_sha": self.git_sha,
            "artifact_hash": self.artifact_hash,
            "counts": self.by_provenance(),
            "facts": [f.to_dict() for f in self.facts],
        }

    def to_json(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # allow_nan=False：台账里的 value 若出现 NaN，宁可抛错也不写出非法 JSON
        p.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2,
                       allow_nan=False),
            encoding="utf-8",
        )
        return p

    @classmethod
    def from_dict(cls, d: dict) -> Ledger:
        return cls(
            facts=[Fact(**f) for f in d.get("facts", [])],
            ticker=d.get("ticker"),
            model=d.get("model"),
            prompt_version=d.get("prompt_version"),
            git_sha=d.get("git_sha"),
            artifact_hash=d.get("artifact_hash"),
            run_id=d.get("run_id"),
        )

    @classmethod
    def load(cls, path: str | Path) -> Ledger:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ── 遍历 ──

_MARKER_DEGRADED = "degraded"
_MARKER_IMPUTED = "imputed"
# 降级/插补原因可能落在这些字段上（各模块历史写法不一）
_REASON_KEYS = ("reason", "degraded_reason", "imputed_reason", "note")


def _reason_in(node: dict) -> str | None:
    for key in _REASON_KEYS:
        v = node.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _columnar_dates(node, path: str, out: dict[str, list[str]]) -> None:
    """收集列式序列的日期轴。

    price_history 是列式的：{'dates': [...], 'close': [...], 'open': [...]}，
    各列按下标与 dates 平行对齐。日期不在这两个列表的节点上，遍历时取不到，
    所以先建一张「容器路径 → 日期轴」的旁表。
    """
    if isinstance(node, dict):
        dates = node.get("dates")
        if (isinstance(dates, list) and dates
                and all(isinstance(d, str) for d in dates[:5])):
            out[path] = dates
        for k, v in node.items():
            _columnar_dates(v, f"{path}.{k}" if path else k, out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _columnar_dates(v, f"{path}[{i}]", out)


def _walk(node, path: str, *, forced: str | None, forced_reason: str | None,
          as_of: str | None, as_of_source: str | None,
          out: list[Fact], dates: dict[str, list[str]] | None = None) -> None:
    if isinstance(node, dict):
        # 祖先标记向下传播：degraded / imputed / available=False
        marker: str | None = None
        if node.get(_MARKER_DEGRADED) is True:
            marker = prov.DEGRADED
        elif node.get(_MARKER_IMPUTED) is True:
            marker = prov.IMPUTED
        elif node.get("available") is False and forced is None:
            marker = prov.DEGRADED
        if marker is not None:
            forced = marker
            forced_reason = _reason_in(node)

        if as_of is None:
            # 期间的写法各模块不一：财报期 period / 显式 as_of / 季度报告日 / 行情K线日期
            for key in ("period", "as_of", "report_date", "date"):
                v = node.get(key)
                if isinstance(v, str) and v:
                    as_of, as_of_source = v, key
                    break

        has_period = isinstance(node.get("period"), str)
        for k, v in node.items():
            if k in (_MARKER_DEGRADED, _MARKER_IMPUTED) or k.endswith("_reason"):
                continue
            sub = f"{path}.{k}" if path else k
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                metric = k
                if k == "value" and has_period:
                    metric = _container_name(sub)
                _emit(sub, metric, float(v), forced, forced_reason,
                      as_of, as_of_source, out)
            else:
                _walk(v, sub, forced=forced, forced_reason=forced_reason,
                      as_of=as_of, as_of_source=as_of_source, out=out, dates=dates)

    elif isinstance(node, list):
        # 列式序列：本列按同一基准的下标对应日期轴
        axis = (dates or {}).get(path.rsplit(".", 1)[0]) if dates else None
        for i, v in enumerate(node):
            sub = f"{path}[{i}]"
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                elem_as_of = axis[i] if axis and i < len(axis) else as_of
                elem_src = "dates[i]" if axis and i < len(axis) else as_of_source
                _emit(sub, _container_name(sub), float(v), forced, forced_reason,
                      elem_as_of, elem_src, out)
            else:
                # 列表元素之间的期间不继承（各元素自带 period）
                _walk(v, sub, forced=forced, forced_reason=forced_reason,
                      as_of=None, as_of_source=None, out=out, dates=dates)


def _container_name(path: str) -> str:
    """`...ratios.ROE[0].value` → `ROE`；`...fcf_history[3]` → `fcf_history`。"""
    head = path.split("[", 1)[0]
    return head.rsplit(".", 1)[-1] if "." in head else head


def _emit(path: str, metric: str, value: float, forced: str | None,
          forced_reason: str | None, as_of: str | None,
          as_of_source: str | None, out: list[Fact]) -> None:
    provenance, evidence = prov.classify(path)
    if forced is not None:
        provenance = forced
        # 带上模块自报的失败原因，降级才说得清是为什么
        evidence = f"{forced}:{forced_reason}" if forced_reason else f"{forced}:marker"
    if provenance == prov.UNKNOWN:
        evidence = None
    out.append(Fact(
        fact_id="",  # 遍历结束后统一编号，保证顺序确定
        path=path,
        metric=metric,
        value=value,
        unit=_unit_for(metric),
        as_of=as_of,
        provenance=provenance,
        module=_module_for(path),
        evidence_ref=evidence,
    ))


# 价格派生事实：它们的期间只有一个可能来源——`pricing.price_as_of`。
# 只补这五条路径，其余一律不动（技术位来自滚动窗口，单日定义不了它的期间）。
_PRICE_ANCHORED_PATHS = frozenset({
    "financial_analysis.current_price",
    "financial_analysis.valuation_context.current_pe",
    "financial_analysis.valuation_context.pe_percentile",
    "valuation_analysis.dcf.upside",
    "trade_plan.current_price",
})
_PE_PERCENTILE_PATH = "financial_analysis.valuation_context.pe_percentile"


def _anchor_price_dates(facts: list[Fact], result: dict) -> list[Fact]:
    """给价格派生事实补上行情交易日。

    只在该事实原本 `as_of` 为空时补，不覆盖任何已有期间；产物没有 `pricing`
    （本改动之前生成的产物）时原样返回，行为与之前逐条一致。
    """
    pricing = result.get("pricing") if isinstance(result, dict) else None
    if not isinstance(pricing, dict):
        return facts
    price_as_of = pricing.get("price_as_of")
    if not isinstance(price_as_of, str) or not price_as_of:
        return facts

    vc = (result.get("financial_analysis") or {}).get("valuation_context")
    # 刷新产物里分位可能比现价旧，它自己带日期；缺省即与现价同日
    pe_as_of = vc.get("pe_percentile_as_of") if isinstance(vc, dict) else None
    if not isinstance(pe_as_of, str) or not pe_as_of:
        pe_as_of = price_as_of

    return [
        replace(f, as_of=pe_as_of if f.path == _PE_PERCENTILE_PATH else price_as_of)
        if f.as_of is None and f.path in _PRICE_ANCHORED_PATHS else f
        for f in facts
    ]


def build_ledger(result: dict, *, ticker: str | None = None,
                 model: str | None = None,
                 prompt_version: str | None = None) -> Ledger:
    """从管线 result 构建台账。fact_id 按遍历顺序编号 F001…（确定性）。"""
    raw: list[Fact] = []
    dates: dict[str, list[str]] = {}
    _columnar_dates(result, "", dates)
    _walk(result, "", forced=None, forced_reason=None,
          as_of=None, as_of_source=None, out=raw, dates=dates)
    raw = _anchor_price_dates(raw, result)
    numbered = [
        Fact(**{**f.to_dict(), "fact_id": f"F{i:03d}"}) for i, f in enumerate(raw, 1)
    ]
    h = content_hash(result)
    return Ledger(
        facts=numbered,
        ticker=ticker or (result.get("ticker") if isinstance(result, dict) else None),
        model=model,
        prompt_version=prompt_version,
        git_sha=git_sha(),
        artifact_hash=h,
        run_id=h[:12],
    )
