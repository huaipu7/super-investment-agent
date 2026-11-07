# -*- coding: utf-8 -*-
"""固收数据源共享 HTTP 助手。

统一定义 User-Agent 与系统代理读取（本机可能走 VPN 系统代理才能连通
政府/海外官方源），供 treasury / edgar / fomc 三个零 key 固收数据源复用。
不引入额外依赖，基于 requests + urllib.getproxies()。
"""
from __future__ import annotations

import json
import urllib.request

import requests

# Treasury 的 CDN(Akamai) 会指纹拦截机器人 UA；用常规浏览器 UA 更易放行。
# 仅作 HTTP 客户端标识，不改动数据源抓取频率，不规避任何鉴权。
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def sec_user_agent() -> str:
    """SEC 专用 UA。

    两家的规矩正好相反：SEC 的《Web 抓取须知》要求 UA 声明应用名与联系方式，
    伪装成浏览器会被直接 403（2026-11-04 实测：浏览器 UA → 403，描述性 UA → 200）。
    默认值里的邮箱是占位符，正式使用请在 .env 里配 SEC_USER_AGENT 为自己的联系方式。
    """
    from app.config import get_settings

    return get_settings().SEC_USER_AGENT


def system_proxies() -> dict:
    """读取系统代理（可能为空 dict）。"""
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    return {k: v for k, v in proxies.items() if v}


def get(url: str, timeout: int = 30, **kw) -> requests.Response:
    """带系统代理与 UA 的 GET。"""
    kw.setdefault("proxies", system_proxies() or None)
    kw.setdefault("headers", {"User-Agent": UA})
    kw.setdefault("timeout", timeout)
    return requests.get(url, **kw)


def get_json(url: str, timeout: int = 30, **kw):
    """GET 并解析 JSON。当前唯一调用方是 EDGAR，故默认用 SEC 声明性 UA。"""
    kw.setdefault("headers", {"User-Agent": sec_user_agent()})
    r = get(url, timeout=timeout, **kw)
    r.raise_for_status()
    return r.json()


def fetch_text(url: str, timeout: int = 40, **kw) -> str:
    """GET 并返回 UTF-8 文本。"""
    r = get(url, timeout=timeout, **kw)
    r.raise_for_status()
    return r.text


def json_default(o):
    """json.dumps 兜底：把无法序列化对象转成 str（供缓存去存放基本结构）。"""
    try:
        return o.isoformat()
    except AttributeError:
        return str(o)
