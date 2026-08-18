"""D3 评测：报告数值落地性（groundedness）。

报告数值声明 ⊆ 注入数据：生成确定性报价报告（无 LLM 段），抽取报告内所有
数字 token，逐一比对"注入数据 + 合法派生形式"集合。groundedness = 可归属
数字 / 总数。若模板里混入硬编码数字，此指标会 < 1.0 → 抓到回归。

同时做反向控制：给报告追加一个伪造数字，断言检测器能识别（groundedness 下降）。

运行: D:\\python\\python.exe scripts\\eval_groundedness.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def extract_numbers(text: str) -> list[str]:
    """抽取数字 token（去千分位逗号），如 '1,234,567' → '1234567'。"""
    out = []
    for t in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        out.append(t.replace(",", ""))
    return out


def _norm(v: float, fmt: str | None = None) -> str:
    s = f"{v:.2f}" if fmt is None else f"{v:{fmt}}"
    return s.replace(",", "")


def _allowed_numbers(data: dict) -> set[str]:
    """注入数据 + 合法派生形式（价格/百分比/市值缩放）的字符串集合。"""
    allowed: set[str] = set()
    raws: list[float] = []

    def collect(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            raws.append(float(v))
        elif isinstance(v, dict):
            for x in v.values():
                collect(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                collect(x)

    collect(data)
    for v in raws:
        allowed.add(_norm(v))
        allowed.add(f"{int(v)}" if v == int(v) else "")
        allowed.add(_norm(v, ".0f"))
        allowed.add(_norm(v * 100, ".2f"))   # 百分比显示
        if v >= 1e8:
            allowed.add(_norm(v / 1e8, ".2f"))   # 亿
        if v >= 1e12:
            allowed.add(_norm(v / 1e12, ".2f"))  # 万亿
        if v >= 1e4:
            allowed.add(_norm(v / 1e4, ".0f"))   # 万
    return {a for a in allowed if a}


def _report_body(report: str) -> str:
    """剥离末尾免责声明（固定文案非数值声明），只测数据正文的数字。"""
    from app.constants import DATA_SOURCE_DISCLAIMER

    return report.split(DATA_SOURCE_DISCLAIMER)[0]


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
    allowed = _allowed_numbers(quote)
    toks = extract_numbers(_report_body(report))
    unattributed = [t for t in toks if t not in allowed]
    groundedness = 0.0 if not toks else (len(toks) - len(unattributed)) / len(toks)

    # 反向控制：在正文内注入一个数据里没有的数字，检测器应能抓出
    from app.constants import DATA_SOURCE_DISCLAIMER

    fake = report.replace(
        DATA_SOURCE_DISCLAIMER, "**预期增长率**: 99.99%\n" + DATA_SOURCE_DISCLAIMER)
    toks2 = extract_numbers(_report_body(fake))
    un2 = [t for t in toks2 if t not in allowed]
    g2 = 0.0 if not toks2 else (len(toks2) - len(un2)) / len(toks2)
    control_detected = g2 < groundedness

    return {
        "groundedness": round(groundedness, 4),
        "n_tokens": len(toks),
        "unattributed": unattributed,
        "control_detected": control_detected,
        "control_groundedness": round(g2, 4),
    }


def main() -> None:
    r = run()
    print(f"[eval_groundedness] n_tokens={r['n_tokens']} "
          f"groundedness={r['groundedness']}")
    if r["unattributed"]:
        print(f"  ✗ 未归属数字: {r['unattributed']}")
    print(f"  反向控制: 伪造 99.99% → groundedness={r['control_groundedness']} "
          f"(detected={r['control_detected']})")
    sys.exit(0 if r["groundedness"] == 1.0 and r["control_detected"] else 1)


if __name__ == "__main__":
    main()
