"""新闻舆情分析器

读取最新公司新闻与业绩说明会/投资者关系公告，
用 LLM 提炼情绪倾向、关键驱动因素、风险与机会。
新闻/公告均为空时不调用 LLM，如实返回 available=False。
"""

import json

from app.utils.logger import logger
from app.analysis.llm import analysis_llm

NEWS_SYSTEM_PROMPT = """你是一位资深证券分析师，擅长从公司新闻与公告中识别市场情绪的边际变化。
你的分析必须严格基于提供的新闻/公告原文，不得引入外部或虚构信息。输出结构化 JSON。"""


NEWS_COT_PROMPT = """请按以下三步推理，分析 {company_name} 的最新新闻与公告舆情。

公司名称: {company_name}
行业: {industry}

## 最新新闻（{article_count} 条，按时间倒序）
{news_lines}

## 业绩说明会 / 投资者关系公告（{briefing_count} 条）
{briefing_lines}

### 第一步：现象扫描
通读新闻与公告，识别近期市场对公司的核心关注点（产品、业绩、政策、事件等）。

### 第二步：情绪归因
判断整体情绪倾向（积极/中性/谨慎）及其边际方向；区分哪些信息已被股价定价，
哪些可能是市场尚未定价的边际变化。

### 第三步：提炼结论
输出结构化 JSON（只返回 JSON，不要额外说明）：

{{
    "sentiment": "积极|中性|谨慎",
    "sentiment_score": 0.0,
    "key_drivers": [{{"driver": "驱动因素", "evidence": "引用具体新闻/公告内容"}}],
    "risks_opportunities": [{{"type": "利好|风险", "point": "具体事项", "evidence": "引用来源"}}],
    "summary": "80 字以内的综合舆情摘要"
}}

要求：
1. sentiment_score 为 -1（极度悲观）~ +1（极度乐观）之间的数字，必须基于上述内容判断
2. key_drivers 2-4 条，每条 evidence 必须引用具体新闻标题或日期
3. risks_opportunities 2-4 条，type 仅取 "利好" 或 "风险"
4. 若新闻不足以判断，可输出中性/N 值，严禁编造"""


class NewsAnalyzer:
    """新闻舆情分析器"""

    def __init__(self):
        self._client = analysis_llm

    def analyze(
        self,
        company_name: str,
        articles: list | None = None,
        briefings: list | None = None,
        industry: str = "",
        source: str = "",
    ) -> dict:
        """分析最新新闻与业绩说明会公告。

        articles:  [{title, summary, published, source, url}]
        briefings: [{title, type, published, source, url}]
        两者均为空 → 不调用 LLM，返回 available=False。
        """
        articles = articles or []
        briefings = briefings or []
        if not company_name:
            return self._unavailable("缺少公司名")
        if not articles and not briefings:
            return self._unavailable("未获取到新闻/公告")

        news_lines = "\n".join(
            f"- [{a.get('published', '')}] ({a.get('source', '')}) {a.get('title', '')}"
            for a in articles[:20]
        ) or "（无）"
        briefing_lines = "\n".join(
            f"- [{b.get('published', '')}] {b.get('title', '')}"
            for b in briefings[:10]
        ) or "（无）"

        prompt = NEWS_COT_PROMPT.format(
            company_name=company_name,
            industry=industry or "未知",
            article_count=len(articles),
            briefing_count=len(briefings),
            news_lines=news_lines,
            briefing_lines=briefing_lines,
        )
        try:
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                system_prompt=NEWS_SYSTEM_PROMPT,
            )
            result = self._parse_json(response, {})
            if not isinstance(result, dict):
                result = {}
        except Exception as e:
            logger.error("新闻舆情分析失败: %s", e)
            result = {}

        return {
            "available": True,
            "source": source or "新闻数据源",
            "article_count": len(articles),
            "briefing_count": len(briefings),
            "articles": articles[:20],
            "briefings": briefings[:10],
            "sentiment": result.get("sentiment"),
            "sentiment_score": self._to_score(result.get("sentiment_score")),
            "key_drivers": self._clean_list(result.get("key_drivers")),
            "risks_opportunities": self._clean_list(result.get("risks_opportunities")),
            "summary": str(result.get("summary", "")) if result.get("summary") else "",
        }

    @staticmethod
    def _to_score(value):
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        return max(-1.0, min(1.0, round(score, 3)))

    @staticmethod
    def _clean_list(value):
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if isinstance(item, dict):
                out.append({k: (str(v) if v is not None else "") for k, v in item.items()})
            elif item is not None:
                out.append({"point": str(item)})
        return out[:6]

    def _unavailable(self, reason: str) -> dict:
        return {
            "available": False,
            "source": "",
            "article_count": 0,
            "briefing_count": 0,
            "articles": [],
            "briefings": [],
            "sentiment": None,
            "sentiment_score": None,
            "key_drivers": [],
            "risks_opportunities": [],
            "summary": "",
            "reason": reason,
        }

    def _parse_json(self, text, default):
        if not text:
            return default
        t = text.strip()
        if "```json" in t:
            t = t.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in t:
            t = t.split("```", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            logger.warning("新闻舆情 LLM 返回非 JSON: %s", t[:100])
            return default
