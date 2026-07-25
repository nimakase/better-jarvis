"""
飞书文件投递 · 最小诊断脚本（手动运行，不入 gate）

目的：绕开潜客工作流（HubSpot / LLM / 几分钟），只测「飞书发文件」这一段——
和潜客 xlsx 走的是完全相同的路径（im.v1.file.create 上传 → 文件消息）。

它会：
  1) 造一个临时 .txt（等价 xlsx 的「非图片文件」上传路径）和一个 .png（图片路径）；
  2) 建 LarkBridge（只用 __init__ 里的 API 客户端，不启长连接）；
  3) 先发一张文字卡（验证发消息权限 OK）；
  4) 逐个真发文件，把飞书返回的 code / msg / success 明确打印出来——
     这正是平时被 _send_file 吞进日志、你看不到的那一层。

用法（在项目根目录）：
    python scripts/test_lark_file.py
前提：.env 里配好 FEISHU_APP_ID / FEISHU_APP_SECRET；且 FEISHU_PUSH_OPEN_ID
已配，或你此前在飞书里给 jarvis 发过消息（会自动记住 open_id）。
"""
import logging
import sys
import tempfile
from pathlib import Path

# 让 [飞书] 的 INFO/ERROR 行直接打到控制台
logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s | %(name)s | %(message)s")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


def _make_test_files() -> list[Path]:
    d = Path(tempfile.mkdtemp(prefix="lark_test_"))
    txt = d / "潜客名单_测试.txt"
    txt.write_text("这是飞书文件投递测试。若你在飞书收到本文件，文件通道就是好的。\n",
                   encoding="utf-8")
    # 1x1 PNG（测图片上传路径；与 xlsx 无关，只为对照两条 API 是否都通）
    png = d / "test_pixel.png"
    png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000100ff ff03000006"
        "0005a4b6b60000000049454e44ae426082".replace(" ", "")))
    return [txt, png]


def main() -> int:
    if not (config.FEISHU_APP_ID and config.FEISHU_APP_SECRET):
        print("❌ .env 里没配 FEISHU_APP_ID / FEISHU_APP_SECRET，无法测试。")
        return 1

    from lark_bridge import LarkBridge
    bridge = LarkBridge(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET)

    # 收件人
    try:
        open_id = bridge._resolve_push_target()
    except Exception as e:  # noqa: BLE001
        print(f"❌ 找不到收件人：{e}")
        return 1
    print(f"✓ 收件人 open_id = {open_id[:6]}…")

    files = _make_test_files()

    # ── 走完整 push()（和潜客交付一模一样的入口）──────────────────────────
    print("\n--- 调 bridge.push()（文字卡 + 附件），和潜客工作流同一条路 ---")
    result = bridge.push("📎 飞书文件投递测试", "如果你只收到这张卡、没收到文件，"
                         "问题就锁定在文件上传权限。", attachments=[str(f) for f in files])
    print("push() 返回：", result)

    # ── 再对「非图片文件」做一次裸上传，把飞书的 code/msg 明确抠出来 ──────────
    # （push 里失败只 logger.error；这里把原始返回打出来，一眼看出是不是权限）
    print("\n--- 裸调 im.v1.file.create，打印原始 code/msg ---")
    try:
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        txt = files[0]
        with open(txt, "rb") as fh:
            up = bridge._api.im.v1.file.create(
                CreateFileRequest.builder().request_body(
                    CreateFileRequestBody.builder()
                    .file_type("stream").file_name(txt.name).file(fh).build()
                ).build()
            )
        print(f"success={up.success()}  code={up.code}  msg={up.msg}")
        if not up.success():
            print("→ 若 code 提示权限/scope，就是飞书应用少了『上传图片/文件』"
                  "（im:resource）权限。去开发者后台加上、发版、重新授权即可。")
    except Exception as e:  # noqa: BLE001
        print(f"裸上传异常：{e!r}")

    print("\n完成。回飞书看：文字卡到了吗？两个文件到了吗？")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
