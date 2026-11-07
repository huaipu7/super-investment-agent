"""
统一配置中心 - 读取 .env，所有模块共用。

来源：整合自 Research-Agent(config/settings.py)、Agent-for-Investment(config.py)、
Risk-Chain(src/config.py) 三个项目的配置。

分层加载（优先级从低到高，后读不覆盖先读）：
  .env → .env.{ENV}（ENV 默认 dev）→ .env.local（最高）
  ENV 由 shell / start.bat 设置；未设置时按 dev 处理。
"""
import os
from pathlib import Path
from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_files() -> None:
    """按优先级加载配置：env.local > env.{ENV} > .env（先读最高优先级）。"""
    env = os.environ.get("ENV", "dev")
    files = [BASE_DIR / ".env.local"]
    if env != "dev":
        files.append(BASE_DIR / f".env.{env}")
    files.append(BASE_DIR / ".env")
    for f in files:
        if f.exists():
            load_dotenv(str(f), override=False)


_load_env_files()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 运行环境（dev / staging / prod，驱动配置分层）────────────────
    ENV: str = "dev"

    # ── LLM（DeepSeek V4 Flash，Anthropic 兼容接口）──────────────────
    LLM_API_KEY: str = ""
    LLM_API_BASE: str = "https://api.deepseek.com/anthropic"
    LLM_MODEL: str = "deepseek-v4-flash"
    LLM_MAX_TOKENS: int = 4096
    LLM_TIMEOUT: float = 120.0
    LLM_THINKING_ENABLED: bool = False

    # ── 服务 ────────────────────────────────────────────────────────
    SERVER_PORT: int = 8000

    # ── 可信层 ──────────────────────────────────────────────────────
    # 严格模式（默认开）：输入缺失时不再静默兜底，而是显式标记降级并写明
    # 假设，或无值就返回 None。关闭后回到历史兜底行为，仅用于前后对比。
    TRUST_STRICT_MODE: bool = True

    # ── 数据源开关 ──────────────────────────────────────────────────
    SOURCE_YFINANCE: bool = True
    SOURCE_AKSHARE: bool = True
    SOURCE_SINA: bool = True
    SOURCE_FUND: bool = True
    # 固收零 key 源（Phase 1）
    SOURCE_TREASURY: bool = True
    SOURCE_EDGAR: bool = True
    SOURCE_FOMC: bool = True
    # SEC 要求 UA 带真实联系方式，否则 403。默认值是占位邮箱，
    # 正式使用请改成自己的（https://www.sec.gov/os/webmaster-faq#developers）。
    SEC_USER_AGENT: str = "Super-Investment-Agent research-agent@example.com"

    # ── RAG（默认可开；无任何嵌入后端时静默降级为无 RAG 分支）──────
    RAG_ENABLED: bool = True
    # 远程嵌入（OpenAI 兼容 /embeddings）。配置了 EMBEDDING_API_KEY 时
    # 优先远程；否则回退本地 bge-small-zh-v1.5（torch 已装）；皆无 → 不可用。
    EMBEDDING_API_BASE: str = ""
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = ""
    # 二段重排（cross-encoder）。默认关：不装模型、不改排序。
    # 模型缺失时自动退回原顺序，不会报错；v1 只读快照永远旁路（L2 基线不许动）。
    RERANK_ENABLED: bool = False

    # ── MCP（工具桥，默认关闭；真实数据源等凭据后再配）────────────
    MCP_ENABLED: bool = False
    # [{name, transport: "stdio"|"http", command, args, url, timeout}]
    MCP_SERVERS: list = []

    # ── 模型网关（任务→模型分层 + 降级链 + 重试）─────────────────
    # MODEL_TIERS: {"intent": "deepseek-v4-flash", "report": "deepseek-v4-flash", ...}
    #   未配置的任务回退 LLM_MODEL；MODEL_FALLBACKS 为任务级降级模型列表。
    MODEL_TIERS: dict = {}
    MODEL_FALLBACKS: dict = {}
    LLM_RETRY: int = 3
    LLM_RETRY_BACKOFF: float = 0.5

    # ── 自主深度调查（预算硬约束：下列数值是天花板，不是建议值）──────
    # 两档：standard（标准，默认）/ deep（深度），由请求的 depth 参数选择。
    # 三重硬上限（轮次 / 工具调用 / LLM 调用）+ 墙钟 deadline，另有去重与
    # 无进展检测。llm_calls 必须单独计数：compare_peers 内部会烧掉 1 次 LLM，
    # 不单独计则预算失真。
    INVESTIGATION_ENABLED: bool = True
    INVESTIGATION_STANDARD_MAX_ROUNDS: int = 6
    INVESTIGATION_STANDARD_MAX_TOOL_CALLS: int = 14
    INVESTIGATION_STANDARD_MAX_LLM_CALLS: int = 10
    INVESTIGATION_STANDARD_DEADLINE_SECONDS: int = 300
    INVESTIGATION_STANDARD_MAX_HYPOTHESES: int = 4
    INVESTIGATION_STANDARD_MAX_EVIDENCE_PER_HYPOTHESIS: int = 4
    INVESTIGATION_STANDARD_MAX_PEERS: int = 3
    INVESTIGATION_DEEP_MAX_ROUNDS: int = 12
    INVESTIGATION_DEEP_MAX_TOOL_CALLS: int = 32
    INVESTIGATION_DEEP_MAX_LLM_CALLS: int = 20
    INVESTIGATION_DEEP_DEADLINE_SECONDS: int = 720
    INVESTIGATION_DEEP_MAX_HYPOTHESES: int = 8
    INVESTIGATION_DEEP_MAX_EVIDENCE_PER_HYPOTHESIS: int = 6
    INVESTIGATION_DEEP_MAX_PEERS: int = 5
    # 连续 N 轮无新增证据/无状态变化 → 停；剩余时间不足该秒数时拒绝开新一轮
    INVESTIGATION_NO_PROGRESS_WINDOW: int = 3
    INVESTIGATION_MIN_ROUND_SECONDS: int = 60

    # ── SQLite（统一缓存与自选股，替换 Redis / Neo4j）──────────────
    CACHE_DB_PATH: str = "data/cache.db"
    ANALYSIS_CACHE_DB_PATH: str = "data/analysis_cache.db"
    WATCHLIST_DB_PATH: str = "data/watchlist.db"
    AUDIT_DB_PATH: str = "data/audit.db"
    COMPLIANCE_DB_PATH: str = "data/compliance.db"
    MEMORY_DB_PATH: str = "data/memory.db"
    FEEDBACK_DB_PATH: str = "data/feedback.db"
    # 论点台账：结论 / 假设 / 证伪条件 / 证据 / 复盘
    THESIS_DB_PATH: str = "data/thesis.db"
    # 👎 待审队列：只进人工审阅，绝不自动进回归集
    FLYWHEEL_DIR_PATH: str = "data/flywheel"

    @property
    def base_dir(self) -> Path:
        return BASE_DIR

    @property
    def cache_db(self) -> Path:
        return BASE_DIR / self.CACHE_DB_PATH

    @property
    def analysis_cache_db(self) -> Path:
        return BASE_DIR / self.ANALYSIS_CACHE_DB_PATH

    @property
    def watchlist_db(self) -> Path:
        return BASE_DIR / self.WATCHLIST_DB_PATH

    @property
    def audit_db(self) -> Path:
        return BASE_DIR / self.AUDIT_DB_PATH

    @property
    def compliance_db(self) -> Path:
        return BASE_DIR / self.COMPLIANCE_DB_PATH

    @property
    def memory_db(self) -> Path:
        return BASE_DIR / self.MEMORY_DB_PATH

    @property
    def feedback_db(self) -> Path:
        return BASE_DIR / self.FEEDBACK_DB_PATH

    @property
    def thesis_db(self) -> Path:
        return BASE_DIR / self.THESIS_DB_PATH

    @property
    def flywheel_dir(self) -> Path:
        return BASE_DIR / self.FLYWHEEL_DIR_PATH

    def check_llm_ready(self) -> bool:
        """LLM 配置是否就绪（未配置密钥时返回 False）。"""
        return bool(self.LLM_API_KEY.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
