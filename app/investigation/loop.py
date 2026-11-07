"""带硬预算的假设驱动取证循环（约束式 ReAct）。

每轮：预算检查 → 让模型从**封闭工具集**里挑动作 → 执行（带去重与逐项预算）→
把本轮的证据指认解析成真实证据 → 重算假设状态 → 无进展/振荡检测。

三条硬上限（轮次 / 工具调用 / LLM 调用）与墙钟都在**消耗之前**检查，
所以预算触顶时会在动作执行前停下，而不是执行完才发现超了。

两个刻意的工程选择：
  * 不建在 `chat_with_tools` 上——它会无条件执行整轮全部工具、没有执行前否决点，
    与"必须有 barrier"直接冲突。改用 `chat_once_with_tools`（单次 POST、不执行），
    执行/计数/去重/刹车全部由本模块掌握。
  * 依赖（llm / clock / toolbox）全部注入，因此可以完全离线验证。

循环里的 LLM 轮数哨兵：`chat_once_with_tools` 本身不产生哨兵，但若有人把它换成
`chat_with_tools`，耗尽轮次时会返回一句硬编码中文串且 `raw` 为空——两者都会被
`_is_exhausted` 识别并立即停止，**绝不让那句话流进报告**。
"""
from __future__ import annotations

import json

from app.audit.log import content_hash
from app.investigation.budget import (
    Budget, Depth, Spend, StopReason, check_stop,
)
from app.investigation.hypotheses import (
    evaluate, frame_hypotheses, normalize_relation, seed_families, strength_for,
)
from app.investigation.signals import extract_signals, material_signals
from app.investigation.tools import (
    TOOL_META, INVESTIGATION_TOOLS, RECORD_EVIDENCE_TOOL, Toolbox,
)
from app.utils.logger import logger

# 引擎轮数耗尽时返回的硬编码串（app/llm/client.py）。见模块 docstring。
EXHAUSTED_SENTINEL = "抱歉，工具调用轮数超限，请简化问题重试。"

# 工具 → 证据来源类型（决定强度，进而决定能否升到"已验证"）
TOOL_SOURCE_KIND: dict[str, str] = {
    "get_statement_summary": "financial_statement",
    "get_statement_line": "financial_statement",
    "get_quarterly_income": "financial_statement",
    "get_news": "news",
    "get_announcements": "announcement",
    "search_knowledge": "rag",
    "compare_peers": "peer",
    "get_company_profile": "profile",
    "get_price_history": "market",
}

# 参数规范化：去重哈希前先把等价写法归一，否则 limit=20 与 limit=25 能绕过去重。
# 形态 (键, 默认值, 下界)：**上界取默认值**——任何 ≥ 默认的写法都折叠成默认，
# 模型无法靠把 limit 从 20 改成 25 来伪装成一次新查询。小于默认的请求仍保留差异
# （要 3 条与要 20 条确实是不同的取数）。
_ARG_CAPS: dict[str, tuple[str, int, int]] = {
    "get_news": ("limit", 20, 1),
    "get_announcements": ("limit", 10, 1),
    "search_knowledge": ("top_k", 5, 1),
    "get_statement_line": ("n_periods", 6, 2),
}
_ARG_UPPER = {"ticker", "line_code"}
_ARG_LOWER = {"period"}

_SYSTEM = """你是投研调查员，任务是为一组**已由真实财务信号匹配出来的**成因假设寻找证据。

铁律：
1. 只能通过我提供的工具获取证据。禁止凭记忆断言任何数字、日期、事件或关系。
2. 不得编造 URL、公告标题、科目名或数值。工具没给的信息一律视为不可得。
3. 找不到证据时，如实说找不到；不要用泛泛的行业常识替代证据。
4. 上游/下游（供应商、客户）关系没有任何真实数据源，只能从工具返回的**真实文本**
   （新闻、公告、语料、A股概念板块）里推断，且必须给出处；推断一律标注为低置信。
5. 每轮最多提出 3 个取数调用，选最有区分度的方向优先。

**两条硬性流程**（不照做的话你的工作不会被计为证据）：
  A. 先取数，再登记。每轮我给出的「可引用的工具结果」清单里会列出你已经取到的
     每一份结果及其编号。读完之后，用 `record_evidence` 工具把它登记到对应假设上
     （hypothesis_id + relation + ref_index）。**只在文本里描述证据是无效的**——
     必须实际调用 `record_evidence` 才会被计入。
  B. 把假设判成已验证需要**至少 2 条支持证据，且来自 2 种不同来源类型**。单条新闻
     永远不够。所以刻意去凑不同来源（报表科目 / 同业 / 公告 / 新闻 / 语料）。

`record_evidence` 的 ref_index 指上面那份清单的编号；source_ref 可选，一旦填写必须
逐字出现在该工具结果里，否则不会被采信。每轮也可以用一句文本说明你的下一步意图。
若证据已足够、或已无法再推进，不要调用任何工具，直接给一句话总结。
"""


