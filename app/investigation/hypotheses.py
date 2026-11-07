"""假设生成与取证判定 —— 反编造闸门 2 与 3 的实现。

三条闸门代码化：
  闸门 1（signals.py）：无真实 material 信号 → 零成因族 → 零假设。
  闸门 2（本模块 seed_families + frame_hypotheses）：模型只能把**已播种的因果族**
      措辞化；返回的族不在播种集合内 → 直接丢弃并记日志。
  闸门 3（本模块 evaluate）：状态判定是**纯代码**，无 LLM。模型对证据只能"指认"
      （给索引，由 loop 解析成真实出处），指认不通过校验的项 `attribution_valid=False`
      计零。因此"没有证据的成因"在结构上不可能被判为已验证。

`evaluate` 的判定表（`证据不足 / 未验证 / 部分验证 / 已验证`）见下方 docstring。
**已验证**要求 ≥2 条支持且来自 ≥2 个不同 source_kind——单条新闻永远不够，
这是"上下游只能作为对真实文本的假设"这一决定的强制机制。
"""
from __future__ import annotations

import json

from app.utils.logger import logger

# ── 证据强度（按来源类型确定性赋值，不由模型决定）──────────────
STRENGTH_RANK = {"weak": 0, "medium": 1, "strong": 2}

SOURCE_KIND_STRENGTH: dict[str, str] = {
    "financial_statement": "strong",   # 报表科目：硬数据
    "peer": "strong",                  # 同业真实财务
    "announcement": "medium",          # 公司公告
    "news": "medium",                  # 新闻
    "rag": "medium",                   # 已入库语料
    "market": "medium",                # 行情走势：有提示性但不足以确认成因
    "profile": "weak",                 # 公司画像
    "llm_hypothesis": "weak",          # 模型推断（无来源）——永远不足以支撑已验证
}

VALID_RELATIONS = ("supports", "contradicts", "neutral")

STATUS_INSUFFICIENT = "证据不足"
STATUS_UNVERIFIED = "未验证"
STATUS_PARTIAL = "部分验证"
STATUS_VERIFIED = "已验证"


# ── 规则表：material 信号模式 → 候选成因族 ─────────────────────
# 条件写法（可检视、可测，不用裸 lambda）：
#   ("down", code)     该信号 material 且方向为 down
#   ("up", code)       该信号 material 且方向为 up
#   ("material", code) 该信号 material（不分方向）
#   ("not_down", code) 不满足"material 且 down"
#   ("lt", code, thr)  该信号有值且 < thr
#   ("gt", code, thr)  该信号有值且 > thr
# 只能引用 signals.py 真实计算出的 12 个信号——引用不存在的信号在结构上不允许。

