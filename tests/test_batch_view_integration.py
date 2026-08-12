"""tests/test_batch_view_integration.py — view_manager.run_view_cycle 改用
core.batch.run_batch 之后的端到端等价性测试(monkeypatch 掉 Breeze/HubSpot/浏览器依赖)。

验证三件事(对应 [[jarvis-architecture-migration-plan]] 的诊断):
  1. 断点续跑:上一轮成功/软错误 vs 硬异常的账户,下一轮该跳过的跳过、该重试的重试。
  2. 单账户失败(软错误 dict / 硬异常)不拖垮同批其它账户,失败原因进 breeze_errors。
  3. limit 只限【未完成】账户数,已完成的不占名额。

跑:python -m tests.test_batch_view_integration
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["JARVIS_CUSTOMER_LOOP_DIR"] = tempfile.mkdtemp()  # 隔离 store 目录,不碰真实数据

from prospecting import view_manager                 # noqa: E402
from prospecting import outreach_store                # noqa: E402
from prospecting import account_reader, breeze_outreach, view_writer, bitable_client  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


RECORDS = [
    {"account_name": "AlphaCo", "existing_type": "prospecting", "num_associated_deals": 0,
     "num_open_deals": 0, "last_activity_date": "2025-05-01", "company_domain": "alpha.com"},
    {"account_name": "BetaCo", "existing_type": "prospecting", "num_associated_deals": 0,
     "num_open_deals": 0, "last_activity_date": "2025-05-01", "company_domain": "beta.com"},
    {"account_name": "GammaCo", "existing_type": "prospecting", "num_associated_deals": 0,
     "num_open_deals": 0, "last_activity_date": "2025-05-01", "company_domain": "gamma.com"},
    {"account_name": "DeltaCo", "existing_type": "core", "num_associated_deals": 3,
     "num_open_deals": 1, "last_activity_date": "2025-05-01", "company_domain": "delta.com"},
    {"account_name": "EpsilonCo", "existing_type": "core", "num_associated_deals": 2,
     "num_open_deals": 0, "last_activity_date": "2025-05-01", "company_domain": "epsilon.com"},
]

VIEW_URL_MAP = {"未开发": "http://view/new", "维护到点": "http://view/maintain"}


class FakeBrowser:
    page = None  # grade_view_url="" 时不会被真的用到 goto/wait_for_table_ready


def _patch_common(monkey):
    """把跑一次 run_view_cycle 需要的外部依赖全部换成受控假实现。返回调用记录字典。"""
    calls = {"ask_breeze": [], "ask_deal_summary": [], "set_view_membership": []}

    def fake_read_all(browser):
        return {"rows": RECORDS, "expected_total": len(RECORDS)}

    def fake_ask_breeze(browser, acct, website=None, logger=None):
        calls["ask_breeze"].append(acct)
        if acct == "AlphaCo":
            return {"contacts": []}                                    # 正常:未开发,成功
        if acct == "BetaCo":
            return {"error": "消歧:找到多个同名公司"}                    # 软错误(非异常)
        if acct == "GammaCo":
            raise ValueError("Breeze 超时")                             # 硬异常
        raise AssertionError(f"不该问到 {acct} 的 outreach")

    def fake_ask_deal_summary(browser, name, website=None, logger=None):
        calls["ask_deal_summary"].append(name)
        if name == "DeltaCo":
            return {"parsed": {"won": 2, "lost": 0, "open": 1}}         # 正常:core,成功
        if name == "EpsilonCo":
            return {"error": "消歧:找到多个同名公司"}                    # 软错误
        raise AssertionError(f"不该问到 {name} 的 deal")

    def fake_set_view_membership(browser, url, names, apply=False, logger=None):
        calls["set_view_membership"].append({"url": url, "names": list(names), "apply": apply})
        return {"ok": True, "count": len(names), "applied": apply}

    def fake_cfg():
        return (False,)   # 没配 Bitable —— 显式关闭,不依赖真实 .env/pydantic 是否可用

    monkey(account_reader, "read_all", fake_read_all)
    monkey(breeze_outreach, "ask_breeze", fake_ask_breeze)
    monkey(breeze_outreach, "ask_deal_summary", fake_ask_deal_summary)
    monkey(view_writer, "set_view_membership", fake_set_view_membership)
    monkey(bitable_client, "_cfg", fake_cfg)
    return calls


class _Monkey:
    """极简 monkeypatch:记住原值,restore() 还原——不依赖 pytest。"""
    def __init__(self):
        self._saved = []

    def __call__(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)


# ── 第一轮:AlphaCo 成功、BetaCo 软错误、GammaCo 硬异常、DeltaCo 成功、EpsilonCo 软错误 ──
mk = _Monkey()
calls1 = _patch_common(mk)
try:
    res1 = view_manager.run_view_cycle(FakeBrowser(), VIEW_URL_MAP, grade_view_url="",
                                       apply=False, resume=True, logger=None)
finally:
    mk.restore()

check(calls1["ask_breeze"] == ["AlphaCo", "BetaCo", "GammaCo"], f"第一轮问了全部 3 个 prospecting 账户, got {calls1['ask_breeze']}")
check(calls1["ask_deal_summary"] == ["DeltaCo", "EpsilonCo"], f"第一轮问了全部 2 个 core 账户, got {calls1['ask_deal_summary']}")
check(res1["processed_prospecting"] == 3, "processed_prospecting = 3(全部尝试过)")
check(res1["processed_core"] == 2, "processed_core = 2")
err_accounts = sorted(e["account"] for e in res1["breeze_errors"])
check(err_accounts == ["BetaCo", "EpsilonCo", "GammaCo"], f"3 个失败账户都进 breeze_errors, got {err_accounts}")
gamma_err = next(e for e in res1["breeze_errors"] if e["account"] == "GammaCo")
check("ValueError" in gamma_err["error"] and "Breeze 超时" in gamma_err["error"],
     f"硬异常的 error 字符串含类型+消息(改进点:旧版本会丢掉消息), got {gamma_err['error']!r}")
beta_err = next(e for e in res1["breeze_errors"] if e["account"] == "BetaCo")
check("消歧" in beta_err["error"], f"软错误的 error 字符串保留原始消息, got {beta_err['error']!r}")

# 成功的两户应该已经落 store(下一轮断点续跑要跳过它们)
check(outreach_store.get_account_state("AlphaCo") is not None, "AlphaCo 成功后已存库")
check(outreach_store.get_account_state("DeltaCo") is not None, "DeltaCo 成功后已存库")
# 失败的三户不该落 store(下一轮要重试)
for name in ("BetaCo", "GammaCo", "EpsilonCo"):
    check(outreach_store.get_account_state(name) is None, f"{name} 失败后不应存库(留给下轮重试)")

# ── 第二轮(断点续跑):AlphaCo/DeltaCo 不该再被问;只重试 Beta/Gamma/Epsilon ──
mk2 = _Monkey()
calls2 = _patch_common(mk2)
try:
    res2 = view_manager.run_view_cycle(FakeBrowser(), VIEW_URL_MAP, grade_view_url="",
                                       apply=False, resume=True, logger=None)
finally:
    mk2.restore()

check(calls2["ask_breeze"] == ["BetaCo", "GammaCo"],
     f"断点续跑:AlphaCo 已成功不再被问,只重试 Beta/Gamma, got {calls2['ask_breeze']}")
check(calls2["ask_deal_summary"] == ["EpsilonCo"],
     f"断点续跑:DeltaCo 已成功不再被问,只重试 Epsilon, got {calls2['ask_deal_summary']}")
check(res2["processed_prospecting"] == 2, "第二轮 processed_prospecting = 2(只有未完成的)")
check(res2["processed_core"] == 1, "第二轮 processed_core = 1")

# ── resume=False:强制全量重跑,不看 store 里的完成状态 ──
mk3 = _Monkey()
calls3 = _patch_common(mk3)
try:
    res3 = view_manager.run_view_cycle(FakeBrowser(), VIEW_URL_MAP, grade_view_url="",
                                       apply=False, resume=False, logger=None)
finally:
    mk3.restore()
check(sorted(calls3["ask_breeze"]) == ["AlphaCo", "BetaCo", "GammaCo"],
     f"resume=False 忽略 store,全部 3 个都重问, got {sorted(calls3['ask_breeze'])}")

# ── limit:只限未完成账户数,已完成的不占名额 ──
os.environ["JARVIS_CUSTOMER_LOOP_DIR"] = tempfile.mkdtemp()  # 全新隔离目录,重新起一轮场景
mk4 = _Monkey()
calls4 = _patch_common(mk4)
try:
    res4 = view_manager.run_view_cycle(FakeBrowser(), VIEW_URL_MAP, grade_view_url="",
                                       apply=False, resume=True, limit=1, logger=None)
finally:
    mk4.restore()
check(len(calls4["ask_breeze"]) == 1, f"limit=1 只处理 1 个 prospecting 账户, got {calls4['ask_breeze']}")
check(len(calls4["ask_deal_summary"]) == 1, f"limit=1 只处理 1 个 core 账户, got {calls4['ask_deal_summary']}")
check(res4["prospecting_total"] == 3 and res4["core_total"] == 2, "total 数不受 limit 影响(仍是全量)")

print("✅ test_batch_view_integration 全部通过")
