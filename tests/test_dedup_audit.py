#!/usr/bin/env python3
"""2026-07-22 重复实现审计的回归锁 —— json_salvage 对象版 + llm 单一构建点。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. salvage_json_objects（对象版抢救，统一 spawn/doc_vault 的自写解析）────
print("[1] 对象版抢救")
from core.json_salvage import salvage_json_objects, strip_fence  # noqa: E402

objs = salvage_json_objects('前缀噪声 {"a": 1} 中间 {"conclusion": "x", "b": {"嵌套": 2}} 尾巴')
check(len(objs) == 2 and objs[1]["conclusion"] == "x", "多对象带噪声全部抢出")
check(objs[1]["b"]["嵌套"] == 2, "嵌套对象不被误切")

objs2 = salvage_json_objects('{"完整": 1} {"截断的": "value')
check(len(objs2) == 1 and objs2[0] == {"完整": 1}, "截断只丢尾巴不炸批")

objs3 = salvage_json_objects('{"s": "字符串里有 } 和 { 和 \\" 转义"}')
check(len(objs3) == 1 and "}" in objs3[0]["s"], "字符串内括号/转义不干扰深度")

check(salvage_json_objects("没有任何 JSON") == [], "无对象返回空（不抛错）")
check(strip_fence("```json\n{\"a\":1}\n```") == '{"a":1}', "围栏剥离共用")

# ── 2. 三个统一后的消费方 ────────────────────────────────────────────────────
print("[2] 消费方")
from core.spawn import parse_contract  # noqa: E402

c = parse_contract('过程… {"foo": 1}\n{"conclusion": "最终", "evidence": []}')
check(c and c["conclusion"] == "最终", "spawn.parse_contract 走统一抢救")

from connectors.doc_vault import _parse_json  # noqa: E402
d = _parse_json('```json\n{"doc_type": "policy", "fields": {"保额": "100万"}}\n```')
check(d.get("fields", {}).get("保额") == "100万", "doc_vault._parse_json 走统一抢救")
check(_parse_json('{"fields": {"保额": "100万"') == {}, "doc_vault 截断安全返回空")

from core.self_review import parse_proposals  # noqa: E402
# 关键升级：提案数组被截断时，此前朴素正则整批丢；现在抢出完整的那条
truncated = ('[{"path": "core/x.py", "new_code": "code", "test_code": "t", '
             '"defect": "d"}, {"path": "core/y.py", "new_code": "被截')
props = parse_proposals(truncated)
check(len(props) == 1 and props[0].path == "core/x.py",
      "parse_proposals 截断抢救（此前整批丢——审计修复的核心）")

# ── 3. llm 单一构建点 ────────────────────────────────────────────────────────
print("[3] llm 工厂")
import os  # noqa: E402
for _v in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
           "HTTP_PROXY", "http_proxy"):
    os.environ.pop(_v, None)
import config  # noqa: E402
if not getattr(config, "OPENROUTER_API_KEY", None):
    config.OPENROUTER_API_KEY = "test-key"
from core import llm  # noqa: E402

check(llm.DEFAULT_TIMEOUT <= 300, f"默认超时有界（{llm.DEFAULT_TIMEOUT}s，绝非 SDK 的 600s）")
client = llm.get_client()
check(float(client.timeout) == llm.DEFAULT_TIMEOUT, "工厂客户端带默认超时")
check(float(llm.get_client(timeout=540).timeout) == 540, "长任务可显式放宽")

# 回归锁：除 llm.py 外，第一方代码不得再新增裸 AsyncOpenAI 构建
import re  # noqa: E402
ROOT = Path(__file__).resolve().parent.parent
offenders = []
for d in ("core", "connectors", "intel", "prospecting", "web"):
    for f in (ROOT / d).rglob("*.py"):
        if "__pycache__" in f.parts or f.name == "llm.py":
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"AsyncOpenAI\s*\(", line) and "=" in line.split("AsyncOpenAI")[0]:
                offenders.append(f"{f.relative_to(ROOT)}:{i}")
check(offenders == [], f"无裸 AsyncOpenAI 构建（发现：{offenders or '无'}）")

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_dedup_audit 全部通过")
sys.exit(0)
