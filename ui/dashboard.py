"""超级投研 Agent —— Streamlit 看板入口。

启动:
    D:\\python\\python.exe -m streamlit run ui/dashboard.py
后端需先启动:
    D:\\python\\python.exe api/main.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from app.config import get_settings

st.set_page_config(page_title="超级投研 Agent", page_icon="📈", layout="wide")

from ui import components
from ui.pages import quotes, research, chat, market_sentiment, fund, watchlist, compare, backtest

MODULES = ["📊 行情与评分", "📈 深度研究", "💬 AI 对话", "🌡 市场情绪",
           "📦 基金分析", "⭐ 自选股", "🆚 多股对比", "🔬 回测验证"]

with st.sidebar:
    st.title("🧭 超级投研 Agent")
    st.caption("统一投资研究系统")
    nav = st.radio("模块", MODULES, key="nav")
    st.divider()
    components.backend_status()
    components.data_source_status()
    st.divider()
    st.caption("💡 提示: 输入代码或名称\n如 600519 / 苹果 / AAPL")
    st.caption(f"后端: 127.0.0.1:{get_settings().SERVER_PORT}")

st.markdown("## 🚀 超级投研 Agent")
st.caption("行情 · 专家评分 · 深度研究 · AI 对话 · 基金 · 自选股 —— 全部真实数据；"
           "🔬 回测验证 —— 无前瞻回测（默认 synthetic，可切真实数据源）")

if nav == MODULES[0]:
    quotes.page()
elif nav == MODULES[1]:
    research.page()
elif nav == MODULES[2]:
    chat.page()
elif nav == MODULES[3]:
    market_sentiment.page()
elif nav == MODULES[4]:
    fund.page()
elif nav == MODULES[5]:
    watchlist.page()
elif nav == MODULES[6]:
    compare.page()
else:
    backtest.page()
