"""分析管线编排器 —— 迁移自 Research-Agent(backend/analysis/agent.py)。

协调完整分析工作流：财务分析 → DCF 估值 → SOTP 分部估值 → 多市场分析 →
事实核查 → 地缘政治 → 供应链 → 竞品对比 → SWOT。输出统一结构化结果。

与源版的差异：数据获取统一走 app.data.router.MarketData（A股/港股/美股自动路由），
LLM 调用走 app.analysis.llm.analysis_llm（统一 DeepSeek 客户端）。
"""
from __future__ import annotations

import time
from typing import Optional

import pandas as pd

from app.analysis.financial import FinancialAnalyzer
from app.analysis.market import MarketAnalyzer
from app.analysis.competitive import CompetitiveAnalyzer
from app.analysis.swot import SwotAnalyzer
from app.analysis.validator import FactChecker
from app.analysis.geopolitical import GeopoliticalAnalyzer
from app.analysis.supply_chain import SupplyChainAnalyzer
from app.analysis.valuation import DCFModel
from app.analysis.segment import SegmentAnalyzer
from app.analysis.news import NewsAnalyzer
from app.analysis.price_series import build_price_history
from app.analysis.pricing import build_pricing
from app.analysis.meeting import MeetingAnalyzer
from app.analysis.industry import (
    IndustryClass,
    SectorAdapter,
    classify_industry,
    get_market_config,
)
from app.config import get_settings
from app.data.cleaning.pipeline import CleaningPipeline
from app.data.router import is_ashare, is_hk, get_market_data
from app.data.sources.akshare_us_source import is_us_ticker
from app.trust.completeness import check_completeness
from app.trust.sanitize import sanitize_result
from app.utils.logger import logger


def _refresh_relative_valuation_summary(valuation_result: dict) -> None:
    """用最新的 relative_valuation 重建摘要里的「相对估值」一项。

    竞品真实 PE 覆盖 industry_avg/premium 发生在估值分析**之后**，
    不重建摘要就会留下覆盖前的溢价，与正文表格口径自相矛盾。
    """
    summary = valuation_result.get("summary")
    rel = valuation_result.get("relative_valuation") or {}
    if not summary or not rel:
        return
    premiums = [f"{mk.upper()} {e['premium']:+.1%}"
                for mk, e in rel.items()
                if isinstance(e, dict) and e.get("premium") is not None]
    if not premiums:
        return
    fresh = "相对估值: " + "，".join(premiums)
    parts, replaced = [], False
    for part in summary.split("；"):
        if part.startswith("相对估值:"):
            parts.append(fresh)
            replaced = True
        else:
            parts.append(part)
    if not replaced:
        parts.append(fresh)
    valuation_result["summary"] = "；".join(parts)


# 插补明细写入产物的条数上限（防止极端缺失时产物膨胀）
_CLEANING_DETAIL_LIMIT = 50