def _is_exhausted(result) -> bool:
    """轮数哨兵检测：两个信号任一命中即认定耗尽（raw 为空 / 文本等于哨兵串）。"""
    if result is None:
        return True
    raw = getattr(result, "raw", None)
    text = (getattr(result, "text", "") or "").strip()
    return (not raw) or (text == EXHAUSTED_SENTINEL)


def canonical_args(name: str, args: dict, toolbox: Toolbox) -> dict:
    """把参数归一化成去重用的规范形式（大小写、截断、默认值）。"""
    out: dict = {}
    for k, v in (args or {}).items():
        if isinstance(v, str):
            v = v.strip()
            if k in _ARG_UPPER:
                v = v.upper()
            elif k in _ARG_LOWER:
                v = v.lower()
        out[k] = v
    if name in _ARG_CAPS:
        key, default, lo = _ARG_CAPS[name]
        try:
            v = int(float(out.get(key, default)))
        except (TypeError, ValueError):
            v = default
        out[key] = max(lo, min(default, v))
    if name == "compare_peers":
        try:
            out["max_peers"] = max(1, min(toolbox.max_peers,
                                          int(float(out.get("max_peers", toolbox.max_peers)))))
        except (TypeError, ValueError):
            out["max_peers"] = toolbox.max_peers
    return out


def _dedup_key(name: str, args: dict) -> str:
    return content_hash(json.dumps({"tool": name, "args": args},
                                   sort_keys=True, ensure_ascii=False))


def _canonical_ref(result: dict) -> str:
    """从工具结果里取一个规范出处字符串（用于展示与校验）。"""
    try:
        if result.get("articles"):
            a = result["articles"][0]
            return str(a.get("url") or a.get("title") or "")
        if result.get("briefings"):
            b = result["briefings"][0]
            return str(b.get("url") or b.get("title") or "")
        if result.get("docs"):
            d = result["docs"][0]
            return str(d.get("source") or "")
        if result.get("line_code"):
            return str(result["line_code"])
        if result.get("peers"):
            return "peer-table"
        if result.get("name"):
            return str(result["name"])
        if result.get("end"):
            return f"{result.get('start')}~{result.get('end')}"
    except Exception:                     # noqa: BLE001
        return ""
    return ""


def _snippet(result: dict, limit: int = 160) -> str:
    for key, field in (("articles", "title"), ("briefings", "title"),
                       ("docs", "content")):
        items = result.get(key) or []
        if items and isinstance(items[0], dict) and items[0].get(field):
            s = str(items[0][field])
            return s[:limit]
    for key in ("reason", "note"):
        if result.get(key):
            return str(result[key])[:limit]
    return ""


