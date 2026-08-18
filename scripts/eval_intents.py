"""D3 评测：意图识别准确率（黄金意图集 ≥10 例）。

默认离线（确定性规则兜底路径）：patch LLM 两路解析 → 测量 parse() 的
规则兜底准确性，结果可复现。加 --live 则走真实 LLM（需配置 LLM_API_KEY）。

运行: D:\\python\\python.exe scripts\\eval_intents.py [--live]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (输入, 期望意图, 期望 tickers)
GOLDEN = [
    ("苹果公司股价", "query_price", ["AAPL"]),
    ("微软最近走势", "query_history", ["MSFT"]),
    ("分析腾讯控股", "analysis", ["0700.HK"]),
    ("对比茅台和五粮液", "compare", ["600519.SS", "000858.SZ"]),
    ("对比茅台和五粮液近三年盈利能力并给出估值", "deep_compare",
     ["600519.SS", "000858.SZ"]),
    ("查一下贵州茅台财报", "financials", ["600519.SS"]),
    ("英伟达市值多少", "query_price", ["NVDA"]),
    ("宁德时代近一年走势", "query_history", ["300750.SZ"]),
    ("分析招商银行投资价值", "analysis", ["600036.SS"]),
    ("特斯拉和比亚迪财务对比", "compare", ["TSLA", "1211.HK"]),
    ("看看恒瑞医药怎么样", "analysis", ["600276.SS"]),
    ("腾讯和美团哪个更好", "compare", ["0700.HK", "3690.HK"]),
]


def run(live: bool = False) -> dict:
    from app.agent.intent import parse

    n = len(GOLDEN)
    intent_hit = 0
    ticker_hit = 0
    exact = 0
    failures: list[dict] = []

    for text, exp_intent, exp_tickers in GOLDEN:
        if live:
            r = parse(text)
        else:
            from unittest import mock

            with mock.patch("app.agent.intent._parse_with_function_calling",
                            return_value=None), \
                    mock.patch("app.agent.intent._parse_with_llm", return_value=None):
                r = parse(text)
        got_intent = r.get("intent")
        got_tickers = sorted((r.get("tickers") or []))
        exp_sorted = sorted(exp_tickers)
        ok_intent = got_intent == exp_intent
        ok_tickers = got_tickers == exp_sorted
        intent_hit += ok_intent
        ticker_hit += ok_tickers
        exact += ok_intent and ok_tickers
        if not (ok_intent and ok_tickers):
            failures.append({
                "input": text, "expected": exp_intent, "got": got_intent,
                "expected_tickers": exp_tickers, "got_tickers": got_tickers,
            })

    return {
        "mode": "live" if live else "offline-fallback",
        "n": n,
        "intent_accuracy": round(intent_hit / n, 4),
        "ticker_accuracy": round(ticker_hit / n, 4),
        "exact_accuracy": round(exact / n, 4),
        "failures": failures,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="走真实 LLM 解析")
    args = ap.parse_args()
    r = run(live=args.live)
    print(f"[eval_intents] mode={r['mode']} n={r['n']}")
    print(f"  intent_accuracy={r['intent_accuracy']}  "
          f"ticker_accuracy={r['ticker_accuracy']}  "
          f"exact_accuracy={r['exact_accuracy']}")
    for f in r["failures"]:
        print(f"  ✗ {f['input']!r} 期望={f['expected']}/{f['expected_tickers']} "
              f"实际={f['got']}/{f['got_tickers']}")
    sys.exit(0 if r["exact_accuracy"] >= 0.9 else 1)


if __name__ == "__main__":
    main()
