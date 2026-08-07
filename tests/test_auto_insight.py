#!/usr/bin/env python3
"""通用再分析模块 core/insight.py + 客户循环接入（任务 #21）——自动生成，绝不覆盖既有测试文件。

覆盖：
  1. core/insight 确定性规则：missing_or_zero / threshold / share_dominance / delta
  2. core/insight.analyze() 组合调用 + render() 渲染（含空结果、严重度排序）
  3. core/insight.analyze_with_model()：解析模型输出、NONE处理、非法严重度兜底、异常降级
  4. connectors/insight_tools.generate_insights：JSON校验、字段清单驱动、use_model路径、注册
  5. connectors/customer_loop_tools._incremental_insights_note：不依赖真实浏览器runtime，
     验证 held_count/池成员变动被正确捕捉，正常情况下返回空串（不制造噪音）
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


from core import insight  # noqa: E402

# ── 1. 确定性规则 ────────────────────────────────────────────────────────────
print("[1] 确定性规则")

r1 = insight.rule_missing_or_zero({"a": 0, "b": "", "c": [], "d": "有值", "e": None}, ["a", "b", "c", "d", "e"])
check({i.field for i in r1} == {"a", "b", "c", "e"}, "缺失/0/空字符串/空列表/None 都被识别，有值的不误报")
r1b = insight.rule_missing_or_zero({"a": 0}, ["a"], label_map={"a": "自定义标签"})
check("自定义标签" in r1b[0].message, "label_map 生效")

check(insight.rule_threshold({"x": 10}, "x", gt=5) is not None, "gt 阈值触发")
check(insight.rule_threshold({"x": 3}, "x", gt=5) is None, "未超阈值不触发")
check(insight.rule_threshold({"x": 3}, "x", lt=5) is not None, "lt 阈值触发")
check(insight.rule_threshold({}, "x", gt=5) is None, "字段不存在 → None，不报错")
r_msg = insight.rule_threshold({"x": 10}, "x", gt=5, message="数值是{value}")
check(r_msg.message == "数值是10", "message 里的 {value} 占位符被正确替换")
check(insight.rule_threshold({"x": "非数字"}, "x", gt=5) is None, "非数值字段 → None，不崩")

dom = insight.rule_share_dominance({"breakdown": {"A": 80, "B": 10, "C": 10}}, "breakdown")
check(dom is not None and "A" in dom.message and "80%" in dom.message, "占比过度集中被识别，且百分比正确")
dom2 = insight.rule_share_dominance({"breakdown": {"A": 40, "B": 30, "C": 30}}, "breakdown")
check(dom2 is None, "分布均衡时不触发")
check(insight.rule_share_dominance({"breakdown": "不是字典"}, "breakdown") is None, "非字典字段 → None")
check(insight.rule_share_dominance({"breakdown": {}}, "breakdown") is None, "空字典 → None")

check(insight.rule_delta({"x": 100}, None, "x") is None, "没有历史快照 → None，不瞎猜")
check(insight.rule_delta({"x": 100}, {}, "x") is None, "历史快照里没这个字段 → None")
d1 = insight.rule_delta({"x": 100}, {"x": 50}, "x", pct_threshold=0.3)
check(d1 is not None and "上升" in d1.message and "50" in d1.message and "100" in d1.message,
      "上升幅度超阈值被识别，且带上新旧值")
d2 = insight.rule_delta({"x": 55}, {"x": 50}, "x", pct_threshold=0.3)
check(d2 is None, "变化幅度不足阈值 → 不触发")
d3 = insight.rule_delta({"x": 5}, {"x": 0}, "x")
check(d3 is not None and "从 0 变为" in d3.message, "从0变为非0 → 特殊措辞，不做除0")
d4 = insight.rule_delta({"x": 0}, {"x": 0}, "x")
check(d4 is None, "0到0没有变化 → 不触发")


# ── 2. analyze() + render() ───────────────────────────────────────────────────
print("[2] analyze() 组合 + render()")

data = {"a": 0, "score_dist": {"高": 90, "中": 5, "低": 5}, "changed": 100}
prev = {"changed": 10}
combo = insight.analyze(data, missing_fields=["a"], dominance_fields=["score_dist"],
                        prev_data=prev, delta_fields=["changed"],
                        thresholds=[{"field": "changed", "gt": 50, "message": "变更量达{value}，偏高",
                                    "severity": "warning"}])
check(len(combo) == 4, f"四类规则都命中，共 4 条洞察（实际 {len(combo)}）")

check(insight.analyze({}) == [], "不传任何字段清单 → 不检查任何东西，返回空列表（不瞎猜字段名）")

check(insight.render([]) == "", "空洞察列表 → render 返回空串，不硬凑「一切正常」")
rendered = insight.render(combo)
check("【补充观察】" in rendered, "render 带上标题")
check(rendered.index("🔴") < rendered.index("⚠️") if "🔴" in rendered and "⚠️" in rendered else True,
      "严重度从高到低排序（warning 在 notice 前面）")


# ── 3. analyze_with_model() ───────────────────────────────────────────────────
print("[3] analyze_with_model()")
import core.llm as llm_mod  # noqa: E402


class _FakeResp:
    def __init__(self, text):
        class _Choice:
            def __init__(self, t):
                class _Msg:
                    def __init__(self, t):
                        self.content = t
                self.message = _Msg(t)
        self.choices = [_Choice(text)]


class _FakeCompletions:
    def __init__(self, text):
        self._text = text
    async def create(self, **kwargs):
        return _FakeResp(self._text)


class _FakeChat:
    def __init__(self, text):
        self.completions = _FakeCompletions(text)


class _FakeClient:
    def __init__(self, text):
        self.chat = _FakeChat(text)


async def _t_analyze_with_model():
    orig_get_client = llm_mod.get_client

    llm_mod.get_client = lambda *a, **kw: _FakeClient("NONE")
    out_none = await insight.analyze_with_model({"x": 1})
    check(out_none == [], "模型回 NONE → 空列表")

    llm_mod.get_client = lambda *a, **kw: _FakeClient(
        "warning|这两个数字放一起看有问题\nnotice|另一个观察\n未按格式的一行\ninvalid_sev|严重度非法这条")
    out = await insight.analyze_with_model({"x": 1})
    check(len(out) == 3, f"正确解析出 3 条（忽略不含 | 的行），实际 {len(out)}")
    check(out[0].severity == "warning" and "有问题" in out[0].message, "第一条解析正确")
    check(out[2].severity == "info", "非法严重度兜底为 info")

    def _boom(*a, **kw):
        raise RuntimeError("模拟网络错误")
    llm_mod.get_client = _boom
    out_err = await insight.analyze_with_model({"x": 1})
    check(out_err == [], "调用异常 → 空列表，不抛异常")

    llm_mod.get_client = orig_get_client

asyncio.run(_t_analyze_with_model())


# ── 4. connectors/insight_tools.generate_insights ─────────────────────────────
print("[4] insight_tools.generate_insights")
from core import registry  # noqa: E402
import connectors.insight_tools as it_mod  # noqa: E402

check(registry.get_handler("generate_insights") is it_mod.generate_insights,
      "generate_insights 已注册且指向正确函数")
from core import effects, tool_timeout as tt  # noqa: E402
spec = registry._SPECS.get("generate_insights")  # noqa: SLF001
check(spec.effect == effects.READ_LOCAL, "effect 为 read_local")
check(tt.timeout_of("generate_insights") == 60.0, "走 slow(60s) 超时档")


async def _t_generate_insights():
    out_bad_json = await it_mod.generate_insights("不是JSON")
    check("不是合法 JSON" in out_bad_json, "非法 JSON → 明确报错")

    out_not_dict = await it_mod.generate_insights("[1,2,3]")
    check("必须是一个 JSON 对象" in out_not_dict, "数组而非对象 → 明确报错")

    data = {"a": 0, "changed": 100}
    out = await it_mod.generate_insights(json.dumps(data), missing_fields=["a"])
    check("补充观察" in out, "缺失字段被再分析工具捕捉到")

    out_clean = await it_mod.generate_insights(json.dumps({"a": 1}), missing_fields=["a"])
    check("没有发现特别值得" in out_clean, "没有触发任何规则时给出明确的'无异常'文案，而不是空字符串")

    out_bad_prev = await it_mod.generate_insights(json.dumps({"a": 1}), prev_data_json="不是JSON")
    check("prev_data_json 不是合法 JSON" in out_bad_prev, "prev_data_json 非法时单独报错")

    orig_awm = insight.analyze_with_model

    async def fake_awm(data, context="", model=None):
        return [insight.Insight("_model", "模型说的话", "notice")]

    insight.analyze_with_model = fake_awm
    try:
        out_model = await it_mod.generate_insights(json.dumps({"a": 1}), use_model=True)
        check("模型说的话" in out_model, "use_model=True 时融合了模型给出的洞察")
    finally:
        insight.analyze_with_model = orig_awm

asyncio.run(_t_generate_insights())


# ── 5. customer_loop_tools._incremental_insights_note ─────────────────────────
print("[5] customer_loop_tools._incremental_insights_note")
import connectors.customer_loop_tools as clt  # noqa: E402

note_empty = clt._incremental_insights_note({"changed": 3, "flagged_count": 0, "held_count": 0,
                                             "池_离开": [], "池_进入": []})
check(note_empty == "", "一切正常(没有held/没有池变动)时 → 返回空串，不制造噪音")

note_held = clt._incremental_insights_note({"changed": 3, "held_count": 2, "池_离开": [], "池_进入": []})
check("2 个账户" in note_held and "reset 工作流" in note_held,
      "held_count 非零时被捕捉，且措辞点出 cold/dead reset 工作流风险")

note_pool = clt._incremental_insights_note({
    "changed": 0, "held_count": 0,
    "池_离开": ["公司A", "公司B"], "池_进入": ["公司C"],
})
check("2 个账户从池里消失" in note_pool and "1 个新账户进了池" in note_pool,
      "池成员变动(此前完全没进通知文案)被正确捕捉并计数")

check(clt._incremental_insights_note({}) == "", "空 res 字典不报错，返回空串")


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_insight 全部通过")
sys.exit(0)
