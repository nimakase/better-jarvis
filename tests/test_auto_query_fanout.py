#!/usr/bin/env python3
"""查询扇出回归测试 —— 2026-08-13 修复"潜客生成全是大厂假容量"。

背景：prospect_daily 生成候选时只发一条宽查询（label+region），augment_with_search
只取 8 条结果拼进 prompt，模型无内置联网、材料面太窄，top 结果被行业头部品牌
霸榜 → 产出全是够不着的巨头（SEW/Beckhoff/NORD 等）。

修复（prospecting/workflows.py）：
  1. 按子区域/国家把单条查询扇出成多条窄查询（EU 8 条、NA 3 条、SEA 6 条）
  2. 查询里拼上全球巨头负向排除词（Exa 支持 -term）

钉住的行为：
  - 已知区域必须扇出多条，而不是退化成单条宽查询
  - 每条查询都带排除词，把巨头从材料面洗掉
  - 排除词列表覆盖上次实际踩到的大厂（SEW-EURODRIVE / Beckhoff / NORD）
  - 未知区域回退单条宽查询（树外结构/测试用例不崩）
"""
import sys
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from prospecting import workflows as pw  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ── 1. 已知区域必须扇出 ─────────────────────────────────────────────────────
qs_eu = pw.build_generation_queries("可编程控制器与工业变频驱动", "EU", "欧洲")
check("EU 扇出为 8 条子区域查询 + 1 条兜底宽查询", len(qs_eu) == 9)
check("EU 查询里覆盖德国", any("Germany" in q for q in qs_eu))
check("EU 查询里覆盖意大利", any("Italy" in q for q in qs_eu))
check("EU 查询里覆盖波兰/捷克（上次漏掉的中小厂集中区）",
      any("Poland" in q for q in qs_eu) and any("Czech" in q for q in qs_eu))

qs_na = pw.build_generation_queries("测试类目", "NA", "美洲")
check("NA 扇出为 3 + 1 条", len(qs_na) == 4)

qs_sea = pw.build_generation_queries("测试类目", "SEA", "东南亚")
check("SEA 扇出为 6 + 1 条", len(qs_sea) == 7)

# ── 2. 每条查询都带巨头排除词 ──────────────────────────────────────────────
check("所有查询都带 -SEW-EURODRIVE（上次踩到的巨头）",
      all("-SEW-EURODRIVE" in q for q in qs_eu))
check("所有查询都带 -Beckhoff（上次踩到的巨头）",
      all("-Beckhoff" in q for q in qs_eu))
check("所有查询都带 -Siemens", all("-Siemens" in q for q in qs_eu))
check("排除词列表覆盖上次 5 家里 3 家巨头",
      all(g in pw._GLOBAL_GIANT_EXCLUDES
          for g in ("SEW-EURODRIVE", "Beckhoff", "NORD")))
check("排除词列表非空且有实质覆盖", len(pw._GLOBAL_GIANT_EXCLUDES) >= 10)

# ── 3. 未知区域回退单条（不崩，行为同旧版）──────────────────────────────────
qs_unknown = pw.build_generation_queries("测试类目", "XX", "未知区")
check("未知区域回退单条宽查询", len(qs_unknown) == 1)
check("未知区域查询仍带排除词（负向在材料层统一生效）",
      "-Siemens" in qs_unknown[0])

# ── 4. 区域代号大小写不敏感（select_node 给的是大写代号）────────────────────
qs_lower = pw.build_generation_queries("测试类目", "eu", "欧洲")
check("小写区域代号也能匹配扇出", len(qs_lower) == 9)

print("\n" + "=" * 52)
if fails:
    print(f"❌ {len(fails)} 项失败：")
    for f in fails:
        print("   - " + f)
    sys.exit(1)
print("✅ 查询扇出全部检查通过")
