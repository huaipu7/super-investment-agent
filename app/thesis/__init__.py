"""论点台账：把「为什么买 / 什么情况下认错」写成可被机器检查的结构化数据。"""
from app.thesis.checks import evaluate, evaluate_falsifier, load_ledger, resolve_metric
from app.thesis.draft import build_draft
from app.thesis.db import (
    add_evidence,
    add_review,
    create_thesis,
    delete_thesis,
    get_thesis,
    list_evidence,
    list_reviews,
    list_theses,
    update_thesis,
)

__all__ = [
    "evaluate", "evaluate_falsifier", "load_ledger", "resolve_metric", "build_draft",
    "add_evidence", "add_review", "create_thesis", "delete_thesis",
    "get_thesis", "list_evidence", "list_reviews", "list_theses", "update_thesis",
]
