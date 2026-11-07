"""D3 评测：报告数值落地性（groundedness）—— L1 的最小用例。

生成确定性报价报告（无 LLM 段），抽取正文全部数字，逐一比对注入数据及
其派生写法（百分比/万/亿/万亿）。若模板里混入硬编码数字，指标 < 1.0，
即抓到回归。

同时做反向控制：往正文里追加一个伪造数字，断言检测器能识别。

匹配逻辑与全管线版本共用 `app.trust.grounding`，此处只保留报价路径这个
最小样本；全管线（含模型撰写的 SWOT/地缘/竞品文本）见 eval_l1_grounding.py。

运行: D:\\python\\python.exe scripts/eval_groundedness.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.constants import DATA_SOURCE_DISCLAIMER  # noqa: E402
from app.trust.grounding import ground_text  # noqa: E402

CONTROL_VALUE = 99.99


def run() -> dict:
    from app.agent import expressor

    quote = {
        "ticker": "AAPL",  # 无数字 ticker，避免代码被误当作数值声明
        "company_name": "贵州茅台",
        "current_price": 123.45,
        "currency": "CNY",
        "change_percent": 0.0234,
        "open": 120.0,
        "previous_close": 118.0,
        "day_high": 125.0,
        "day_low": 119.0,
        "volume": 1234567,
        "market_cap": 123400000000.0,
        "pe_ratio": 25.5,
    }
    execution = {
        "results": [
            {"step": {"action": "get_quote"}, "data": quote, "error": None},
        ],
        "summary": {"total": 1, "success": 1, "failed": 0},
    }
    intent = {"intent": "query_price", "tickers": ["AAPL"]}
    report = expressor.generate_report(execution, intent)

    # 免责声明是固定文案，不是数值声明
    body = report.split(DATA_SOURCE_DISCLAIMER)[0]
    base = ground_text(body, quote)

    fake = ground_text(body + f"\n**预期增长率**: {CONTROL_VALUE}%\n", quote)

    return {
        "groundedness": base["rate"],
        "n_tokens": base["tokens"],
        "unattributed": base["unmatched"],
        "control_detected": fake["rate"] < base["rate"],
        "control_groundedness": fake["rate"],
    }


def main() -> None:
    r = run()
    print(f"[eval_groundedness] n_tokens={r['n_tokens']} "
          f"groundedness={r['groundedness']}")
    if r["unattributed"]:
        print(f"  ✗ 未归属数字: {r['unattributed']}")
    print(f"  反向控制: 伪造 {CONTROL_VALUE}% → groundedness={r['control_groundedness']} "
          f"(detected={r['control_detected']})")
    sys.exit(0 if r["groundedness"] == 1.0 and r["control_detected"] else 1)


if __name__ == "__main__":
    main()
