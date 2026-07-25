"""
潜客进度复位 —— 把潜客链路清回「从零开始」。

用途：改了潜客树/提示词/口径之后，之前跑出来的进度和历史已经不可比，
清掉重跑比带着旧数据继续更干净。

清什么：
  1. 潜客树进度   data/prospect_tree.json 里所有 done_regions 清空、status 复位 pending
  2. 存盘批       DATA_DIR/prospects/pending_batch.json（未登录时存的待续跑批）
  3. 今日名单卡   DATA_DIR/workflows/today_prospects.json（情报台读它）
  4. 历次名单     DATA_DIR/prospects/*.xlsx（可选，--files）

（v0.5 起没有潜客历史库了——history.py 已整条移除。老机器上残留的
prospect_history.db* 也顺手清掉。）

**不清**信号库（signal_library.db）——那是情报侧的资产，跟潜客进度无关，
采集一次要花 1–3 分钟且依赖联网。真要清加 --signals。

用法（仓库根目录）：
    python -m prospecting.reset            # 预演，只报告不动手
    python -m prospecting.reset --yes      # 真的清
    python -m prospecting.reset --yes --files --signals
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    try:
        sys.path.insert(0, str(REPO))
        import config
        return config.DATA_DIR
    except Exception as exc:
        print(f"  ⚠ 读不到 config.DATA_DIR（{exc}）——只能复位仓库内的树")
        return None


def _tree_path() -> Path:
    """与 workflow_defs 同一套解析顺序：env → 仓库 data/ → DATA_DIR。"""
    try:
        sys.path.insert(0, str(REPO))
        import config
        cands = []
        if getattr(config, "PROSPECT_TREE_PATH", ""):
            cands.append(Path(config.PROSPECT_TREE_PATH))
        cands += [REPO / "data" / "prospect_tree.json",
                  config.DATA_DIR / "prospect_tree.json"]
        return next((p for p in cands if p.exists()), REPO / "data" / "prospect_tree.json")
    except Exception:
        return REPO / "data" / "prospect_tree.json"


def reset_tree(path: Path, apply: bool) -> dict:
    """所有类目回到未开跑状态。返回 {类目数, 曾跑过的组合数}。"""
    tree = json.loads(path.read_text(encoding="utf-8"))
    leaves = 0
    done_combos = 0
    for sec in tree.get("sectors", []):
        for leaf in sec.get("children", []):
            leaves += 1
            done_combos += len(leaf.get("done_regions") or [])
            leaf["done_regions"] = []
            leaf["status"] = "pending"
        sec["status"] = "pending"
    if apply:
        path.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(l.get("regions") or [])
                for s in tree.get("sectors", []) for l in s.get("children", []))
    return {"leaves": leaves, "was_done": done_combos, "combos": total}


def main() -> int:
    ap = argparse.ArgumentParser(description="把潜客链路清回从零开始")
    ap.add_argument("--yes", action="store_true", help="真的执行（缺省只预演）")
    ap.add_argument("--files", action="store_true", help="连历次 xlsx 名单一起删")
    ap.add_argument("--signals", action="store_true", help="连信号库一起删（慎用）")
    args = ap.parse_args()
    apply = args.yes

    print("潜客进度复位" + ("" if apply else "（预演 · 不会改任何东西，加 --yes 才执行）"))
    print("=" * 56)

    tp = _tree_path()
    if tp.exists():
        st = reset_tree(tp, apply)
        print(f"{'✓' if apply else '·'} 潜客树 {tp}")
        print(f"    {st['leaves']} 个类目 / {st['combos']} 个 (类目×区域) 组合，"
              f"其中曾跑过 {st['was_done']} 个 → 全部复位 pending")
    else:
        print(f"✗ 找不到潜客树：{tp}")

    dd = _data_dir()
    if dd:
        targets = [
            (dd / "prospects" / "pending_batch.json", "存盘批（待续跑的候选）"),
            (dd / "prospect_history.db", "潜客历史库残留（v0.5 已移除历史线）"),
            (dd / "prospect_history.db-wal", None),
            (dd / "prospect_history.db-shm", None),
            (dd / "workflows" / "today_prospects.json", "情报台「今日名单」卡"),
        ]
        if args.signals:
            targets += [(dd / "signal_library.db", "信号库（情报侧资产！）"),
                        (dd / "signal_library.db-wal", None),
                        (dd / "signal_library.db-shm", None)]
        for p, label in targets:
            if not p.exists():
                if label:
                    print(f"·  {label} 本来就没有：{p.name}")
                continue
            size = p.stat().st_size
            if apply:
                p.unlink()
            print(f"{'✓' if apply else '·'} 删除 {label or p.name}（{size} 字节）")

        pdir = dd / "prospects"
        if pdir.exists():
            xs = sorted(pdir.glob("*.xlsx"))
            if args.files and xs:
                if apply:
                    for x in xs:
                        x.unlink()
                print(f"{'✓' if apply else '·'} 删除历次名单 {len(xs)} 份")
            elif xs:
                print(f"·  保留历次名单 {len(xs)} 份（要删加 --files）：{pdir}")

    print("=" * 56)
    if apply:
        print("✅ 已复位。下次跑 prospect_daily 会从第一个类目的第一个区域重新开始。")
    else:
        print("这是预演。确认无误后加 --yes 执行：")
        print("    python -m prospecting.reset --yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
