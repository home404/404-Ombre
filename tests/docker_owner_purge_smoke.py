from __future__ import annotations

import argparse
import http.client
import json
import time
from pathlib import Path
from typing import Any


EXPECTED_TOOLS = {
    "breath",
    "breath_search",
    "breath_advanced",
    "hold",
    "grow",
    "source_read",
    "trace",
    "anchor",
    "release",
    "pulse",
    "plan",
    "letter_write",
    "letter_read",
    "dream",
    "I",
}
TARGET_ID = "smoke-delete-001"
CONTROL_ID = "smoke-control-001"
TARGET_SECRET = "SMOKE_PURGE_SECRET_7f4e6c1b"
UNIQUE_SOURCE_REF = "src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHARED_SOURCE_REF = "src_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class SmokeFailure(RuntimeError):
    pass


def _decode_mcp_body(raw: bytes) -> dict[str, Any] | None:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("data:"):
            candidate = line[5:].strip()
            if candidate:
                return json.loads(candidate)
    return json.loads(text)


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    try:
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, response_headers, raw
    finally:
        connection.close()


def _wait_for_health(host: str, port: int, token: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last_detail = "no response"
    while time.monotonic() < deadline:
        try:
            status, _headers, raw = _request(
                host,
                port,
                "GET",
                "/health",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
            last_detail = f"status={status} body={raw[:300]!r}"
            if status == 200:
                data = json.loads(raw.decode("utf-8"))
                if data.get("status") == "ok":
                    return
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise SmokeFailure(f"health endpoint did not become ready: {last_detail}")


class McpClient:
    def __init__(self, host: str, port: int, token: str) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.session_id = ""
        self.protocol_version = ""
        self.request_id = 0

    def _headers(self, *, protocol: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if protocol and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if protocol and self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(
        self,
        payload: dict[str, Any],
        *,
        protocol: bool,
        allow_empty: bool = False,
    ) -> dict[str, Any] | None:
        status, headers, raw = _request(
            self.host,
            self.port,
            "POST",
            "/mcp",
            body=payload,
            headers=self._headers(protocol=protocol),
            timeout=20.0,
        )
        if status not in {200, 202, 204}:
            raise SmokeFailure(
                f"MCP request failed: status={status} body={raw[:1000]!r}"
            )
        if not raw.strip():
            if allow_empty:
                return None
            raise SmokeFailure("MCP returned an empty body")
        data = _decode_mcp_body(raw)
        if data is None and not allow_empty:
            raise SmokeFailure("MCP body could not be decoded")
        if data and data.get("error"):
            raise SmokeFailure(f"MCP JSON-RPC error: {data['error']}")
        if not protocol:
            self.session_id = headers.get("mcp-session-id", "")
        return data

    def _next_id(self) -> int:
        self.request_id += 1
        return self.request_id

    def initialize(self) -> None:
        data = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "ombre-docker-owner-purge-smoke",
                        "version": "1",
                    },
                },
            },
            protocol=False,
        )
        result = (data or {}).get("result") or {}
        self.protocol_version = str(result.get("protocolVersion") or "")
        if self.protocol_version != "2025-06-18":
            raise SmokeFailure(
                f"unexpected MCP protocol version: {self.protocol_version!r}"
            )
        self._post(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            protocol=True,
            allow_empty=True,
        )

    def list_tools(self) -> set[str]:
        data = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/list",
                "params": {},
            },
            protocol=True,
        )
        tools = ((data or {}).get("result") or {}).get("tools")
        if not isinstance(tools, list):
            raise SmokeFailure(f"invalid tools/list payload: {data!r}")
        return {str(tool.get("name")) for tool in tools if isinstance(tool, dict)}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        data = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            protocol=True,
        )
        result = (data or {}).get("result") or {}
        if result.get("isError") is True:
            raise SmokeFailure(f"tool {name} returned isError: {result!r}")
        content = result.get("content")
        if not isinstance(content, list):
            raise SmokeFailure(f"tool {name} returned invalid content: {result!r}")
        text = "\n".join(
            str(item.get("text"))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        if not text:
            raise SmokeFailure(f"tool {name} returned no text: {result!r}")
        return text


def _assert_filesystem_erasure(vault: Path) -> None:
    target_files = [
        path
        for path in vault.rglob("*.md")
        if TARGET_ID in path.name or TARGET_ID in path.read_text("utf-8", errors="ignore")
    ]
    if target_files:
        raise SmokeFailure(f"target Markdown still exists: {target_files}")

    control_files = [
        path
        for path in vault.rglob("*.md")
        if CONTROL_ID in path.name or CONTROL_ID in path.read_text("utf-8", errors="ignore")
    ]
    if not control_files:
        raise SmokeFailure("control bucket disappeared")

    if (vault / "_media" / TARGET_ID).exists():
        raise SmokeFailure("target media directory still exists")
    if (vault / "_sources" / f"{UNIQUE_SOURCE_REF}.source").exists():
        raise SmokeFailure("unique source evidence still exists")
    if not (vault / "_sources" / f"{SHARED_SOURCE_REF}.source").exists():
        raise SmokeFailure("shared source evidence was incorrectly removed")

    receipt = None
    for jsonl in (vault / "_ledger").rglob("*.jsonl"):
        for line in jsonl.read_text("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                event.get("event_type") == "TraceOwnerPurged"
                and event.get("trace_id") == TARGET_ID
            ):
                receipt = event
    if receipt is None:
        raise SmokeFailure("TraceOwnerPurged receipt was not written")
    if TARGET_SECRET in json.dumps(receipt, ensure_ascii=False):
        raise SmokeFailure("purge receipt leaked erased content")
    payload = receipt.get("payload") or {}
    if payload.get("content_erased") is not True:
        raise SmokeFailure(f"purge receipt is incomplete: {receipt!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18001)
    parser.add_argument("--token", required=True)
    parser.add_argument("--vault", type=Path, required=True)
    args = parser.parse_args()

    _wait_for_health(args.host, args.port, args.token)

    client = McpClient(args.host, args.port, args.token)
    client.initialize()
    tools = client.list_tools()
    if tools != EXPECTED_TOOLS:
        raise SmokeFailure(
            "unexpected tool inventory: "
            f"missing={sorted(EXPECTED_TOOLS - tools)} extra={sorted(tools - EXPECTED_TOOLS)}"
        )

    result = client.call_tool(
        "trace",
        {
            "bucket_id": TARGET_ID,
            "hard_delete": True,
            "delete_reason": f"永久删除:{TARGET_ID} | GitHub Actions smoke test",
        },
    )
    expected = f"已永久删除所有者授权记忆桶: {TARGET_ID}"
    if expected not in result:
        raise SmokeFailure(f"unexpected purge response: {result}")

    _assert_filesystem_erasure(args.vault.resolve())
    print("Docker MCP owner-purge smoke test passed")


if __name__ == "__main__":
    main()
