"""对话 API —— /api/v1/chat/*（迁移自 Violetz main.py）

股票对话：非流式 + SSE 流式（含歧义澄清）；
基金对话：非流式 + SSE 流式。历史为内存存储。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Form
from fastapi.responses import StreamingResponse

from app.audit.log import audit, content_hash
from app.audit.middleware import get_request_id
from app.observability.metrics import metrics
from app.agent.intent import parse as parse_intent
from app.agent.planner import build_plan
from app.agent.runner import execute_plan, ACTION_MAP
from app.agent.expressor import (
    generate_report, stream_analysis_report, stream_compare_pros_cons,
    _format_price_report, _format_sentiment_report, _format_violetz_score,
    _find_result, _filter_results_by_ticker,
)
from app.agent.scorer import VioletzScorer
from app.agent.ambiguity import (
    check_ambiguity, check_ambiguity_from_input, build_question,
    set_pending, get_pending, clear_pending, resolve_ambiguity,
    PendingClarification,
)
from app.agent.fund.intent import parse as parse_fund_intent
from app.agent.fund.planner import build_fund_plan
from app.agent.fund.runner import execute_fund_plan
from app.agent.fund.expressor import generate_fund_report, stream_fund_report_tokens
from app.memory import db as memory_db
from app.memory.summary import maybe_roll_summary

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


class _PersistentHistory(list):
    """内存会话历史；append 时自动落库 memory（长时记忆，单用户）。

    对话主流程继续用内存列表做上下文（intent/expressor 接口不变），
    每条消息同时持久化到 data/memory.db，/history 改从库读取。
    """

    def append(self, item):
        super().append(item)
        try:
            memory_db.add_message(
                item.get("role", "user"),
                item.get("text", ""),
                intent=item.get("intent"),
                tickers=item.get("tickers"),
                # 反馈关联键：assistant 条目 id 形如 "ab12cd34-r"
                msg_id=item.get("id"),
            )
        except Exception:  # noqa: BLE001 记忆落库失败不影响对话主流程
            pass


# 简易对话历史（内存存储，append 自动落库）
chat_history = _PersistentHistory()


def sse_event(event: str, data: dict) -> str:
    """格式化 SSE 事件"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _assistant_ids(msg_id: str, rid: str) -> dict:
    """助手消息的关联键。前端没有它们就提交不了可归因的反馈。"""
    return {"msg_id": f"{msg_id}-r", "request_id": rid}


def _reply_payload(text: str, msg_id: str, rid: str) -> dict:
    return {"reply": text, **_assistant_ids(msg_id, rid)}