FAMILY_RULES: list[dict] = [
    {
        "id": "demand_contraction", "label": "需求收缩",
        "when": [("down", "revenue_growth")],
        "observables": ["订单/需求相关新闻", "同业同期营收增速", "公司指引变化"],
        "why": "营收同比下滑是最直接的经营信号，但下滑的成因需外部证据区分。",
    },
    {
        "id": "demand_weakness", "label": "需求走弱",
        "when": [("down", "gross_margin_delta"), ("material", "inventory_growth_minus_revenue")],
        "observables": ["下游需求/去库存新闻", "存货周转相关公告", "同业存货与毛利变化"],
        "why": "毛利率下滑伴随存货增速超营收，是需求走弱导致跌价与周转变慢的典型组合。",
    },
    {
        "id": "price_competition", "label": "价格竞争",
        "when": [("down", "gross_margin_delta"), ("material", "revenue_growth")],
        "observables": ["降价/价格战新闻", "同业毛利率同期变化", "行业供需新闻"],
        "why": "收入仍有增长而毛利率下滑，可能是以价换量。",
    },
    {
        "id": "product_mix_shift", "label": "产品结构下移",
        "when": [("down", "gross_margin_delta"), ("material", "revenue_growth")],
        "observables": ["新品/业务结构相关公告", "分部收入变化", "行业结构新闻"],
        "why": "低毛利业务占比提升也会压低综合毛利率，与降价是不同的因。",
    },
    {
        "id": "upstream_cost_pass_through", "label": "上游成本传导",
        "when": [("down", "gross_margin_delta"), ("material", "cogs_growth_minus_revenue")],
        "observables": ["上游原材料价格新闻", "成本/涨价公告", "同业成本端变化"],
        "why": "成本增速显著快于营收，指向单位成本或上游投入品涨价。",
    },
    {
        "id": "expense_expansion", "label": "费用扩张",
        "when": [("down", "net_margin_delta"), ("not_down", "gross_margin_delta")],
        "observables": ["费用/人员相关公告", "销售与管理费用趋势", "扩张相关新闻"],
        "why": "毛利未恶化而净利下滑，指向费用端而非成本端。",
    },
    {
        "id": "rd_erosion", "label": "研发投入侵蚀",
        "when": [("up", "rd_to_revenue_delta"), ("down", "net_margin_delta")],
        "observables": ["研发投入/新品公告", "研发项目进展新闻", "同业研发强度"],
        "why": "研发占比上升同时净利率下滑，可能是主动投入侵蚀当期利润。",
    },
    {
        "id": "channel_stuffing", "label": "渠道压货",
        "when": [("material", "ar_growth_minus_revenue"),
                 ("material", "inventory_growth_minus_revenue")],
        "observables": ["经销商/应收账款相关公告", "渠道库存新闻", "回款政策变化"],
        "why": "应收与存货双双超营收增长，可能是向渠道压货或放宽信用。",
    },
    {
        "id": "earnings_quality", "label": "盈利质量下降",
        "when": [("lt", "cash_conversion", 0.7)],
        "observables": ["经营现金流相关科目", "回款/应收公告", "盈利质量相关新闻"],
        "why": "经营现金流长期低于净利润，利润的现金含量不足。",
    },
    {
        "id": "counter_cyclical_capex", "label": "逆周期扩产",
        "when": [("up", "capex_to_revenue_delta"), ("material", "revenue_growth")],
        "observables": ["产能/扩产公告", "资本开支指引", "行业产能新闻"],
        "why": "收入承压时资本开支占比上升，可能是逆周期扩产或被动投入。",
    },
    {
        "id": "leverage_rise", "label": "杠杆上升",
        "when": [("material", "interest_expense_growth"), ("up", "debt_to_equity_delta")],
        "observables": ["融资/发债公告", "有息负债变化", "利率环境新闻"],
        "why": "利息支出与负债权益比同时上升，财务费用将侵蚀后续利润。",
    },
]


# ── 条件求值 ──────────────────────────────────────────────────

def _smap(signals: list) -> dict:
    """信号列表 → {code: Signal}。接受 Signal 对象或已 to_dict 的字典。"""
    out: dict = {}
    for s in signals or []:
        code = getattr(s, "code", None) or (s.get("code") if isinstance(s, dict) else None)
        if code:
            out[code] = s
    return out


def _get(m, code: str, field: str):
    s = m.get(code)
    if s is None:
        return None
    return getattr(s, field, None) if not isinstance(s, dict) else s.get(field)


def _cond_ok(cond: tuple, m: dict) -> bool:
    kind = cond[0]
    code = cond[1]
    material = bool(_get(m, code, "material"))
    direction = _get(m, code, "direction")
    if kind == "material":
        return material
    if kind == "down":
        return material and direction == "down"
    if kind == "up":
        return material and direction == "up"
    if kind == "not_down":
        return not (material and direction == "down")
    if kind in ("lt", "gt"):
        v = _get(m, code, "value")
        if v is None:
            return False
        return v < cond[2] if kind == "lt" else v > cond[2]
    return False


def seed_families(signals: list) -> list[dict]:
    """规则表匹配：返回被真实 material 信号触发的成因族（带触发信号，可追溯）。

    零 material 信号 → 返回空列表 → 下游零假设（闸门 1 的落地）。
    """
    m = _smap(signals)
    out: list[dict] = []
    for rule in FAMILY_RULES:
        if all(_cond_ok(c, m) for c in rule["when"]):
            out.append({
                "family": rule["id"], "label": rule["label"],
                "seeded_by": [c[1] for c in rule["when"]],
                "observables": list(rule["observables"]),
                "why": rule["why"],
            })
    return out


# ── 闸门 2：LLM 只做措辞，越界族被丢弃 ────────────────────────

