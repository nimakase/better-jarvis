#!/usr/bin/env python3
"""查询扇出回归测试 —— 2026-08-13 修复"潜客生成全是大厂假容量"，
同日的潜客生成 v3 重构（子类目扇出 + 轮次化搜到目标为止），
以及 2026-08-14 的区域框架 v2 扩容（EU/NA 补漏 + 新增 EA/SA）。

背景 1（v2，国家扇出）：prospect_daily 生成候选时只发一条宽查询（label+region），
augment_with_search 只取 8 条结果拼进 prompt，模型无内置联网、材料面太窄，top
结果被行业头部品牌霸榜 → 产出全是够不着的巨头（SEW/Beckhoff/NORD 等）。

修复（prospecting/workflows.py）：
  1. 按子区域/国家把单条查询扇出成多条窄查询
  2. 查询里拼上全球巨头负向排除词（Exa 支持 -term）

背景 2（v3）：即使材料面扩到 40+ 条，一个宽类目节点（如"工业自动化"）
一次搜索、一次生成，能挖到的公司数仍然被这一个宽概念限死——实测跑出来只有
5-6 家。改成：给每个叶子类目配子类目产品词（_LEAF_SUBTERMS），与国家扇出
两条路径交替编排成查询队列（build_query_queue），配合轮次化循环（loop-until-
target，见 make_llm_generate_fn 内的 _run）逐轮搜索+生成+去重合并，直到凑够
软目标（约 100 家）或连续几轮挖不出新公司。真机首跑 ia_robotics×EU 只挖出
22 家，查出是查询队列本身太短提前耗尽（不是挖尽/撞轮数上限），已修复
（_subcategory_queries 改成子类目词×国家真交叉）。

背景 3（区域框架 v2，本次）：原来只有 EU/NA/SEA 三区域，是树最初随手定的范围。
用户追问"NA 为什么只有 3 国""捷克等是什么意思"后系统盘了一遍：EU 清单本身漏了
西班牙/比利时/奥地利/瑞士；NA 原来把美国当一个整体搜（跟"整个欧洲一条查询"是
同一问题换了尺度），拆成几个制造业集群；新增 EA（东亚：日/韩/台，电子制造体量
大且不属于已排除的中国大陆/香港）、SA（南美：巴西/阿根廷/哥伦比亚）；评估过
但没加中亚/非洲发达地区（预期产出个位数，判断标准与当初清理 4 个低价值树叶子
一致）。同步扩了 _GLOBAL_GIANT_EXCLUDES（原列表清一色西方工业巨头，新区域必须
配对应的巨头才不会重演"搜出来全是大厂"）。

钉住的行为：
  - 已知区域必须扇出多条，而不是退化成单条宽查询
  - 每条查询都带排除词，把巨头从材料面洗掉
  - 排除词列表覆盖上次实际踩到的大厂（SEW-EURODRIVE / Beckhoff / NORD）
  - 未知区域回退单条宽查询（树外结构/测试用例不崩）
  - build_query_queue 交替编排子类目查询与国家查询，按批打包成轮次
  - _LEAF_SUBTERMS 覆盖当前树上的每一个叶子类目（不漏配）
  - _company_key 能把"同名不同大小写/官网带不带 https 前缀"的记录判成同一家
  - 队列轮数不该早于 _MAX_ROUNDS 就耗尽（2026-08-14 真机踩到的坑）
  - _REGION_QUERY_TERMS 覆盖树上 regions_meta 声明的每一个区域（新区域别漏配）
  - 新区域（EA/SA）也带巨头排除词、也能正常扇出
"""
import json
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
check("EU 扇出为 12 国子区域查询 + 1 条兜底宽查询",
      len(qs_eu) == len(pw._REGION_QUERY_TERMS["EU"]) + 1)
check("EU 查询里覆盖德国", any("Germany" in q for q in qs_eu))
check("EU 查询里覆盖意大利", any("Italy" in q for q in qs_eu))
check("EU 查询里覆盖波兰/捷克（上次漏掉的中小厂集中区）",
      any("Poland" in q for q in qs_eu) and any("Czech" in q for q in qs_eu))
check("EU 查询里覆盖 2026-08-14 补的西班牙/瑞士（原清单漏掉的两个大项）",
      any("Spain" in q for q in qs_eu) and any("Switzerland" in q for q in qs_eu))

qs_na = pw.build_generation_queries("测试类目", "NA", "北美")
check("NA 扇出条数 = 制造业集群数 + 1（原来笼统一个 USA，已拆分）",
      len(qs_na) == len(pw._REGION_QUERY_TERMS["NA"]) + 1)
