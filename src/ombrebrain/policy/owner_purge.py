"""Owner-authorized physical erasure for already archived memory buckets.

This module deliberately sits outside the ordinary ``trace`` implementation.
The upstream contract remains unchanged: ``hard_delete`` still means
"erasable test data only" unless the deployment owner explicitly enables this
extra policy and supplies the exact per-bucket confirmation phrase.

The owner path is intentionally narrow:
- disabled by default (``OMBRE_ALLOW_OWNER_PURGE`` must be true)
- archived buckets only
- clean, single-purpose ``hard_delete`` calls only
- exact confirmation: ``永久删除:<bucket_id>`` (an optional reason may follow
  after `` | ``)
- Markdown truth, media owned by the bucket, vector/outbox state and
  unshared source evidence are removed
- the append-only ledger receives only a content-free purge receipt
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any

import frontmatter

from ombrebrain.projection.projection_sqlite import TraceSQLiteProjection
from ombrebrain.storage.source_store import (
    normalize_source_refs,
    referenced_source_ids_from_markdown,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_EMPTY_TEXT_FIELDS = {
    "name",
    "domain",
    "tags",
    "content",
    "status",
    "why_remembered",
    "meaning_append",
    "old_str",
}
_NEGATIVE_ONE_FIELDS = {
    "valence",
    "arousal",
    "importance",
    "resolved",
    "pinned",
    "digested",
    "weight",
    "dont_surface",
}
_NONE_FIELDS = {"meaning_replace", "media_replace", "new_str"}


def _enabled() -> bool:
    return (
        os.environ.get("OMBRE_ALLOW_OWNER_PURGE", "").strip().lower()
        in _TRUE_VALUES
    )


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_VALUES
    return bool(value)


def _confirmation_matches(bucket_id: str, value: Any) -> bool:
    supplied = str(value or "").strip()
    expected = f"永久删除:{bucket_id}"
    return supplied == expected or supplied.startswith(expected + " | ")


def _request_has_conflicts(request: dict[str, Any]) -> bool:
    if _truthy(request.get("delete")) or _truthy(request.get("restore")):
        return True
    for key in _EMPTY_TEXT_FIELDS:
        if str(request.get(key) or ""):
            return True
    for key in _NEGATIVE_ONE_FIELDS:
        value = request.get(key, -1)
        try:
            if float(value) != -1:
                return True
        except (TypeError, ValueError, OverflowError):
            return True
    for key in _NONE_FIELDS:
        if request.get(key) is not None:
            return True
    media_append = request.get("media_append")
    if media_append not in (None, [], ""):
        return True
    return False


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_media_dir(bucket_mgr: Any, bucket_id: str) -> Path:
    media_root = Path(bucket_mgr.media_store.media_dir).resolve()
    safe_bucket = re.sub(r"[^a-zA-Z0-9_.-]", "_", bucket_id)[:128]
    target = (media_root / safe_bucket).resolve()
    if target != media_root and media_root not in target.parents:
        raise ValueError("媒体目录越界")
    return target


def _remaining_source_ids(bucket_mgr: Any, excluded_path: Path) -> set[str]:
    directories = [
        bucket_mgr.permanent_dir,
        bucket_mgr.dynamic_dir,
        bucket_mgr.archive_dir,
        bucket_mgr.feel_dir,
        bucket_mgr.plan_dir,
        bucket_mgr.letter_dir,
    ]
    used: set[str] = set()
    excluded = excluded_path.resolve()
    for _root, _filename, candidate in bucket_mgr._iter_md_files(directories):
        path = Path(candidate)
        try:
            if path.resolve() == excluded:
                continue
            used.update(referenced_source_ids_from_markdown(path.read_bytes()))
        except Exception as exc:
            raise RuntimeError(
                f"无法安全核对其余桶的原文引用：{path.name}: {exc}"
            ) from exc
    return used


def _remove_unshared_sources(
    bucket_mgr: Any,
    source_ids: set[str],
    remaining_ids: set[str],
) -> tuple[int, int]:
    source_root = (Path(bucket_mgr.base_dir).resolve() / "_sources").resolve()
    removed = 0
    retained_shared = 0
    for ref in sorted(source_ids):
        if ref in remaining_ids:
            retained_shared += 1
            continue
        target = (source_root / f"{ref}.source").resolve()
        if source_root not in target.parents:
            raise RuntimeError(f"原文证据路径越界：{ref}")
        if target.exists():
            if target.is_symlink():
                raise RuntimeError(f"拒绝删除符号链接原文证据：{ref}")
            target.unlink()
            removed += 1
    return removed, retained_shared


def _rebuild_shadow_projection(bucket_mgr: Any) -> None:
    projection_path = (
        Path(bucket_mgr.base_dir)
        / "_ledger"
        / "projections"
        / "trace_catalog.sqlite3"
    )
    projection = TraceSQLiteProjection(projection_path)
    projection.rebuild(bucket_mgr.ledger_mirror.iter_events())


async def maybe_owner_purge(
    bucket_mgr: Any,
    request: dict[str, Any],
) -> str | None:
    """Handle an enabled owner purge or return ``None`` for upstream behavior."""

    if not _truthy(request.get("hard_delete")) or not _enabled():
        return None

    bucket_id = str(request.get("bucket_id") or "").strip()
    if not bucket_id:
        return "拒绝所有者永久删除：缺少 bucket_id；本次未删除。"

    # Only archived ordinary memories are intercepted. Active/test buckets keep
    # using upstream hard_delete semantics without any behavior change.
    initial_path_raw = bucket_mgr._find_bucket_file(bucket_id)
    if not initial_path_raw:
        return None
    initial_path = Path(initial_path_raw)
    archive_root = Path(bucket_mgr.archive_dir)
    if not _is_inside(initial_path, archive_root):
        return None

    if _request_has_conflicts(request):
        return (
            "拒绝所有者永久删除：该调用必须只包含 bucket_id、hard_delete=True "
            "和 delete_reason；本次未删除。"
        )

    confirmation = str(request.get("delete_reason") or "").strip()
    if len(confirmation) > 500:
        return "拒绝所有者永久删除：delete_reason 不能超过 500 个字符；本次未删除。"
    if not _confirmation_matches(bucket_id, confirmation):
        return (
            "拒绝所有者永久删除：必须精确提供 "
            f"delete_reason=\"永久删除:{bucket_id}\"；本次未删除。"
        )

    source_ids: set[str] = set()
    remaining_source_ids: set[str] = set()
    bucket_type = "archived"
    file_path = initial_path

    async with bucket_mgr._bucket_turn(bucket_id):
        current_path_raw = bucket_mgr._find_bucket_file(bucket_id)
        if not current_path_raw:
            return f"未找到记忆桶: {bucket_id}"
        file_path = Path(current_path_raw)
        if not _is_inside(file_path, archive_root):
            return (
                "拒绝所有者永久删除：记忆桶已经不在 archive 中；本次未删除。"
            )
        if file_path.is_symlink():
            return "拒绝所有者永久删除：归档桶不能是符号链接；本次未删除。"

        try:
            post = frontmatter.load(str(file_path))
            metadata = dict(post.metadata or {})
            bucket_type = str(metadata.get("type") or "archived")
            source_ids = {
                item["ref"]
                for item in normalize_source_refs(metadata.get("source_refs") or [])
            }
            # Resolve sharing before deleting the truth file. A malformed
            # unrelated reference fails closed instead of risking shared data.
            remaining_source_ids = _remaining_source_ids(bucket_mgr, file_path)
        except Exception as exc:
            return f"拒绝所有者永久删除：删除前核对失败：{exc}；本次未删除。"

        try:
            file_path.unlink()
        except OSError as exc:
            return f"所有者永久删除失败：无法删除 Markdown：{exc}"

        with bucket_mgr._bucket_path_index_guard:
            bucket_mgr._bucket_path_index.pop(bucket_id, None)
        bucket_mgr._invalidate_bm25()

    cleanup_errors: list[str] = []

    try:
        await bucket_mgr._discard_derived_index_if_terminal(bucket_id)
    except Exception as exc:  # defensive: upstream helper normally swallows details
        cleanup_errors.append(f"派生索引清理失败：{exc}")

    media_removed = False
    try:
        media_dir = _safe_media_dir(bucket_mgr, bucket_id)
        if media_dir.exists():
            if media_dir.is_symlink():
                raise RuntimeError("媒体目录是符号链接")
            shutil.rmtree(media_dir)
            media_removed = True
    except Exception as exc:
        cleanup_errors.append(f"媒体清理失败：{exc}")

    sources_removed = 0
    sources_retained_shared = 0
    try:
        sources_removed, sources_retained_shared = _remove_unshared_sources(
            bucket_mgr,
            source_ids,
            remaining_source_ids,
        )
    except Exception as exc:
        cleanup_errors.append(f"原文证据清理失败：{exc}")

    reason_hash = hashlib.sha256(confirmation.encode("utf-8")).hexdigest()
    bucket_mgr._record_ledger_event(
        "TraceOwnerPurged",
        bucket_id,
        bucket_type,
        "",
        {},
        {
            "content_erased": True,
            "owner_authorized": True,
            "reason_hash": f"sha256:{reason_hash}",
            "source_refs_removed": sources_removed,
            "source_refs_retained_shared": sources_retained_shared,
            "media_removed": media_removed,
            "cleanup_complete": not cleanup_errors,
        },
    )

    try:
        _rebuild_shadow_projection(bucket_mgr)
    except Exception as exc:
        cleanup_errors.append(f"影子投影重建失败：{exc}")

    if cleanup_errors:
        return (
            f"⚠️ 已物理删除记忆桶正文: {bucket_id}，但清理未完全确认："
            + "；".join(cleanup_errors)
        )

    return (
        f"已永久删除所有者授权记忆桶: {bucket_id}"
        f"（原文证据删除 {sources_removed}，共享保留 {sources_retained_shared}，"
        f"媒体目录{'已删除' if media_removed else '不存在'}）"
    )
