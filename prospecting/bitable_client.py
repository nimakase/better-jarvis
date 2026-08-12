"""prospecting/bitable_client.py — 飞书多维表格「客户循环驾驶舱」读写客户端。

用 lark-oapi。凭据取 config.FEISHU_APP_ID/SECRET;Base/表取环境变量(或 prospecting.settings,
  2026-08-12 起从 config 迁出,见该模块 docstring):
  BITABLE_APP_TOKEN / BITABLE_TABLE_ID(见 scripts.bitable_bootstrap 建表后打印)。

分工(见 docs/客户循环-view管理重设计.md §12/13):
  - 贾维斯【写】的列:账户名 / 段 / 状态 / 分层 / 轮次 / 上次outreach / decay阶段 / 赢单数 / 更新时间;
  - Ned【手写】的列:list质量(你写) / 处置(你选)—— 贾维斯【绝不覆盖】,只【读回】当处置信号。
按"账户名"upsert(存在则 patch 贾维斯列、不碰 Ned 列;不存在则新建)。纯 API(不碰浏览器)。
"""
from __future__ import annotations

import os
from typing import Optional

# 贾维斯写的列 / Ned 手写的列(读回)
JARVIS_FIELDS = ["账户名", "段", "状态", "分层", "轮次", "上次outreach", "decay阶段", "赢单数", "更新时间"]
NED_FIELDS = ["list质量(你写)", "处置(你选)"]


def _norm(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def _cfg() -> tuple:
    """(app_token, table_id):优先环境变量,回退 prospecting.settings 同名属性。

    2026-08-12:回退目标从 config.BITABLE_APP_TOKEN/TABLE_ID 迁到 prospecting.settings
    (诊断见项目记忆 jarvis-architecture-migration-plan ②),.env 变量名不变。
    """
    at = os.environ.get("BITABLE_APP_TOKEN")
    tid = os.environ.get("BITABLE_TABLE_ID")
    if not (at and tid):
        try:
            from prospecting import settings as cl_settings
            at = at or getattr(cl_settings, "BITABLE_APP_TOKEN", None)
            tid = tid or getattr(cl_settings, "BITABLE_TABLE_ID", None)
        except Exception:
            pass
    return at, tid


def get_client():
    """构建 lark 客户端(飞书应用凭据)。缺凭据/缺 lark-oapi 抛异常,调用方兜底。"""
    import config
    import lark_oapi as lark
    app_id = getattr(config, "FEISHU_APP_ID", "") or ""
    app_secret = getattr(config, "FEISHU_APP_SECRET", "") or ""
    if not (app_id and app_secret):
        raise RuntimeError("缺飞书应用凭据(FEISHU_APP_ID/SECRET)")
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def list_records(client=None) -> dict:
    """列全表 → {归一账户名: {"record_id", "fields"}}。翻页拉全。"""
    from lark_oapi.api.bitable.v1 import ListAppTableRecordRequest
    client = client or get_client()
    at, tid = _cfg()
    out, token = {}, None
    while True:
        b = ListAppTableRecordRequest.builder().app_token(at).table_id(tid).page_size(500)
        if token:
            b = b.page_token(token)
        resp = client.bitable.v1.app_table_record.list(b.build())
        if not resp.success():
            raise RuntimeError(f"bitable list 失败 code={resp.code} msg={resp.msg}")
        data = resp.data
        for r in (getattr(data, "items", None) or []):
            fields = getattr(r, "fields", None) or {}
            name = fields.get("账户名")
            if name:
                out[_norm(name)] = {"record_id": getattr(r, "record_id", None), "fields": fields}
        if getattr(data, "has_more", False) and getattr(data, "page_token", None):
            token = data.page_token
        else:
            break
    return out


def _chunks(lst, n=400):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _jarvis_only(row: dict) -> dict:
    """只留贾维斯该写的列(丢掉 Ned 手写列 + 未知列),避免覆盖 Ned 的输入。"""
    return {k: row.get(k) for k in JARVIS_FIELDS if k in row and row.get(k) is not None}


def upsert_accounts(rows: list, client=None) -> dict:
    """按"账户名"把各账户的贾维斯列写进驾驶舱:存在→patch(不碰 Ned 列),不存在→新建。

    rows: [{"账户名":..., "段":..., "状态":..., "分层":..., "轮次":int, ...}]。
    返回 {created, updated, skipped}。
    """
    from lark_oapi.api.bitable.v1 import (
        AppTableRecord,
        BatchCreateAppTableRecordRequest, BatchCreateAppTableRecordRequestBody,
        BatchUpdateAppTableRecordRequest, BatchUpdateAppTableRecordRequestBody,
    )
    client = client or get_client()
    at, tid = _cfg()
    existing = list_records(client)

    to_create, to_update, skipped = [], [], 0
    for row in rows:
        name = row.get("账户名")
        if not name:
            continue
        payload = _jarvis_only(row)
        cur = existing.get(_norm(name))
        if cur is None:
            to_create.append(AppTableRecord.builder().fields(payload).build())
        else:
            old = cur["fields"] or {}
            if all(old.get(k) == v for k, v in payload.items()):
                skipped += 1                        # 贾维斯列没变 → 不写
                continue
            to_update.append(AppTableRecord.builder().record_id(cur["record_id"]).fields(payload).build())

    for chunk in _chunks(to_create):
        req = BatchCreateAppTableRecordRequest.builder().app_token(at).table_id(tid).request_body(
            BatchCreateAppTableRecordRequestBody.builder().records(chunk).build()).build()
        resp = client.bitable.v1.app_table_record.batch_create(req)
        if not resp.success():
            raise RuntimeError(f"bitable batch_create 失败 code={resp.code} msg={resp.msg}")
    for chunk in _chunks(to_update):
        req = BatchUpdateAppTableRecordRequest.builder().app_token(at).table_id(tid).request_body(
            BatchUpdateAppTableRecordRequestBody.builder().records(chunk).build()).build()
        resp = client.bitable.v1.app_table_record.batch_update(req)
        if not resp.success():
            raise RuntimeError(f"bitable batch_update 失败 code={resp.code} msg={resp.msg}")
    return {"created": len(to_create), "updated": len(to_update), "skipped": skipped}


def read_dispositions(client=None) -> dict:
    """读回 Ned 手写的处置列 → {归一账户名: {"处置","list质量","账户名"}}(供编排当处置信号)。"""
    recs = list_records(client)
    out = {}
    for key, rec in recs.items():
        f = rec["fields"] or {}
        out[key] = {"账户名": f.get("账户名"), "处置": f.get("处置(你选)"), "list质量": f.get("list质量(你写)")}
    return out
