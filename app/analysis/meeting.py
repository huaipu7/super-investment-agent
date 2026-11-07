"""股东大会分析器

读取最新股东大会结构化信息，用 LLM 解读重大提案与公司治理信号。
无数据（None/空列表）时如实返回 available=False，不调用 LLM。
"""

from app.utils.logger import logger
from app.analysis.llm import analysis_llm
from app.llm.schemas import MeetingResult
from app.llm.structured import parse_llm_json

MEETING_SYSTEM_PROMPT = """你是一位公司治理分析师，擅长从股东大会材料中识别重大治理与经营信号。
你的分析必须严格基于提供的股东大会记录，不得引入外部或虚构信息。输出结构化 JSON。"""


MEETING_COT_PROMPT = """请按以下三步推理，解读 {company_name} 最新股东大会。

公司名称: {company_name}

## 最新股东大会记录（{meeting_count} 场，按时间倒序）
{meeting_lines}

### 第一步：逐场扫描
读取每场股东大会的名称、召开日期、股权登记日、决议公告日，确认会议基本盘。

### 第二步：提案解读
逐条阅读提案内容，区分重大经营事项（资本运作、重大投资、人事变动、分红等）与例行事项。

### 第三步：治理评估
综合评估公司治理信号与潜在治理风险，输出结构化 JSON（只返回 JSON，不要额外说明）：

{{
    "key_proposals": [{{"proposal": "提案摘要", "meeting": "所属会议", "significance": "对经营/治理的含义"}}],
    "governance_signals": "一句话整体治理信号",
    "governance_risks": ["治理风险点，最多3条"],
    "summary": "80 字以内的综合评估摘要"
}}

要求：
1. key_proposals 2-5 条，significance 说明其投资/治理含义
2. governance_signals 必须基于真实提案，例如"高分红预案显示股东回报意愿增强"
3. governance_risks 0-3 条，无风险时返回空数组
4. 不得编造提案内容；提案字段缺失时在意义处如实说明
5. 只返回 JSON，不要额外说明"""


class MeetingAnalyzer:
    """股东大会分析器"""

    def __init__(self):
        self._client = analysis_llm

    def analyze(self, company_name: str, meetings) -> dict:
        """分析最新股东大会。

        meetings: router.get_meetings() 返回值，A 股为 list[dict]，
                  非 A 股为 None。None → available=False（美股暂无数据源）。
        """
        if not company_name:
            return self._unavailable("缺少公司名")
        if meetings is None:
            return self._unavailable("美股暂无股东大会数据源")
        if not meetings:
            return self._unavailable("未获取到股东大会数据")

        lines = []
        for m in meetings[:5]:
            proposals = str(m.get("proposals", "")).strip()
            lines.append(
                f"- {m.get('name', '')}（{m.get('start_date', '')}）\n"
                f"  股权登记日: {m.get('record_date') or '—'} | "
                f"决议公告日: {m.get('resolution_date') or '—'}\n"
                f"  提案: {proposals or '—'}"
            )
        meeting_lines = "\n".join(lines)
        # 数据源：yfinance 日历记录自带 source_note（仅披露日期）；否则东财
        source_note = ""
        if meetings and isinstance(meetings[0], dict):
            source_note = str(meetings[0].get("source_note") or "")

        prompt = MEETING_COT_PROMPT.format(
            company_name=company_name,
            meeting_count=len(meetings),
            meeting_lines=meeting_lines,
        )
        try:
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                system_prompt=MEETING_SYSTEM_PROMPT,
            )
            result = parse_llm_json(response, model=MeetingResult, default={},
                                    context="股东大会")
            if not isinstance(result, dict):
                result = {}
        except Exception as e:
            logger.error("股东大会分析失败: %s", e)
            result = {}

        risks = result.get("governance_risks") or []
        if not isinstance(risks, list):
            risks = [risks]
        return {
            "available": True,
            "source": source_note or "东财股东大会数据",
            "meeting_count": len(meetings),
            "meetings": meetings[:5],
            "key_proposals": self._clean_list(result.get("key_proposals")),
            "governance_signals": (
                str(result.get("governance_signals", "")) if result.get("governance_signals") else ""
            ),
            "governance_risks": [str(r) for r in risks if r][:3],
            "summary": str(result.get("summary", "")) if result.get("summary") else "",
        }

    @staticmethod
    def _clean_list(value):
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if isinstance(item, dict):
                out.append({k: (str(v) if v is not None else "") for k, v in item.items()})
            elif item is not None:
                out.append({"proposal": str(item)})
        return out[:5]

    def _unavailable(self, reason: str) -> dict:
        return {
            "available": False,
            "source": "",
            "meeting_count": 0,
            "meetings": [],
            "key_proposals": [],
            "governance_signals": "",
            "governance_risks": [],
            "summary": "",
            "reason": reason,
        }

