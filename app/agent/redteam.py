"""红队 —— 给结论找反方，但每句话都必须挂得住引用。

核心纪律：**没有引用就不许说话**。模型提出的每一条反方论证都必须指向一个
已经存在的事实（台账路径 / 证据 id），引用对不上就整条丢弃并留档。丢弃后
条数不够就如实说不够——宁可红队只剩 1 条，也不要凑够 3 条编的。
否则红队会变成第二个幻觉源，比没有红队更糟。

输出三段（对应计划 B3）：
  counter_arguments  反方论证，每条挂 evidence_ref
  fragile_assumption 结论依赖、但证据最弱的那条假设
  open_questions     不知道的就写不知道，不用推测填

第三段与「脆弱假设」的兜底部分是**确定性**的：直接从未验证假设、未采信证据、
台账缺数里取，不经过模型，因此不会编。
"""
from __future__ import annotations

from datetime import datetime, timezone

MIN_COUNTER_ARGUMENTS = 3
MAX_OPEN_QUESTIONS = 8
# 给模型看的引用清单上限（全量池仍用于校验；给模型看太多只是烧 token）
_PROMPT_REF_LIMIT = 60

_SALIENT_PREFIXES = (
    "valuation_analysis.dcf",
    "valuation_analysis.relative_valuation",
    "valuation_analysis.",
    "financial_analysis.ratios",
    "financial_analysis.",
    "market_analysis.",
    "competitive_analysis.",
    "sotp_analysis.",
    "supply_chain_analysis.",
    "geopolitical_analysis.",
    "fact_checks.",
    "trade_plan.",
)

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_NO_LLM = "llm_unavailable"
_STATUSES = {STATUS_OK, STATUS_INSUFFICIENT, STATUS_NO_LLM}

_SYSTEM = """你是投研红队。你的任务不是附和结论，而是攻击它。

硬规则：
1. 每条反方论证必须给出 evidence_ref，取值只能来自我给你的「可引用事实」清单；
2. 给不出引用的观点，一律不要写——写了也会被系统丢弃；
3. 不知道的事写进 open_questions，不要用推测填满；
4. 只输出 JSON，不要 markdown 包裹。"""


# ── 引用池 ────────────────────────────────────────────────────

def _is_investigation(source: dict) -> bool:
    return isinstance(source.get("hypotheses"), list) and isinstance(source.get("evidence"), list)


def ref_pool(source: dict) -> dict[str, dict]:
    """可被引用的既有事实：引用标识 → 事实。两种产物形状各取各的。"""
    if _is_investigation(source):
        pool: dict[str, dict] = {}
        for e in source.get("evidence") or []:
            if not isinstance(e, dict):
                continue
            eid = str(e.get("id") or "").strip()
            # 未采信的证据（自述出处对不上工具结果）不能拿来当引用
            if not eid or not e.get("attribution_valid"):
                continue
            pool[eid] = {
                "kind": e.get("source_kind"),
                "source_ref": e.get("source_ref"),
                "value": e.get("snippet"),
                "hypothesis_id": e.get("hypothesis_id"),
            }
        return pool

    from app.trust import build_ledger

    ledger = build_ledger(source, ticker=source.get("ticker"))
    pool = {}
    for f in ledger.facts:
        if f.value is None:
            continue
        pool[f.path] = {"kind": f.provenance, "value": f.value,
                        "as_of": f.as_of, "unit": f.unit}
    return pool


def _salient(pool: dict[str, dict], limit: int = _PROMPT_REF_LIMIT) -> dict[str, dict]:
    """挑一份给模型看的子集：按业务分区的优先级 + 路径排序，确定性可复现。"""

    def rank(path: str) -> tuple[int, str]:
        for i, p in enumerate(_SALIENT_PREFIXES):
            if path.startswith(p):
                return i, path
        return len(_SALIENT_PREFIXES), path

    return {k: pool[k] for k in sorted(pool, key=rank)[:limit]}


def _conclusion(source: dict, pool: dict[str, dict]) -> str:
    """被攻击的对象：调查结论 / 分析摘要。"""
    if _is_investigation(source):
        lines = [f"- [{h.get('id')}] {h.get('statement')} → {h.get('status')}"
                 f"（支持 {h.get('support_count', 0)}、反证 {h.get('contradict_count', 0)}）"
                 for h in (source.get("hypotheses") or [])]
        return "\n".join(lines) or "（无假设）"
    summary = ((source.get("valuation_analysis") or {}).get("summary")
               or (source.get("company_info") or {}).get("name") or "")
    return str(summary)[:800] or "（无摘要）"


# ── 确定性部分 ────────────────────────────────────────────────

def _fragile_deterministic(source: dict, pool: dict[str, dict]) -> dict | None:
    """调查跑的：已验假设里支持证据最少的那条——它就是结论最薄的那块板。"""
    if not _is_investigation(source):
        return None
    verified = [h for h in (source.get("hypotheses") or [])
                if h.get("status") == "verified" and h.get("evidence")]
    if not verified:
        return None
    weakest = min(verified, key=lambda h: (h.get("support_count") or 0, str(h.get("id"))))
    refs = [str(e.get("id")) for e in weakest.get("evidence") or []
            if isinstance(e, dict) and e.get("attribution_valid") and str(e.get("id")) in pool]
    if not refs:
        return None
    return {"assumption": weakest.get("statement"),
            "evidence_ref": refs[0],
            "why": f"该假设只拿到 {len(refs)} 条可采信证据，是整个结论里证据最薄的一环",
            "evidence": pool.get(refs[0])}


def _open_questions_deterministic(source: dict, pool: dict[str, dict]) -> list[str]:
    """不知道的事情，从未验证假设 / 未采信证据 / 台账缺数里如实取。"""
    out: list[str] = []
    if _is_investigation(source):
        for h in source.get("hypotheses") or []:
            status = str(h.get("status") or "")
            if status and status != "verified":
                out.append(f"未验证：{h.get('statement')}（{status}）")
        rejected = [e for e in (source.get("evidence") or [])
                    if isinstance(e, dict) and not e.get("attribution_valid")]
        if rejected:
            out.append(f"有 {len(rejected)} 条证据因出处对不上而未被采信，结论没有把它们算进去")
    else:
        unknown = [f for f in _ledger_facts(source) if f.value is None]
        for f in unknown[:5]:
            out.append(f"台账缺数：{f.path}")
    return out[:MAX_OPEN_QUESTIONS]


def _ledger_facts(source: dict):
    from app.trust import build_ledger

    return build_ledger(source, ticker=source.get("ticker")).facts


# ── 主入口 ────────────────────────────────────────────────────

def build_redteam(source: dict, *, llm=None, min_counter: int = MIN_COUNTER_ARGUMENTS,
                  ask=None) -> dict:
    """给一份产物出红队报告。

    Args:
        source: 调查 run（含 hypotheses/evidence）或分析产物
        llm:    可注入的 LLM 客户端；None 时尝试取全局客户端，取不到就降级
        ask:    可注入的问答函数 ask(messages, system) -> str（测试用）

    Returns:
        dict，结构见模块 docstring。status 为 ok 时 counter_arguments 至少 min_counter 条，
        且每条都通过了引用校验。
    """
    pool = ref_pool(source)
    redteam: dict = {
        "status": STATUS_NO_LLM,
        "counter_arguments": [],
        "dropped": [],
        "fragile_assumption": _fragile_deterministic(source, pool),
        "open_questions": _open_questions_deterministic(source, pool),
        "refs_available": len(pool),
        "min_counter": min_counter,
        "note": "",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if not pool:
        redteam["note"] = "没有任何可引用的事实，红队无从谈起（不编）。"
        return redteam

    raw = _ask_llm(source, pool, llm=llm, ask=ask)
    if raw is None:
        redteam["note"] = "模型不可用或未返回可解析结果；反方论证留空，不拿推测充当。"
        return redteam

    kept, dropped = _vet(raw.get("counter_arguments"), pool)
    redteam["counter_arguments"] = kept
    redteam["dropped"] = dropped
    redteam["fragile_assumption"] = _vet_fragile(raw.get("fragile_assumption"), pool) \
        or redteam["fragile_assumption"]
    redteam["open_questions"] = ([str(q).strip() for q in (raw.get("open_questions") or [])
                                  if str(q).strip()] + redteam["open_questions"]
                                 )[:MAX_OPEN_QUESTIONS]

    if len(kept) >= min_counter:
        redteam["status"] = STATUS_OK
        redteam["note"] = f"{len(kept)} 条反方论证全部有真实引用"
    else:
        redteam["status"] = STATUS_INSUFFICIENT
        redteam["note"] = (f"只有 {len(kept)}/{min_counter} 条反方论证挂得住引用，"
                           f"另 {len(dropped)} 条因引用对不上被丢弃——证据不足就说不足，不凑数。")
    return redteam


def _ask_llm(source: dict, pool: dict[str, dict], *, llm=None, ask=None) -> dict | None:
    from app.llm.structured import parse_llm_json

    shown = _salient(pool)
    refs_txt = "\n".join(
        f"- {k} = {str(v.get('value'))[:120]}" for k, v in shown.items())
    user = (f"【被攻击的结论】\n{_conclusion(source, pool)}\n\n"
            f"【可引用事实（evidence_ref 只能取这些）】\n{refs_txt}\n\n"
            f"请输出 JSON：{{\n"
            f'  "counter_arguments": [{{"point": "反方观点", "evidence_ref": "上面清单里的标识", "why": "为什么它支持反方"}}],\n'
            f'  "fragile_assumption": {{"assumption": "结论最依赖但证据最薄的一条", "evidence_ref": "...", "why": "..."}},\n'
            f'  "open_questions": ["目前确实不知道的问题"]\n'
            f"}}\n"
            f"counter_arguments 请给 {MIN_COUNTER_ARGUMENTS}~5 条；给不出引用的就不要写。")

    try:
        if ask is not None:
            text = ask(user, _SYSTEM)
        else:
            if llm is None:
                from app.llm.client import get_llm_client

                llm = get_llm_client()
            text = llm.chat([{"role": "user", "content": user}], system=_SYSTEM,
                            task="report")
    except Exception:  # noqa: BLE001 LLM 不可用 → 降级，不编造
        return None

    parsed = parse_llm_json(text, default=None, context="红队")
    return parsed if isinstance(parsed, dict) else None


def _vet(items, pool: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """引用校验：对不上池子的整条丢弃，但留档说明丢在哪。"""
    kept: list[dict] = []
    dropped: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        point = str(item.get("point") or "").strip()
        ref = str(item.get("evidence_ref") or "").strip()
        if not point:
            continue
        if not ref:
            dropped.append({"point": point, "evidence_ref": "", "reason": "没有引用"})
        elif ref not in pool:
            dropped.append({"point": point, "evidence_ref": ref,
                            "reason": f"引用 {ref} 不在事实池里"})
        else:
            kept.append({"point": point, "evidence_ref": ref,
                         "why": str(item.get("why") or "").strip(),
                         "evidence": pool[ref]})
    return kept, dropped


def _vet_fragile(item, pool: dict[str, dict]) -> dict | None:
    if not isinstance(item, dict):
        return None
    assumption = str(item.get("assumption") or "").strip()
    ref = str(item.get("evidence_ref") or "").strip()
    if not assumption or ref not in pool:
        return None
    return {"assumption": assumption, "evidence_ref": ref,
            "why": str(item.get("why") or "").strip(), "evidence": pool[ref]}


# ── 校验（给 L3 断言用）──────────────────────────────────────

def validate_redteam(redteam, refs: dict | None = None) -> list[str]:
    """返回违规说明列表；空列表 = 合格。产物里没红队字段也返回空（默认不触发）。"""
    if redteam is None:
        return []
    if not isinstance(redteam, dict):
        return ["redteam 不是对象"]
    problems: list[str] = []

    status = redteam.get("status")
    if status not in _STATUSES:
        problems.append(f"status 非法：{status!r}")

    counter = redteam.get("counter_arguments")
    if not isinstance(counter, list):
        return problems + ["counter_arguments 不是列表"]

    for i, c in enumerate(counter):
        if not isinstance(c, dict):
            problems.append(f"第 {i} 条反方论证不是对象")
            continue
        ref = c.get("evidence_ref")
        if not isinstance(ref, str) or not ref.strip():
            problems.append(f"第 {i} 条反方论证没有 evidence_ref")
        elif refs is not None and ref not in refs:
            problems.append(f"第 {i} 条引用 {ref} 不在事实池里")

    if status == STATUS_OK:
        need = int(redteam.get("min_counter") or MIN_COUNTER_ARGUMENTS)
        if len(counter) < need:
            problems.append(f"status=ok 却只有 {len(counter)} 条（要求 {need}）")

    fragile = redteam.get("fragile_assumption")
    if fragile is not None:
        if not isinstance(fragile, dict):
            problems.append("fragile_assumption 不是对象")
        else:
            ref = fragile.get("evidence_ref")
            if not isinstance(ref, str) or not ref.strip():
                problems.append("fragile_assumption 没有 evidence_ref")
            elif refs is not None and ref not in refs:
                problems.append(f"fragile_assumption 引用 {ref} 不在事实池里")

    oq = redteam.get("open_questions")
    if not isinstance(oq, list) or any(not isinstance(q, str) for q in oq):
        problems.append("open_questions 必须是字符串列表")

    return problems


__all__ = [
    "MIN_COUNTER_ARGUMENTS", "STATUS_INSUFFICIENT", "STATUS_NO_LLM", "STATUS_OK",
    "build_redteam", "ref_pool", "validate_redteam",
]
