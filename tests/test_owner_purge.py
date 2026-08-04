from __future__ import annotations

import hashlib
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import frontmatter
import pytest

from ombrebrain.eventsourcing.ledger_mirror import LedgerMirror
from ombrebrain.policy.owner_purge import maybe_owner_purge
from ombrebrain.projection.projection_mirror import TraceCatalogProjection


class FakeBucketManager:
    def __init__(self, root: Path):
        self.base_dir = str(root)
        self.permanent_dir = str(root / "permanent")
        self.dynamic_dir = str(root / "dynamic")
        self.archive_dir = str(root / "archive")
        self.feel_dir = str(root / "feel")
        self.plan_dir = str(root / "plans")
        self.letter_dir = str(root / "letters")
        for directory in (
            self.permanent_dir,
            self.dynamic_dir,
            self.archive_dir,
            self.feel_dir,
            self.plan_dir,
            self.letter_dir,
        ):
            Path(directory).mkdir(parents=True, exist_ok=True)

        media_dir = root / "_media"
        media_dir.mkdir(parents=True, exist_ok=True)
        self.media_store = SimpleNamespace(media_dir=media_dir)
        self.ledger_mirror = LedgerMirror(root / "_ledger" / "events.jsonl")
        self._bucket_path_index_guard = threading.RLock()
        self._bucket_path_index: dict[str, str] = {}
        self.invalidated = False
        self.derived_discarded: list[str] = []

    def _find_bucket_file(self, bucket_id: str):
        indexed = self._bucket_path_index.get(bucket_id)
        if indexed and Path(indexed).is_file():
            return indexed
        for directory in (
            self.permanent_dir,
            self.dynamic_dir,
            self.archive_dir,
            self.feel_dir,
            self.plan_dir,
            self.letter_dir,
        ):
            for path in Path(directory).rglob("*.md"):
                try:
                    post = frontmatter.load(str(path))
                except Exception:
                    continue
                if str(post.get("id") or "") == bucket_id:
                    self._bucket_path_index[bucket_id] = str(path)
                    return str(path)
        return None

    @asynccontextmanager
    async def _bucket_turn(self, _bucket_id: str):
        yield

    def _iter_md_files(self, directories):
        for directory in directories:
            for path in Path(directory).rglob("*.md"):
                yield str(path.parent), path.name, str(path)

    def _invalidate_bm25(self):
        self.invalidated = True

    async def _discard_derived_index_if_terminal(self, bucket_id: str):
        self.derived_discarded.append(bucket_id)

    def _record_ledger_event(
        self,
        event_type,
        bucket_id,
        bucket_type,
        content,
        metadata,
        extra_payload=None,
    ):
        payload = dict(metadata or {})
        payload.update(extra_payload or {})
        self.ledger_mirror.append_event(
            event_type=event_type,
            trace_id=bucket_id,
            trace_kind=bucket_type,
            payload=payload,
            body=content,
        )


def _write_bucket(
    directory: str,
    bucket_id: str,
    *,
    source_refs=None,
    content="secret",
):
    path = Path(directory) / f"{bucket_id}.md"
    post = frontmatter.Post(content)
    post["id"] = bucket_id
    post["type"] = "dynamic"
    post["domain"] = ["test"]
    if source_refs is not None:
        post["source_refs"] = source_refs
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _request(bucket_id: str, confirmation: str, **overrides):
    request = {
        "bucket_id": bucket_id,
        "name": "",
        "domain": "",
        "valence": -1,
        "arousal": -1,
        "importance": -1,
        "tags": "",
        "resolved": -1,
        "pinned": -1,
        "digested": -1,
        "content": "",
        "delete": False,
        "status": "",
        "weight": -1,
        "dont_surface": -1,
        "why_remembered": "",
        "meaning_append": "",
        "meaning_replace": None,
        "media_append": None,
        "media_replace": None,
        "hard_delete": True,
        "delete_reason": confirmation,
        "restore": False,
        "old_str": "",
        "new_str": None,
    }
    request.update(overrides)
    return request


@pytest.mark.asyncio
async def test_owner_purge_disabled_preserves_archive(tmp_path, monkeypatch):
    manager = FakeBucketManager(tmp_path)
    path = _write_bucket(manager.archive_dir, "abc")
    monkeypatch.delenv("OMBRE_ALLOW_OWNER_PURGE", raising=False)

    result = await maybe_owner_purge(
        manager,
        _request("abc", "永久删除:abc"),
    )

    assert result is None
    assert path.exists()