class InvestigationLoop:
    """一次调查运行。依赖全部注入，可离线测试。"""

    def __init__(self, *, budget: Budget, toolbox: Toolbox, llm=None,
                 clock=None, progress=None):
        import time
        self.budget = budget
        self.toolbox = toolbox
        self.llm = llm
        self._clock = clock or time.monotonic
        self.progress = progress or (lambda ev: None)

    # ── 进度 ──
    def _emit(self, phase: str, label: str, message: str, spend: Spend,
              *, tool=None, hypotheses=None):
        pct = 0 if not self.budget.max_rounds else int(
            min(95, round(spend.rounds / self.budget.max_rounds * 100)))
        if phase == "conclude":
            pct = 100
        try:
            self.progress({
                "phase": phase, "round": spend.rounds,
                "max_rounds": self.budget.max_rounds,
                "label": label, "message": message, "pct": pct,
                "tool": tool,
                "spent": spend.as_dict(self.budget, self._clock()),
                "hypotheses": [{"id": h["id"], "status": h["status"]}
                               for h in (hypotheses or [])],
            })
        except Exception as e:            # noqa: BLE001
            logger.warning("调查进度回调失败: %s", e)

    # ── 主流程 ──
    def run(self, ticker: str, *, period: str = "annual") -> dict:
        t0 = self._clock()
        spend = Spend(started_at=t0)
        ticker = str(ticker or "").strip().upper()

        # ── 阶段 1：真实信号 → 成因族（闸门 1）──
        self._emit("signals", "读取财报", f"正在取 {ticker} 的三表并计算信号…", spend)
        frames = self.toolbox.frames(ticker, period)
        signals = extract_signals(frames, period=period)
        material = material_signals(signals)
        families = seed_families(signals)

        no_data = not material
        data_note = ""
        if no_data:
            from app.investigation.signals import has_any_statement_data
            data_note = ("未取得可计算的三表数据，因此不生成任何假设（不编造）"
                         if not has_any_statement_data(frames)
                         else "财报未出现达到阈值的异常信号，因此不生成假设")

        # ── 阶段 2：措辞成假设（闸门 2）──
        profile = self.toolbox.profile(ticker) if not no_data else {}
        hypotheses, degrade_note = frame_hypotheses(
            families, signals, profile, client=self.llm,
            max_hypotheses=self.budget.max_hypotheses)
        for h in hypotheses:
            h.setdefault("evidence", [])
            h["status"] = "证据不足"
            h["status_reason"] = "尚未取证"

        self._emit("hypotheses", "形成假设",
                   f"播种 {len(families)} 个成因族，采纳 {len(hypotheses)} 条假设",
                   spend, hypotheses=hypotheses)

        if not hypotheses:
            return self._finalize(
                ticker, spend, StopReason.ALL_SETTLED, signals, material, families,
                hypotheses, [], narrative="", degrade_note=degrade_note,
                data_note=data_note, period=period)

        # ── 阶段 3：多轮取证 ──
        msgs: list[dict] = [{"role": "user", "content": self._brief(
            ticker, profile, material, hypotheses)}]
        anthropic_tools = _anthropic_tools()
        seen: dict[str, dict] = {}
        refs: list[dict] = []          # 全局可引用工具结果（0 起编号），跨轮次累积
        evidence_log: list[dict] = []
        round_action_keys: list[frozenset] = []
        narrative = ""
        stop_reason: StopReason | None = None

        while True:
            now = self._clock()
            reason = check_stop(self.budget, spend, now, llm_cost=1)
            if reason:
                stop_reason = reason
                break

            spend.rounds += 1
            self._emit("round", f"第 {spend.rounds}/{self.budget.max_rounds} 轮",
                       "正在决定下一步调查方向…", spend, hypotheses=hypotheses)

            try:
                res = self.llm.chat_once_with_tools(
                    msgs, system=_SYSTEM, tools=anthropic_tools,
                    task="investigation", max_tokens=2048)
            except Exception as e:                     # noqa: BLE001
                logger.warning("调查第 %d 轮 LLM 调用失败: %s", spend.rounds, e)
                stop_reason = StopReason.LLM_ERROR
                break
            spend.llm_calls += 1

            if _is_exhausted(res):
                # 哨兵只可能来自引擎轮数耗尽；其 text 不是结论，绝不采用
                stop_reason = StopReason.LLM_EXHAUSTED
                narrative = ""
                break

            text_props, note = _parse_attributions(getattr(res, "text", "") or "")
            actions = list(getattr(res, "tool_calls", []) or [])
            if note:
                narrative = note

            # 执行动作（逐项预算 + 去重），并为每个 tool_use 都产出 tool_result
            results, inline_props = self._execute_actions(actions, spend, seen,
                                                          hypotheses, refs)

            # 证据指认解析（闸门 3）：record_evidence 与文本 JSON 两条路都过同一套校验
            new_ev = self._attach(inline_props + text_props, refs, hypotheses,
                                  spend, evidence_log)
            self._append_tool_turn(msgs, res, results, spend, hypotheses, refs, new_ev)

            # 无进展 / 振荡
            status_changes = self._recompute(hypotheses)
            if not new_ev and status_changes == 0:
                spend.consecutive_no_progress += 1
            else:
                spend.consecutive_no_progress = 0
            action_keys = frozenset(
                _dedup_key(str((a or {}).get("name") or ""),
                           canonical_args(str((a or {}).get("name") or ""),
                                          (a or {}).get("input") or {}, self.toolbox))
                for a in actions) if actions else frozenset()
            round_action_keys.append(action_keys)
            # 振荡 = 本轮动作集合与两轮前完全相同（A,B,A）——模型在原地打转
            if (len(round_action_keys) >= 3 and action_keys
                    and action_keys == round_action_keys[-3]):
                spend.oscillation = True
                logger.info("调查检测到振荡（动作集合 A,B,A 重复），将停止")

            self._emit("evidence", f"第 {spend.rounds} 轮取证",
                       f"新增证据 {len(new_ev)} 条，状态变化 {status_changes} 处",
                       spend, hypotheses=hypotheses)

            if not actions:
                stop_reason = StopReason.ALL_SETTLED
                break

        self._recompute(hypotheses)
        return self._finalize(
            ticker, spend, stop_reason or StopReason.ALL_SETTLED, signals, material,
            families, hypotheses, evidence_log,
            narrative=narrative, degrade_note=degrade_note, data_note=data_note,
            period=period)

    # ── 单轮内的事 ──

    def _execute_actions(self, actions: list[dict], spend: Spend,
                         seen: dict[str, dict], hypotheses: list,
                         refs: list[dict]) -> tuple[list[dict], list[dict]]:
        """执行本轮动作。

        返回 (results, inline_proposals)：每个 tool_use 都会产出一条结果；
        `record_evidence` 不取数、不走 Toolbox、不占 tool_calls 预算，而是被转成
        证据指认（与文本 JSON 走同一套闸门 3 校验），其 tool_result 回报登记回执。
        """
        out: list[dict] = []
        props: list[dict] = []
        for act in actions:
            name = str((act or {}).get("name") or "")
            raw_args = (act or {}).get("input") or {}
            tool_id = str((act or {}).get("id") or "")

            if name == "record_evidence":                # 证据登记：由循环处理
                props.append({
                    "hypothesis_id": str(raw_args.get("hypothesis_id") or ""),
                    "relation": raw_args.get("relation"),
                    "source_ref_index": raw_args.get("ref_index"),
                    "source_ref": raw_args.get("source_ref") or "",
                    "rationale": raw_args.get("rationale") or "",
                })
                payload = {"ok": True, "registered": True,
                           "hypothesis_id": raw_args.get("hypothesis_id"),
                           "ref_index": raw_args.get("ref_index"),
                           "note": "已登记，本轮末尾统一校验；结果见下一轮的假设台账。"}
                out.append(_pack(tool_id, name, raw_args, payload, "llm_hypothesis"))
                continue

            meta = TOOL_META.get(name)
            if meta is None:
                payload = {"ok": False, "error": f"未知工具 {name}"}
                out.append(_pack(tool_id, name, raw_args, payload, None))
                continue
            args = canonical_args(name, raw_args, self.toolbox)
            key = _dedup_key(name, args)
            if key in seen:                              # 去重命中：复用，不计调用数
                spend.duplicates += 1
                prior = dict(seen[key].get("payload") or {})
                prior["duplicate"] = True
                prior["note_dup"] = "本轮查询与之前相同，已复用其结果（不计入预算）"
                packed = _pack(tool_id, name, args, prior, seen[key].get("source_kind"))
                packed["ref_index"] = seen[key].get("ref_index")   # 复用同一引用编号
                out.append(packed)
                continue

            reason = check_stop(self.budget, spend, self._clock(),
                                llm_cost=int(meta.get("llm_cost") or 0),
                                check_rounds=False)   # 轮内不再按轮次判定（见 budget 注释）
            if reason:
                payload = {"ok": False, "reason": f"预算已耗尽（{reason.value}），本轮未执行"}
                out.append(_pack(tool_id, name, args, payload, TOOL_SOURCE_KIND.get(name)))
                continue

            self._emit("tool", f"调用 {name}", f"正在执行 {name}…", spend, tool=name,
                       hypotheses=hypotheses)
            payload = self.toolbox.execute(name, args)
            spend.tool_calls += 1
            spend.llm_calls += int(meta.get("llm_cost") or 0)
            packed = _pack(tool_id, name, args, payload, TOOL_SOURCE_KIND.get(name))
            packed["ref_index"] = len(refs)              # 全局引用编号（0 起）
            refs.append(packed)                          # 供后续轮次登记证据
            seen[key] = {"payload": payload, "source_kind": TOOL_SOURCE_KIND.get(name),
                         "ref_index": packed["ref_index"]}
            out.append(packed)
        return out, props

    def _refs_ledger(self, refs: list[dict], limit: int = 12) -> list[dict]:
        """给模型的「可引用的工具结果」清单：编号 + 工具 + 摘要 + 候选出处。"""
        items = []
        for r in refs[-limit:]:
            payload = r.get("payload") or {}
            urls: list[str] = []
            for key, field in (("articles", "url"), ("briefings", "url"),
                               ("docs", "source")):
                for it in (payload.get(key) or [])[:3]:
                    if isinstance(it, dict) and it.get(field):
                        urls.append(str(it[field]))
            canon = _canonical_ref(payload)
            items.append({
                "index": r.get("ref_index"), "tool": r.get("tool"),
                "ok": r.get("ok"),
                "summary": _snippet(payload, 100) or None,
                "candidates": urls or ([canon] if canon else []),
                "缺失说明": (payload.get("reason")
                             if payload.get("ok") is False else None),
            })
        return items

    def _append_tool_turn(self, msgs: list[dict], res, results: list[dict],
                         spend: Spend, hypotheses: list, refs: list[dict],
                         new_ev: list[dict]) -> None:
        """追加 assistant(tool_use) + user(tool_result + 可引用清单 + 假设台账)。"""
        content = (getattr(res, "raw", None) or {}).get("content") or []
        if content:
            msgs.append({"role": "assistant", "content": content})
        blocks = []
        for r in results:
            if not r.get("tool_id"):
                continue
            blocks.append({"type": "tool_result", "tool_use_id": r["tool_id"],
                           "content": r["result_str"]})
        ledger = self._ledger(hypotheses, spend, refs, new_ev)
        if blocks or ledger:
            user_content: list = list(blocks)
            if ledger:
                user_content.append({"type": "text", "text": ledger})
            msgs.append({"role": "user", "content": user_content})

    def _attach(self, proposals: list[dict], refs: list[dict],
                hypotheses: list, spend: Spend, evidence_log: list) -> list[dict]:
        """把模型的指认解析成真实证据。**闸门 3**：校验不通过即计零。

        `refs` 是**全局**可引用工具结果（跨轮次累积），所以模型可以在后续轮次里
        回头登记前面读到的结果——这比要求它在同一次调用里既取数又引用更可靠。
        """
        by_id = {h["id"]: h for h in hypotheses}
        new_ev: list[dict] = []
        for p in proposals or []:
            hid = str(p.get("hypothesis_id") or "").strip()
            relation = normalize_relation(p.get("relation"))
            idx = p.get("source_ref_index")
            valid, why = True, []
            kind, ok, src, snip = "llm_hypothesis", True, "", ""
            tool_name, tool_args = "(模型推断)", {}

            if hid not in by_id:
                valid = False
                why.append(f"假设 id 不存在：{hid or '<空>'}")
            if idx is None:
                kind = "llm_hypothesis"           # 无索引 = 模型推断，弱证据
                snip = str(p.get("rationale") or "")[:160]
                src = str(p.get("source_ref") or "")
                if src:
                    # 无索引却自述出处 → 无法核验，视为未采信
                    valid = False
                    why.append("只给了出处没给索引，无法核验")
            else:
                idx_int = idx if isinstance(idx, int) and not isinstance(idx, bool) else None
                if idx_int is None:
                    valid = False
                    why.append(f"证据索引不是整数：{idx!r}")
                elif idx_int < 0 or idx_int >= len(refs):
                    valid = False
                    why.append(f"证据索引越界：{idx_int}（当前可引用 0~{len(refs) - 1}）")
                else:
                    r = refs[idx_int]
                    kind = r.get("source_kind") or "llm_hypothesis"
                    ok = r.get("ok") is not False
                    tool_name, tool_args = r.get("tool"), r.get("args") or {}
                    payload = r.get("payload") or {}
                    snip = _snippet(payload)
                    claimed = str(p.get("source_ref") or "").strip()
                    if claimed:
                        if claimed not in r.get("result_str", ""):
                            valid = False
                            why.append("自述出处未出现在该工具结果中（疑似编造）")
                        src = claimed
                    else:
                        src = _canonical_ref(payload)

            ev = {
                "id": f"E{len(evidence_log) + 1}",
                "hypothesis_id": hid, "relation": relation,
                "tool": tool_name, "args": tool_args,
                "source_kind": kind, "strength": strength_for(kind),
                "source_ref": src, "snippet": snip, "ok": ok,
                "attribution_valid": valid,
                "reject_reason": "；".join(why) if not valid else "",
                "rationale": str(p.get("rationale") or "")[:200],
                "round": spend.rounds,
            }
            evidence_log.append(ev)
            new_ev.append(ev)
            h = by_id.get(hid)
            if h is not None:
                # 未采信的项也留档（展示在"未采信"里），但 evaluate 计零
                h.setdefault("evidence", []).append(ev)
                cap = self.budget.max_evidence_per_hypothesis
                if len(h["evidence"]) > cap:            # 只保留最强的 cap 条
                    h["evidence"].sort(
                        key=lambda e: (e["attribution_valid"], e["ok"],
                                       {"strong": 2, "medium": 1, "weak": 0}
                                       .get(e["strength"], 0)),
                        reverse=True)
                    del h["evidence"][cap:]
        return new_ev

    def _recompute(self, hypotheses: list) -> int:
        """重算每条假设的状态，返回状态变化的条数。"""
        changed = 0
        for h in hypotheses:
            verdict = evaluate(h.get("evidence") or [])
            if verdict["status"] != h.get("status"):
                changed += 1
            h["status"] = verdict["status"]
            h["status_reason"] = verdict["reason"]
            h.update({
                "support_count": verdict["support_count"],
                "contradict_count": verdict["contradict_count"],
                "rejected_count": verdict["rejected_count"],
                "support_kinds": verdict["support_kinds"],
            })
        return changed

    # ── 提示语 ──

    def _brief(self, ticker: str, profile: dict, material: list,
               hypotheses: list) -> str:
        tools = [{"name": n, "llm_cost": m["llm_cost"],
                  "est_seconds": m["est_seconds"]} for n, m in TOOL_META.items()]
        return json.dumps({
            "任务": f"为 {ticker} 的以下成因假设寻找证据",
            "公司背景": {k: profile.get(k) for k in
                     ("name", "industry", "sector", "country", "concepts")
                     if profile.get(k)},
            "触发的真实财务信号": [{
                "code": s.code, "label": s.label, "value": s.value,
                "rel_value": s.rel_value, "unit": s.unit,
                "direction": s.direction, "threshold": s.threshold,
            } for s in material],
            "待验证假设": [{"id": h["id"], "family_label": h["family_label"],
                        "statement": h["statement"],
                        "predicted_observables": h["predicted_observables"],
                        "status": h["status"]} for h in hypotheses],
            "可用工具": tools,
            "说明": ("上游/下游关系没有真实数据源，只能从工具返回的真实文本里推断，"
                     "并给出处。找不到就如实说找不到。"),
        }, ensure_ascii=False, indent=1)

    def _ledger(self, hypotheses: list, spend: Spend, refs: list[dict],
                new_ev: list[dict]) -> str:
        remaining = self.budget.max_rounds - spend.rounds
        payload = {
            "可引用的工具结果": self._refs_ledger(refs),
            "下一个可用编号": len(refs),
            "登记提示": ("本轮若有新取数，其编号顺延为上面的「下一个可用编号」起；"
                         "建议下一轮再登记它们，那时清单里已能看到。"),
            "本轮证据登记结果": [{
                "假设": e.get("hypothesis_id"),
                "关系": e.get("relation"),
                "采信": bool(e.get("attribution_valid")),
                "未采信原因": e.get("reject_reason") or None,
                "出处": e.get("source_ref") or None,
            } for e in (new_ev or [])],
            "假设台账": [{
                "id": h["id"], "statement": h["statement"], "status": h["status"],
                "status_reason": h.get("status_reason"),
                "支持数": h.get("support_count", 0),
                "反驳数": h.get("contradict_count", 0),
                "未采信数": h.get("rejected_count", 0),
                "来源类型": h.get("support_kinds", []),
            } for h in hypotheses],
            "剩余轮次": remaining,
            "剩余工具调用": self.budget.max_tool_calls - spend.tool_calls,
            "请继续": ("用 record_evidence 把上面清单里已读到的结果登记为证据"
                       "（判已验证需要 ≥2 条支持且来自 ≥2 种来源类型）；"
                       "若证据已足够或无法再推进，不要调用工具，直接给一句话总结。"),
        }
        return json.dumps(payload, ensure_ascii=False, indent=1)

    # ── 收尾 ──

    def _finalize(self, ticker, spend, stop_reason, signals, material, families,
                  hypotheses, evidence_log, *, narrative, degrade_note, data_note,
                  period) -> dict:
        now = self._clock()
        overshoot = max(0.0, spend.elapsed(now) - self.budget.deadline_seconds)
        self._emit("conclude", "调查结束", f"停止原因：{stop_reason.value}", spend,
                   hypotheses=hypotheses)
        status_counts: dict[str, int] = {}
        for h in hypotheses:
            status_counts[h["status"]] = status_counts.get(h["status"], 0) + 1

        return {
            "ticker": ticker,
            "period": period,
            "depth": self.budget.depth.value,
            "stop_reason": stop_reason.value,
            "limits": self.budget.as_dict(),
            "spent": spend.as_dict(self.budget, now),
            "budget_overshoot_seconds": round(overshoot, 3),
            "signals": [s.to_dict() for s in signals],
            "material_signals": [s.to_dict() for s in material],
            "seeded_families": families,
            "hypotheses": hypotheses,
            "status_counts": status_counts,
            "evidence": evidence_log,
            "narrative": narrative,
            "degrade_note": degrade_note,
            "data_note": data_note,
            "no_data": bool(data_note),
            "disclaimer": ("因果结论均为**基于真实数据与真实来源的假设**，按证据强度分级；"
                           "标注为「模型推断」的条目无原始来源，仅为待验证线索；"
                           "未采信项已单独列出且不计入任何结论。"
                           "上游/下游关系无真实数据源，仅来自真实文本的推断。"),
        }


