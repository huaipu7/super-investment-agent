"""宏观内容入库 —— 宏观新闻（东财全球财经快讯）+ 宏观数据快照（12 个核心指标）。

数据源说明（本机 akshare 1.18.60 实测）：
- 宏观新闻：`stock_info_global_em`（东财全球财经快讯，实时，含标题/摘要/时间/链接）。
  news_cctv 数据停留在 2024-04-24，news_economic_baidu 是欧美经济日历（非中国宏观新闻），均不可用。
- 宏观数据：12 个国内源指标，除 LPR/社融取 tail（最新在末行）外均取 head（最新在首行）；
  rmb（汇率）/urban_unemployment（失业率）接口在本机报错、工业增加值国内源缺失，暂不纳入（如实缺省）。
"""
from __future__ import annotations

import datetime
import warnings

from app.rag.chunking import chunk_text
from app.rag.store import doc_id, get_knowledge_store
from app.utils.logger import logger

MACRO_NEWS_LIMIT = 100  # 东财全球快讯每次入库条数

_INDICATORS = [
    # name, akshare 接口, 取最新端(head/tail), 文本格式化
    ("居民消费价格指数 CPI", "macro_china_cpi", "head",
     lambda r: f"{r['月份']} 全国同比 {r['全国-同比增长']}%、环比 {r['全国-环比增长']}%"),
    ("工业生产者出厂价格指数 PPI", "macro_china_ppi", "head",
     lambda r: f"{r['月份']} 同比 {r['当月同比增长']}%"),
    ("国内生产总值 GDP", "macro_china_gdp", "head",
     lambda r: f"{r['季度']} 同比 {r['国内生产总值-同比增长']}%"),
    ("制造业采购经理指数 PMI", "macro_china_pmi", "head",
     lambda r: f"{r['月份']} 制造业指数 {r['制造业-指数']}、非制造业指数 {r['非制造业-指数']}"),
    ("社会消费品零售总额", "macro_china_consumer_goods_retail", "head",
     lambda r: f"{r['月份']} 同比 {r['同比增长']}%、累计 {r['累计']} 亿元"),
    ("进出口总额", "macro_china_hgjck", "head",
     lambda r: f"{r['月份']} 出口同比 {r['当月出口额-同比增长']}%、进口同比 {r['当月进口额-同比增长']}%"),
    ("货币供应 M2/M1", "macro_china_money_supply", "head",
     lambda r: f"{r['月份']} M2 同比 {r['货币和准货币(M2)-同比增长']}%、M1 同比 {r['货币(M1)-同比增长']}%"),
    ("贷款市场报价利率 LPR", "macro_china_lpr", "tail",
     lambda r: f"{r['TRADE_DATE']} 1年期 {r['LPR1Y']}%、5年期 {r['LPR5Y']}%"),
    ("社会融资规模", "macro_china_shrzgm", "tail",
     lambda r: f"{r['月份']} 增量 {r['社会融资规模增量']} 亿元、其中人民币贷款 {r['其中-人民币贷款']} 亿元"),
    ("固定资产投资", "macro_china_gdzctz", "head",
     lambda r: f"{r['月份']} 同比 {r['同比增长']}%、自年初累计 {r['自年初累计']} 亿元"),
    ("存款准备金率", "macro_china_reserve_requirement_ratio", "head",
     lambda r: f"{r['公布时间']} 公布、大型金融机构调整后 {r['大型金融机构-调整后']}%"),
    ("企业商品价格指数", "macro_china_qyspjg", "head",
     lambda r: f"{r['月份']} 总指数同比 {r['总指数-同比增长']}%"),
]


def _fetch_macro_news() -> list[dict]:
    """东财全球财经快讯 → 新闻 docs（id 按标题+时间，重复拉取覆盖不膨胀）。"""
    import akshare as ak

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = ak.stock_info_global_em()
    if df is None or df.empty:
        return []
    docs: list[dict] = []
    for _, row in df.head(MACRO_NEWS_LIMIT).iterrows():
        title = str(row.get("标题", "")).strip()
        summary = str(row.get("摘要", "")).strip()[:300]
        published = str(row.get("发布时间", ""))
        if not title:
            continue
        content = f"{title}\n{summary}" if summary else title
        docs.append(
            {
                "id": doc_id("宏观新闻", title, published),
                "page_content": content,
                "metadata": {
                    "kind": "宏观新闻",
                    "ticker": "",
                    "date": published[:10],
                    "source": "东财全球财经快讯",
                    "section": "宏观新闻",
                    "url": str(row.get("链接", "")),
                },
            }
        )
    return docs


def _fetch_macro_data() -> list[dict]:
    """12 个核心指标最新值 → 快照 docs（id 按指标名，每次覆盖该指标最新快照）。"""
    import akshare as ak

    today = datetime.date.today().isoformat()
    docs: list[dict] = []
    for name, api, end, fmt in _INDICATORS:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                df = getattr(ak, api)()
            if df is None or df.empty:
                logger.warning("宏观指标 %s 返回空", api)
                continue
            row = df.head(1) if end == "head" else df.tail(1)
            text = f"{name}：{fmt(row.iloc[0])}"
            docs.append(
                {
                    "id": doc_id("宏观数据", name),
                    "page_content": text,
                    "metadata": {
                        "kind": "宏观数据",
                        "ticker": "",
                        "date": today,
                        "source": f"akshare {api}",
                        "section": "宏观数据",
                        "url": "",
                    },
                }
            )
        except Exception as e:
            logger.warning("宏观指标 %s 拉取失败: %s", api, e)
    return docs


def index_macro() -> dict:
    """宏观新闻 + 宏观数据快照入库，返回 {macro_news, macro_data}。"""
    store = get_knowledge_store()
    result = {"macro_news": 0, "macro_data": 0}
    try:
        news = _fetch_macro_news()
        result["macro_news"] = store.upsert(news)
    except Exception as e:
        logger.warning("宏观新闻采集失败: %s", e)
    try:
        data = _fetch_macro_data()
        result["macro_data"] = store.upsert(data)
    except Exception as e:
        logger.warning("宏观数据采集失败: %s", e)
    return result