@pytest.mark.asyncio
async def test_owner_purge_does_not_intercept_active_bucket(tmp_path, monkeypatch):
    manager = FakeBucketManager(tmp_path)
    path = _write_bucket(manager.dynamic_dir, "abc")
    monkeypatch.setenv("OMBRE_ALLOW_OWNER_PURGE", "true")

    result = await maybe_owner_purge(
        manager,
        _request("abc", "永久删除:abc"),
    )

    assert result is None
    assert path.exists()


@pytest.mark.asyncio
async def test_owner_purge_requires_exact_confirmation(tmp_path, monkeypatch):
    manager = FakeBucketManager(tmp_path)
    path = _write_bucket(manager.archive_dir, "abc")
    monkeypatch.setenv("OMBRE_ALLOW_OWNER_PURGE", "true")

    result = await maybe_owner_purge(
        manager,
        _request("abc", "delete it"),
    )

    assert "必须精确提供" in result
    assert path.exists()


@pytest.mark.asyncio
async def test_owner_purge_rejects_mixed_mutation(tmp_path, monkeypatch):
    manager = FakeBucketManager(tmp_path)
    path = _write_bucket(manager.archive_dir, "abc")
    monkeypatch.setenv("OMBRE_ALLOW_OWNER_PURGE", "true")

    result = await maybe_owner_purge(
        manager,
        _request("abc", "永久删除:abc", content="also replace me"),
    )

    assert "必须只包含" in result
    assert path.exists()


@pytest.mark.asyncio
async def test_owner_purge_removes_truth_media_vector_and_unshared_source(
    tmp_path,
    monkeypatch,
):
    manager = FakeBucketManager(tmp_path)
    monkeypatch.setenv("OMBRE_ALLOW_OWNER_PURGE", "true")

    unique_raw = b"unique source"
    unique_ref = f"src_{hashlib.sha256(unique_raw).hexdigest()}"
    shared_raw = b"shared source"
    shared_ref = f"src_{hashlib.sha256(shared_raw).hexdigest()}"
    source_root = tmp_path / "_sources"
    source_root.mkdir()
    (source_root / f"{unique_ref}.source").write_bytes(unique_raw)
    (source_root / f"{shared_ref}.source").write_bytes(shared_raw)

    target = _write_bucket(
        manager.archive_dir,
        "abc",
        source_refs=[
            {"ref": unique_ref, "ranges": []},
            {"ref": shared_ref, "ranges": []},
        ],
    )
    _write_bucket(
        manager.dynamic_dir,
        "other",
        source_refs=[{"ref": shared_ref, "ranges": []}],
    )

    media_dir = tmp_path / "_media" / "abc"
    media_dir.mkdir(parents=True)
    (media_dir / "asset.bin").write_bytes(b"media")

    result = await maybe_owner_purge(
        manager,
        _request("abc", "永久删除:abc | user requested cleanup"),
    )

    assert result.startswith("已永久删除所有者授权记忆桶: abc")
    assert not target.exists()
    assert not media_dir.exists()
    assert not (source_root / f"{unique_ref}.source").exists()
    assert (source_root / f"{shared_ref}.source").exists()
    assert manager.invalidated is True
    assert manager.derived_discarded == ["abc"]

    events = list(manager.ledger_mirror.iter_events())
    assert events[-1]["event_type"] == "TraceOwnerPurged"
    assert events[-1]["body_hash"] == f"sha256:{hashlib.sha256(b'').hexdigest()}"
    assert events[-1]["payload"]["content_erased"] is True
    assert "secret" not in str(events[-1])


def test_owner_purge_event_removes_trace_from_projection():
    projection = TraceCatalogProjection()
    projection.apply(
        {
            "seq": 1,
            "event_type": "TraceCreated",
            "trace_id": "abc",
            "trace_kind": "dynamic",
            "body_hash": "sha256:old",
            "payload": {"name": "private"},
        }
    )
    assert "abc" in projection.traces

    projection.apply(
        {
            "seq": 2,
            "event_type": "TraceOwnerPurged",
            "trace_id": "abc",
            "trace_kind": "dynamic",
            "body_hash": "sha256:empty",
            "payload": {"content_erased": True},
        }
    )

    assert "abc" not in projection.traces
    assert projection.unknown_event_count == 0