check("NA 查询覆盖加拿大/墨西哥", any("Canada" in q for q in qs_na)
      and any("Mexico" in q for q in qs_na))
check("NA 查询里美国被拆成多个制造业集群，而不是笼统一个 USA",
      sum(1 for q in qs_na if any(us in q for us in
          ("Midwest", "Southeast USA", "Texas", "California", "Northeast USA"))) >= 3)

qs_sea = pw.build_generation_queries("测试类目", "SEA", "东南亚")
check("SEA 扇出为 6 + 1 条", len(qs_sea) == 7)

# ── 1b. 新区域 EA（东亚）/ SA（南美）能正常扇出 ──────────────────────────────
qs_ea = pw.build_generation_queries("测试类目", "EA", "东亚")
check("EA 扇出为日/韩/台 + 1 条兜底", len(qs_ea) == 4)
check("EA 查询覆盖日本/韩国/台湾", any("Japan" in q for q in qs_ea)
      and any("Korea" in q for q in qs_ea) and any("Taiwan" in q for q in qs_ea))

qs_sa = pw.build_generation_queries("测试类目", "SA", "南美")
check("SA 扇出为巴西/阿根廷/哥伦比亚 + 1 条兜底", len(qs_sa) == 4)
check("SA 查询覆盖巴西", any("Brazil" in q for q in qs_sa))

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
check("新区域查询也带排除词（EA/SA 不是特例）",
      all("-Siemens" in q for q in qs_ea) and all("-Siemens" in q for q in qs_sa))
check("排除词表已扩容覆盖东亚/南美头部品牌（不然新区域会重演'搜出来全是大厂'）",
      all(g in pw._GLOBAL_GIANT_EXCLUDES
          for g in ("Panasonic", "Samsung", "Foxconn", "WEG")))

# ── 3. 未知区域回退单条（不崩，行为同旧版）──────────────────────────────────
qs_unknown = pw.build_generation_queries("测试类目", "XX", "未知区")
check("未知区域回退单条宽查询", len(qs_unknown) == 1)
check("未知区域查询仍带排除词（负向在材料层统一生效）",
      "-Siemens" in qs_unknown[0])

# ── 4. 区域代号大小写不敏感（select_node 给的是大写代号）────────────────────
qs_lower = pw.build_generation_queries("测试类目", "eu", "欧洲")
check("小写区域代号也能匹配扇出", len(qs_lower) == len(pw._REGION_QUERY_TERMS["EU"]) + 1)

# ── 4b. _REGION_QUERY_TERMS 覆盖树上 regions_meta 声明的每一个区域 ───────────
# 跟 _LEAF_SUBTERMS 覆盖检查同一个思路：树侧声明了一个区域（regions_meta 或任意
# 叶子的 regions 数组里出现），查询侧就必须配对应的国家词表，否则那个区域会
# 静默退化成"未知区域"的单条兜底查询——不报错，但材料面直接打回 2026-08-13
# 修复之前的水平，问题会在生产里悄悄复发而不是在测试里被抓到。
_tree_regions_path = Path(JARVIS) / "data" / "prospect_tree.json"
if _tree_regions_path.exists():
    _tree2 = json.loads(_tree_regions_path.read_text(encoding="utf-8"))
    _declared_regions = set(_tree2.get("regions_meta", {}).keys())
    _missing_terms = _declared_regions - set(pw._REGION_QUERY_TERMS.keys())
    check(f"_REGION_QUERY_TERMS 覆盖 regions_meta 声明的每个区域（缺：{sorted(_missing_terms) if _missing_terms else '无'}）",
          not _missing_terms)
else:
    print("SKIP _REGION_QUERY_TERMS 覆盖检查（data/prospect_tree.json 不存在）")

# ── 5. build_query_queue：子类目路径 × 国家路径交替编排，按批打包成轮次 ──────
queue_plc = pw.build_query_queue("ia_plc_drives", "可编程控制器与工业变频驱动",
                                 "EU", "欧洲")
check("已知叶子的查询队列非空", len(queue_plc) > 0)
flat_plc = [q for batch in queue_plc for q in batch]
check("每轮批大小不超过 _ROUND_BATCH_SIZE",
      all(len(batch) <= pw._ROUND_BATCH_SIZE for batch in queue_plc))
check("队列里既有子类目产品词查询（PLC/VFD/servo 等），也有国家查询（Germany 等）",
      any("PLC" in q or "VFD" in q or "servo" in q for q in flat_plc)
      and any("Germany" in q for q in flat_plc))
