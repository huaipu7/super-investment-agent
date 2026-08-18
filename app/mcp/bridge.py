"""MCP 桥 —— 极简 MCP client（不引入 mcp 依赖）。

双传输：
  - stdio：子进程 + JSON-RPC 2.0 over stdin/stdout（默认；内置 mock server 用）
  - http：JSON-RPC 2.0 POST {url}/rpc（简化传输；真实源的完整 SSE 接入时扩展）

配置（config.MCP_SERVERS）：
  [{"name", "transport": "stdio"|"http", "command", "args", "url", "timeout"}]

统一接口：
  MCPServer.discover_tools() -> list[str]     工具发现（缓存）
  MCPServer.call(tool, params) -> dict        工具调用
  MCPServer.close()
未配置/未知传输/工具不存在 → 抛 MCPConfigError（干净错误）。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading

import httpx

from app.utils.logger import logger


class MCPError(RuntimeError):
    """MCP 桥错误基类。"""


class MCPConfigError(MCPError):
    """server 未配置 / 传输方式未知 / 工具不存在。"""


class MCPServer:
    def __init__(self, name: str, transport: str = "stdio",
                 command: str = "", args: list | None = None,
                 url: str = "", timeout: float = 15.0):
        self.name = name
        self.transport = transport
        self.command = command
        self.args = list(args or [])
        self.url = (url or "").rstrip("/")
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._request_id = 0
        self._tools: list[str] | None = None

    # ── stdio 子进程传输 ─────────────────────────────────────

    def _ensure_stdio(self):
        if self._proc is None or self._proc.poll() is not None:
            if not self.command:
                raise MCPConfigError(
                    f"MCP server {self.name} 缺 command（stdio 传输）")
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
            )
        return self._proc

    def _stdio_request(self, method: str, params: dict) -> dict:
        proc = self._ensure_stdio()
        self._request_id += 1
        msg = json.dumps(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method,
             "params": params},
            ensure_ascii=False,
        )
        try:
            proc.stdin.write(msg + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
        except Exception as e:
            raise MCPError(f"MCP stdio 通信失败 ({self.name}/{method}): {e}") from e
        if not line:
            raise MCPError(f"MCP stdio 无响应 ({self.name}/{method})，进程已退出")
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise MCPError(f"MCP stdio 返回非法 JSON: {line[:200]}") from e
        return self._unwrap(data, method)

    # ── http 传输（JSON-RPC POST {url}/rpc）──────────────────

    def _http_request(self, method: str, params: dict) -> dict:
        if not self.url:
            raise MCPConfigError(f"MCP server {self.name} 缺 url（http 传输）")
        self._request_id += 1
        body = {"jsonrpc": "2.0", "id": self._request_id, "method": method,
                "params": params}
        try:
            # trust_env=False：本机 VPN 系统代理会改写 localhost 请求的分帧
            #（实测把 \r\n\r\n 塞进 body 且 Content-Length 虚高），本地 MCP 需直连
            resp = httpx.post(f"{self.url}/rpc", json=body, timeout=self.timeout,
                              trust_env=False)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            raise MCPError(f"MCP http 调用失败 ({self.name}/{method}): {e}") from e
        return self._unwrap(data, method)

    @staticmethod
    def _unwrap(data: dict, method: str) -> dict:
        if data.get("jsonrpc") != "2.0":
            raise MCPError(f"MCP 响应非 JSON-RPC: {str(data)[:200]}")
        if "error" in data:
            raise MCPError(f"MCP {method} 错误: {data['error']}")
        result = data.get("result")
        return result if isinstance(result, dict) else {"value": result}

    def _request(self, method: str, params: dict) -> dict:
        if self.transport == "stdio":
            return self._stdio_request(method, params)
        if self.transport == "http":
            return self._http_request(method, params)
        raise MCPConfigError(
            f"MCP server {self.name} 未知传输方式: {self.transport}")

    # ── 对外接口 ─────────────────────────────────────────────

    def discover_tools(self) -> list[str]:
        """发现工具列表（进程内缓存，避免重复调用 tools/list）。"""
        if self._tools is None:
            with self._lock:
                if self._tools is None:
                    res = self._request("tools/list", {})
                    tools = res.get("tools") or []
                    self._tools = [t["name"] for t in tools if t.get("name")]
        return self._tools

    def call(self, tool: str, params: dict | None = None) -> dict:
        return self._request("tools/call", {"name": tool, "arguments": params or {}})

    def close(self):
        if self._proc is not None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        logger.info("MCP server 已关闭: %s", self.name)