def _pack(tool_id: str, name: str, args: dict, payload: dict,
          source_kind: str | None) -> dict:
    return {
        "tool_id": tool_id, "tool": name, "args": args, "payload": payload,
        "result_str": json.dumps(payload, ensure_ascii=False, default=str),
        "ok": payload.get("ok") is not False,
        "source_kind": source_kind or TOOL_SOURCE_KIND.get(name, "llm_hypothesis"),
    }


def _parse_attributions(text: str) -> tuple[list[dict], str]:
    """从模型文本里取 {"attributions": [...], "note": ...}；取不到就空手继续。"""
    if not text or not text.strip():
        return [], ""
    try:
        from app.llm.structured import parse_llm_json
        parsed = parse_llm_json(text, model=_round_plan_model(), default=None,
                                context="调查轮计划")
    except Exception as e:                              # noqa: BLE001
        logger.warning("调查轮计划解析失败: %s", e)
        return [], ""
    if not isinstance(parsed, dict):
        return [], ""
    attributions = [p for p in (parsed.get("attributions") or []) if isinstance(p, dict)]
    return attributions, str(parsed.get("note") or "")


def _round_plan_model():
    from app.llm.schemas import RoundPlan
    return RoundPlan


def _anthropic_tools() -> list[dict]:
    try:
        from app.llm.client import to_anthropic_tool
        return [to_anthropic_tool(t)
                for t in (INVESTIGATION_TOOLS + [RECORD_EVIDENCE_TOOL])]
    except Exception:                                   # noqa: BLE001
        return []


def run_investigation(ticker: str, *, depth: Depth | str = Depth.STANDARD,
                      period: str = "annual", budget: Budget | None = None,
                      toolbox: Toolbox | None = None, llm=None, clock=None,
                      progress=None, settings=None) -> dict:
    """跑一次自主深度调查。依赖均可注入，便于离线验证。"""
    b = budget or Budget.from_settings(depth, settings=settings)
    tb = toolbox or Toolbox(max_peers=b.max_peers)
    if llm is None:
        try:
            from app.llm.client import get_llm_client
            llm = get_llm_client()
        except Exception as e:                          # noqa: BLE001
            logger.warning("调查无法获取 LLM 客户端: %s", e)
            llm = None
    loop = InvestigationLoop(budget=b, toolbox=tb, llm=llm, clock=clock,
                             progress=progress)
    return loop.run(ticker, period=period)
