import asyncio
import json

from starlette.requests import Request

from web import _shared as sh
from web import startup_anchor


class FakeMcp:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            self.routes[(path, tuple(methods))] = handler
            return handler

        return decorator


class FakeBucketManager:
    def __init__(self, bucket=None):
        self.bucket = bucket
        self.calls = []

    async def get(self, bucket_id):
        self.calls.append(bucket_id)
        return self.bucket


def _request(bucket_id="memory-1", token="secret"):
    headers = []
    if token is not None:
        headers.append(
            (b"authorization", f"Bearer {token}".encode("utf-8"))
        )
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/internal/memory/{bucket_id}",
            "headers": headers,
            "path_params": {"bucket_id": bucket_id},
        }
    )


def _handler():
    mcp = FakeMcp()
    startup_anchor.register(mcp)
    return mcp.routes[
        ("/api/internal/memory/{bucket_id}", ("GET",))
    ]


def _json(response):
    return json.loads(response.body.decode("utf-8"))


def test_exact_memory_requires_service_token(monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "secret")
    sh.bucket_mgr = FakeBucketManager()

    response = asyncio.run(_handler()(_request(token="wrong")))

    assert response.status_code == 401
    assert _json(response)["error"] == "invalid_bearer_token"
    assert sh.bucket_mgr.calls == []


def test_exact_memory_reads_non_anchor_without_mutation(monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "secret")
    sh.bucket_mgr = FakeBucketManager(
        {
            "id": "memory-1",
            "content": "一条普通记忆。",
            "metadata": {
                "name": "普通记忆",
                "domain": "home",
                "anchor": False,
                "importance": 7,
                "tags": ["404"],
                "valence": 0.8,
                "arousal": 0.3,
            },
        }
    )

    response = asyncio.run(_handler()(_request()))
    payload = _json(response)

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["bucket_id"] == "memory-1"
    assert payload["content"] == "一条普通记忆。"
    assert payload["metadata"]["anchor"] is False
    assert payload["metadata"]["domain"] == "home"
    assert len(payload["content_sha256"]) == 64
    assert sh.bucket_mgr.calls == ["memory-1"]


def test_exact_memory_rejects_deleted_bucket(monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "secret")
    sh.bucket_mgr = FakeBucketManager(
        {
            "id": "memory-1",
            "content": "已删除记忆",
            "metadata": {
                "deleted_at": "2026-08-06T00:00:00Z",
            },
        }
    )

    response = asyncio.run(_handler()(_request()))

    assert response.status_code == 409
    assert _json(response)["error"] == "memory_unavailable"


def test_exact_memory_returns_not_found_for_unknown_id(monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "secret")
    sh.bucket_mgr = FakeBucketManager()

    response = asyncio.run(_handler()(_request()))

    assert response.status_code == 404
    assert _json(response)["error"] == "memory_not_found"
