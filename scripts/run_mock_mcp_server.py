"""内置演示 MCP server —— stdio / http 双模式，JSON-RPC 2.0。

实现工具：
  ping        健康检查
  mock_quote  固定演示报价（显式标注非真实数据，仅供链路验证）

用法:
  D:\\python\\python.exe scripts\\run_mock_mcp_server.py --mode stdio
  D:\\python\\python.exe scripts\\run_mock_mcp_server.py --mode http --port 8090
"""
from __future__ import annotations

import argparse
import json
import sys

# 管道模式下 stdout 编码可能退化为 GBK，强制 UTF-8（供 bridge 按 UTF-8 解码）
for _s in (sys.stdout, sys.stderr, sys.stdin):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── 工具定义 ─────────────────────────────────────────────────

MOCK_QUOTES = {
    "TEST": {"ticker": "TEST", "company_name": "MCP Mock 测试股",
             "current_price": 100.0, "currency": "USD", "change_percent": 0.5},
    "DEMO": {"ticker": "DEMO", "company_name": "MCP Mock 演示股",
             "current_price": 200.0, "currency": "USD", "change_percent": -1.2},
}

_TOOLS = [
    {"name": "ping",
     "description": "健康检查，返回 {}",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mock_quote",
     "description": "返回固定演示报价（非真实行情，仅供 MCP 链路验证）",
     "inputSchema": {"type": "object",
                     "properties": {"ticker": {"type": "string"}},
                     "required": ["ticker"]}},
]


def _handle(method: str, params: dict) -> dict:
    if method == "initialize":
        return {"protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp", "version": "1.0.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _TOOLS}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if name == "ping":
            return {}
        if name == "mock_quote":
            ticker = str(args.get("ticker", "TEST")).upper()
            quote = dict(MOCK_QUOTES.get(ticker, MOCK_QUOTES["TEST"]))
            return {
                "content": [{"type": "text",
                             "text": f"Mock 报价 {ticker}: {quote['current_price']}"}],
                "quote": quote,
                "isError": False,
            }
        return {"content": [{"type": "text", "text": f"未知工具: {name}"}],
                "isError": True}
    if method == "notifications/initialized":
        return {}
    return {"content": [{"type": "text", "text": f"未知方法: {method}"}],
            "isError": True}


def _respond(msg: dict) -> dict:
    """处理一条 JSON-RPC 请求，返回响应（通知类无 id → None）。"""
    if "id" not in msg:
        return None  # notification
    try:
        result = _handle(msg.get("method", ""), msg.get("params") or {})
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}
    except Exception as e:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32603, "message": str(e)}}


# ── stdio 模式 ──────────────────────────────────────────────

def run_stdio() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _respond(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


# ── http 模式（简化 JSON-RPC POST /rpc）────────────────────
# 用原始 socket 手写 HTTP 解析：本机 http.server 的头部解析会把空行算进 body
#（实测 Content-Length 虚高 4 字节），这里按 \r\n\r\n 定位头部边界，天然正确。

def run_http(port: int) -> int:
    import socket
    import threading

    def _handle(conn) -> None:
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            request_line = head.split(b"\r\n", 1)[0].decode("utf-8", "replace")
            if request_line.split(" ", 2)[1].rstrip("/") != "/rpc":
                _send(conn, 404, {"jsonrpc": "2.0", "error": {"code": -32601,
                                                              "message": "Not Found"}})
                return
            length = 0
            for line in head.split(b"\r\n"):
                low = line.lower()
                if low.startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip() or 0)
            body = rest
            while len(body) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk
            body = body[:length]
            try:
                msg = json.loads(body.decode("utf-8"))
                resp = _respond(msg)
                _send(conn, 200, resp or {})
            except (json.JSONDecodeError, UnicodeDecodeError):
                _send(conn, 400, {"jsonrpc": "2.0",
                                  "error": {"code": -32700, "message": "非法 JSON"}})
        finally:
            conn.close()

    def _send(conn, status: int, payload: dict) -> None:
        out = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status_line = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
        conn.sendall(
            f"HTTP/1.1 {status} {status_line}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(out)}\r\n"
            f"Connection: close\r\n\r\n".encode("ascii") + out,
        )

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    print(f"mock MCP http server 启动: http://127.0.0.1:{port}/rpc", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser(description="内置演示 MCP server")
    ap.add_argument("--mode", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()
    if args.mode == "http":
        return run_http(args.port)
    return run_stdio()


if __name__ == "__main__":
    sys.exit(main())
