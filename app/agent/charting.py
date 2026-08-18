"""图表生成模块 - Plotly 交互式金融图表（迁移自 workshops/charting.py）"""

import pandas as pd
import plotly.graph_objects as go


def price_line_chart(df: pd.DataFrame, ticker: str = "", title: str = "") -> str:
    """简易折线图（仅收盘价走势）"""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df.index, y=df["Close"],
        mode="lines",
        name="收盘价",
        line=dict(color="#1976D2", width=2),
        fill="tozeroy",
        fillcolor="rgba(25, 118, 210, 0.08)",
    ))

    fig.update_layout(
        title=dict(text=title or f"{ticker} 收盘价走势", x=0.5, font=dict(size=16)),
        xaxis=dict(rangeslider=dict(visible=False)),
        yaxis=dict(title="价格", side="right"),
        hovermode="x unified",
        template="plotly_white",
        height=400,
        margin=dict(l=40, r=40, t=50, b=40),
    )
    return fig.to_html(full_html=False, include_plotlyjs=True)
