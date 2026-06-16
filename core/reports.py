"""
报告中心（Reports hub）—— 多种日报统一注册 + 归档。

  - 报告"类型"注册：register_report_type(type_id, name, generator)
    generator 是 async，返回 {"path": pdf路径, "title": 标题}
  - generate(type_id, **kw)：跑生成器 → 把结果登记进归档索引（index.json）
  - list_reports() / get_report(id)：供 /reports 中心页与下载用
  - list_types()：可生成的报告类型（中心页"生成"按钮用）

情报台是实时看板；本模块管的是"按日期+类型归档的报告快照"。两者分离。
新增层，纯标准库。
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

try:
    import config
    _DIR = config.DATA_DIR / "reports"
except Exception:
    _DIR = Path(__file__).resolve().parent / "reports"
_INDEX = _DIR / "index.json"

# type_id -> {"name": str, "generator": async (**kw)->{"path","title"}}
_TYPES: dict[str, dict] = {}


def register_report_type(type_id: str, name: str,
                         generator: Callable[..., Awaitable[dict]]) -> None:
    _TYPES[type_id] = {"name": name, "generator": generator}


def list_types() -> list[dict]:
    return [{"type": k, "name": v["name"]} for k, v in _TYPES.items()]


def _load_index() -> list:
    if _INDEX.exists():
        try:
            return json.loads(_INDEX.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_index(idx: list) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    _INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


async def generate(type_id: str, **kwargs) -> dict:
    """生成某类型报告并登记进归档。返回 {"ok":bool, "report"/"error"}。"""
    spec = _TYPES.get(type_id)
    if not spec:
        return {"ok": False, "error": f"未知报告类型：{type_id}"}
    try:
        out = await spec["generator"](**kwargs) or {}
    except Exception as e:
        return {"ok": False, "error": f"生成失败：{e}"}
    rec = {
        "id": uuid.uuid4().hex[:10],
        "type": type_id,
        "type_name": spec["name"],
        "title": out.get("title") or f'{spec["name"]} {date.today().isoformat()}',
        "date": date.today().isoformat(),
        "path": out.get("path"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    idx = _load_index()
    idx.insert(0, rec)
    _save_index(idx)
    return {"ok": True, "report": rec}


def list_reports(limit: int = 50) -> list:
    return _load_index()[:limit]


def get_report(report_id: str) -> Optional[dict]:
    for r in _load_index():
        if r.get("id") == report_id:
            return r
    return None
