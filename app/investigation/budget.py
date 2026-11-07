"""调查预算 —— 三重硬上限（轮次 / 工具调用 / LLM 调用）+ 墙钟 deadline。

纯逻辑、零 I/O、不 import 任何 LLM 或数据源：每条上限都能独立单测，
不需要真 LLM、不需要网络。这是整个调查包可离线验证的前提。

预算不是"建议值"：`check_stop()` 在每一次消耗**之前**被调用，返回非 None
即必须停。默认数值来自 app/config.py（那里是唯一真源）。

关于 deadline 的诚实边界：**它不是严格硬停**。单次在途 LLM 请求最长可超
`LLM_TIMEOUT`（默认 120s），因此墙钟会超出 deadline 最多约 120s。我们不用
"假装硬停"来掩盖这点——改用 `min_round_seconds` 拒绝开启来不及跑完的新轮，
并把实际超时量如实写进报告（见 report.py 的 `budget_overshoot_seconds`）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Depth(str, Enum):
    """调查档位。"""
    STANDARD = "standard"
    DEEP = "deep"

    @classmethod
    def coerce(cls, value) -> "Depth":
        """把请求参数归一化：只认 'deep'，其余一律 standard。

        注意：本类是 str 混入枚举，`str(Depth.DEEP)` 在 3.11+ 返回 "Depth.DEEP"
        而非 "deep"，所以必须先处理枚举实例，不能直接 str()。
        """
        if isinstance(value, cls):
            return value
        return cls.DEEP if str(value or "").strip().lower() == "deep" else cls.STANDARD


class StopReason(str, Enum):
    """停止原因。全部会出现在报告里，不静默。"""
    ALL_SETTLED = "all_hypotheses_settled"   # 正常收尾：模型不再提出动作
    MAX_ROUNDS = "max_rounds"
    MAX_TOOL_CALLS = "max_tool_calls"
    MAX_LLM_CALLS = "max_llm_calls"
    DEADLINE = "deadline"
    NO_PROGRESS = "no_progress"
    OSCILLATION = "oscillation"
    LLM_EXHAUSTED = "llm_exhausted"          # 引擎返回超限哨兵，不是真结论
    LLM_ERROR = "llm_error"


@dataclass(frozen=True)
class Budget:
    """不可变旋钮。默认值来自 app.config，构造后不可改。"""
    max_rounds: int
    max_tool_calls: int
    max_llm_calls: int
    deadline_seconds: int
    max_hypotheses: int
    max_evidence_per_hypothesis: int
    max_peers: int
    no_progress_window: int
    min_round_seconds: int
    depth: Depth = Depth.STANDARD

    @classmethod
    def from_settings(cls, depth: Depth | str = Depth.STANDARD, settings=None) -> "Budget":
        """按档位从配置读预算。settings 缺省走 get_settings()。"""
        d = Depth.coerce(depth)
        if settings is None:
            from app.config import get_settings
            settings = get_settings()
        p = "INVESTIGATION_DEEP_" if d is Depth.DEEP else "INVESTIGATION_STANDARD_"
        return cls(
            max_rounds=int(getattr(settings, p + "MAX_ROUNDS")),
            max_tool_calls=int(getattr(settings, p + "MAX_TOOL_CALLS")),
            max_llm_calls=int(getattr(settings, p + "MAX_LLM_CALLS")),
            deadline_seconds=int(getattr(settings, p + "DEADLINE_SECONDS")),
            max_hypotheses=int(getattr(settings, p + "MAX_HYPOTHESES")),
            max_evidence_per_hypothesis=int(
                getattr(settings, p + "MAX_EVIDENCE_PER_HYPOTHESIS")),
            max_peers=int(getattr(settings, p + "MAX_PEERS")),
            no_progress_window=int(settings.INVESTIGATION_NO_PROGRESS_WINDOW),
            min_round_seconds=int(settings.INVESTIGATION_MIN_ROUND_SECONDS),
            depth=d,
        )

    def as_dict(self) -> dict:
        return {
            "depth": self.depth.value,
            "max_rounds": self.max_rounds,
            "max_tool_calls": self.max_tool_calls,
            "max_llm_calls": self.max_llm_calls,
            "deadline_seconds": self.deadline_seconds,
            "max_hypotheses": self.max_hypotheses,
            "max_evidence_per_hypothesis": self.max_evidence_per_hypothesis,
            "max_peers": self.max_peers,
            "no_progress_window": self.no_progress_window,
            "min_round_seconds": self.min_round_seconds,
        }


@dataclass
class Spend:
    """可变计数器。`started_at` 为开始时刻（由注入的时钟给出）。"""
    started_at: float
    rounds: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    duplicates: int = 0
    consecutive_no_progress: int = 0
    oscillation: bool = False

    def elapsed(self, now: float) -> float:
        return max(0.0, float(now) - float(self.started_at))

    def remaining(self, budget: Budget, now: float) -> float:
        return budget.deadline_seconds - self.elapsed(now)

    def as_dict(self, budget: Budget, now: float) -> dict:
        return {
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "llm_calls": self.llm_calls,
            "duplicates": self.duplicates,
            "elapsed": round(self.elapsed(now), 3),
            "remaining": round(self.remaining(budget, now), 3),
        }


def check_stop(
    budget: Budget,
    spend: Spend,
    now: float,
    *,
    llm_cost: int = 0,
    llm_exhausted: bool = False,
    check_rounds: bool = True,
) -> StopReason | None:
    """消耗前的刹车检查。返回 None 表示可以继续。

    Args:
        llm_cost: 本次动作将要消耗的 LLM 调用数（开启一轮 = 1；某些工具自带 1）。
        llm_exhausted: 引擎返回了轮数超限哨兵 → 必须停，且其 text 不是结论。
        check_rounds: 轮内检查工具调用时传 False。否则"最后一轮"会变成死轮——
            轮次计数在开轮时已自增，若轮内仍按轮次判定，第 max_rounds 轮里
            所有工具都会被误判为超预算而跳过。

    判定顺序即优先级：哨兵 > 墙钟 > 轮次 > 工具调用 > LLM 调用 > 振荡 > 无进展。
    """
    if llm_exhausted:
        return StopReason.LLM_EXHAUSTED
    if spend.remaining(budget, now) < budget.min_round_seconds:
        return StopReason.DEADLINE
    if check_rounds and spend.rounds >= budget.max_rounds:
        return StopReason.MAX_ROUNDS
    if spend.tool_calls >= budget.max_tool_calls:
        return StopReason.MAX_TOOL_CALLS
    if spend.llm_calls + max(0, int(llm_cost)) > budget.max_llm_calls:
        return StopReason.MAX_LLM_CALLS
    if spend.oscillation:
        return StopReason.OSCILLATION
    if spend.consecutive_no_progress >= budget.no_progress_window:
        return StopReason.NO_PROGRESS
    return None
