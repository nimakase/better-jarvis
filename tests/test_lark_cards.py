"""
飞书交互升级的纯逻辑单测：卡片构建（JSON 2.0）、回调→用户意图合成、
带外动作载荷、富文本抽取。全部不依赖 lark-oapi / 网络，可确定性断言。

跑：python3 tests/test_lark_cards.py   （或并入 tests/run_all.py）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import lark_cards as lc
from core.results import present_options, present_form


def test_text_card_is_json_v2():
    card = lc.text_card("你好 **世界**")
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == lc.DEFAULT_TITLE
    els = card["body"]["elements"]
    assert els[0]["tag"] == "markdown"
    assert els[0]["content"] == "你好 **世界**"
    # 非流式卡不应带 streaming_mode
    assert "streaming_mode" not in card["config"]


def test_text_card_streaming_config():
    card = lc.text_card("…", streaming=True)
    assert card["config"]["streaming_mode"] is True
    assert "streaming_config" in card["config"]
    # 流式正文组件要有稳定 element_id 供增量定位
    assert card["body"]["elements"][0]["element_id"] == "md"


def test_text_card_truncates_overlong():
    big = "字" * 20000
    card = lc.text_card(big)
    content = card["body"]["elements"][0]["content"]
    assert len(content) <= lc._MAX_MD + 40
    assert content.endswith("（内容较长已截断）")


def test_interactive_card_button_carries_intent():
    card = lc.interactive_card("选一个", [
        {"label": "激活", "intent": "激活 stock_quote", "style": "primary"},
        {"label": "放弃", "intent": "先不激活"},
    ])
    # 找到 column_set 里的按钮
    col_set = card["body"]["elements"][1]
    assert col_set["tag"] == "column_set"
    btn = col_set["columns"][0]["elements"][0]
    assert btn["tag"] == "button"
    assert btn["type"] == "primary"
    beh = btn["behaviors"][0]
    assert beh["type"] == "callback"
    assert beh["value"][lc.INTENT_KEY] == "激活 stock_quote"
    # 非法样式回落 default
    btn2 = col_set["columns"][1]["elements"][0]
    assert btn2["type"] == "default"


def test_form_card_structure():
    card = lc.form_card("填一下", [
        {"name": "title", "label": "标题"},
        {"name": "when", "label": "时间", "type": "input"},
        {"name": "kind", "label": "类型", "type": "select",
         "options": [{"label": "会议", "value": "meeting"}]},
    ], submit_label="创建", submit_intent="新建日历事件")
    form = card["body"]["elements"][1]
    assert form["tag"] == "form"
    # 飞书 2.0 无 form_item：字段直接是 input / select_static 组件（label 挂组件上）
    tags = [e["tag"] for e in form["elements"]]
    assert "form_item" not in tags
    assert tags[:3] == ["input", "input", "select_static"]
    # 每个输入组件自带 label
    assert form["elements"][0]["label"]["content"] == "标题"
    submit = form["elements"][-1]
    assert submit["tag"] == "button"
    assert submit["action_type"] == "form_submit"
    assert submit["behaviors"][0]["value"][lc.INTENT_KEY] == "新建日历事件"
    # select 字段渲染成 select_static
    kind = form["elements"][2]
    assert kind["tag"] == "select_static"
    assert kind["options"][0]["value"] == "meeting"


def test_json_helpers_roundtrip():
    s = lc.interactive_card_json("x", [{"label": "a", "intent": "do a"}])
    assert json.loads(s)["schema"] == "2.0"
    s2 = lc.form_card_json("y", [{"name": "n", "label": "名"}])
    assert json.loads(s2)["body"]["elements"][1]["tag"] == "form"


def test_intent_from_callback_button_only():
    assert lc.intent_from_callback({lc.INTENT_KEY: "激活 X"}, None) == "激活 X"


def test_intent_from_callback_form_only():
    out = lc.intent_from_callback(None, {"title": "复盘", "when": "周一"})
    assert out.startswith("表单提交：")
    assert "title=复盘" in out and "when=周一" in out


def test_intent_from_callback_button_and_form():
    out = lc.intent_from_callback({lc.INTENT_KEY: "新建日历事件"},
                                  {"title": "复盘"})
    assert out.startswith("新建日历事件")
    assert "title=复盘" in out


def test_intent_from_callback_empty():
    assert lc.intent_from_callback(None, None) == ""
    assert lc.intent_from_callback({}, {}) == ""


def test_present_options_action_payload():
    act = present_options("选一个", [{"label": "A", "intent": "选 A"}], title="T")
    assert act.type == "interactive"
    assert act.payload["mode"] == "options"
    assert act.payload["options"][0]["intent"] == "选 A"
    assert act.payload["title"] == "T"


def test_present_form_action_payload():
    act = present_form("填", [{"name": "n", "label": "名"}],
                       submit_intent="录入")
    assert act.type == "interactive"
    assert act.payload["mode"] == "form"
    assert act.payload["submit_intent"] == "录入"


def test_extract_post_text():
    # 复用 bridge 的静态方法（纯函数，不需要 SDK 客户端）
    from lark_bridge import LarkBridge
    content = {"title": "标题", "content": [
        [{"tag": "text", "text": "第一行"}, {"tag": "a", "text": "链接"}],
        [{"tag": "text", "text": "第二行"}],
    ]}
    out = LarkBridge._extract_post_text(content)
    assert "标题" in out and "第一行" in out and "第二行" in out and "链接" in out


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ✓ {fn.__name__}")
    print(f"\n{passed}/{len(fns)} 通过")


if __name__ == "__main__":
    _run_all()
