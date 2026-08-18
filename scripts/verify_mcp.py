"""Phase C3 验证：MCP 框架（bridge stdio/http + registry + mock server）。

覆盖：
  (a) stdio bridge：发现工具 / 调用 mock_quote 断言契约 / ping / close
  (b) registry.call_mcp_tool + quote_fallback（source=mcp_mock, is_mock=True）
  (c) MCP_ENABLED=false → 干净报错
  (d) 无 server → 干净报错；工具不存在 → 干净报错
  (e) 未知传输 → 干净报错
  (f) http bridge：子进程 http 模式 → 发现/调用

运行: D:\\python\\python.exe scripts\\verify_mcp.py
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MCP_SCRIPT = ROOT / "scripts" / "run_mock_mcp_server.py"

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _raises(exc: type[BaseException], fn) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception as e:  # noqa: BLE001
        check("(unexpected)", False, f"{type(e).__name__}: {e}")
        return False
    return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Cfg:
    def __init__(self, enabled: bool = True, servers: list | None = None):
        self.MCP_ENABLED = enabled
        self.MCP_SERVERS = servers or []


def main():
    from app.mcp.bridge import MCPConfigError, MCPError, MCPServer

    # ── (a) stdio bridge ──
    server = MCPServer(name="mock", transport="stdio",
                       command=sys.executable,
                       args=[str(MCP_SCRIPT), "--mode", "stdio"])
    tools = server.discover_tools()
    check("(a) 工具发现", "mock_quote" in tools and "ping" in tools, str(tools))

    res = server.call("mock_quote", {"ticker": "TEST"})
    quote = res.get("quote") or {}
    check("(a) mock_quote 返回契约", quote.get("current_price") == 100.0
          and quote.get("ticker") == "TEST", str(quote))
    check("(a) 带内容块", any("Mock 报价" in (c.get("text") or "")
                            for c in res.get("content") or []))
    check("(a) ping 正常", server.call("ping") == {})
    server.close()

    # ── (b) registry + quote_fallback ──
    from app.mcp import registry

    registry._server_cache.clear()
    cfg = _Cfg(True, [{"name": "mock", "transport": "stdio",
                       "command": sys.executable,
                       "args": [str(MCP_SCRIPT), "--mode", "stdio"]}])
    with mock.patch("app.mcp.registry.get_settings", lambda: cfg):
        q = registry.quote_fallback("TEST")
        check("(b) quote_fallback 标注 mock",
              q.get("source") == "mcp_mock" and q.get("is_mock") is True
              and q.get("current_price") == 100.0, str(q))
        t = registry.call_mcp_tool("ping")
        check("(b) call_mcp_tool ping", t == {})

    # ── (c) MCP_ENABLED=false ──
    with mock.patch("app.mcp.registry.get_settings",
                    lambda: _Cfg(False, cfg.MCP_SERVERS)):
        ok = _raises(MCPConfigError, lambda: registry.call_mcp_tool("mock_quote"))
        check("(c) 未启用 → 干净报错", ok)

    # ── (d) 无 server / 工具不存在 ──
    with mock.patch("app.mcp.registry.get_settings", lambda: _Cfg(True, [])):
        ok = _raises(MCPConfigError, lambda: registry.call_mcp_tool("mock_quote"))
        check("(d) 无 server → 干净报错", ok)
    with mock.patch("app.mcp.registry.get_settings", lambda: cfg):
        ok = _raises(MCPConfigError, lambda: registry.call_mcp_tool("nope_tool"))
        check("(d) 工具不存在 → 干净报错", ok)

    # ── (e) 未知传输 ──
    bad = MCPServer(name="bad", transport="xyz")
    ok = _raises(MCPConfigError, lambda: bad.discover_tools())
    check("(e) 未知传输 → 干净报错", ok)

    # ── (f) http bridge（子进程 http 模式）──
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(MCP_SCRIPT), "--mode", "http", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        http_srv = MCPServer(name="mock-http", transport="http",
                             url=f"http://127.0.0.1:{port}")
        # 等待 http server 就绪（最多 5s）
        tools_http = []
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                tools_http = http_srv.discover_tools()
                break
            except MCPError:
                time.sleep(0.2)
        check("(f) http 工具发现", "mock_quote" in tools_http, str(tools_http))
        res_http = http_srv.call("mock_quote", {"ticker": "DEMO"})
        check("(f) http mock_quote 契约",
              (res_http.get("quote") or {}).get("ticker") == "DEMO",
              str(res_http.get("quote")))
        http_srv.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
