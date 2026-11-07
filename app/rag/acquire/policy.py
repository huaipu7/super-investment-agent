"""采集白名单：只有 sources.yaml 里登记过的源与 host 才允许访问。

这里的校验是硬门槛，不是提示：license_class 写错、下限被突破、host 未登记，
一律在加载时抛错，宁可启动失败也不让违规采集悄悄发生。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

# 合规依据只有这三类，新增类别必须先生效于《著作权法》/站点条款，不允许随意扩展
LICENSE_CLASSES = frozenset({"public_domain_statute", "public_record", "open_access"})

# 与入库契约一致：kind 决定检索路由，拼错会让新语料永远检索不到
KINDS = frozenset({"政策", "披露文件", "论文"})

_USER_AGENT_MODES = frozenset({"browser", "sec"})

DEFAULT_PATH = Path("data") / "external" / "sources.yaml"


class SourcePolicyError(ValueError):
    """sources.yaml 不合规 —— 加载即失败，不做兜底猜测。"""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


@dataclass(frozen=True)
class SourcePolicy:
    id: str
    name: str
    home: str
    hosts: tuple[str, ...]
    license_class: str
    license_note: str
    kinds: tuple[str, ...]
    quota: int
    min_interval_s: float
    max_retries: int
    user_agent: str
    crawl_delay_s: float | None = None
    robots_denied_prefixes: tuple[str, ...] = ()
    robots_note: str = ""

    @property
    def interval_s(self) -> float:
        """两次请求的最小间隔：站点 Crawl-delay 与自定义间隔取大者。"""
        return max(self.min_interval_s, self.crawl_delay_s or 0.0)

    def allows_kind(self, kind: str) -> bool:
        return kind in self.kinds

    def allows_url(self, url: str) -> bool:
        """host 必须在登记表内 —— 防止适配器把链接指到白名单外。"""
        return _host(url) in self.hosts

    def is_robots_denied(self, url: str) -> bool:
        """静态 deny 前缀（robots 的已知硬规则），Fetcher 仍会再跑一遍真实 robots。"""
        path = urlsplit(url).path
        return any(path.startswith(p) for p in self.robots_denied_prefixes)

    def user_agent_value(self) -> str:
        from app.data.sources.fi_http import UA, sec_user_agent

        if self.user_agent == "sec":
            return sec_user_agent()
        if self.user_agent == "browser":
            return UA
        return self.user_agent

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "license_class": self.license_class,
            "kinds": list(self.kinds),
            "quota": self.quota,
            "interval_s": self.interval_s,
        }


@dataclass(frozen=True)
class SourceTable:
    version: int
    updated: str
    sources: dict[str, SourcePolicy] = field(default_factory=dict)

    def get(self, source_id: str) -> SourcePolicy:
        try:
            return self.sources[source_id]
        except KeyError:
            known = ", ".join(sorted(self.sources)) or "(空)"
            raise SourcePolicyError(
                f"未登记的采集源 {source_id!r}；白名单只有: {known}"
            ) from None

    def __iter__(self):
        return iter(self.sources.values())

    def __len__(self) -> int:
        return len(self.sources)


def _require(raw: dict, key: str, where: str):
    if key not in raw or raw[key] in (None, "", [], {}):
        raise SourcePolicyError(f"{where} 缺少必填字段 {key!r}")
    return raw[key]


def _build(raw: dict, floors: dict) -> SourcePolicy:
    where = f"源 {raw.get('id')!r}"
    sid = str(_require(raw, "id", "某个源")).strip()
    where = f"源 {sid!r}"

    license_class = str(_require(raw, "license_class", where))
    if license_class not in LICENSE_CLASSES:
        raise SourcePolicyError(
            f"{where} 的 license_class={license_class!r} 不在合规枚举内"
            f"（{sorted(LICENSE_CLASSES)}）；未证成合规的来源不得采集"
        )

    home = str(_require(raw, "home", where))
    hosts = tuple(str(h).lower() for h in _require(raw, "hosts", where))
    if _host(home) not in hosts:
        raise SourcePolicyError(
            f"{where} 的 home host {_host(home)!r} 不在 hosts {hosts} 内"
        )

    kinds = tuple(str(k) for k in _require(raw, "kinds", where))
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise SourcePolicyError(
            f"{where} 的 kinds={bad} 不在入库契约 {sorted(KINDS)} 内"
        )

    quota = int(_require(raw, "quota", where))
    if quota <= 0:
        raise SourcePolicyError(f"{where} 的 quota 必须为正，实际 {quota}")

    interval = float(_require(raw, "min_interval_s", where))
    floor_interval = float(floors.get("min_interval_s", 0.0))
    if interval < floor_interval:
        raise SourcePolicyError(
            f"{where} 的 min_interval_s={interval} 低于全网下限 {floor_interval}"
        )

    retries = int(raw.get("max_retries", floors.get("max_retries", 3)))
    floor_retries = int(floors.get("max_retries", 3))
    if not 1 <= retries <= floor_retries:
        raise SourcePolicyError(
            f"{where} 的 max_retries={retries} 超出 1..{floor_retries}"
        )

    crawl_delay = raw.get("crawl_delay_s")
    crawl_delay = float(crawl_delay) if crawl_delay not in (None, "") else None

    ua = str(raw.get("user_agent", "browser"))
    if ua not in _USER_AGENT_MODES and "@" not in ua:
        raise SourcePolicyError(
            f"{where} 的 user_agent 只能是 {sorted(_USER_AGENT_MODES)} "
            f"或带联系方式的声明性 UA，实际 {ua!r}"
        )

    return SourcePolicy(
        id=sid,
        name=str(raw.get("name", sid)),
        home=home,
        hosts=hosts,
        license_class=license_class,
        license_note=str(raw.get("license_note", "")),
        kinds=kinds,
        quota=quota,
        min_interval_s=interval,
        max_retries=retries,
        user_agent=ua,
        crawl_delay_s=crawl_delay,
        robots_denied_prefixes=tuple(raw.get("robots_denied_prefixes") or ()),
        robots_note=str(raw.get("robots_note", "")),
    )


def _default_path() -> Path:
    from app.config import get_settings

    return get_settings().base_dir / DEFAULT_PATH


def load_sources(path: str | Path | None = None) -> SourceTable:
    p = Path(path) if path else _default_path()
    if not p.exists():
        raise SourcePolicyError(f"源登记表不存在: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    floors = raw.get("floors") or {}
    entries = raw.get("sources") or []
    if not entries:
        raise SourcePolicyError(f"{p} 里没有任何源，白名单为空等同于禁止采集")

    table: dict[str, SourcePolicy] = {}
    for entry in entries:
        policy = _build(entry, floors)
        if policy.id in table:
            raise SourcePolicyError(f"源 id 重复: {policy.id!r}")
        table[policy.id] = policy

    return SourceTable(
        version=int(raw.get("version", 1)),
        updated=str(raw.get("updated", "")),
        sources=table,
    )


@lru_cache(maxsize=1)
def get_sources() -> SourceTable:
    return load_sources()
