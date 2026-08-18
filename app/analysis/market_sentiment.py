"""市场情绪 AI —— 市场级情绪评分（A股 / 美股 / 港股）。

多源真实信号 → 各维度归一化到 [-100, +100] → 加权合成总分 → 标签。
任一信号取不到 → score=None 并诚实标注；总分仅基于可用信号并按剩余权重
归一化（红线 #7：绝不补造数值，缺失字段显式标注）。

信号源（已实测）：
  - A股：沪深300/中证1000（akshare 指数日线）、涨停/跌停池（东财）、
        北向资金、两融余额（上交所）、QVIX 中国波指（上证50ETF期权）
  - 美股：标普500（Sina）、VIX（CBOE 官方 CSV，东财/Sina 本环境不可用）
  - 港股：恒指（Sina）、南向资金（港股通沪+深）、恒指已实现波动率（派生）
  - 舆情：东财新闻搜索（关键词词频，确定性词典，非 LLM）
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import urllib.request

import pandas as pd

from app.config import get_settings
from app.utils.logger import logger

MARKETS = {
    "a_share": "A股",
    "us": "美股",
    "hk": "港股",
}

DIM_LABELS = {
    "momentum": "指数动量",
    "breadth": "涨跌广度",
    "fund": "资金流",
    "margin": "两融",
    "panic": "恐慌/贪婪",
    "news": "舆情",
}

# 各市场维度权重；缺失信号自动剔除，并按剩余权重归一化
WEIGHTS = {
    "a_share": {"momentum": 0.30, "breadth": 0.25, "fund": 0.20,
                "margin": 0.10, "panic": 0.15, "news": 0.10},
    "us": {"momentum": 0.55, "panic": 0.30, "news": 0.15},
    "hk": {"momentum": 0.45, "fund": 0.30, "panic": 0.15, "news": 0.10},
}

_POS_KW = ["上涨", "大涨", "净流入", "增长", "利好", "回暖", "突破", "新高",
           "回升", "走强", "买入", "增持", "超预期", "向好", "企稳", "反弹"]
_NEG_KW = ["下跌", "大跌", "净流出", "下滑", "利空", "风险", "承压", "走弱",
           "危机", "暴跌", "减持", "抛售", "违约", "制裁", "放缓", "退市"]

_EM_NEWS_KEYWORD = {"a_share": "A股", "us": "美股", "hk": "港股"}


def _clip(x: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _pct_rank(series: pd.Series, value: float) -> float:
    """value 在 series 中的经验分位 [0,1]（等值计入）。"""
    if len(series) == 0:
        return 0.5
    return float((series <= value).mean())


def label_for(score: float | None) -> str:
    """评分 → 情绪标签（≤-50 恐慌 / ≤-20 谨慎 / 中性 / ≥+20 偏暖 / ≥+50 贪婪）。"""
    if score is None:
        return "数据不足"
    if score <= -50:
        return "恐慌"
    if score <= -20:
        return "谨慎"
    if score >= 50:
        return "贪婪"
    if score >= 20:
        return "偏暖"
    return "中性"


# ── 底层抓取 ─────────────────────────────────────────────

def _fetch_close(fn, name: str) -> pd.Series:
    """调 akshare 拉日线，归一化为按日期升序的 close Series；失败抛异常。"""
    try:
        df = fn()
    except Exception as e:
        raise ValueError(f"{name}: {e}") from e
    if df is None or df.empty:
        raise ValueError(f"{name}: 空数据")
    date_col = "date" if "date" in df.columns else ("日期" if "日期" in df.columns else None)
    close_col = "close" if "close" in df.columns else ("收盘" if "收盘" in df.columns else None)
    if not date_col or not close_col:
        raise ValueError(f"{name}: 缺列 {list(df.columns)}")
    s = pd.Series(
        pd.to_numeric(df[close_col], errors="coerce").values,
        index=pd.to_datetime(df[date_col], errors="coerce"),
    )
    s = s.dropna().sort_index()
    if s.empty:
        raise ValueError(f"{name}: 无有效收盘")
    return s


def _cboe_vix_close() -> pd.Series:
    """CBOE 官方 VIX 历史 CSV（无密钥；东财/Sina 本环境不可用）。"""
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8")
    rows = [ln.split(",") for ln in text.splitlines()
            if ln and not ln.upper().startswith("DATE")]
    dates = pd.to_datetime([r[0] for r in rows], format="%m/%d/%Y", errors="coerce")
    closes = pd.to_numeric([r[4] for r in rows], errors="coerce")
    s = pd.Series(closes, index=dates).dropna().sort_index()
    if s.empty:
        raise ValueError("CBOE VIX: 无数据")
    return s


def _fetch_headlines(keyword: str, limit: int = 15) -> list[str]:
    """东财新闻搜索（与个股新闻同一端点），返回标题列表；失败 → []。"""
    inner = {
        "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                       "pageIndex": 1, "pageSize": limit,
                                       "preTag": "<em>", "postTag": "</em>"}},
    }
    url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=jq&param="
           + urllib.parse.quote(json.dumps(inner, ensure_ascii=False)) + "&_=1")
    req = urllib.request.Request(url, headers={
        "Referer": "https://so.eastmoney.com/news/s", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8")
        data = json.loads(text[text.index("(") + 1: text.rindex(")")])
        arts = data["result"]["cmsArticleWebOld"]
    except Exception:
        return []
    out = []
    for a in arts[:limit]:
        t = re.sub(r"</?em>", "", str(a.get("title", "")))
        if t.strip():
            out.append(t.strip())
    return out


# ── 信号构建器 ───────────────────────────────────────────

def _dim(dim: str):
    """给构建器打上维度标记，供失败时诚实标注。"""
    def deco(fn):
        fn._dim = dim
        return fn
    return deco


def _momentum(close: pd.Series, lookback: int = 21, hist: int = 252):
    """近 lookback 日涨跌幅在近 hist 滚动涨跌幅中的分位 → [-100,100]。

    Returns (score, cur_ret_pct, as_of)。
    """
    if len(close) < lookback + 5:
        raise ValueError("指数历史不足")
    cur_ret = close.iloc[-1] / close.iloc[-lookback] - 1.0
    rolling = close.pct_change(lookback).dropna().tail(hist)
    pct = _pct_rank(rolling, cur_ret)
    as_of = str(close.index[-1].date())
    return (pct - 0.5) * 200.0, cur_ret * 100.0, as_of


@_dim("momentum")
def _b_ashare_momentum(ctx):
    import akshare as ak
    s300 = ctx.close("sh000300",
                     lambda: ak.stock_zh_index_daily(symbol="sh000300"), "沪深300")
    s1000 = ctx.close("sh000852",
                      lambda: ak.stock_zh_index_daily(symbol="sh000852"), "中证1000")
    sc1, r1, d1 = _momentum(s300)
    sc2, r2, d2 = _momentum(s1000)
    return {
        "dim": "momentum", "label": DIM_LABELS["momentum"],
        "value": f"沪深300 {r1:+.1f}% · 中证1000 {r2:+.1f}%",
        "score": round(0.6 * sc1 + 0.4 * sc2, 1),
        "source": "akshare 沪深300+中证1000",
        "as_of": max(d1, d2),
        "note": "20日涨跌幅历史分位 (0.6×300 + 0.4×1000)",
    }


@_dim("breadth")
def _b_ashare_breadth(ctx):
    import akshare as ak
    found = None
    for i in range(10):
        d = dt.date.today() - dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            pool = ak.stock_zt_pool_em(date=d.strftime("%Y%m%d"))
        except Exception:
            continue
        if pool is not None and not pool.empty:
            found = (d, pool)
            break
    if found is None:
        raise ValueError("无近期涨停池数据")
    day, up_pool = found
    down_pool = ak.stock_zt_pool_dtgc_em(date=day.strftime("%Y%m%d"))
    up = len(up_pool)
    down = len(down_pool) if down_pool is not None else 0
    ratio = up / max(up + down, 1)
    return {
        "dim": "breadth", "label": DIM_LABELS["breadth"],
        "value": f"涨停 {up} / 跌停 {down}",
        "score": round((ratio - 0.5) * 200.0, 1),
        "source": "akshare 东财涨停/跌停池",
        "as_of": day.strftime("%Y-%m-%d"),
        "note": "涨停/(涨停+跌停) 偏离 50%",
    }


@_dim("fund")
def _b_ashare_fund(ctx):
    import akshare as ak
    df = ctx.df("hsgt", lambda: ak.stock_hsgt_hist_em(symbol="北向资金"), "北向资金")
    net = pd.to_numeric(df["当日成交净买额"], errors="coerce").dropna()
    if net.empty:
        raise ValueError("北向资金: 无净买额")
    last3 = float(net.tail(3).sum())
    as_of = str(pd.to_datetime(df["日期"], errors="coerce").iloc[-1].date())
    return {
        "dim": "fund", "label": DIM_LABELS["fund"],
        "value": f"北向 3日累计净买 {last3:+.0f}亿",
        "score": round(_clip(last3 / 100.0 * 100.0), 1),
        "source": "akshare 北向资金(沪深股通)",
        "as_of": as_of,
        "note": "3日累计净买额/100亿映射",
    }


@_dim("margin")
def _b_ashare_margin(ctx):
    import akshare as ak
    end = dt.date.today()
    start = end - dt.timedelta(days=15)
    df = ctx.df("margin", lambda: ak.stock_margin_sse(
        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")), "两融")
    bal = pd.to_numeric(df["融资余额"], errors="coerce").dropna()
    if len(bal) < 6:
        raise ValueError("两融历史不足")
    chg = bal.iloc[-1] / bal.iloc[-6] - 1.0
    as_of = str(pd.to_datetime(df["信用交易日期"], errors="coerce").iloc[-1].date())
    return {
        "dim": "margin", "label": DIM_LABELS["margin"],
        "value": f"融资余额 5日 {chg * 100:+.2f}%",
        "score": round(_clip(chg * 100.0 * 20.0), 1),
        "source": "上交所两融(akshare)",
        "as_of": as_of,
        "note": "融资余额5日变化率 ×20 映射",
    }


@_dim("panic")
def _b_ashare_panic(ctx):
    import akshare as ak
    df = ctx.df("qvix", lambda: ak.index_option_50etf_qvix(), "QVIX")
    close = pd.Series(
        pd.to_numeric(df["close"], errors="coerce").values,
        index=pd.to_datetime(df["date"], errors="coerce"),
    ).dropna().sort_index()
    cur = float(close.iloc[-1])
    pct = _pct_rank(close.tail(252), cur)
    return {
        "dim": "panic", "label": DIM_LABELS["panic"],
        "value": f"QVIX 中国波指 {cur:.2f}",
        "score": round((0.5 - pct) * 200.0, 1),
        "source": "上证50ETF期权中国波指(akshare)",
        "as_of": str(close.index[-1].date()),
        "note": "QVIX 历史分位反向（高波=恐慌）",
    }


@_dim("momentum")
def _b_us_momentum(ctx):
    import akshare as ak
    close = ctx.close("inx", lambda: ak.index_us_stock_sina(symbol=".INX"), "标普500")
    sc, r, d = _momentum(close)
    return {
        "dim": "momentum", "label": DIM_LABELS["momentum"],
        "value": f"标普500 20日 {r:+.1f}%",
        "score": round(sc, 1),
        "source": "Sina 标普500(akshare)",
        "as_of": d,
        "note": "20日涨跌幅历史分位",
    }


@_dim("panic")
def _b_us_panic(ctx):
    close = ctx.series("vix", _cboe_vix_close, "CBOE VIX")
    cur = float(close.iloc[-1])
    pct = _pct_rank(close.tail(252), cur)
    return {
        "dim": "panic", "label": DIM_LABELS["panic"],
        "value": f"VIX {cur:.2f}",
        "score": round((0.5 - pct) * 200.0, 1),
        "source": "CBOE VIX_History.csv(官方)",
        "as_of": str(close.index[-1].date()),
        "note": "VIX 历史分位反向（高波=恐慌）",
    }


@_dim("momentum")
def _b_hk_momentum(ctx):
    import akshare as ak
    close = ctx.close("hsi", lambda: ak.stock_hk_index_daily_sina(symbol="HSI"), "恒指")
    sc, r, d = _momentum(close)
    return {
        "dim": "momentum", "label": DIM_LABELS["momentum"],
        "value": f"恒生指数 20日 {r:+.1f}%",
        "score": round(sc, 1),
        "source": "Sina 恒指(akshare)",
        "as_of": d,
        "note": "20日涨跌幅历史分位",
    }


@_dim("fund")
def _b_hk_fund(ctx):
    import akshare as ak
    sh = ctx.df("hsgt_sh", lambda: ak.stock_hsgt_hist_em(symbol="港股通沪"), "港股通沪")
    sz = ctx.df("hsgt_sz", lambda: ak.stock_hsgt_hist_em(symbol="港股通深"), "港股通深")
    n1 = pd.to_numeric(sh["当日成交净买额"], errors="coerce").dropna()
    n2 = pd.to_numeric(sz["当日成交净买额"], errors="coerce").dropna()
    if n1.empty and n2.empty:
        raise ValueError("南向资金: 无净买额")
    last3 = float(n1.tail(3).sum() + n2.tail(3).sum())
    as_of = str(pd.to_datetime(sh["日期"], errors="coerce").iloc[-1].date())
    return {
        "dim": "fund", "label": DIM_LABELS["fund"],
        "value": f"南向 3日累计净买 {last3:+.0f}亿",
        "score": round(_clip(last3 / 80.0 * 100.0), 1),
        "source": "akshare 南向资金(港股通沪+深)",
        "as_of": as_of,
        "note": "3日累计净买额/80亿映射",
    }


@_dim("panic")
def _b_hk_panic(ctx):
    import akshare as ak
    close = ctx.close("hsi", lambda: ak.stock_hk_index_daily_sina(symbol="HSI"), "恒指")
    ret = close.pct_change().dropna()
    vol20 = ret.rolling(20).std().dropna() * (252 ** 0.5) * 100.0
    cur = float(vol20.iloc[-1])
    pct = _pct_rank(vol20.tail(252), cur)
    return {
        "dim": "panic", "label": DIM_LABELS["panic"],
        "value": f"恒指 20日实现波动 {cur:.1f}%",
        "score": round((0.5 - pct) * 200.0, 1),
        "source": "恒指日线派生(akshare)",
        "as_of": str(close.index[-1].date()),
        "note": "实现波动率历史分位反向",
    }


@_dim("news")
def _b_news(ctx):
    kw = _EM_NEWS_KEYWORD[ctx.market]
    titles = _fetch_headlines(kw, limit=15)
    if not titles:
        return {
            "dim": "news", "label": DIM_LABELS["news"], "value": None,
            "score": None, "source": "东财新闻搜索", "as_of": None,
            "note": "诚实标注：市场新闻抓取失败/无可用新闻",
        }
    pos = sum(1 for t in titles if any(k in t for k in _POS_KW))
    neg = sum(1 for t in titles if any(k in t for k in _NEG_KW))
    return {
        "dim": "news", "label": DIM_LABELS["news"],
        "value": f"{len(titles)}条新闻 · 积极{pos} / 消极{neg}",
        "score": round(_clip((pos - neg) / len(titles) * 200.0), 1),
        "source": "东财新闻搜索(关键词词频)",
        "as_of": dt.date.today().isoformat(),
        "note": "标题关键词(积极-消极)/条数 ×200",
    }


_BUILDERS = {
    "a_share": [_b_ashare_momentum, _b_ashare_breadth, _b_ashare_fund,
                _b_ashare_margin, _b_ashare_panic, _b_news],
    "us": [_b_us_momentum, _b_us_panic, _b_news],
    "hk": [_b_hk_momentum, _b_hk_fund, _b_hk_panic, _b_news],
}


class _Ctx:
    """单次 compute 内的数据抓取缓存（避免重复拉同一序列）。"""

    def __init__(self, market: str):
        self.market = market
        self._cache: dict[str, object] = {}

    def close(self, key: str, fn, name: str) -> pd.Series:
        if key not in self._cache:
            self._cache[key] = _fetch_close(fn, name)
        return self._cache[key]

    def series(self, key: str, fn, name: str) -> pd.Series:
        if key not in self._cache:
            try:
                self._cache[key] = fn()
            except Exception as e:
                raise ValueError(f"{name}: {e}") from e
            if self._cache[key] is None or self._cache[key].empty:
                raise ValueError(f"{name}: 无数据")
        return self._cache[key]

    def df(self, key: str, fn, name: str) -> pd.DataFrame:
        if key not in self._cache:
            try:
                self._cache[key] = fn()
            except Exception as e:
                raise ValueError(f"{name}: {e}") from e
            if self._cache[key] is None or self._cache[key].empty:
                raise ValueError(f"{name}: 空数据")
        return self._cache[key]


# ── 评分合成 ─────────────────────────────────────────────

def _compose(market: str, dims: list[dict]) -> tuple[float | None, str, list[str], str | None]:
    weights = WEIGHTS[market]
    avail = [d for d in dims if d.get("score") is not None]
    total_w = sum(weights.get(d["dim"], 0.0) for d in avail)
    if not avail or total_w <= 0:
        return None, label_for(None), [], None
    score = round(_clip(sum(weights[d["dim"]] * d["score"] for d in avail) / total_w), 1)
    sources = sorted({d.get("source", "") for d in avail if d.get("source")})
    # 顶层 as_of 取非舆情信号的最新市场数据日期（舆情为当日快照，不纳入观测时点）
    as_of = max((d.get("as_of") for d in avail
                 if d.get("as_of") and d.get("dim") != "news"), default=None)
    if as_of is None:
        as_of = max((d.get("as_of") for d in avail if d.get("as_of")), default=None)
    return score, label_for(score), sources, as_of


def compute_market_sentiment(market: str, llm: bool = True) -> dict:
    """计算单一市场情绪。llm=True 时追加 LLM 解读（LLM 未就绪/失败 → None）。

    返回 {market, market_label, score, label, breakdown, llm_interpretation,
          sources, as_of, generated_at}
    """
    market = market.strip().lower()
    if market not in MARKETS:
        raise ValueError(f"未知市场: {market}")

    ctx = _Ctx(market)
    dims = []
    for builder in _BUILDERS[market]:
        dim = getattr(builder, "_dim", "momentum")
        try:
            dims.append(builder(ctx))
        except Exception as e:
            logger.warning("market_sentiment %s 信号 %s 失败: %s",
                           market, builder.__name__, e)
            dims.append({
                "dim": dim,
                "label": DIM_LABELS[dim],
                "value": None, "score": None,
                "source": "（信号获取失败）", "as_of": None,
                "note": "诚实标注：数据源获取失败",
            })

    score, label, sources, as_of = _compose(market, dims)
    result = {
        "market": market,
        "market_label": MARKETS[market],
        "score": score,
        "label": label,
        "breakdown": dims,
        "llm_interpretation": None,
        "sources": sources,
        "as_of": as_of,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if llm:
        result["llm_interpretation"] = llm_interpret(market, score, label, dims)
    return result


def llm_interpret(market: str, score: float | None, label: str,
                  dims: list[dict]) -> str | None:
    """基于真实数值生成 2-3 句解读；LLM 未就绪或调用失败 → None（不阻塞）。"""
    if not get_settings().check_llm_ready():
        return None
    lines = [f"市场={MARKETS[market]} 总分={score} 标签={label}"]
    for d in dims:
        if d.get("score") is not None:
            lines.append(f"{d['label']}: {d.get('value')} (分={d['score']})")
        else:
            lines.append(f"{d['label']}: 数据缺失")
    try:
        from app.analysis.llm import analysis_llm
        text = analysis_llm.chat(
            [{"role": "user",
              "content": "请基于以下真实数值生成 2-3 句市场情绪解读，"
                          "只陈述给定数据，不要编造任何新数字：\n"
                          + "\n".join(lines)}],
            system_prompt="你是市场情绪解读助手。只基于给定的真实数据解读，"
                          "禁止编造或引用外部未知数据。输出 2-3 句中文。",
            max_tokens=300,
        )
        return text.strip() or None
    except Exception as e:
        logger.warning("market_sentiment LLM 解读失败: %s", e)
        return None
