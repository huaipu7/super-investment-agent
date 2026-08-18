"""中文字体注册 —— reportlab 与 matplotlib 共用（Windows 系统字体）。

reportlab 通过角色（heading/title/body）注册字体；matplotlib 直接 addfont 并
设置 rcParams。全部失败时回退英文字体，保证报告仍可生成（中文会缺字）。
"""
from __future__ import annotations

import os

FONT_DIR = "C:/Windows/Fonts"

# 每个角色按优先级列出候选字体文件（.ttc 使用 subfontIndex=0 取首面子体）
FONT_ROLES: dict[str, list[str]] = {
    "heading": ["simhei.ttf", "msyh.ttc", "simsun.ttc"],
    "title": ["simhei.ttf", "msyh.ttc", "simsun.ttc"],
    "body": ["simfang.ttf", "msyh.ttc", "simsun.ttc", "simhei.ttf"],
}

_REGISTERED: dict[str, str] | None = None


def reportlab_fonts() -> dict[str, str]:
    """注册中文字体到 reportlab，返回 {role: fontName}（结果缓存）。

    主字体缺失或注册失败时依次尝试后备字体；全部失败回退 Times-Roman。
    """
    global _REGISTERED
    if _REGISTERED is not None:
        return _REGISTERED

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    registered: dict[str, str] = {}
    for role, files in FONT_ROLES.items():
        for fname in files:
            path = os.path.join(FONT_DIR, fname)
            if not os.path.exists(path):
                continue
            try:
                if fname.lower().endswith(".ttc"):
                    pdfmetrics.registerFont(TTFont(f"CJK-{role}", path, subfontIndex=0))
                else:
                    pdfmetrics.registerFont(TTFont(f"CJK-{role}", path))
                registered[role] = f"CJK-{role}"
                break
            except Exception:
                continue

    if not registered:
        registered = {"heading": "Times-Roman", "title": "Times-Roman", "body": "Times-Roman"}
    else:
        first = next(iter(registered.values()))
        for role in ("heading", "title", "body"):
            registered.setdefault(role, first)

    _REGISTERED = registered
    return registered


def matplotlib_register() -> list[str]:
    """将中文字体加入 matplotlib 字体缓存并设置 rcParams。

    Returns:
        list[str]: 已加入的字体族名（供 font.sans-serif 引用）。
    """
    import matplotlib.font_manager as fm

    registered: list[str] = []
    known = {"simhei.ttf": "SimHei", "msyh.ttc": "Microsoft YaHei", "simsun.ttc": "SimSun"}
    for fname, family in known.items():
        path = os.path.join(FONT_DIR, fname)
        if os.path.exists(path):
            try:
                fm.fontManager.addfont(path)
                registered.append(family)
            except Exception:
                continue

    import matplotlib.pyplot as plt

    if registered:
        plt.rcParams["font.sans-serif"] = registered + ["DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
    return registered