async def _run_sync(fn, *args, **kwargs):
    """在线程池中运行同步函数"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# ── 股票对话（非流式） ───────────────────────────────────────

@router.post("")
async def chat(user_input: str = Form(...)):
    """处理用户对话（返回完整 reply）"""
    rid = get_request_id()
    msg_id = str(uuid.uuid4())[:8]
    metrics.inc("chat_requests")
    chat_history.append({"role": "user", "text": user_input, "id": msg_id})
    audit("chat.request", {"input": user_input[:500], "msg_id": msg_id}, request_id=rid)

    # ===== 检查是否有待处理的歧义澄清 =====
    pending = get_pending()
    if pending:
        resolved = resolve_ambiguity(user_input, pending)
        if resolved:
            clear_pending()
            intent = dict(pending.original_intent)
            intent["tickers"] = [resolved["ticker"]]
            intent["market"] = resolved["market"]
        else:
            reply = "未能识别您的选择。"
            ambiguous = check_ambiguity([pending.company_name])
            if ambiguous:
                reply += "\n" + build_question(ambiguous)
            chat_history.append({"role": "assistant", "text": reply, "id": msg_id + "-r"})
            return _reply_payload(reply, msg_id, rid)
    else:
        # ===== 正常流程：意图识别 =====
        try:
            context_history = chat_history[:-1]
            intent = parse_intent(user_input, history=context_history)
        except Exception as e:
            reply = f"意图识别异常：{str(e)}"
            chat_history.append({"role": "assistant", "text": reply, "id": msg_id + "-r"})
            return _reply_payload(reply, msg_id, rid)

        # ===== 检查多市场歧义 =====
        if intent.get("tickers"):
            raw = intent.get("raw_input", user_input)
            ambiguous = check_ambiguity_from_input(raw)
            if ambiguous:
                set_pending(PendingClarification(
                    company_name=ambiguous[0]["name"],
                    options=ambiguous[0]["options"],
                    original_intent=intent,
                    user_input=user_input,
                ))
                reply = build_question(ambiguous)
                chat_history.append({"role": "assistant", "text": reply, "id": msg_id + "-r"})
                return _reply_payload(reply, msg_id, rid)

    try:
        memory_db.tag_last_user_message(intent.get("intent"), intent.get("tickers"))
    except Exception:  # noqa: BLE001
        pass

    try:
        context_history = chat_history[:-1]
        if not intent.get("tickers"):
            reply = ("未能识别出股票代码。请明确公司名称或股票代码，例如：\n"
                     "- 苹果公司股价\n- NVDA 最近走势\n- 分析腾讯控股")
            chat_history.append({"role": "assistant", "text": reply, "id": msg_id + "-r"})
            return _reply_payload(reply, msg_id, rid)

        t0 = time.perf_counter()
        plan = build_plan(intent)
        execution = execute_plan(plan)
        plan_secs = time.perf_counter() - t0
        metrics.record("chat_plan_seconds", plan_secs)
        report = generate_report(execution, intent, history=context_history)
        chat_history.append({"role": "assistant", "text": report, "id": msg_id + "-r"})
        audit("chat.reply", {"msg_id": msg_id, "content_hash": content_hash(report)},
              request_id=rid)
        try:
            maybe_roll_summary()
        except Exception:  # noqa: BLE001
            pass
        return _reply_payload(report, msg_id, rid)

    except Exception as e:
        reply = f"处理请求时出现异常：{str(e)}"
        chat_history.append({"role": "assistant", "text": reply, "id": msg_id + "-r"})
        audit("chat.error", {"msg_id": msg_id, "error": str(e)}, request_id=rid)
        return _reply_payload(reply, msg_id, rid)


@router.get("/history")
async def history():
    return {"history": memory_db.recent_messages(50)}


@router.post("/clear")
async def clear():
    chat_history.clear()
    memory_db.clear_messages()
    audit("chat.clear", {}, request_id=get_request_id())
    return {"ok": True}


# ── 基金对话（非流式） ───────────────────────────────────────

@router.post("/fund")
async def chat_fund(user_input: str = Form(...)):
    """处理基金相关对话（非流式）"""
    rid = get_request_id()
    msg_id = str(uuid.uuid4())[:8]
    metrics.inc("chat_fund_requests")
    chat_history.append({"role": "user", "text": user_input, "id": msg_id})
    audit("chat.fund_request", {"input": user_input[:500], "msg_id": msg_id}, request_id=rid)

    try:
        intent = parse_fund_intent(user_input)
        try:
            memory_db.tag_last_user_message(intent.get("intent"), intent.get("fund_codes"))
        except Exception:  # noqa: BLE001
            pass
        if not intent.get("fund_codes"):
            reply = ("未能识别出基金代码。请明确基金代码或名称，例如：\n"
                     "- 查询 005827 净值\n- 分析易方达蓝筹\n- 110011 和 005827 对比")
            chat_history.append({"role": "assistant", "text": reply, "id": msg_id + "-r"})
            return _reply_payload(reply, msg_id, rid)

        plan = build_fund_plan(intent)
        if not plan.get("steps"):
            reply = "未能为基金生成执行计划。"
            chat_history.append({"role": "assistant", "text": reply, "id": msg_id + "-r"})
            return _reply_payload(reply, msg_id, rid)

        execution = execute_fund_plan(plan)
        report = generate_fund_report(execution, intent)
        chat_history.append({"role": "assistant", "text": report, "id": msg_id + "-r"})
        audit("chat.fund_reply", {"msg_id": msg_id, "content_hash": content_hash(report)},
              request_id=rid)
        return _reply_payload(report, msg_id, rid)

    except Exception as e:
        reply = f"处理基金请求时出现异常：{str(e)}"
        chat_history.append({"role": "assistant", "text": reply, "id": msg_id + "-r"})
        audit("chat.fund_error", {"msg_id": msg_id, "error": str(e)}, request_id=rid)
        return _reply_payload(reply, msg_id, rid)


# ── SSE 流式：股票 ───────────────────────────────────────────

async def _stream_chat(user_input: str, history: list[dict] = None,
                       pre_resolved_intent: dict = None,
                       rid: str = "", msg_id: str = ""):
    """SSE 事件流生成器：逐步解析 → 获取数据 → 生成报告。

    done 事件带回 msg_id / request_id：流式文本本身没有可关联的 id，
    前端拿不到就无法提交可归因的反馈。
    """
    done = _assistant_ids(msg_id, rid)
    try:
        if pre_resolved_intent:
            intent = pre_resolved_intent
        else:
            yield sse_event("status", {"message": "正在理解您的投资意图..."})
            intent = await _run_sync(parse_intent, user_input, history=history)
            yield sse_event("intent", {
                "intent": intent.get("intent"),
                "tickers": intent.get("tickers"),
                "timeframe": intent.get("timeframe"),
            })

            # ── 1b. 检查多市场歧义 ──
            if intent.get("tickers"):
                raw = intent.get("raw_input", user_input)
                ambiguous = check_ambiguity_from_input(raw)
                if ambiguous:
                    set_pending(PendingClarification(
                        company_name=ambiguous[0]["name"],
                        options=ambiguous[0]["options"],
                        original_intent=intent,
                        user_input=user_input,
                    ))
                    yield sse_event("token", {"text": build_question(ambiguous)})
                    yield sse_event("done", done)
                    return

        try:
            memory_db.tag_last_user_message(intent.get("intent"), intent.get("tickers"))
        except Exception:  # noqa: BLE001
            pass

        if not intent.get("tickers"):
            yield sse_event("token", {"text": (
                "未能识别出股票代码。请明确公司名称或股票代码，例如：\n"
                "- 苹果公司股价\n- NVDA 最近走势\n- 分析腾讯控股")})
            yield sse_event("done", done)
            return

        # ── 2. 任务规划 ──
        yield sse_event("status", {"message": "正在制定数据获取计划..."})
        plan = await _run_sync(build_plan, intent)
        steps = plan.get("steps", [])

        # ── 3. 逐步执行 ──
        results = []
        total = len(steps)
        for i, step in enumerate(steps):
            yield sse_event("status", {
                "message": f"正在获取市场数据 ({i + 1}/{total})",
                "detail": step.get("description", ""),
            })
            action = step["action"]
            params = step["params"]
            fn = ACTION_MAP.get(action)
            if not fn:
                results.append({"step": step, "data": None, "error": f"未知操作: {action}"})
                continue
            try:
                data = await _run_sync(fn, **params)
                results.append({"step": step, "data": data, "error": None})
            except Exception as e:
                results.append({"step": step, "data": None, "error": str(e)})

        # ── 4. 生成报告 ──
        yield sse_event("status", {"message": "正在生成分析报告..."})
        intent_type = intent.get("intent", "query_price")
        tickers = intent.get("tickers", [])
        success_count = len([r for r in results if not r["error"]])
        execution_summary = {
            "results": results,
            "summary": {"total": total, "success": success_count,
                        "failed": total - success_count},
        }

        if intent_type == "compare":
            for i, t in enumerate(tickers):
                if i > 0:
                    yield sse_event("token", {"text": "\n\n---\n\n"})
                ticker_results = _filter_results_by_ticker(results, t)
                quote = _find_result(ticker_results, "get_quote")
                if quote:
                    yield sse_event("token", {"text": _format_price_report(quote) + "\n\n"})
                for token in stream_analysis_report(ticker_results, "analysis", history=history):
                    yield sse_event("token", {"text": token})
                vz_scorer = VioletzScorer()
                fin = _find_result(ticker_results, "get_financials")
                q = _find_result(ticker_results, "get_quote")
                s = _find_result(ticker_results, "get_news_sentiment")
                if fin or q or s:
                    vz = vz_scorer.compute(financials=fin, quote=q, sentiment=s)
                    yield sse_event("token", {"text": "\n\n" + _format_violetz_score(vz) + "\n\n"})
                sentiment = _find_result(ticker_results, "get_news_sentiment")
                if sentiment:
                    sentiment_text = _format_sentiment_report(sentiment)
                    if sentiment_text:
                        yield sse_event("token", {"text": "\n\n" + sentiment_text})
            yield sse_event("token", {"text": "\n\n---\n\n## 🆚 综合对比\n\n"})
            yield sse_event("token", {"text": "[__CMP_START__]"})
            for token in stream_compare_pros_cons(results, tickers, history=history):
                yield sse_event("token", {"text": token})
            yield sse_event("token", {"text": "[__CMP_END__]"})

        elif intent_type == "analysis":
            for t in tickers:
                quote = _find_result(results, "get_quote")
                if quote and quote.get("ticker") == t.upper():
                    yield sse_event("token", {"text": _format_price_report(quote) + "\n\n"})
            for token in stream_analysis_report(results, intent_type, history=history):
                yield sse_event("token", {"text": token})
            vz_scorer = VioletzScorer()
            for t in tickers:
                tr = _filter_results_by_ticker(results, t)
                fin = _find_result(tr, "get_financials")
                q = _find_result(tr, "get_quote")
                s = _find_result(tr, "get_news_sentiment")
                if fin or q or s:
                    vz = vz_scorer.compute(financials=fin, quote=q, sentiment=s)
                    yield sse_event("token", {"text": "\n\n" + _format_violetz_score(vz) + "\n\n"})
            for t in tickers:
                sentiment = _find_result(results, "get_news_sentiment")
                if sentiment and sentiment.get("ticker") == t.upper():
                    sentiment_text = _format_sentiment_report(sentiment)
                    if sentiment_text:
                        yield sse_event("token", {"text": "\n\n" + sentiment_text})
        else:
            report = await _run_sync(generate_report, execution_summary, intent, history=history)
            yield sse_event("token", {"text": report})

        yield sse_event("done", done)

    except Exception as e:
        yield sse_event("error", {"message": str(e)})
        yield sse_event("done", done)


@router.post("/stream")
async def chat_stream(user_input: str = Form(...)):
    """SSE 流式对话端点"""
    rid = get_request_id()
    msg_id = str(uuid.uuid4())[:8]
    metrics.inc("chat_stream_requests")
    chat_history.append({"role": "user", "text": user_input, "id": msg_id})
    audit("chat.stream", {"input": user_input[:500], "msg_id": msg_id}, request_id=rid)
    context_history = [h for h in chat_history[:-1]]

    pre_resolved = None
    pending = get_pending()
    if pending:
        resolved = resolve_ambiguity(user_input, pending)
        if resolved:
            clear_pending()
            pre_resolved = dict(pending.original_intent)
            pre_resolved["tickers"] = [resolved["ticker"]]
            pre_resolved["market"] = resolved["market"]
        else:
            ambiguous = check_ambiguity([pending.company_name])
            reply = "未能识别您的选择。"
            if ambiguous:
                reply += "\n" + build_question(ambiguous)
            chat_history.append({"role": "assistant", "text": reply, "id": msg_id + "-r"})

            async def _question_stream():
                yield sse_event("token", {"text": reply})
                yield sse_event("done", _assistant_ids(msg_id, rid))

            return StreamingResponse(_question_stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "Connection": "keep-alive",
                                              "X-Accel-Buffering": "no"})

    return StreamingResponse(
        _stream_chat(user_input, history=context_history, pre_resolved_intent=pre_resolved,
                     rid=rid, msg_id=msg_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


# ── SSE 流式：基金 ───────────────────────────────────────────

async def _stream_fund_chat(user_input: str, rid: str = "", msg_id: str = ""):
    """SSE 事件流生成器：基金意图解析 → 数据获取 → 报告生成"""
    done = _assistant_ids(msg_id, rid)
    try:
        yield sse_event("status", {"message": "正在识别基金意图..."})
        intent = await _run_sync(parse_fund_intent, user_input)
        yield sse_event("intent", {
            "intent": intent.get("intent"),
            "fund_codes": intent.get("fund_codes"),
        })

        if not intent.get("fund_codes"):
            yield sse_event("token", {"text": (
                "未能识别出基金代码。请明确基金代码或名称，例如：\n"
                "- 查询 005827 净值\n- 分析易方达蓝筹\n- 110011 和 005827 对比")})
            yield sse_event("done", done)
            return

        yield sse_event("status", {"message": "正在制定基金数据获取计划..."})
        plan = await _run_sync(build_fund_plan, intent)
        if not plan.get("steps"):
            yield sse_event("token", {"text": "未能为基金生成执行计划。"})
            yield sse_event("done", done)
            return

        steps = plan["steps"]
        total = len(steps)
        for i, step in enumerate(steps):
            yield sse_event("status", {
                "message": f"正在获取基金数据 ({i + 1}/{total})",
                "detail": step.get("description", ""),
            })

        execution = await _run_sync(execute_fund_plan, plan)
        yield sse_event("status", {"message": "正在生成基金分析报告..."})

        async for token in _iter_fund_stream_tokens(execution, intent):
            yield token

        yield sse_event("done", done)

    except Exception as e:
        yield sse_event("error", {"message": str(e)})
        yield sse_event("done", done)


async def _iter_fund_stream_tokens(execution: dict, intent: dict):
    """将 stream_fund_report_tokens 生成器包装为异步 SSE 事件序列"""
    loop = asyncio.get_event_loop()
    gen = stream_fund_report_tokens(execution, intent)

    def _safe_next():
        try:
            return next(gen)
        except StopIteration:
            return None

    while True:
        try:
            token = await loop.run_in_executor(None, _safe_next)
            if token is None:
                break
            yield sse_event("token", {"text": token})
        except Exception as e:
            yield sse_event("token", {"text": "\n\n⚠️ 报告生成中断：" + str(e)})
            break


@router.post("/fund/stream")
async def chat_fund_stream(user_input: str = Form(...)):
    """SSE 流式基金对话端点"""
    rid = get_request_id()
    msg_id = str(uuid.uuid4())[:8]
    metrics.inc("chat_fund_stream_requests")
    chat_history.append({"role": "user", "text": user_input, "id": msg_id})
    audit("chat.fund_stream", {"input": user_input[:500], "msg_id": msg_id}, request_id=rid)
    return StreamingResponse(
        _stream_fund_chat(user_input, rid=rid, msg_id=msg_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )
