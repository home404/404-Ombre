"""
========================================
tools/trace/__init__.py — trace 工具入口
========================================

普通调用原样转发给上游 trace_core。只有部署所有者显式开启额外门禁、
目标桶已经归档且确认短语完全匹配时，才由 owner_purge 处理物理擦除。
这让上游 hard_delete 的“仅测试桶”契约保持不变。

对外暴露：dispatch(...) → str（参数与 server.py 中的 trace tool 同名）
========================================
"""

from typing import Optional

from ombrebrain.policy.owner_purge import maybe_owner_purge

from .. import _runtime as rt
from .core import trace_core


async def dispatch(
    bucket_id: str,
    name: Optional[str] = "",
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    importance: Optional[int] = -1,
    tags: Optional[str] = "",
    resolved: Optional[int] = -1,
    pinned: Optional[int] = -1,
    protected: Optional[int] = -1,
    digested: Optional[int] = -1,
    content: Optional[str] = "",
    delete: Optional[bool] = False,
    status: Optional[str] = "",
    weight: Optional[float] = -1,
    dont_surface: Optional[int] = -1,
    why_remembered: Optional[str] = "",
    meaning_append: Optional[str] = "",
    meaning_replace: Optional[list] = None,
    media_append: Optional[list | str] = None,
    media_replace: Optional[list | str] = None,
    hard_delete: Optional[bool] = False,
    delete_reason: Optional[str] = "",
    restore: Optional[bool] = False,
    old_str: Optional[str] = "",
    new_str: Optional[str] = None,
) -> str:
    request = {
        "bucket_id": bucket_id,
        "name": name,
        "domain": domain,
        "valence": valence,
        "arousal": arousal,
        "importance": importance,
        "tags": tags,
        "resolved": resolved,
        "pinned": pinned,
        "protected": protected,
        "digested": digested,
        "content": content,
        "delete": delete,
        "status": status,
        "weight": weight,
        "dont_surface": dont_surface,
        "why_remembered": why_remembered,
        "meaning_append": meaning_append,
        "meaning_replace": meaning_replace,
        "media_append": media_append,
        "media_replace": media_replace,
        "hard_delete": hard_delete,
        "delete_reason": delete_reason,
        "restore": restore,
        "old_str": old_str,
        "new_str": new_str,
    }

    owner_result = await maybe_owner_purge(rt.bucket_mgr, request)
    if owner_result is not None:
        return owner_result

    return await trace_core(**request)