check("队列内查询去重（不会把同一条塞两遍）", len(flat_plc) == len(set(flat_plc)))

queue_unknown_leaf = pw.build_query_queue("no_such_leaf", "未知类目", "EU", "欧洲")
check("未配子类目词表的叶子仍能拿到队列（回退成类目名查询，不崩、不空）",
      len(queue_unknown_leaf) > 0 and len(queue_unknown_leaf[0]) > 0)

queue_unknown_region = pw.build_query_queue("ia_plc_drives", "可编程控制器与工业变频驱动",
                                            "XX", "未知区")
check("未知区域仍能拿到队列（国家路径回退单条，子类目路径正常）",
      len(queue_unknown_region) > 0)

# ── 5b. 队列长度不该早于 _MAX_ROUNDS 就耗尽（钉住 2026-08-14 真机踩到的坑）────
#
# 真机首跑 ia_robotics×EU 只挖出 22 家就停了：当时 _subcategory_queries 只拼
# "子类目词 + 区域名"（不按国家展开），队列总共才 5 轮，远小于 _MAX_ROUNDS，
# 循环因为【没查询可搜了】提前结束，不是因为挖尽或撞到轮数上限。
# 修法：子类目词 × 国家做真正交叉，让队列本身不再是瓶颈——这里钉住这个前提：
# 已知叶子 + 已知区域时，队列的轮数应 >= _MAX_ROUNDS（或至少远多于修复前的量级），
# 循环该由 _MAX_ROUNDS / _DRY_ROUND_LIMIT / _TARGET_CANDIDATES 来收尾，
# 不该被一个意外偏短的队列提前打断。
queue_ia_eu = pw.build_query_queue("ia_robotics", "工业机器人与机械臂", "EU", "欧洲")
check(f"ia_robotics×EU 队列轮数（{len(queue_ia_eu)}）足够撑到 _MAX_ROUNDS"
      f"（{pw._MAX_ROUNDS}），不会提前耗尽",
      len(queue_ia_eu) >= pw._MAX_ROUNDS)

# ── 6. _LEAF_SUBTERMS 覆盖树上现存的每一个叶子类目 ───────────────────────────
_tree_path = Path(JARVIS) / "data" / "prospect_tree.json"
if _tree_path.exists():
    def _leaves(node):
        children = node.get("children")
        if not children:
            return [node]
        out = []
        for c in children:
            out += _leaves(c)
        return out

    _tree = json.loads(_tree_path.read_text(encoding="utf-8"))
    _tree_leaf_ids = {leaf.get("id") for sec in _tree.get("sectors", []) for leaf in _leaves(sec)}
    _missing = _tree_leaf_ids - set(pw._LEAF_SUBTERMS.keys())
    check(f"树上每个叶子都配了子类目词表（缺：{sorted(_missing) if _missing else '无'}）",
          not _missing)
else:
    print("SKIP _LEAF_SUBTERMS 覆盖检查（data/prospect_tree.json 不存在）")

check("_LEAF_SUBTERMS 每个词表都至少 2 条词（太少等于没细分）",
      all(len(v) >= 2 for v in pw._LEAF_SUBTERMS.values()))

# ── 7. _company_key：跨轮去重键要能识别"同名不同写法" ────────────────────────
k1 = pw._company_key({"company_name": "Acme Robotics", "website": "https://www.acme.com/"})
k2 = pw._company_key({"company_name": "Acme Robotics", "website": "acme.com"})
check("公司名相同、官网带不带 https://www. 前缀都判成同一家", k1 == k2)
k3 = pw._company_key({"company_name": "Acme Robotics", "website": "other.com"})
check("同名但官网不同 → 不同 key（当前实现按整串拼接，官网是判重的一部分）",
      k1 != k3)

# ── 8. 轮次化循环的旋钮都存在且是合理的正数 ──────────────────────────────────
check("_TARGET_CANDIDATES 是正数", pw._TARGET_CANDIDATES > 0)
check("_MAX_ROUNDS 是正数（成本护栏，防止死循环烧钱）", pw._MAX_ROUNDS > 0)
check("_DRY_ROUND_LIMIT 是正数", pw._DRY_ROUND_LIMIT > 0)
check("_ROUND_BATCH_SIZE 是正数", pw._ROUND_BATCH_SIZE > 0)

print("\n" + "=" * 52)
if fails:
    print(f"❌ {len(fails)} 项失败：")
    for f in fails:
        print("   - " + f)
    sys.exit(1)
print("✅ 查询扇出全部检查通过")
