"""统一研究编排入口 —— 只决定「这个问题该走哪条通路」，不重写执行。

三档对应三条**已存在**的通路，本模块一行执行逻辑都没有：

    quick    app/agent 单遍管线   intent → planner → runner → expressor（5 个工具）
    standard app/analysis 固定管线  单标的，财务→DCF→分部→竞品→供应链→SWOT
    deep     app/investigation 循环  单标的，封闭工具集 + 硬预算 + 证据落库

B1 的验收是「经编排的结果与直接调用逐字相同」，所以这里对既有函数的引用
必须是**同一批函数对象**，不能复制一份逻辑。`scripts/verify_orchestrator.py`
用 `is` 断言把这件事钉住。

档位与标的形状不匹配时**显式降级并把原因写进 route.reason**，
不静默换一条通路——用户在 UI 上要能看到「你要的 deep 没跑成」。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.agent.expressor import generate_report
from app.agent.intent import parse as parse_intent
from app.agent.planner import build_plan
from app.agent.runner import execute_plan

CHAT = "chat"
ANALYSIS = "analysis"
INVESTIGATION = "investigation"
PIPELINES = (CHAT, ANALYSIS, INVESTIGATION)


class Depth(str, Enum):
    """研究档位。

    与 `app.investigation.budget.Depth` **不是同一个枚举**：那边只有
    standard/deep（它没有「单遍快查」这条通路）。两边的 standard/deep 取值相同，
    所以传给 run_investigation 时可以直接给 `.value`。
    """

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"

    @classmethod
    def coerce(cls, value) -> "Depth":
        """非法值一律落 standard（与其他入口同风格：参数写错不报错）。

        注意 str 混入枚举在 3.11+ 的 `str(Depth.DEEP) == "Depth.DEEP"`，
        必须先处理枚举实例，不能直接 str()。
        """
        if isinstance(value, cls):
            return value
        return _DEPTH_ALIASES.get(str(value or "").strip().lower(), cls.STANDARD)


_DEPTH_ALIASES = {
    "quick": Depth.QUICK,
    "standard": Depth.STANDARD,
    "deep": Depth.DEEP,
}

# 意图 → 档位的默认推断。只在这条路径上生效：显式 depth 永远优先。
_INTENT_DEPTH: dict[str, Depth] = {
    "query_price": Depth.QUICK,
    "query_history": Depth.QUICK,
    "financials": Depth.QUICK,
    "compare": Depth.QUICK,
    "analysis": Depth.STANDARD,
    "deep_compare": Depth.DEEP,
}

DEFAULT_DEPTH = Depth.STANDARD


def coerce_depth(value) -> tuple[Depth, str | None]:
    """返回 (档位, 警告)。入参非法时警告非 None——不静默吞掉写错的档位名。"""
    raw = "" if value is None else str(value).strip()
    if isinstance(value, Depth) or raw.lower() in _DEPTH_ALIASES:
        return Depth.coerce(value), None
    return Depth.STANDARD, f"未知档位 {raw!r}，按 standard 处理"


def resolve_depth(intent: dict | None, *, depth=None
                  ) -> tuple[Depth, str, str | None]:
    """显式参数 > 意图 > 默认。返回 (档位, 来源, 警告)。"""
    if depth is not None:
        d, warn = coerce_depth(depth)
        return d, "explicit", warn
    name = str((intent or {}).get("intent") or "").strip().lower()
    if name in _INTENT_DEPTH:
        return _INTENT_DEPTH[name], f"intent:{name}", None
    return DEFAULT_DEPTH, "default", None


@dataclass(frozen=True)
class Route:
    """一次分派的决定。frozen：决定一旦做出就不许中途改。"""

    pipeline: str
    depth: Depth          # 实际执行的档位
    requested: Depth      # 请求的（或由意图推出的）档位
    source: str           # explicit | intent:<name> | default
    reason: str
    ticker: str | None = None

    @property
    def downgraded(self) -> bool:
        return self.depth is not self.requested

    def as_dict(self) -> dict:
        return {
            "pipeline": self.pipeline,
            "depth": self.depth.value,
            "requested": self.requested.value,
            "source": self.source,
            "reason": self.reason,
            "ticker": self.ticker,
            "downgraded": self.downgraded,
        }


def plan_route(intent: dict | None, *, depth=None) -> Route:
    """只算路由，不执行。UI 可以先问「会怎么跑」再决定要不要跑。"""
    requested, source, warn = resolve_depth(intent, depth=depth)
    tickers = [t for t in ((intent or {}).get("tickers") or []) if t]
    one = tickers[0] if len(tickers) == 1 else None

    if requested is Depth.QUICK:
        return Route(CHAT, Depth.QUICK, Depth.QUICK, source,
                     _with_warn("quick：单遍管线，一次取数一次成文", warn), one)

    if one is None:
        got = f"{len(tickers)} 个标的" if tickers else "没有标的"
        return Route(CHAT, Depth.QUICK, requested, source,
                     _with_warn(f"{requested.value} 档只吃单一标的，当前{got}；降级为单遍管线",
                                warn),
                     None)

    if requested is Depth.STANDARD:
        return Route(ANALYSIS, Depth.STANDARD, Depth.STANDARD, source,
                     _with_warn("standard：固定分析管线（财务→DCF→分部→竞品→供应链→SWOT）",
                                warn),
                     one)

    return Route(INVESTIGATION, Depth.DEEP, Depth.DEEP, source,
                 _with_warn("deep：假设驱动循环（封闭工具集 + 硬预算 + 证据落库）",
                            warn),
                 one)


def _with_warn(reason: str, warn: str | None) -> str:
    return f"{reason}；{warn}" if warn else reason


# ── 执行器默认实现：全部指向既有通路 ─────────────────────────

def _run_chat(text: str, intent: dict, history: list[dict] | None = None) -> dict:
    """与 api/routes_chat.py 的 chat() 内同一串调用、同一批函数对象。"""
    plan = build_plan(intent)
    execution = execute_plan(plan)
    report = generate_report(execution, intent, history=history)
    return {"intent": intent, "plan": plan, "execution": execution, "report": report}


def _run_analysis(ticker: str, *, period: str = "annual", retriever=None,
                  progress_cb=None) -> dict:
    # 延迟 import：pandas / 全套分析模块不该在只走 quick 时被拖起来
    from app.analysis.pipeline import AnalysisOrchestrator

    return AnalysisOrchestrator().run(ticker, period=period, retriever=retriever,
                                      progress_cb=progress_cb)


def _run_investigation(ticker: str, *, period: str = "annual", progress_cb=None) -> dict:
    from app.investigation.loop import run_investigation

    return run_investigation(ticker, depth=Depth.DEEP.value, period=period,
                             progress=progress_cb)


def _run_redteam(result: dict, *, llm=None) -> dict:
    """deep 档唯一的附加环节。红队自己出错不该毁掉整次调查。"""
    from app.agent.redteam import build_redteam

    try:
        return build_redteam(result, llm=llm)
    except Exception as e:  # noqa: BLE001
        return {"status": "llm_unavailable", "counter_arguments": [], "dropped": [],
                "fragile_assumption": None, "open_questions": [],
                "note": f"红队环节异常，已跳过：{type(e).__name__}"}


def _synthetic_intent(text: str, ticker: str) -> dict:
    """调用方直接给了标的：不必为解析意图再花一次 LLM。"""
    return {"intent": "analysis", "tickers": [ticker], "timeframe": None,
            "analysis_type": "general", "raw_input": text, "tickers_source": "caller"}


def dispatch(text: str, *, depth=None, intent: dict | None = None,
             ticker: str | None = None, history: list[dict] | None = None,
             period: str = "annual", retriever=None, progress_cb=None, llm=None,
             chat_runner: Callable | None = None,
             analysis_runner: Callable | None = None,
             investigation_runner: Callable | None = None,
             redteam_runner: Callable | None = None) -> dict:
    """分派并执行。

    intent 给了就不再解析；否则用 ticker 合成（不花 token）；再否则调 intent.parse。
    四个 runner 只为离线验证注入，生产路径不要传。

    Returns:
        {"route": {...}, ...}，其余字段按通路不同：
        chat → intent / plan / execution / report
        analysis → ticker / artifact
        investigation → ticker / investigation（结果里带 redteam，仅 deep 档）
    """
    if intent is None:
        if ticker:
            intent = _synthetic_intent(text, ticker)
        else:
            intent = parse_intent(text, history=history)

    route = plan_route(intent, depth=depth)
    head = {"route": route.as_dict(), "intent": intent}

    if route.pipeline == CHAT:
        runner = chat_runner or _run_chat
        return {**head, **runner(text, intent, history)}

    if route.pipeline == ANALYSIS:
        runner = analysis_runner or _run_analysis
        artifact = runner(route.ticker, period=period, retriever=retriever,
                          progress_cb=progress_cb)
        return {**head, "ticker": route.ticker, "artifact": artifact}

    runner = investigation_runner or _run_investigation
    result = runner(route.ticker, period=period, progress_cb=progress_cb)
    # 只有 deep 档跑红队：standard/quick 的产物不进这一段，L3/L4 数字不受影响
    redteam = (redteam_runner or _run_redteam)(result, llm=llm)
    if isinstance(result, dict) and redteam is not None:
        result["redteam"] = redteam
    return {**head, "ticker": route.ticker, "investigation": result}


__all__ = [
    "ANALYSIS", "CHAT", "DEFAULT_DEPTH", "Depth", "INVESTIGATION", "PIPELINES",
    "Route", "coerce_depth", "dispatch", "plan_route", "resolve_depth",
]