_FRAME_PROMPT = """你是投研假设生成器。下面是**已经由确定性财务信号匹配出来**的候选成因族，
以及触发它们的真实信号。你的唯一任务是：为每个族写出一句可验证的假设陈述。

下面是候选成因族（只能使用这些，不得新增）：
{families}

触发它们的真实财务信号（这些数值来自真实报表，不要改动、不要编造新数值）：
{signals}

{profile}

输出严格的 JSON（不要 markdown 包裹）：
{{"hypotheses": [
  {{"id": "H1", "family": "<必须来自上面候选族的 family 值>",
    "statement": "针对本公司的、可被外部证据验证的一句话假设",
    "predicted_observables": ["如果该假设成立，应能观察到什么"],
    "why_it_matters": "该成因对投资判断的意义",
    "prior_confidence": "high|medium|low"}}
]}}

规则：
1. family 必须严格等于候选族里给出的 family 值，不得自创
2. statement 要结合本公司情况，但不得引入上面未给出的任何数字
3. 每条假设必须能被"看新闻/看公告/看同业/看语料"证伪或证实
4. 不要给出买卖建议"""


def _fallback(families: list[dict], limit: int) -> list[dict]:
    """LLM 不可用时的确定性回退：族标签直接作为假设陈述（不编造、不解释）。"""
    out = []
    for i, f in enumerate(families[:limit], 1):
        out.append({
            "id": f"H{i}", "family": f["family"], "family_label": f["label"],
            "statement": f"{f['label']}（规则族：{'、'.join(f['seeded_by'])} 触发）",
            "predicted_observables": list(f["observables"]),
            "why_it_matters": f["why"],
            "prior_confidence": "low",
            "seeded_by": list(f["seeded_by"]),
        })
    return out


def frame_hypotheses(families: list[dict], signals: list, profile: dict | None = None,
                     *, client=None, max_hypotheses: int = 4
                     ) -> tuple[list[dict], str]:
    """把播种的成因族措辞成假设。

    Returns:
        (hypotheses, degradation_note)。note 非空表示发生了降级（如实标注）。
    """
    limit = max(0, int(max_hypotheses))
    if not families or limit == 0:
        return [], ""

    if client is None:
        return _fallback(families, limit), "未配置 LLM，假设陈述回退为规则族标签"

    allowed = {f["family"] for f in families}
    fam_txt = json.dumps([{"family": f["family"], "label": f["label"],
                           "why": f["why"], "observables": f["observables"]}
                          for f in families], ensure_ascii=False, indent=1)
    sig_txt = json.dumps([{
        "code": getattr(s, "code", None),
        "value": getattr(s, "value", None),
        "unit": getattr(s, "unit", None),
        "direction": getattr(s, "direction", None),
        "threshold": getattr(s, "threshold", None),
    } for s in signals if getattr(s, "material", False)], ensure_ascii=False, indent=1)
    prof_txt = ""
    if profile:
        prof_txt = ("公司背景（真实取数，仅供参考，不得据此编造数字）："
                    + json.dumps({k: profile.get(k) for k in
                                  ("name", "industry", "sector", "country")
                                  if profile.get(k)}, ensure_ascii=False))

    prompt = _FRAME_PROMPT.format(families=fam_txt, signals=sig_txt, profile=prof_txt)
    try:
        from app.llm.structured import parse_llm_json
        raw = client.chat(
            messages=[{"role": "user", "content": prompt}],
            task="investigation", max_tokens=2048,
        )
        parsed = parse_llm_json(raw, model=_hypothesis_set_model(), default=None,
                                context="调查假设生成")
    except Exception as e:                       # noqa: BLE001
        logger.warning("调查假设生成失败: %s", e)
        return _fallback(families, limit), f"假设生成调用失败（{type(e).__name__}），已回退规则族"

    if not parsed or not isinstance(parsed, dict):
        return _fallback(families, limit), "假设生成未按契约返回，已回退规则族"

    kept: list[dict] = []
    dropped: list[str] = []
    by_family = {f["family"]: f for f in families}
    for item in parsed.get("hypotheses") or []:
        if not isinstance(item, dict):
            continue
        fam = str(item.get("family") or "").strip()
        if fam not in allowed:                   # 闸门 2：越界族直接丢
            dropped.append(fam or "<空>")
            continue
        f = by_family[fam]
        kept.append({
            "id": str(item.get("id") or f"H{len(kept) + 1}"),
            "family": fam, "family_label": f["label"],
            "statement": str(item.get("statement") or f["label"]),
            "predicted_observables": [str(x) for x in
                                      (item.get("predicted_observables") or [])] or
                                     list(f["observables"]),
            "why_it_matters": str(item.get("why_it_matters") or f["why"]),
            "prior_confidence": str(item.get("prior_confidence") or "low"),
            "seeded_by": list(f["seeded_by"]),
        })

    note = ""
    if dropped:
        note = f"模型返回了 {len(dropped)} 个不在播种集合内的成因族，已丢弃：{dropped}"
        logger.warning("调查：丢弃越界假设族 %s", dropped)
    if not kept:
        return _fallback(families, limit), (note or "模型未返回可用假设") + "，已回退规则族"

    # 按先验置信度排序后截断（截断规则是代码的，不是模型的）
    rank = {"high": 0, "medium": 1, "low": 2}
    kept.sort(key=lambda h: rank.get(h.get("prior_confidence", "low"), 2))
    return kept[:limit], note


