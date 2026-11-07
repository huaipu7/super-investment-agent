"""可信层：事实台账、出处归类、数值接地。"""
from app.trust.provenance import (
    COMPUTED,
    DEGRADED,
    IMPUTED,
    LLM,
    SOURCE,
    UNKNOWN,
    classify,
    is_valid,
)
from app.trust.ledger import Ledger, build_ledger, git_sha
from app.trust.sanitize import sanitize_numbers, sanitize_result


def strict_mode() -> bool:
    """严格模式：输入缺失时显式降级，而非静默兜底。默认开启。"""
    from app.config import get_settings

    return bool(get_settings().TRUST_STRICT_MODE)


__all__ = [
    "COMPUTED", "SOURCE", "LLM", "IMPUTED", "DEGRADED", "UNKNOWN",
    "classify", "is_valid",
    "Ledger", "build_ledger", "git_sha", "strict_mode",
    "sanitize_numbers", "sanitize_result",
]
