# -*- coding: utf-8 -*-
"""政策信号 —— FOMC 官方声明 → 鸽鹰分（hawkish/dovish stance）。

JD 职责②：量化「政策信号 → 收益率走势」的关系。
本模块对 FOMC 会后声明的正文做**可复现的词典法**情绪计分，映射到 [-1,+1] 连续
政策立场（<0 偏鸽、>0 偏鹰），并给出关键词命中明细。这不是黑箱 LLM 语义猜测，
而是明文词典 + 词频统计，每条输出带源与文本，供复核。

诚实边界：
  - 输入是官方声明**原文**（FomcSource.fetch_latest_statement），不做逐句语义
    解析；dot plot / 点阵图 / 纪要的路径定量分析后续版本补。
  - 立场分为启发式（词典权重），非美联储官方口径；报告里显式标注为模型估计。
"""
from __future__ import annotations

import re
from collections import Counter

import pandas as pd

# 词典：命中词 → 权重。鸽词=负、鹰词=正。权重仅反映方向与相对强烈度，非官方刻度。
_HAWKISH = {
    "tighten": 2.0, "tightening": 2.0, "restrictive": 1.5,
    "inflation elevated": 2.0, "elevated inflation": 2.0,
    "highly elevated": 1.5, "upside risks": 1.5, "upside risk": 1.5,
    "further increases": 2.0, "raise the target": 2.0, "higher for longer": 1.5,
    "persistent": 1.0, "accelerating": 1.0, "overheating": 2.0,
    "wage pressure": 1.0, "capacity constraints": 1.0,
}
_DOVISH = {
    "ease": 2.0, "easing": 2.0, "accommodative": 1.5,
    "downside risks": 1.5, "downside risk": 1.5, "slowing": 1.0,
    "disinflation": 1.5, "weaker": 1.0, "softening": 1.0,
    "downgrade": 1.0, "labour market cooling": 1.0, "lower rates": 1.5,
    "cuts": 1.0, "less restrictive": 1.5, "subdued": 1.0,
}


class PolicySignal:
    """从 FOMC 官方文本计算鸽鹰分。"""

    def score_text(self, text: str) -> dict:
        """对文本计分，返回方向分、命中明细与解读。

        总分 = Σ(鹰词权重) - Σ(鸽词权重)，再除以命中词基准长度归一化到近似 [-1,1]。
        """
        if not text:
            return {"ok": False, "score": None, "stance": "无法判定", "hits": {}}
        low = re.sub(r"\s+", " ", str(text)).lower()
        hawk_hits, dove_hits = Counter(), Counter()
        hawk_w, dove_w = 0.0, 0.0
        for w, weight in _HAWKISH.items():
            n = low.count(w)
            if n:
                hawk_hits[w] = n
                hawk_w += weight * n
        for w, weight in _DOVISH.items():
            n = low.count(w)
            if n:
                dove_hits[w] = n
                dove_w += weight * n

        denom = sum(hawk_hits.values()) + sum(dove_hits.values())
        if denom == 0:
            score = 0.0
        else:
            raw = (hawk_w - dove_w) / denom  # 每个命中词的平均强度，∈约[-2,2]
            score = float(max(-1.0, min(1.0, raw)))

        if score >= 0.2:
            stance = "偏鹰"
        elif score <= -0.2:
            stance = "偏鸽"
        else:
            stance = "中性"

        return {
            "ok": True,
            "score": round(score, 4),
            "stance": stance,
            "hawkish_hits": dict(hawk_hits),
            "dovish_hits": dict(dove_hits),
            "num_hawk_tokens": int(sum(hawk_hits.values())),
            "num_dove_tokens": int(sum(dove_hits.values())),
            "method": "词典法鸽鹰计分（启发式，非官方口径）",
            "on_raw_text": True,
        }

    def score_statement(self, statement: dict) -> dict:
        """对 FomcSource 返回的 statement 字典计分，直接并入报告。"""
        if not statement or not statement.get("ok"):
            return {"ok": False, "score": None, "stance": "无法判定",
                    "reason": "FOMC 声明不可用", "hits": {}}
        result = self.score_text(statement.get("body") or statement.get("excerpt") or "")
        result.update({
            "meeting_date": statement.get("meeting_date"),
            "url": statement.get("url"),
            "source": statement.get("source", "fomc"),
            "text_len": statement.get("text_len"),
        })
        return result

    def to_lists(self, scores: list[dict]) -> dict:
        """序列化多期政策分到 compact 结构（看板/API）。"""
        return {
            "meeting_dates": [s.get("meeting_date") for s in scores],
            "scores": [s.get("score") for s in scores],
            "stances": [s.get("stance") for s in scores],
        }
