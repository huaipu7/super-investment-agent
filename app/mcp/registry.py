"""MCP 工具目录 —— name→handler，风格对齐 app/agent/runner.py::ACTION_MAP。

MCP 作为数据源 fallback：MCP_ENABLED 且对应工具在已配置 server 中存在时，
call_mcp_tool 经 bridge 调用并统一返回。默认关闭；真实数据源等凭据后再接。
"""
from __future__ import annotations

from app.config import get_settings
from app.mcp.bridge import MCPServer, MCPConfigError, MCPError
from app.utils.logger import logger

_server_cache: dict[str, MCPServer] = {}


def _servers() -> list[MCPServer]:
    """按 config.MCP_SERVERS 实例化（进程内缓存 server 连接）。"""
    servers: list[MCPServer] = []
    for cfg in get_settings().MCP_SERVERS or []:
        name = cfg.get("name", "")
        if not name:
            continue
        if name in _server_cache:
            servers.append(_server_cache[name])
            continue
        try:
            server = MCPServer(
                name=name,
                transport=cfg.get("transport", "stdio"),
                command=cfg.get("command", ""),
                args=cfg.get("args") or [],
                url=cfg.get("url", ""),
                timeout=float(cfg.get("timeout", 15.0)),
            )
            _server_cache[name] = server
            servers.append(server)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP server %s 初始化失败: %s", name, e)
    return servers


def call_mcp_tool(tool: str, params: dict | None = None) -> dict:
    """按工具名在已配置 MCP server 中查找并调用。不可用 → MCPConfigError。"""
    if not get_settings().MCP_ENABLED:
        raise MCPConfigError("MCP_ENABLED=false，工具桥未启用")
    servers = _servers()
    if not servers:
        raise MCPConfigError("MCP 未配置任何 server（config.MCP_SERVERS 为空）")
    for server in servers:
        try:
            tools = server.discover_tools()
        except MCPError:
            continue
        if tool in tools:
            return server.call(tool, params)
    raise MCPConfigError(f"MCP 工具 {tool} 在已配置 server 中不存在")


def quote_fallback(ticker: str) -> dict:
    """mock 报价 fallback（演示数据，显式标注 source=mcp_mock / is_mock，非真实行情）。"""
    res = call_mcp_tool("mock_quote", {"ticker": ticker})
    quote = dict(res.get("quote") or {})
    quote["source"] = "mcp_mock"
    quote["is_mock"] = True
    return quote
