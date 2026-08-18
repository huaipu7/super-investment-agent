"""Phase D1 验证：模型网关（任务→模型路由 + 降级链 + 重试 + token 计数）。

运行: D:\\python\\python.exe scripts\\verify_model_router.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def main():
    import app.llm.router as rtr

    fake_settings = SimpleNamespace(
        MODEL_TIERS={"intent": "model-flash", "report": "model-strong"},
        MODEL_FALLBACKS={"report": ["model-flash"]},
        LLM_MODEL="deepseek-v4-flash",
    )

    # ── (a) 任务→模型路由 ──
    with mock.patch("app.llm.router.get_settings", lambda: fake_settings):
        mi = rtr.resolve_model("intent")
        mr = rtr.resolve_model("report")
        check("(a) 不同任务选不同模型", mi == "model-flash" and mr == "model-strong"
              and mi != mr, f"{mi} vs {mr}")
        check("(a) 未配置任务回退 LLM_MODEL",
              rtr.resolve_model("summary") == "deepseek-v4-flash")
        check("(a) 无任务返回 None", rtr.resolve_model() is None)
        check("(b) 降级链主+备", rtr.get_model_chain("report") ==
              ["model-strong", "model-flash"], str(rtr.get_model_chain("report")))
        check("(b) 无任务链为空", rtr.get_model_chain() == [])

    # ── (c) 首次 500 → 重试成功 + llm_calls 计数 ──
    from app.llm.client import LLMClient, LLMError
    from app.observability.metrics import metrics

    metrics.reset()
    client = LLMClient(api_key="test", retry=3, retry_backoff=0.01)
    with mock.patch(
        "httpx.Client.post",
        side_effect=[LLMError("HTTP 500 mock"),
                     _FakeResp(200, {"content": [{"type": "text", "text": "ok"}]})],
    ):
        out = client.chat([{"role": "user", "content": "hi"}])
    check("(c) 首次 500 重试成功", out == "ok", repr(out))
    snap = metrics.snapshot()
    check("(c) llm_calls=1", snap["counters"].get("llm_calls") == 1, str(snap["counters"]))

    # ── (d) 请求体使用路由模型（端到端）──
    captured: dict = {}

    def fake_post_assert(self, url, headers=None, json=None):
        captured["model"] = json.get("model")
        return _FakeResp(200, {"content": [{"type": "text", "text": "ok"}]})

    with mock.patch("httpx.Client.post", new=fake_post_assert), \
            mock.patch("app.llm.router.get_settings",
                       lambda: SimpleNamespace(
                           MODEL_TIERS={"report": "report-model"},
                           MODEL_FALLBACKS={},
                           LLM_MODEL="base-model")):
        c2 = LLMClient(api_key="test")
        check("(d) _model_for 无任务=默认", c2._model_for(None) == c2.model)
        c2.chat([{"role": "user", "content": "hi"}], task="report")
    check("(d) 请求体 model 来自路由", captured.get("model") == "report-model",
          str(captured))

    # ── (e) token 计数（usage 优先 / 缺省按字符估计）──
    metrics.reset()
    client = LLMClient(api_key="test")
    client._record_usage({"usage": {"input_tokens": 10, "output_tokens": 5}},
                         [{"role": "user", "content": "hi"}])
    snap = metrics.snapshot()
    check("(e) usage 字段优先",
          snap["counters"].get("llm_tokens_in") == 10
          and snap["counters"].get("llm_tokens_out") == 5, str(snap["counters"]))
    metrics.reset()
    client._record_usage(None, [{"role": "user", "content": "你好世界"}],
                         output_chars=6)
    snap = metrics.snapshot()
    check("(e) 无 usage 按字符估计",
          snap["counters"].get("llm_tokens_in") == 1  # 4 字符 //3
          and snap["counters"].get("llm_tokens_out") == 2, str(snap["counters"]))

    # ── (f) 重试耗尽 → 抛 LLMError + errors/llm_errors 计数 ──
    metrics.reset()
    client = LLMClient(api_key="test", retry=2, retry_backoff=0)
    raised = False
    with mock.patch("httpx.Client.post", side_effect=LLMError("always 500")):
        try:
            client.chat([{"role": "user", "content": "hi"}])
        except LLMError:
            raised = True
    snap = metrics.snapshot()
    check("(f) 重试耗尽抛 LLMError", raised)
    check("(f) errors/llm_errors=1",
          snap["counters"].get("errors") == 1
          and snap["counters"].get("llm_errors") == 1, str(snap["counters"]))

    # ── (g) 流式重试：首次状态失败 → 重试成功 ──
    metrics.reset()

    class _FakeStream:
        def __init__(self, status_code, lines=None):
            self.status_code = status_code
            self._lines = lines or []
            self._i = 0

        def read(self):
            return b"boom"

        def iter_lines(self):
            for ln in self._lines:
                yield ln

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    _stream_calls = {"n": 0}

    def fake_stream(self, method, url, headers=None, json=None):
        if method != "POST":
            raise AssertionError(f"unexpected method {method}")
        _stream_calls["n"] += 1
        if _stream_calls["n"] == 1:
            return _FakeStream(500)
        return _FakeStream(200, [
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"A"}}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"B"}}',
        ])

    client = LLMClient(api_key="test", retry=2, retry_backoff=0)
    with mock.patch("httpx.Client.stream", new=fake_stream):
        got = "".join(client.stream([{"role": "user", "content": "hi"}]))
    check("(g) 流式首次失败重试成功", got == "AB", repr(got))
    check("(g) 流式共调用 2 次", _stream_calls["n"] == 2, str(_stream_calls))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