def _hypothesis_set_model():
    from app.llm.schemas import HypothesisSet
    return HypothesisSet


# ── 闸门 3：纯代码状态判定 ───────────────────────────────────

def normalize_relation(value) -> str:
    """把模型的自由文本关系归一到三值。不认识 → neutral（宁可不支持）。"""
    s = str(value or "").strip().lower()
    if s in ("supports", "support", "支持", "印证", "证实"):
        return "supports"
    if s in ("contradicts", "contradict", "反驳", "矛盾", "证伪"):
        return "contradicts"
    return "neutral"


def strength_for(kind: str) -> str:
    return SOURCE_KIND_STRENGTH.get(str(kind or ""), "weak")


def evaluate(evidences: list[dict]) -> dict:
    """由证据集合判定假设状态。**纯代码，无 LLM。**

    Args:
        evidences: 该假设下的证据项，每项至少含
            attribution_valid / ok / relation / source_kind / strength

    判定（顺序即优先级）：
        证据不足 —— 无有效证据，或全部 ok=False / 强度不可用
        未验证   —— 有有效证据但无一支持
        已验证   —— ≥2 条支持 且 来源类型 ≥2 种 且 无强反驳
        部分验证 —— 其余（含"只有 1 条支持""支持全来自同一来源类型"）
    """
    valid = [e for e in (evidences or []) if e.get("attribution_valid")]
    # 强度未知（键缺失/拼错）一律视为不可用 —— 与"强度低于 weak"同义，
    # 避免一个写错的 strength 字符串被当成合格证据静默采信。
    usable = [e for e in valid
              if e.get("ok") is not False
              and str(e.get("strength") or "") in STRENGTH_RANK]
    support = [e for e in usable if e.get("relation") == "supports"]
    contra = [e for e in usable if e.get("relation") == "contradicts"]
    kinds = sorted({str(e.get("source_kind") or "") for e in support})
    strong_contra = [e for e in contra
                     if STRENGTH_RANK.get(e.get("strength"), 0)
                     >= STRENGTH_RANK["strong"]]
    # 全是"模型推断"（无来源）的支持永远不足以判已验证
    all_llm = bool(support) and all(
        str(e.get("source_kind")) == "llm_hypothesis" for e in support)

    if not usable:
        status = STATUS_INSUFFICIENT
    elif not support:
        status = STATUS_UNVERIFIED
    elif len(support) >= 2 and len(kinds) >= 2 and not strong_contra and not all_llm:
        status = STATUS_VERIFIED
    else:
        status = STATUS_PARTIAL

    reason = {
        STATUS_INSUFFICIENT: "无可采信证据（未采信项计零）",
        STATUS_UNVERIFIED: "有可采信证据但无一支持该假设",
        STATUS_PARTIAL: ("支持证据不足 2 条，或全部来自同一来源类型"
                         + ("，或存在强反驳" if strong_contra else "")),
        STATUS_VERIFIED: f"≥2 条支持且来自 {len(kinds)} 种来源类型：{'、'.join(kinds)}",
    }[status]

    return {
        "status": status, "reason": reason,
        "support_count": len(support), "contradict_count": len(contra),
        "valid_count": len(valid), "rejected_count": len(evidences or []) - len(valid),
        "support_kinds": kinds,
    }
