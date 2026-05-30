"""
下载历史持久化。

每次用户成功通过课程 ID 拉取到课程列表时，调用 record_lookup() 把
{course_id, course_name, professor, last_used, count} 写入
<app_dir>/download_history.json，按 last_used 倒序，最多保留 50 条。

UI 侧（gui_app.py）从课程 ID 输入框旁的 "▼ 历史" 按钮读取并展示，
支持点选回填、单条删除、整体清空。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from typing import Dict, List, Optional

from app_paths import get_app_dir

_FILENAME = "download_history.json"
_MAX_ENTRIES = 50


def _path() -> str:
    return os.path.join(get_app_dir(), _FILENAME)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_history() -> List[Dict]:
    """读取历史；文件缺失或损坏时返回空列表，绝不抛异常。"""
    p = _path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    cleaned: List[Dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cid = item.get("course_id")
        if cid is None or str(cid).strip() == "":
            continue
        cleaned.append({
            "course_id": str(cid).strip(),
            "course_name": str(item.get("course_name") or ""),
            "professor": str(item.get("professor") or ""),
            "last_used": str(item.get("last_used") or ""),
            "count": int(item.get("count") or 1),
        })
    cleaned.sort(key=lambda x: x.get("last_used", ""), reverse=True)
    return cleaned


def save_history(items: List[Dict]) -> None:
    """原子写入：先写同目录临时文件再 replace，避免崩溃留下半截文件。"""
    p = _path()
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    except Exception:
        pass
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(
            prefix=".history_", suffix=".tmp",
            dir=os.path.dirname(p) or None,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        # 历史文件失败不应影响主流程
        try:
            if tmp and os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass


def add_entry(course_id: str,
              course_name: str = "",
              professor: str = "") -> List[Dict]:
    """
    成功拉到课程列表后调用。
    - 已存在同 course_id：刷新 name/professor、count+1、last_used 置当前
    - 不存在：插入新条目
    返回更新后的完整历史列表（已截断至 _MAX_ENTRIES）。
    """
    cid = str(course_id).strip()
    if not cid:
        return load_history()

    items = load_history()
    found: Optional[Dict] = None
    for it in items:
        if it["course_id"] == cid:
            found = it
            break

    if found is not None:
        if course_name:
            found["course_name"] = course_name
        if professor:
            found["professor"] = professor
        found["count"] = int(found.get("count", 1)) + 1
        found["last_used"] = _now()
    else:
        items.append({
            "course_id": cid,
            "course_name": course_name or "",
            "professor": professor or "",
            "last_used": _now(),
            "count": 1,
        })

    items.sort(key=lambda x: x.get("last_used", ""), reverse=True)
    if len(items) > _MAX_ENTRIES:
        items = items[:_MAX_ENTRIES]
    save_history(items)
    return items


def remove_entry(course_id: str) -> List[Dict]:
    """删除指定 course_id 的历史条目，返回更新后的列表。"""
    cid = str(course_id).strip()
    items = [it for it in load_history() if it["course_id"] != cid]
    save_history(items)
    return items


def clear_history() -> None:
    """清空所有历史。"""
    save_history([])


def format_label(item: Dict, max_name_len: int = 28) -> str:
    """统一的展示文本：'40524  数据结构 · 张教授  · 3 次'。"""
    cid = item.get("course_id", "")
    name = (item.get("course_name") or "").strip()
    prof = (item.get("professor") or "").strip()
    cnt = int(item.get("count") or 1)

    if name and len(name) > max_name_len:
        name = name[:max_name_len - 1] + "…"

    parts = [cid]
    if name:
        meta = name + (f" · {prof}" if prof else "")
        parts.append(meta)
    parts.append(f"{cnt} 次")
    return "  ·  ".join(parts)
