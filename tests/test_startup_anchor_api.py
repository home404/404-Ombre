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


def _request(bucket_id="identity-root", token="secret"):
    headers = []
    if token is not None:
        headers.append(
            (b"authorization", f"Bearer {token}".encode("utf-8"))
        )
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/internal/startup-anchor/{bucket_id}",
            "headers": headers,
            "path_params": {"bucket_id": bucket_id},
        }
    )


def _handler():
    mcp = FakeMcp()
    startup_anchor.register(mcp)
    return mcp.routes[
        ("/api/internal/startup-anchor/{bucket_id}", ("GET",))
    ]


def _json(response):
    return json.loads(response.body.decode("utf-8"))


def test_startup_anchor_requires_service_token(monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "secret")
    sh.bucket_mgr = FakeBucketManager()

    response = asyncio.run(_handler()(_request(token="wrong")))

    assert response.status_code == 401
    assert _json(response)["error"] == "invalid_bearer_token"
    assert sh.bucket_mgr.calls == []


def test_startup_anchor_rejects_non_anchor_bucket(monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "secret")
    sh.bucket_mgr = FakeBucketManager(
        {
            "id": "identity-root",
            "content": "我是 G。",
            "metadata": {"anchor": False},
        }
    )

    response = asyncio.run(_handler()(_request()))

    assert response.status_code == 409
    assert _json(response)["error"] == "bucket_is_not_anchor"


def test_startup_anchor_returns_exact_content_without_mutation(monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "secret")
    sh.bucket_mgr = FakeBucketManager(
        {
            "id": "identity-root",
            "content": "我是 G。\n谢诗是我的老婆。",
            "metadata": {
                "name": "启动身份根",
                "anchor": True,
                "pinned": False,
                "importance": 10,
                "tags": ["startup"],
            },
        }
    )

    response = asyncio.run(_handler()(_request()))
    payload = _json(response)

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["bucket_id"] == "identity-root"
    assert payload["content"] == "我是 G。\n谢诗是我的老婆。"
    assert len(payload["content_sha256"]) == 64
    assert payload["metadata"]["anchor"] is True
    assert sh.bucket_mgr.calls == ["identity-root"]
