"""scripts/bitable_clear_all.py — 清空「客户循环·贾维斯驾驶舱」账户表的全部记录(本机跑)。

用于冷启动失败重来前的"清干净重开"。默认只列出、不删(dry-run);要真删加 --yes。

    python -m scripts.bitable_clear_all            # dry-run:只打印会删多少条 + 前几条样例
    python -m scripts.bitable_clear_all --yes       # 真删:清空整张表

只删记录,不删表本身/不动字段结构。按 400 条一批调用 batch_delete。
"""
from __future__ import annotations

import sys

from prospecting import bitable_client as bc


def main() -> int:
    yes = "--yes" in sys.argv[1:]

    at, tid = bc._cfg()
    print("app_token/table_id:", at, tid)
    if not (at and tid):
        print("❌ 没配 BITABLE_APP_TOKEN / BITABLE_TABLE_ID(.env)")
        return 2

    client = bc.get_client()
    print("\n[1] 读现有全部记录 …")
    try:
        existing = bc.list_records(client)
    except Exception as e:
        print(f"❌ 读失败:{type(e).__name__}: {e}")
        return 3

    record_ids = [v["record_id"] for v in existing.values() if v.get("record_id")]
    print(f"  现有 {len(record_ids)} 条记录。")
    if not record_ids:
        print("✅ 表本来就是空的,不用清。")
        return 0

    sample = list(existing.items())[:5]
    print("  样例(前5条):", [name for name, _ in sample])

    if not yes:
        print("\n[dry-run] 没加 --yes,不会真删。确认要清空就加 --yes 重跑。")
        return 0

    from lark_oapi.api.bitable.v1 import (
        BatchDeleteAppTableRecordRequest, BatchDeleteAppTableRecordRequestBody,
    )

    print(f"\n[2] 真删 {len(record_ids)} 条 …")
    deleted = 0
    for i in range(0, len(record_ids), 400):
        chunk = record_ids[i:i + 400]
        req = (BatchDeleteAppTableRecordRequest.builder()
               .app_token(at).table_id(tid)
               .request_body(BatchDeleteAppTableRecordRequestBody.builder()
                             .records(chunk).build())
               .build())
        resp = client.bitable.v1.app_table_record.batch_delete(req)
        if not resp.success():
            print(f"❌ 第 {i}-{i+len(chunk)} 批删除失败 code={resp.code} msg={resp.msg}")
            print(f"   已成功删除 {deleted} 条,中断于此批,重跑本脚本会继续清剩下的。")
            return 4
        deleted += len(chunk)
        print(f"  已删 {deleted}/{len(record_ids)} …")

    print(f"\n✅ 清空完成,共删 {deleted} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
