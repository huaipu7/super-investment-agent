"""版本戳 —— 飞轮归因的三件套：model / prompt_version / git_sha。

没有版本戳，反馈只能说明「这条不好」，说不出「哪个版本变差了」，
闭环就断在归因这一步。

prompt_version 取「定义提示词常量的模块」源码哈希，而不是人工维护的
"p3" 之类的编号：改提示词必变，改无关代码不变，且不会忘记 bump。
被扫描的模块 = app/ 下存在模块级 `*PROMPT*` 赋值的 .py 文件。
"""
from __future__ import annotations

import ast
import hashlib
from functools import lru_cache
from pathlib import Path

from app.config import BASE_DIR, get_settings


def _defines_prompt(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and "PROMPT" in t.id.upper():
                return True
    return False


@lru_cache(maxsize=1)
def _prompt_files() -> tuple[Path, ...]:
    app_dir = BASE_DIR / "app"
    out = []
    for p in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in p.parts or not _defines_prompt(p):
            continue
        out.append(p)
    return tuple(out)


@lru_cache(maxsize=1)
def prompt_version() -> str | None:
    """提示词基线短哈希；一个提示词模块都没扫到则返回 None（不编造）。"""
    files = _prompt_files()
    if not files:
        return None
    digest = hashlib.sha256()
    for p in files:
        digest.update(p.relative_to(BASE_DIR).as_posix().encode("utf-8"))
        digest.update(p.read_bytes())
    return f"p{digest.hexdigest()[:8]}"


def model_version() -> str | None:
    return get_settings().LLM_MODEL or None


def stamp() -> dict:
    """三件套快照。git_sha 复用 app.trust.ledger 的缓存（非 git 环境为 None）。"""
    from app.trust.ledger import git_sha

    return {
        "model": model_version(),
        "prompt_version": prompt_version(),
        "git_sha": git_sha(),
    }
