"""
Internal startup-anchor read route for trusted 404 workers.

This is intentionally not a model-facing MCP tool.  A resident worker uses it
before any model call to verify the exact identity/relationship anchor buckets
that are required for an autonomous wake.  Reads are exact by bucket id and do
not touch activation counters, decay timestamps, embeddings, or model APIs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh


_BUCKET_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,500}$")


def _bearer_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "").strip()
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def _service_token_status(request: Request) -> tuple[bool, str]:
    expected = str(os.environ.get("OMBRE_MCP_TOKEN") or "").strip()
    if not expected:
        return False, "service_token_unconfigured"

    presented = _bearer_token(request)
    if not presented:
        return False, "missing_bearer_token"

    if not hmac.compare_digest(
        hashlib.sha256(presented.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    ):
        return False, "invalid_bearer_token"

    return True, ""


def _public_metadata(metadata: dict) -> dict:
    return {
        "name": str(metadata.get("name") or ""),
        "type": str(metadata.get("type") or ""),
        "domain": str(metadata.get("domain") or ""),
        "anchor": bool(metadata.get("anchor", False)),
        "pinned": bool(metadata.get("pinned", False)),
        "protected": bool(metadata.get("protected", False)),
        "importance": metadata.get("importance"),
        "tags": list(metadata.get("tags") or []),
        "valence": metadata.get("valence"),
        "arousal": metadata.get("arousal"),
        "resolved": bool(metadata.get("resolved", False)),
        "digested": bool(metadata.get("digested", False)),
        "source_bucket": metadata.get("source_bucket"),
        "created_at": metadata.get("created_at") or metadata.get("created"),
        "updated_at": metadata.get("updated_at") or metadata.get("last_active"),
    }


def register(mcp) -> None:
    @mcp.custom_route(
        "/api/internal/memory/{bucket_id}",
        methods=["GET"],
    )
    async def api_internal_memory(request: Request) -> Response:
        """Read one exact, available bucket without search or state mutation."""
        authorized, auth_error = _service_token_status(request)
        if not authorized:
            status_code = 503 if auth_error == "service_token_unconfigured" else 401
            return JSONResponse(
                {
                    "ok": False,
                    "error": auth_error,
                },
                status_code=status_code,
                headers={"Cache-Control": "no-store"},
            )

        bucket_id = str(request.path_params.get("bucket_id") or "").strip()
        if not _BUCKET_ID_RE.fullmatch(bucket_id):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "invalid_bucket_id",
                },
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )

        try:
            bucket = await sh.bucket_mgr.get(bucket_id)
        except Exception:
            sh.logger.exception(
                "exact memory read failed for bucket_id=%s",
                bucket_id,
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": "memory_read_failed",
                },
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

        if not bucket:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "memory_not_found",
                    "bucket_id": bucket_id,
                },
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )

        metadata = dict(bucket.get("metadata") or {})
        if metadata.get("deleted_at") or metadata.get("tombstone"):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "memory_unavailable",
                    "bucket_id": bucket_id,
                },
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )

        content = str(bucket.get("content") or "").strip()
        if not content:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "memory_empty",
                    "bucket_id": bucket_id,
                },
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )

        return JSONResponse(
            {
                "ok": True,
                "bucket_id": bucket_id,
                "content": content,
                "content_sha256": hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest(),
                "metadata": _public_metadata(metadata),
            },
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route(
        "/api/internal/startup-anchor/{bucket_id}",
        methods=["GET"],
    )
    async def api_internal_startup_anchor(request: Request) -> Response:
        authorized, auth_error = _service_token_status(request)
        if not authorized:
            status_code = 503 if auth_error == "service_token_unconfigured" else 401
            return JSONResponse(
                {
                    "ok": False,
                    "error": auth_error,
                },
                status_code=status_code,
                headers={"Cache-Control": "no-store"},
            )

        bucket_id = str(request.path_params.get("bucket_id") or "").strip()
        if not _BUCKET_ID_RE.fullmatch(bucket_id):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "invalid_bucket_id",
                },
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )

        try:
            bucket = await sh.bucket_mgr.get(bucket_id)
        except Exception:
            sh.logger.exception(
                "startup anchor read failed for bucket_id=%s",
                bucket_id,
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": "startup_anchor_read_failed",
                },
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

        if not bucket:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "startup_anchor_not_found",
                    "bucket_id": bucket_id,
                },
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )

        metadata = dict(bucket.get("metadata") or {})
        if metadata.get("deleted_at") or metadata.get("tombstone"):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "startup_anchor_unavailable",
                    "bucket_id": bucket_id,
                },
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )

        if not bool(metadata.get("anchor", False)):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "bucket_is_not_anchor",
                    "bucket_id": bucket_id,
                },
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )

        content = str(bucket.get("content") or "").strip()
        if not content:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "startup_anchor_empty",
                    "bucket_id": bucket_id,
                },
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )

        return JSONResponse(
            {
                "ok": True,
                "bucket_id": bucket_id,
                "content": content,
                "content_sha256": hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest(),
                "metadata": _public_metadata(metadata),
            },
            headers={"Cache-Control": "no-store"},
        )
