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
from ui.pages import fixed_income, investigation, evals, thesis, search

MODULES = ["📊 行情与评分", "📈 深度研究", "💬 AI 对话", "🌡 市场情绪",
           "📦 基金分析", "⭐ 自选股", "🆚 多股对比", "🔬 回测验证",
           "🧱 固收研究", "🧭 自主深度调查", "🧪 评测看板", "🧾 论点台账",
           "🔍 知识库检索"]

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
elif nav == MODULES[7]:
    backtest.page()
elif nav == MODULES[8]:
    fixed_income.page()
elif nav == MODULES[9]:
    investigation.page()
elif nav == MODULES[10]:
    evals.page()
elif nav == MODULES[11]:
    thesis.page()
elif nav == MODULES[12]:
    search.page()
else:
    # 刻意不写兜底页面：新增模块忘记加 elif 时，这里显式报错而不是静默错路由
    st.error(f"未注册的模块：{nav}（请检查 MODULES 与 if/elif 链是否同步）")
