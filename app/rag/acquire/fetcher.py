"""带合规约束的 HTTP 抓取器 —— 白名单、robots、限速、退避、原始响应缓存。

所有外部请求都必须走这里，适配器不直接碰 requests。四条纪律硬编码：

1. host 必须在 sources.yaml 登记，否则连 robots 都不去读；
2. robots.txt 逐 host 校验并缓存 7 天，5xx / 网络失败按「禁止」处理（从严）；
3. 按 host 单线程串行限速，间隔取站点 Crawl-delay 与配置值的较大者；
4. 429 按 Retry-After 退避，超过 max_retries 即 not_collected，不无限重试。

抓不到 ≠ 抓到空：前者抛 NotCollected，后者由适配器落 Document.status="empty_text"。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.robotparser import RobotFileParser

from app.rag.acquire.policy import SourcePolicy, SourceTable, get_sources

ACCEPT = {
    "html": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "pdf": "application/pdf,*/*;q=0.8",
    "json": "application/json,*/*;q=0.8",
    "atom": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
    "text": "text/plain,*/*;q=0.8",
}

ROBOTS_TTL_S = 7 * 24 * 3600
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class NotCollected(Exception):
    """未采集成功。reason_code 必须能用一句话解释，禁止「静默跳过」。"""

    def __init__(self, source_id: str, url: str, reason_code: str,
                 http_status: int | None = None, detail: str = ""):
        # not_whitelisted|robots_denied|rate_limited|http_error
        self.source_id = source_id
        self.url = url
        self.reason_code = reason_code
        self.http_status = http_status
        self.detail = detail
        super().__init__(f"{reason_code}: {url} {detail}".strip())

    def to_dict(self) -> dict:
        return {
            "status": "not_collected",
            "source_id": self.source_id,
            "url": self.url,
            "reason_code": self.reason_code,
            "http_status": self.http_status,
            "detail": self.detail,
        }


@dataclass
class FetchResult:
    source_id: str
    url: str
    status: int
    body: bytes
    content_type: str = ""
    encoding: str = ""
    cache_hit: bool = False
    fetched_at: str = ""
    headers: dict = field(default_factory=dict)

    def text(self) -> str:
        """按 charset 解码；站点常见 utf-8 / gb18030 兜底。"""
        candidates = []
        ct = self.content_type.lower()
        if "charset=" in ct:
            candidates.append(ct.split("charset=", 1)[1].split(";")[0].strip())
        if self.encoding:
            candidates.append(self.encoding)
        candidates += ["utf-8", "gb18030"]
        for enc in candidates:
            try:
                return self.body.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", errors="replace")

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "status": self.status,
            "content_type": self.content_type,
            "cache_hit": self.cache_hit,
            "fetched_at": self.fetched_at,
            "bytes": len(self.body),
        }


def _charset_of(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "charset=" in ct:
        return ct.split("charset=", 1)[1].split(";")[0].strip()
    return ""


class Fetcher:
    def __init__(
        self,
        sources: SourceTable | None = None,
        base_dir: str | Path | None = None,
        session=None,
        sleep=None,
        clock=None,
        robots_ttl_s: int = ROBOTS_TTL_S,
    ):
        from app.config import get_settings

        self.sources = sources or get_sources()
        base = Path(base_dir) if base_dir else get_settings().base_dir / "data" / "external"
        self.raw_dir = base / "raw"
        self.robots_path = base / "robots.json"
        self._session = session or self._default_session()
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._robots_ttl = robots_ttl_s
        self._last: dict[str, float] = {}
        self._robots: dict[str, dict] | None = None

    @staticmethod
    def _default_session():
        """本机代理会中间签名，系统 CA 不够用 —— 必须显式用 certifi 根证书。"""
        import certifi
        import requests

        from app.data.sources.fi_http import system_proxies

        s = requests.Session()
        s.verify = certifi.where()
        s.proxies = system_proxies() or {}
        return s

    # ── robots ───────────────────────────────────────────────────────

    def _load_robots(self) -> dict[str, dict]:
        if self._robots is None:
            if self.robots_path.exists():
                try:
                    self._robots = json.loads(self.robots_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    self._robots = {}
            else:
                self._robots = {}
        return self._robots

    def _save_robots(self) -> None:
        self.robots_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.robots_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._robots or {}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.robots_path)

    def _fetch_robots(self, policy: SourcePolicy, host: str) -> tuple[str, int | None, bool]:
        """返回 (robots 正文, http 状态, 是否允许抓取)。

        状态判定按 RFC 9309：4xx → 视为无限制；5xx / 网络失败 → 从严禁止。
        """
        cache = self._load_robots()
        entry = cache.get(host)
        fresh = False
        if entry:
            try:
                age = time.time() - float(entry.get("fetched_ts", 0))
                fresh = age < self._robots_ttl
            except (TypeError, ValueError):
                fresh = False
        if fresh:
            return entry.get("text", ""), entry.get("status"), bool(entry.get("allowed", True))

        url = f"https://{host}/robots.txt"
        status: int | None = None
        text = ""
        allowed = True
        try:
            self._throttle(host, policy.interval_s)
            resp = self._session.get(
                url,
                headers={"User-Agent": policy.user_agent_value()},
                timeout=20,
                allow_redirects=True,
            )
            status = int(resp.status_code)
            if 200 <= status < 300:
                text = resp.text if isinstance(getattr(resp, "text", None), str) else ""
                if not text:
                    text = (resp.content or b"").decode("utf-8", errors="replace")
            elif 500 <= status < 600:
                allowed = False
        except Exception:
            allowed = False  # 网络失败不能当作「无 robots」

        cache[host] = {
            "fetched_ts": time.time(),
            "fetched_at": _now(),
            "status": status,
            "allowed": allowed,
            "text": text,
        }
        self._save_robots()
        return text, status, allowed

    def robots_allowed(self, source_id: str, url: str) -> bool:
        policy = self.sources.get(source_id)
        if not policy.allows_url(url):
            return False
        if policy.is_robots_denied(url):
            return False
        text, _status, allowed = self._fetch_robots(policy, _host(url))
        if not allowed:
            return False
        if not text.strip():
            return True
        rp = RobotFileParser()
        rp.parse(text.splitlines())
        return rp.can_fetch(policy.user_agent_value(), url)

    # ── 限速 ─────────────────────────────────────────────────────────

    def _throttle(self, host: str, interval: float) -> None:
        last = self._last.get(host)
        now = self._clock()
        if last is not None:
            wait = interval - (now - last)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last[host] = now

    # ── 缓存 ─────────────────────────────────────────────────────────

    def _cache_paths(self, source_id: str, cache_key: str) -> tuple[Path, Path]:
        key = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()
        d = self.raw_dir / source_id
        return d / f"{key}.gz", d / f"{key}.json"

    def _read_cache(self, source_id: str, cache_key: str) -> FetchResult | None:
        body_p, meta_p = self._cache_paths(source_id, cache_key)
        if not (body_p.exists() and meta_p.exists()):
            return None
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            body = gzip.decompress(body_p.read_bytes())
        except (OSError, json.JSONDecodeError, gzip.BadGzipFile):
            return None
        return FetchResult(
            source_id=source_id,
            url=meta.get("url", ""),
            status=int(meta.get("status", 200)),
            body=body,
            content_type=meta.get("content_type", ""),
            encoding=meta.get("encoding", ""),
            cache_hit=True,
            fetched_at=meta.get("fetched_at", ""),
            headers=meta.get("headers", {}),
        )

    def _write_cache(self, res: FetchResult, cache_key: str) -> None:
        body_p, meta_p = self._cache_paths(res.source_id, cache_key)
        body_p.parent.mkdir(parents=True, exist_ok=True)
        tmp = body_p.with_suffix(".gz.tmp")
        tmp.write_bytes(gzip.compress(res.body, compresslevel=6))
        os.replace(tmp, body_p)
        meta = {
            "url": res.url,
            "status": res.status,
            "content_type": res.content_type,
            "encoding": res.encoding,
            "fetched_at": res.fetched_at,
            "headers": res.headers,
        }
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 抓取 ─────────────────────────────────────────────────────────

    def get(
        self,
        url: str,
        *,
        source_id: str,
        expect: str = "html",
        force: bool = False,
        timeout: int = 40,
    ) -> FetchResult:
        return self._request("GET", url, source_id=source_id, expect=expect,
                             force=force, timeout=timeout)

    def post(
        self,
        url: str,
        *,
        source_id: str,
        data: dict,
        expect: str = "json",
        force: bool = False,
        timeout: int = 40,
    ) -> FetchResult:
        """带表单体的 POST。白名单/robots/限速/退避/缓存与 GET 完全同一套。"""
        return self._request("POST", url, source_id=source_id, expect=expect,
                             force=force, timeout=timeout, data=data)

    def _request(
        self,
        method: str,
        url: str,
        *,
        source_id: str,
        expect: str,
        force: bool,
        timeout: int,
        data: dict | None = None,
    ) -> FetchResult:
        policy = self.sources.get(source_id)
        if not policy.allows_url(url):
            raise NotCollected(source_id, url, "not_whitelisted", None,
                               f"host {_host(url)!r} 不在登记表 {policy.hosts}")
        if policy.is_robots_denied(url):
            raise NotCollected(source_id, url, "robots_denied", None,
                               f"命中静态 deny 前缀 {policy.robots_denied_prefixes}")
        if not self.robots_allowed(source_id, url):
            raise NotCollected(source_id, url, "robots_denied", None,
                               f"robots.txt 不允许抓取 {_host(url)}")

        # POST 的缓存键必须含表单体，否则同一地址的不同查询会互相覆盖
        cache_key = url if data is None else f"POST {url} {urlencode(sorted(data.items()))}"
        if not force:
            cached = self._read_cache(source_id, cache_key)
            if cached is not None:
                return cached

        host = _host(url)
        headers = {
            "User-Agent": policy.user_agent_value(),
            "Accept": ACCEPT.get(expect, ACCEPT["html"]),
            "Accept-Encoding": "gzip, deflate",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        delay = 1.0
        attempts = max(1, policy.max_retries)
        last_err = ""

        for attempt in range(1, attempts + 1):
            self._throttle(host, policy.interval_s)
            try:
                if data is None:
                    resp = self._session.get(
                        url, headers=headers, timeout=timeout, allow_redirects=True
                    )
                else:
                    resp = self._session.post(
                        url, data=data, headers=headers, timeout=timeout,
                        allow_redirects=True,
                    )
            except Exception as e:  # 网络层异常：重试后放弃
                last_err = f"{type(e).__name__}: {e}"
                if attempt == attempts:
                    raise NotCollected(source_id, url, "http_error", None, last_err) from None
                self._sleep(delay)
                delay *= 2
                continue

            status = int(resp.status_code)
            if status == 429:
                if attempt == attempts:
                    raise NotCollected(source_id, url, "rate_limited", 429,
                                       f"连续 {attempts} 次 429，已按 Retry-After 退避")
                wait = self._retry_after(resp) or delay
                self._sleep(wait)
                delay = max(delay * 2, wait)
                continue
            if status in _RETRYABLE_STATUS:
                if attempt == attempts:
                    raise NotCollected(source_id, url, "http_error", status, "服务端持续错误")
                self._sleep(delay)
                delay *= 2
                continue
            if status >= 400:
                raise NotCollected(source_id, url, "http_error", status,
                                   f"HTTP {status}（不可重试）")

            resp_headers = {k.lower(): v for k, v in dict(getattr(resp, "headers", {})).items()}
            content_type = resp_headers.get("content-type", "")
            res = FetchResult(
                source_id=source_id,
                url=url,
                status=status,
                body=resp.content or b"",
                content_type=content_type,
                encoding=_charset_of(content_type),
                cache_hit=False,
                fetched_at=_now(),
                headers={"content-type": content_type,
                         "content-length": resp_headers.get("content-length", "")},
            )
            self._write_cache(res, cache_key)
            return res

        raise NotCollected(source_id, url, "http_error", None, last_err or "未知失败")

    @staticmethod
    def _retry_after(resp) -> float | None:
        try:
            raw = dict(getattr(resp, "headers", {})).get("Retry-After")
        except Exception:
            return None
        if raw is None:
            return None
        try:
            return max(0.0, float(str(raw).strip()))
        except ValueError:
            return None
