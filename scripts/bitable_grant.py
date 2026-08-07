"""scripts/bitable_grant.py — 给 Ned 授予驾驶舱 Base 的编辑权。

应用建的 Base 归应用所有,用户默认只有查看权。这里用应用凭据把你(open_id)加成 full_access 协作者。
open_id 自动取:config.FEISHU_PUSH_OPEN_ID → DATA_DIR/lark_push_target.json(你跟贾维斯飞书说过话就有)→ 命令行参数。

    python -m scripts.bitable_grant [open_id]

若报权限不足:去开发者后台给应用加「云文档管理协作者」类权限(如 drive:drive)、重发布,再跑。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _resolve_open_id(argv) -> str:
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    try:
        import config
        if getattr(config, "FEISHU_PUSH_OPEN_ID", ""):
            return config.FEISHU_PUSH_OPEN_ID
        p = Path(config.DATA_DIR) / "lark_push_target.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")).get("open_id") or ""
    except Exception:
        pass
    return ""


def main() -> int:
    from prospecting import bitable_client as bc
    from lark_oapi.api.drive.v1 import CreatePermissionMemberRequest, BaseMember

    at, _ = bc._cfg()
    if not at:
        print("❌ 没配 BITABLE_APP_TOKEN(.env)")
        return 2
    open_id = _resolve_open_id(sys.argv)
    if not open_id:
        print("❌ 没拿到你的 open_id。先跟贾维斯飞书说句话(会自动记),或:python -m scripts.bitable_grant <你的open_id>")
        return 2

    print(f"给 open_id {open_id[:8]}… 授予 Base {at[:8]}… 的 full_access(编辑权)…")
    client = bc.get_client()
    req = CreatePermissionMemberRequest.builder().token(at).type("bitable").need_notification(True).request_body(
        BaseMember.builder().member_type("openid").member_id(open_id).perm("full_access").build()
    ).build()
    resp = client.drive.v1.permission_member.create(req)
    if resp.success():
        print("✅ 授权成功。去飞书打开那个 Base,现在应该能编辑了(手写 list质量/处置 两列)。")
        return 0
    print(f"❌ 失败 code={resp.code} msg={resp.msg}")
    print("   多半是应用缺『云文档管理协作者』权限 → 开发者后台加权限(如 drive:drive)、重发布,再跑。")
    try:
        print("   raw:", resp.raw.content[:400])
    except Exception:
        pass
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