class AnalysisOrchestrator:
    """分析编排器

    用法:
        orchestrator = AnalysisOrchestrator()
        result = orchestrator.run("AAPL")
    """

    def __init__(self):
        self.market_data = get_market_data()
        self.financial_analyzer = FinancialAnalyzer()
        self.market_analyzer = MarketAnalyzer()
        self.competitive_analyzer = CompetitiveAnalyzer()
        self.swot_analyzer = SwotAnalyzer()
        self.fact_checker = FactChecker()
        self.geopolitical_analyzer = GeopoliticalAnalyzer()
        self.supply_chain_analyzer = SupplyChainAnalyzer()
        self.dcf_model = DCFModel()
        self.segment_analyzer = SegmentAnalyzer()
        self.news_analyzer = NewsAnalyzer()
        self.meeting_analyzer = MeetingAnalyzer()

    def _cleaning_record(self) -> dict:
        """清洗阶段的插补/去重痕迹。

        以前 CleaningPipeline.report() 从没被调用过，插补记录收集完就丢了——
        财务比率里混进前向填充值，从产物上完全看不出来。
        """
        handler = getattr(self.cleaning_pipeline, "missing_handler", None)
        entries = list(getattr(getattr(handler, "record", None), "entries", []) or [])

        imputed = [
            {
                "statement": e.get("statement", ""),
                # 索引来自 pandas Timestamp，去掉时间部分好让台账的 as_of 干净
                "period": str(e.get("period", "")).split(" ")[0],
                "metric": e.get("metric", ""),
                "method": e.get("method", ""),
                "value": float(e["value"]),
                "imputed": True,
                "imputed_reason": f"{e.get('method') or '插补'} 插补",
            }
            for e in entries[:_CLEANING_DETAIL_LIMIT]
            if isinstance(e.get("value"), (int, float))
        ]

        out = {
            "imputed_count": len(entries),
            "imputed_metrics": imputed,
            "note": "imputed_metrics 为被填补的单元格原值（用于对照组），非原始财报数",
        }
        if len(entries) > _CLEANING_DETAIL_LIMIT:
            out["truncated"] = True
            out["note"] += f"；明细仅列前 {_CLEANING_DETAIL_LIMIT} 条"
        return out

    def _fetch_competitor_revenues(self, competitors: list[dict]) -> list[dict]:
        """获取竞品企业的营收数据（用于市场集中度计算）"""
        if not competitors:
            return []

        # 常见货币兑美元近似汇率（用于归一化）
        FX_RATES: dict[str, float] = {
            "USD": 1.0, "KRW": 0.00072, "JPY": 0.0067, "CNY": 0.14,
            "HKD": 0.128, "EUR": 1.08, "GBP": 1.27, "TWD": 0.031,
            "INR": 0.012, "BRL": 0.19, "CAD": 0.73, "AUD": 0.66,
            "SGD": 0.75, "SEK": 0.094, "CHF": 1.12, "NOK": 0.093,
            "PHP": 0.017, "MYR": 0.21, "VND": 0.000039, "THB": 0.028,
            "IDR": 0.000062, "MXN": 0.050, "ZAR": 0.053,
        }

        def _valid_ticker(t: str | None) -> bool:
            if not t:
                return False
            t = t.strip().upper()
            return t not in ("N/A", "NULL", "NONE", "", "TBD")

        def _to_usd(amount: float, currency: str) -> float:
            rate = FX_RATES.get(currency.upper(), 1.0)
            return amount * rate

        yf_src = self.market_data.yfinance
        md = self.market_data
        competitor_data = []
        for comp in competitors[:15]:
            name = comp.get("name", "")
            if not name:
                continue

            ticker = None
            raw_ticker = comp.get("ticker")
            if _valid_ticker(raw_ticker):
                ticker = raw_ticker.strip()
            if not ticker:
                ticker = CompetitiveAnalyzer.resolve_ticker(name)

            if not ticker:
                try:
                    results = yf_src.search_tickers(name, top_k=1)
                    if results:
                        ticker = results[0].get("ticker")
                except Exception:
                    pass

            if not ticker:
                continue

            try:
                # 走 router 而非裸 yfinance：美股竞品才能经东财拿到财报（Yahoo 已 403）
                fin = md.get_financials(ticker)
                rev = fin.get("revenue")
                cinfo = md.fetch_company_info(ticker)
                # 营收的币种必须取**报表记账币种**（fin.currency，ADR 常见 CNY/KRW），
                # 不能取 cinfo.currency（那是对外报价的挂牌币种，美股恒为 USD）
                currency = fin.get("currency") or cinfo.get("currency", "USD")
                # 真实 trailing PE（yfinance，免费源）与市值，供行业 PE 中位数估算
                pe = fin.get("pe_ratio") or fin.get("forward_pe")
                market_cap = cinfo.get("market_cap")
                if rev and rev > 0:
                    rev_usd = _to_usd(float(rev), currency)
                    competitor_data.append({
                        "name": name,
                        "revenue": rev_usd,
                        "period": "latest",
                        "currency_original": currency,
                        "pe": pe,
                        "market_cap": market_cap,
                    })
                    logger.debug("竞品 %s (%s) = %.0f %s -> %.0f USD, PE=%s",
                                 name, ticker, float(rev), currency, rev_usd, pe)
                else:
                    logger.debug("竞品 %s (%s) 无营收数据", name, ticker)
            except Exception as e:
                logger.debug("获取竞品 %s (%s) 营收失败: %s", name, ticker, e)

        logger.info("成功获取 %d/%d 家竞品营收数据（已归一化至 USD）",
                    len(competitor_data), len(competitors))
        return competitor_data

    def run(
        self,
        ticker: str,
        period: str = "annual",
        retriever=None,
        progress_cb=None,
    ) -> dict:
        """对指定 ticker 执行完整分析管线

        Args:
            ticker: 股票代码（支持 A 股 / 港股 / 美股）
            period: "annual" | "quarterly"
            retriever: 可选的 ReportRetriever，用于 RAG 增强 SWOT
            progress_cb: 可选回调 progress_cb(dict)，dict 形如
                         {"step","label","message","pct"}，每步结束触发一次

        Returns:
            {
                "ticker": str,
                "company_info": dict,
                "financial_analysis": dict,
                "valuation_analysis": dict,
                "sotp_analysis": dict,
                "market_analysis": dict,
                "competitive_analysis": dict,
                "swot_analysis": dict,
                "fact_checks": dict,
                "geopolitical_analysis": dict,
                "supply_chain_analysis": dict,
                "news_analysis": dict,
                "meeting_analysis": dict,
                "knowledge": dict,
                "trade_plan": dict,
                "sector_adapter": dict,
                "pipeline_time": float,
            }
        """
        logger.info("=" * 50)
        logger.info("开始分析: %s", ticker)
        logger.info("=" * 50)
        start = time.time()

        def _emit(step: str, label: str, message: str, pct: int):
            if progress_cb is not None:
                try:
                    progress_cb({"step": step, "label": label, "message": message, "pct": pct})
                except Exception:
                    pass

        md = self.market_data
        ashare = is_ashare(ticker)
        if ashare:
            cleaning_source = "akshare"
        elif is_us_ticker(ticker):
            cleaning_source = "akshare_us"
        else:
            cleaning_source = "yfinance"

        # ── 1. 数据获取 ──
        logger.info("[1/7] 获取财务数据...")

        company_info = md.fetch_company_info(ticker)
        company_info["ticker"] = ticker

        raw_financials = md.fetch_financial_statements(ticker, period=period)

        # 清洗
        self.cleaning_pipeline = CleaningPipeline(source=cleaning_source)
        cleaned = self.cleaning_pipeline.run(raw_financials)

        # 获取股价数据（用于 PE/PB/PS 等市值比率，取 5 年）
        try:
            price_data = md.get_history(ticker, "5y")
        except Exception as e:
            logger.warning("获取历史行情失败: %s", e)
            price_data = cleaned.get("income", __import__("pandas").DataFrame()).iloc[:0].copy()

        # 基准指数（供 beta/ERP 估算，免费源真实数据；失败不阻断主流程）
        try:
            benchmark_df = md.get_benchmark_history(ticker, "5y")
        except Exception:
            benchmark_df = None

        # 估算参数 Rf/Beta/ERP —— 缺失显式标注来源与置信度，不静默编造
        param_estimates: dict = {}
        # 提示词里的宏观利率必须与产物里那条事实同源；拿不到真实利率时传空串，
        # 由 build_system_prompt 注入「禁止给具体数值」，而不是让模型自己编一个。
        market = "cn" if ashare else ("hk" if is_hk(ticker) else "us")
        macro_line = ""
        try:
            from app.analysis import estimates as est_mod
            from app.analysis.master_prompt import macro_context

            rf_est = est_mod.get_risk_free_rate(market=market)
            beta_est = est_mod.compute_beta(price_data, benchmark_df)
            erp_est = est_mod.estimate_erp(benchmark_df, rf_est.get("value"), fallback_erp=0.05)
            param_estimates = {"risk_free_rate": rf_est, "beta": beta_est, "erp": erp_est}
            macro_line = macro_context(rf_est, market)
        except Exception as e:
            logger.warning("估算参数计算失败: %s", e)

        logger.info("数据获取完成: %s, %d 期间, %d 股价数据点",
                    company_info.get("name", ticker),
                    max((v.shape[0] for v in cleaned.values() if not v.empty), default=0),
                    price_data.shape[0] if not price_data.empty else 0)
        _emit("1/7", "数据获取", f"已获取 {company_info.get('name', ticker)} 财务与行情数据", 8)

        # ── 1.5 知识库检索（Phase 10 RAG）──────────────────────────
        knowledge = {"available": False, "reason": "知识库未启用", "snippets": []}
        rag_retriever = retriever
        if rag_retriever is None and get_settings().RAG_ENABLED:
            try:
                from app.rag.retriever import get_knowledge_retriever

                rag_retriever = get_knowledge_retriever()
                if not rag_retriever.available:
                    rag_retriever = None
            except Exception as e:
                logger.warning("知识库检索器初始化失败: %s", e)
                rag_retriever = None

        if rag_retriever is not None:
            _emit("1.5/7", "知识库检索", "检索历史研报/宏观数据/相关新闻", 14)
            try:
                industry = company_info.get("industry", "")
                docs = rag_retriever.query_for_ticker(ticker, industry)
                if not docs:
                    knowledge = {"available": False, "reason": "知识库为空", "snippets": []}
                    logger.info("知识库检索: 无结果")
                else:
                    knowledge = {
                        "available": True,
                        "source": (f"RAG 知识库 (Chroma + "
                                   f"{rag_retriever._store.backend_label})"),
                        "count": len(docs),
                        "snippets": [
                            {"content": d.page_content, "metadata": d.metadata} for d in docs
                        ],
                    }
                    logger.info("知识库检索: %d 条片段", len(docs))
            except Exception as e:
                logger.warning("知识库检索失败: %s", e)
                knowledge = {"available": False, "reason": "知识库检索失败", "snippets": []}

        # ── 2. 财务分析 ──
        logger.info("[2/7] 执行财务分析...")

        industry_cls = classify_industry(
            industry=company_info.get("industry", ""),
            sector=company_info.get("sector", ""),
            business_desc=company_info.get("summary", ""),
        )
        financial_result = self.financial_analyzer.analyze(
            financials=cleaned,
            company_info=company_info,
            price_data=price_data,
            industry_cls=industry_cls,
        )
        # 插补痕迹：清洗阶段填过的值必须能追溯，否则财务比率里混着插补值却无人知晓
        financial_result["cleaning"] = self._cleaning_record()
        # ── 2.1 财报季度化（Phase 4）：A 股近 4 季度营收/净利趋势 + 同比 ──
        # 美股季度财报未接入（本报告为年报口径），如实标注，不编造。
        if ashare:
            try:
                from app.analysis.financial import compute_quarterly_trend
                qt = compute_quarterly_trend(md.fetch_quarterly_income(ticker))
                financial_result["quarterly_trend"] = qt
            except Exception as e:
                financial_result["quarterly_trend"] = {
                    "available": False,
                    "reason": f"季度数据获取失败: {e}",
                    "periods": [],
                }
        else:
            financial_result["quarterly_trend"] = {
                "available": False,
                "reason": "美股季度财报未接入（本报告为年报口径）",
                "periods": [],
            }
        _emit("2/7", "财务分析", f"财务比率 {len(financial_result.get('ratios', {}))} 项", 16)

        # ── 2.5 估值分析（DCF + 相对估值） ──
        logger.info("[2.5/7] 执行估值分析...")
        valuation_result = self.dcf_model.analyze(
            ratios=financial_result.get("ratios", {}),
            growth_rates=financial_result.get("growth_rates", {}),
            raw_metrics=financial_result.get("raw_metrics", {}),
            company_info=company_info,
            industry_cls=industry_cls,
            current_price=financial_result.get("current_price"),
            estimates=param_estimates,
        )
        _emit("2.5/7", "估值分析", "DCF + 相对估值完成", 24)

        # ── 2.75 SOTP 分部估值 ──
        logger.info("[2.75/7] 执行 SOTP 分部估值...")
        try:
            company_name_step = company_info.get("name", ticker)
            segments = self.segment_analyzer.identify_segments(company_info, macro_line=macro_line)
            raw_metrics = financial_result.get("raw_metrics", {})
            ni_series = raw_metrics.get("NET_INCOME", [])
            total_ni = ni_series[-1]["value"] if ni_series and ni_series[-1]["value"] is not None else None
            shares = company_info.get("shares_outstanding")
            current_px = financial_result.get("current_price")
            sotp_result = self.segment_analyzer.compute_sotp(
                segments=segments,
                total_net_income=total_ni,
                shares_outstanding=shares,
                current_price=current_px,
            )
            logger.info("SOTP 估值完成: 区间 $%.1f ~ $%.1f",
                        sotp_result.get("per_share_low") or 0,
                        sotp_result.get("per_share_high") or 0)
        except Exception as e:
            logger.warning("SOTP 分部估值失败: %s", e)
            sotp_result = {"segments": [], "sotp_value_low": None, "sotp_value_high": None,
                           "per_share_low": None, "per_share_high": None}
        _emit("2.75/7", "SOTP 分部估值", "分部加总估值完成", 30)

        # ── 3. 多市场分析 ──
        logger.info("[3/7] 执行多市场分析...")
        company_name = company_info.get("name", "")
        industry = company_info.get("industry", "")
        business_summary = company_info.get("summary", "")

        country = company_info.get("country", "")
        raw_market_config = get_market_config(country)
        market_configs = [(label, mk_key) for mk_key, label in raw_market_config]

        markets_data = {}
        all_competitors_for_comparison = None
        company_revenue = None

        for market_label, market_key in market_configs:
            logger.info("  识别 %s 竞品...", market_label)
            competitors = self.competitive_analyzer.identify_competitors(
                company_name=company_name,
                industry=industry,
                business_summary=business_summary,
                market=market_label,
                macro_line=macro_line,
            )

            competitor_data = self._fetch_competitor_revenues(competitors)
            competitor_names = [c.get("name", "") for c in competitor_data]

            market_result = self.market_analyzer.analyze(
                financials=cleaned,
                company_info=company_info,
                competitor_data=competitor_data,
                industry_cls=industry_cls,
            )

            markets_data[market_key] = {
                "competitors": competitors,
                "competitor_data": competitor_data,
                "market_share": market_result.get("market_share"),
                "industry_concentration": market_result.get("industry_concentration"),
                "tam_estimate": market_result.get("tam_estimate"),
                "company": market_result.get("company", {}),
            }

            logger.info("  %s: 识别 %d 家竞品, 获取 %d 家营收数据",
                        market_label, len(competitors), len(competitor_data))

            if market_key == "global":
                all_competitors_for_comparison = competitors
                company_revenue = market_result.get("company", {}).get("revenue")

        global_market = markets_data.get("global", {})
        market_result = {
            "company": global_market.get("company", {}),
            "competitors": global_market.get("competitors", []),
            "market_share": global_market.get("market_share"),
            "industry_concentration": global_market.get("industry_concentration"),
            "tam_estimate": global_market.get("tam_estimate"),
            "markets": markets_data,
        }
        _emit("3/7", "多市场分析",
              f"识别 {sum(len(m.get('competitors', [])) for m in markets_data.values())} 家竞品", 38)

        # ── 3.25 新闻舆情分析（Phase 9）──
        logger.info("[3.25/7] 执行新闻舆情分析...")
        try:
            news_data = md.get_news(ticker, limit=20)
            news_analysis = self.news_analyzer.analyze(
                company_name=company_name,
                articles=news_data.get("articles", []),
                briefings=news_data.get("briefings", []),
                industry=industry,
                source=("东财新闻 + 巨潮公告" if ashare
                        else ("东财美股（暂无新闻接口）" if is_us_ticker(ticker)
                              else "yfinance 新闻")),
            )
            logger.info("  新闻舆情: %d 篇新闻 / %d 条公告, 情绪=%s",
                        news_analysis.get("article_count", 0),
                        news_analysis.get("briefing_count", 0),
                        news_analysis.get("sentiment", "N/A"))
        except Exception as e:
            logger.warning("新闻舆情分析失败: %s", e)
            news_analysis = {
                "available": False, "reason": "新闻获取失败", "source": "",
                "article_count": 0, "briefing_count": 0, "articles": [], "briefings": [],
                "sentiment": None, "sentiment_score": None,
                "key_drivers": [], "risks_opportunities": [], "summary": "",
            }
        _emit("3.25/7", "新闻舆情",
              f"{news_analysis.get('article_count', 0)} 篇新闻, 情绪 {news_analysis.get('sentiment') or '—'}",
              45)

        # ── 3.375 股东大会分析（Phase 9）──
        logger.info("[3.375/7] 执行股东大会分析...")
        try:
            meeting_analysis = self.meeting_analyzer.analyze(
                company_name=company_name, meetings=md.get_meetings(ticker)
            )
            logger.info("  股东大会: available=%s, %d 场",
                        meeting_analysis.get("available"), meeting_analysis.get("meeting_count", 0))
        except Exception as e:
            logger.warning("股东大会分析失败: %s", e)
            meeting_analysis = {
                "available": False, "reason": "股东大会数据获取失败", "source": "",
                "meeting_count": 0, "meetings": [], "key_proposals": [],
                "governance_signals": "", "governance_risks": [], "summary": "",
            }
        _emit("3.375/7", "股东大会",
              f"{meeting_analysis.get('meeting_count', 0)} 场股东大会", 50)

        # ── 3.5 事实核查 ──
        competitors_for_check = all_competitors_for_comparison or []
        fact_checks = self.fact_checker.run(
            financial_analysis=financial_result,
            market_analysis=market_result,
            company_info=company_info,
            competitors=competitors_for_check,
        )

        if fact_checks.get("corrected_competitors"):
            corrected = fact_checks["corrected_competitors"]
            if len(corrected) == len(competitors_for_check):
                all_competitors_for_comparison = corrected
                for mk_key in markets_data:
                    old = markets_data[mk_key].get("competitors", [])
                    if len(old) == len(corrected):
                        markets_data[mk_key]["competitors"] = corrected
                market_result["competitors"] = corrected

        for w in fact_checks.get("warnings", []):
            logger.warning("事实核查: %s", w)
        _emit("3.5/7", "事实核查", f"{len(fact_checks.get('warnings', []))} 条预警", 58)

        # ── 4. 地缘政治分析 ──
        logger.info("[4/7] 执行地缘政治分析...")
        tariff_sensitivity = self.geopolitical_analyzer.analyze_tariff_sensitivity(
            financial_analysis=financial_result,
            company_info=company_info,
            market_analysis=market_result,
        )
        geopolitical_events = self.geopolitical_analyzer.analyze_geopolitical_events(
            company_name=company_name,
            industry=industry,
            business_summary=business_summary,
            macro_line=macro_line,
        )
        geopolitical_result = {
            "tariff_sensitivity": tariff_sensitivity,
            "geopolitical_events": geopolitical_events,
        }
        if tariff_sensitivity.get("applicable"):
            logger.info("  关税敏感性: 毛利率影响 %.1fpp", tariff_sensitivity["gross_margin_impact"] * 100)
        logger.info("  地缘事件: %d 个, 风险等级: %s",
                    len(geopolitical_events.get("events", [])),
                    geopolitical_events.get("risk_level", "N/A"))
        _emit("4/7", "地缘政治分析", f"风险等级 {geopolitical_events.get('risk_level', 'N/A')}", 65)

        # ── 4.5 供应链深度分析 ──
        logger.info("[4.5/7] 执行供应链深度分析...")
        try:
            supply_chain_result = self.supply_chain_analyzer.analyze(
                company_info=company_info,
                financial_analysis=financial_result,
                geopolitical_analysis=geopolitical_result,
                market_analysis=market_result,
                industry_cls=industry_cls,
            )
            logger.info("  供应链分析: %s", supply_chain_result.get("summary", ""))

            if valuation_result and "supply_chain_risk_premium" in valuation_result:
                existing = valuation_result.get("supply_chain_risk_premium", 0) or 0
                sc_premium = DCFModel._compute_supply_chain_risk_premium(supply_chain_result)
                if sc_premium > existing:
                    valuation_result["supply_chain_risk_premium"] = sc_premium
                    logger.info("  估值供应链风险溢价更新: +%.0fbp", sc_premium * 10000)
        except Exception as e:
            logger.warning("供应链深度分析失败: %s", e)
            supply_chain_result = {"summary": "供应链分析暂不可用"}
        _emit("4.5/7", "供应链分析", "供应链风险梳理完成", 72)

        # ── 5. 竞品分析 ──
        logger.info("[5/7] 执行竞品分析...")

        financial_summary = self.competitive_analyzer.build_financial_summary(
            financial_result, company_info
        )

        competitors_for_comp = all_competitors_for_comparison or []
        competitive_result = self.competitive_analyzer.generate_comparison(
            company_name=company_name,
            industry=industry,
            financial_summary=financial_summary,
            competitors=competitors_for_comp,
            industry_cls=industry_cls,
            macro_line=macro_line,
        )
        _emit("5/7", "竞品分析", "竞品对比完成", 80)

        # 竞品集合真实 PE 覆盖行业均值（免费源真实数据，n≥3 才 high 置信）
        try:
            from app.analysis.estimates import estimate_peer_pe
            global_comp = market_result.get("markets", {}).get("global", {}).get("competitor_data", [])
            peer_pe_est = estimate_peer_pe(global_comp) if global_comp else None
            if peer_pe_est and peer_pe_est.get("value"):
                rel = valuation_result.get("relative_valuation") or {}
                pe = rel.get("pe") if isinstance(rel.get("pe"), dict) else None
                if pe is not None:
                    pe["industry_avg"] = peer_pe_est["value"]
                    pe["industry_avg_source"] = peer_pe_est.get("source")
                    c = pe.get("company")
                    if c and c > 0:
                        pe["premium"] = (c - peer_pe_est["value"]) / peer_pe_est["value"]
                valuation_result.setdefault("estimates", {})["industry_pe"] = peer_pe_est
                _refresh_relative_valuation_summary(valuation_result)
                logger.info("行业 PE 用竞品真实中位数覆盖: %s", peer_pe_est["source"])
        except Exception as e:
            logger.warning("竞品 PE 覆盖失败: %s", e)

        # ── 6. SWOT 分析 ──
        logger.info("[6/7] 执行 SWOT 分析...")

        rag_context = ""
        if rag_retriever is not None:
            rag_query = f"{company_name} {industry} 财务状况 行业分析"
            rag_context = self.swot_analyzer.build_rag_context(rag_retriever, rag_query)
            if rag_context:
                logger.info("RAG 检索到相关文档片段")

        market_summary = self.swot_analyzer.build_market_summary(market_result)
        competitive_summary = competitive_result.get("summary", "")

        swot_result = self.swot_analyzer.analyze(
            company_name=company_name,
            industry=industry,
            business_summary=business_summary,
            financial_summary=financial_summary,
            market_summary=market_summary,
            competitive_summary=competitive_summary,
            rag_context=rag_context,
            financial_analysis=financial_result,
            industry_cls=industry_cls,
            macro_line=macro_line,
        )
        _emit("6/7", "SWOT 分析", f"评级 {swot_result.get('rating', 'N/A')}", 90)

        # ── 6.5 投资策略与交易信号（技术 + 估值混合，确定性，无 LLM）──
        logger.info("[6.5/7] 合成投资策略与交易信号...")
        try:
            from app.analysis.trade_plan import build_trade_plan
            trade_plan = build_trade_plan(
                price_data=price_data,
                valuation_result=valuation_result,
                swot_rating=swot_result.get("rating"),
                company_info=company_info,
                financial_result=financial_result,
            )
            logger.info("  交易信号: 评级=%s, 进场 %s~%s, 仓位=%s",
                        trade_plan.get("rating"),
                        trade_plan.get("entry_zone", [None, None])[0],
                        trade_plan.get("entry_zone", [None, None])[1],
                        trade_plan.get("position_hint"))
        except Exception as e:
            logger.warning("投资策略合成失败: %s", e)
            trade_plan = {"available": False, "reason": f"交易信号计算失败: {e}",
                          "rating": None, "position_hint": None,
                          "entry_zone": [None, None], "stop_loss": None,
                          "take_profit": None, "reasons": ["交易信号计算失败"]}
        _emit("6.5/7", "投资策略", f"评级 {trade_plan.get('rating') or '—'}", 94)

        elapsed = time.time() - start
        _emit("done", "分析完成", f"总耗时 {elapsed:.1f}s", 100)

        # 行情序列（供报告 K线+均线叠加图，取最近 300 交易日，列式真实数据）
        price_history = build_price_history(price_data)
        # 行情时点：现价对应哪个交易日（字符串/布尔，不产生台账事实）
        pricing = build_pricing(price_data)

        result = {
            "ticker": ticker,
            "company_info": company_info,
            "financial_analysis": financial_result,
            "valuation_analysis": valuation_result,
            "sotp_analysis": sotp_result,
            "market_analysis": market_result,
            "competitive_analysis": {
                "competitors": competitors_for_comp,
                "comparison": competitive_result,
            },
            "swot_analysis": swot_result,
            "fact_checks": fact_checks,
            "geopolitical_analysis": geopolitical_result,
            "supply_chain_analysis": supply_chain_result,
            "news_analysis": news_analysis,
            "meeting_analysis": meeting_analysis,
            "knowledge": knowledge,
            "trade_plan": trade_plan,
            "price_history": price_history,
            "pricing": pricing,
            "sector_adapter": SectorAdapter.get_config(industry_cls),
            "pipeline_time": round(elapsed, 2),
        }

        logger.info("=" * 50)
        logger.info("分析完成: %s (%.1fs)", ticker, elapsed)
        logger.info("  财务比率: %d 项", len(financial_result.get("ratios", {})))
        for mk_key in markets_data:
            mk_cfg_labels = dict(raw_market_config)
            label = mk_cfg_labels.get(mk_key, mk_key)
            logger.info("  %s 竞品: %d 家", label, len(markets_data[mk_key].get("competitors", [])))
        logger.info("  SWOT 评级: %s", swot_result.get("rating", "N/A"))
        logger.info("=" * 50)

        # 落地前净化：非有限值转 None 并留痕，保证产物是合法 JSON（见 app/trust/sanitize.py）
        result = sanitize_result(result)
        if result.get("data_quality", {}).get("non_finite"):
            logger.warning("产物含 %d 处非有限数值，已置为 None: %s",
                           len(result["data_quality"]["non_finite"]),
                           [p["path"] for p in result["data_quality"]["non_finite"][:5]])

        # 章节完整性门：核心章节缺数据时明确标出来（见 app/trust/completeness.py）
        result["completeness"] = check_completeness(result)
        if not result["completeness"]["complete"]:
            logger.warning("核心章节不完整: %s", result["completeness"]["gaps"])

        return result
