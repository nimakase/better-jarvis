"""
飞书表单卡 · 提交按钮写法探针（手动运行，不入 gate）

背景：present_form 的表单卡被飞书拒收——
  code=230099 "Failed to create card content" … tag: form; there is no submit
  button in the form container, at least one。
即飞书没把我们放进 form 的按钮认成"提交按钮"。飞书 2.0 的准确字段靠猜不可靠，
本脚本把几种候选写法【逐个真发到你的飞书】，哪种飞书接受（success）就是答案。

按顺序试，发成功即停并打印中选变体。会往你飞书发最多几张测试卡。

用法（项目根，需已配飞书凭据 + 收件人）：
    python scripts/test_lark_form.py
"""
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


def _input(name="title", label="事件标题"):
    return {"tag": "input", "name": name, "label": {"tag": "plain_text", "content": label},
            "label_position": "top",
            "placeholder": {"tag": "plain_text", "content": f"请输入{label}"}}


def _card(variant_name, submit_btn):
    """一张最小表单卡：正文 + 一个输入 + 待测的提交按钮。"""
    form = {"tag": "form", "name": "jarvis_form", "elements": [_input(), submit_btn]}
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": f"表单探针 · {variant_name}"},
                   "template": "blue"},
        "body": {"elements": [{"tag": "markdown", "content": f"变体 **{variant_name}**：能看到并点提交就是它对了。"}, form]},
    }


_TXT = {"tag": "plain_text", "content": "提交"}
_CB = [{"type": "callback", "value": {"jarvis_intent": "提交表单"}}]

# 候选提交按钮写法（按可能性排序）
VARIANTS = {
    "V0_现状_action_type+behaviors":
        {"tag": "button", "name": "submit", "text": _TXT, "type": "primary",
         "action_type": "form_submit", "behaviors": _CB},
    "V1_form_action_type=submit":
        {"tag": "button", "name": "submit", "text": _TXT, "type": "primary",
         "form_action_type": "submit", "behaviors": _CB},
    "V2_action_type无behaviors":
        {"tag": "button", "name": "submit", "text": _TXT, "type": "primary",
         "action_type": "form_submit"},
    "V3_behaviors里form_submit":
        {"tag": "button", "name": "submit", "text": _TXT, "type": "primary",
         "behaviors": [{"type": "form_submit", "value": {"jarvis_intent": "提交表单"}}]},
    "V4_action_type=request+form_action":
        {"tag": "button", "name": "submit", "text": _TXT, "type": "primary",
         "action_type": "request", "form_action_type": "submit", "behaviors": _CB},
}


def main() -> int:
    if not (config.FEISHU_APP_ID and config.FEISHU_APP_SECRET):
        print("❌ .env 未配飞书凭据。")
        return 1
    from lark_bridge import LarkBridge
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    bridge = LarkBridge(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET)
    try:
        open_id = bridge._resolve_push_target()
    except Exception as e:  # noqa: BLE001
        print(f"❌ 找不到收件人：{e}")
        return 1
    print(f"✓ 收件人 {open_id[:6]}…\n")

    winner = None
    for name, btn in VARIANTS.items():
        card_json = json.dumps(_card(name, btn), ensure_ascii=False)
        req = (CreateMessageRequest.builder().receive_id_type("open_id")
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(open_id).msg_type("interactive")
                             .content(card_json).build()).build())
        resp = bridge._api.im.v1.message.create(req)
        ok = resp.success()
        print(f"[{name}] success={ok} code={resp.code} msg={resp.msg}")
        if ok:
            winner = name
            print(f"\n✅ 命中：{name} —— 飞书接受了这种写法。去飞书点一下这张卡的提交，"
                  f"看回调是否回流。")
            break

    if not winner:
        print("\n❌ 全部被拒。把每行的 code/msg 贴给我，我据此再调（可能字段名又变了）。")
    else:
        print(f"\n下一步：把 core/lark_cards.form_card 里的提交按钮改成 {winner} 这种写法。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
