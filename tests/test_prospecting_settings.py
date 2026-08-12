"""tests/test_prospecting_settings.py — prospecting/settings.py(客户循环专属配置)单测。

验证:①字段默认值 + env var 名字(validation_alias)对得上;②跟 config.py 已经解耦
(config 模块不再暴露这四个属性,不留新旧并存的死代码);③模块级常量类型对。

跑:python -m tests.test_prospecting_settings
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospecting.settings import (       # noqa: E402
    CustomerLoopSettings, CUSTOMER_LOOP_APPLY, GRADE_VIEW_URL,
    BITABLE_APP_TOKEN, BITABLE_TABLE_ID,
)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ── 默认值(_env_file=None 跳过读 .env,只看类字段默认值)────────
bare = CustomerLoopSettings(_env_file=None)
check(bare.customer_loop_apply is False, "customer_loop_apply 默认 False")
check(bare.grade_view_url == "", "grade_view_url 默认空")
check(bare.bitable_app_token == "", "bitable_app_token 默认空")
check(bare.bitable_table_id == "", "bitable_table_id 默认空")

# ── env var 名字对得上(validation_alias),同时验证跟历史 .env 变量名完全兼容 ──
os.environ["JARVIS_CUSTOMER_LOOP_APPLY"] = "1"
os.environ["JARVIS_GRADE_VIEW_URL"] = "http://example.com/view"
os.environ["BITABLE_APP_TOKEN"] = "tok123"
os.environ["BITABLE_TABLE_ID"] = "tbl456"
try:
    overridden = CustomerLoopSettings(_env_file=None)
    check(overridden.customer_loop_apply is True, "JARVIS_CUSTOMER_LOOP_APPLY=1 → True")
    check(overridden.grade_view_url == "http://example.com/view", "JARVIS_GRADE_VIEW_URL 生效")
    check(overridden.bitable_app_token == "tok123",
         "BITABLE_APP_TOKEN 生效(不加 JARVIS_ 前缀,跟历史 .env 变量名一致)")
    check(overridden.bitable_table_id == "tbl456", "BITABLE_TABLE_ID 生效")
finally:
    for k in ("JARVIS_CUSTOMER_LOOP_APPLY", "JARVIS_GRADE_VIEW_URL",
             "BITABLE_APP_TOKEN", "BITABLE_TABLE_ID"):
        os.environ.pop(k, None)

# ── 模块级常量(实例化时已从当前 .env/env 读入,类型应符合预期)──
check(isinstance(CUSTOMER_LOOP_APPLY, bool), "模块级 CUSTOMER_LOOP_APPLY 是 bool")
check(isinstance(GRADE_VIEW_URL, str), "模块级 GRADE_VIEW_URL 是 str")
check(isinstance(BITABLE_APP_TOKEN, str), "模块级 BITABLE_APP_TOKEN 是 str")
check(isinstance(BITABLE_TABLE_ID, str), "模块级 BITABLE_TABLE_ID 是 str")

# ── 已跟 config.py 解耦:迁移后 config 模块不该再暴露这四个属性(不留新旧并存)──
import config  # noqa: E402
for name in ("CUSTOMER_LOOP_APPLY", "GRADE_VIEW_URL", "BITABLE_APP_TOKEN", "BITABLE_TABLE_ID"):
    check(not hasattr(config, name), f"config.{name} 应已随迁移删除")

print("✅ test_prospecting_settings 全部通过")
