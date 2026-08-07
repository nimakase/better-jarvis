"""scripts/bitable_bootstrap.py — 一次性:建飞书多维表格(Bitable)Base + "账户"表 + 样例记录,
并打印 app_token / table_id / 各响应形状(供写正式客户端对形状)。

用法(本机,已配飞书应用凭据 + 已开 bitable:app 权限):
    python -m scripts.bitable_bootstrap
可选:BITABLE_FOLDER_TOKEN=<飞书文件夹token> python -m scripts.bitable_bootstrap
      (不给则建在应用的默认空间)

跑完把整段输出贴/给贾维斯读:拿到 app_token + table_id 后写进 .env,正式客户端就用它俩。
只建一次;重复跑会建多个 Base。
"""
from __future__ import annotations

import os
import sys

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    CreateAppRequest, ReqApp,
    CreateAppTableRequest, CreateAppTableRequestBody, ReqTable, AppTableField,
    BatchCreateAppTableRecordRequest, BatchCreateAppTableRecordRequestBody, AppTableRecord,
    ListAppTableRecordRequest,
)


def _dump(tag, resp):
    ok = resp.success()
    print(f"\n[{tag}] success={ok} code={resp.code} msg={resp.msg}")
    if not ok:
        print(f"  ⚠ 失败。log_id={getattr(resp, 'get_log_id', lambda: '?')()}")
        try:
            print("  raw:", resp.raw.content[:500])
        except Exception:
            pass
        return None
    try:
        print("  data:", lark.JSON.marshal(resp.data)[:900])
    except Exception as e:
        print("  (marshal 失败)", e)
    return resp.data


# "账户"表字段(type: 1=多行文本 2=数字)。段/处置先用文本,你想要下拉可后续在 UI 改单选。
_FIELDS = [
    ("账户名", 1), ("段", 1), ("状态", 1), ("分层", 1), ("轮次", 2),
    ("上次outreach", 1), ("decay阶段", 1), ("赢单数", 2),
    ("list质量(你写)", 1), ("处置(你选)", 1), ("更新时间", 1),
]


def main() -> int:
    import config
    app_id = getattr(config, "FEISHU_APP_ID", "") or ""
    app_secret = getattr(config, "FEISHU_APP_SECRET", "") or ""
    if not (app_id and app_secret):
        print("❌ 没读到飞书应用凭据(config.FEISHU_APP_ID/SECRET)。")
        return 2
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    # 1) 建 Base
    app_builder = ReqApp.builder().name("客户循环 · 贾维斯驾驶舱")
    folder = os.environ.get("BITABLE_FOLDER_TOKEN")
    if folder:
        app_builder = app_builder.folder_token(folder)
    req = CreateAppRequest.builder().request_body(app_builder.build()).build()
    data = _dump("建 Base app.create", client.bitable.v1.app.create(req))
    if data is None:
        return 3
    app_token = getattr(getattr(data, "app", None), "app_token", None) or getattr(data, "app_token", None)
    print("  ★ app_token =", app_token)
    if not app_token:
        print("  ⚠ 没从响应里取到 app_token,把上面的 data 形状发我。")
        return 3

    # 2) 建"账户"表
    fields = [AppTableField.builder().field_name(n).type(t).build() for n, t in _FIELDS]
    table = ReqTable.builder().name("账户").default_view_name("全部").fields(fields).build()
    treq = CreateAppTableRequest.builder().app_token(app_token).request_body(
        CreateAppTableRequestBody.builder().table(table).build()).build()
    tdata = _dump("建表 app_table.create", client.bitable.v1.app_table.create(treq))
    table_id = getattr(tdata, "table_id", None) if tdata else None
    print("  ★ table_id =", table_id)
    if not table_id:
        return 4

    # 3) 写一条样例记录
    rec = AppTableRecord.builder().fields({
        "账户名": "Sample Co", "段": "开发中", "状态": "sequencing", "分层": "-",
        "轮次": 1, "上次outreach": "2026-05-01", "decay阶段": "-", "赢单数": 0,
        "list质量(你写)": "", "处置(你选)": "", "更新时间": "2026-08-07",
    }).build()
    breq = BatchCreateAppTableRecordRequest.builder().app_token(app_token).table_id(table_id).request_body(
        BatchCreateAppTableRecordRequestBody.builder().records([rec]).build()).build()
    _dump("写记录 record.batch_create", client.bitable.v1.app_table_record.batch_create(breq))

    # 4) 列回来看记录形状
    lreq = ListAppTableRecordRequest.builder().app_token(app_token).table_id(table_id).page_size(5).build()
    _dump("列记录 record.list", client.bitable.v1.app_table_record.list(lreq))

    print("\n==============================================")
    print(f"★★ 写进 .env:\n  BITABLE_APP_TOKEN={app_token}\n  BITABLE_TABLE_ID={table_id}")
    print("==============================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
