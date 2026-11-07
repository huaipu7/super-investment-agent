"""采集适配器注册表。

新增源 = 在 sources.yaml 登记 + 在这里注册适配器，两处缺一不可。
注册表本身不含未登记源：get_adapter 会先用白名单校验。
"""
from __future__ import annotations

from app.rag.acquire.adapters.arxiv import ArxivAdapter
from app.rag.acquire.adapters.base import Adapter
from app.rag.acquire.adapters.cninfo import CninfoAdapter
from app.rag.acquire.adapters.gov_cn import GovCnAdapter
from app.rag.acquire.adapters.hkexnews import HkexnewsAdapter
from app.rag.acquire.adapters.sec_edgar import SecEdgarAdapter

_ADAPTERS: dict[str, type[Adapter]] = {
    "arxiv": ArxivAdapter,
    "cninfo": CninfoAdapter,
    "gov_cn": GovCnAdapter,
    "hkexnews": HkexnewsAdapter,
    "sec_edgar": SecEdgarAdapter,
}


def get_adapter(source_id: str) -> Adapter:
    from app.rag.acquire.policy import get_sources

    get_sources().get(source_id)  # 未登记源直接抛 SourcePolicyError
    try:
        cls = _ADAPTERS[source_id]
    except KeyError:
        raise NotImplementedError(
            f"{source_id} 已在白名单登记但还没有适配器；已实现: {sorted(_ADAPTERS)}"
        ) from None
    return cls()


def available() -> list[str]:
    return sorted(_ADAPTERS)
