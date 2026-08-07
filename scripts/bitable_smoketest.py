"""scripts/bitable_smoketest.py — 验证 bitable_client 读写(本机,需 .env 配 BITABLE_APP_TOKEN/TABLE_ID)。

    python -m scripts.bitable_smoketest

流程:读现有记录 + 处置列 → upsert 两个测试账户 → 再读回确认。只动"账户"表,幂等(重复跑不会重复建同名)。
验完可去表里手写一格「处置(你选)」,再跑一次看 read_dispositions 有没有读到你写的值。
"""
from __future__ import annotations

import sys

from prospecting import bitable_client as bc


def main() -> int:
    at, tid = bc._cfg()
    print("app_token/table_id:", at, tid)
    if not (at and tid):
        print("❌ 没配 BITABLE_APP_TOKEN / BITABLE_TABLE_ID(.env)")
        return 2

    print("\n[1] 读现有记录 + 处置列 …")
    try:
        disp = bc.read_dispositions()
    except Exception as e:
        print(f"❌ 读失败:{type(e).__name__}: {e}")
        return 3
    print(f"  现有 {len(disp)} 条。样例:", list(disp.items())[:3])

    print("\n[2] upsert 两个测试账户(幂等)…")
    rows = [
        {"账户名": "Sample Co", "段": "待处理·换人或放弃", "状态": "exhausted", "分层": "-",
         "轮次": 3, "上次outreach": "2026-05-03", "decay阶段": "-", "赢单数": 0, "更新时间": "2026-08-07"},
        {"账户名": "Test Core Ltd", "段": "维护到点", "状态": "core", "分层": "T1",
         "轮次": 0, "上次outreach": "-", "decay阶段": "-", "赢单数": 2, "更新时间": "2026-08-07"},
    ]
    try:
        res = bc.upsert_accounts(rows)
    except Exception as e:
        print(f"❌ upsert 失败:{type(e).__name__}: {e}")
        return 4
    print("  upsert 结果:", res)

    print("\n[3] 再读回确认 …")
    disp2 = bc.read_dispositions()
    print(f"  现有 {len(disp2)} 条 | 含 Sample Co:", "sample co" in disp2,
          "| 含 Test Core Ltd:", "test core ltd" in disp2)
    print("  Test Core Ltd 处置列读回:", disp2.get("test core ltd"))

    print("\n✅ upsert 有 created/updated + 读回条数对得上 → 客户端读写通。")
    print("   下一步验处置回读:去表里给某行手写一格「处置(你选)」(如'已处理'),再跑一次本脚本,")
    print("   看 read_dispositions 能不能读到你写的值(那就是编排的处置信号来源)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
