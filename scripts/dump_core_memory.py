"""
导出当前长期记忆（core memory / 用户档案）—— 只读，供瘦身审查用。

打印 memory.db 里 core_memory 表的全部【活跃】条目（软删的不算），
连同 id / 出处 / 最近确认时间，方便判断哪些该保留在 L1、哪些该降级到 L2。

用法（项目根）：
    python scripts/dump_core_memory.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import profile  # noqa: E402


def main() -> int:
    facts = profile.list_facts()
    if not facts:
        print("（core_memory 为空）")
        return 0
    print(f"当前活跃长期记忆共 {len(facts)} 条（上限 {profile.MAX_FACTS}）：\n")
    for f in facts:
        ev = f.get("evidence") or ""
        conf = (f.get("last_confirmed_at") or "")[:10]
        print(f"[{f['id']:>3}] {f['text']}")
        if ev or conf:
            print(f"      · 出处：{ev or '—'}   · 最近确认：{conf or '—'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
