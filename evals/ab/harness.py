"""
步骤 3：A/B harness —— 真实调用模型测路由，但隔离工具副作用。

在仓库根目录跑（需已配好 OPENROUTER_API_KEY）：
    python evals/ab/harness.py                # 跑 A 和 B
    REPS=10 python evals/ab/harness.py        # 改每条用例的重复次数
    ARMS=A,B,C python evals/ab/harness.py     # C = B + 拆分 general（见末尾说明）

产物：
    evals/ab/out/raw_<时间戳>.json   每次跑的明细
    控制台打印聚合对比表 + 配对差值的 bootstrap 95% CI
"""
import sys, os, json, asyncio, random, statistics, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from core import registry
from core import controller as ctrl
from core.results import ToolResult
from evals.ab.eval_set import CASES

REPS = int(os.environ.get("REPS", "6"))
ARMS = os.environ.get("ARMS", "A,B").split(",")

registry.discover_connectors()
REGISTERED = {n for names in registry.groups().values() for n in names}

# 校验评测集里的期望工具名是否真实存在，避免拿错名字白跑
for c in CASES:
    for t in c["expected_tools"]:
        if t not in REGISTERED:
            print(f"⚠️  用例 {c['id']} 的期望工具 '{t}' 不在注册表里，请按 dump_tools 改名。")


# ── 合成工具结果：拦截真实执行，避免记忆/发文件等副作用 ──────────
def _fake_execute_factory(rec):
    async def _fake_execute_tool(name, inputs):
        rec["executed"].append(name)
        return ToolResult(text=f"[模拟结果] 工具 {name} 已成功执行。")
    return _fake_execute_tool


def _extract_calls(messages):
    """从对话历史里按轮次顺序取出全部工具调用名（含 load_tools）。"""
    seq = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                seq.append(tc["function"]["name"])
    return seq


async def run_once(case, arm) -> dict:
    """跑一条用例一次，返回一次的原始记录。"""
    config.PROGRESSIVE_TOOLS = (arm in ("B", "C"))   # controller 运行时读此属性
    rec = {"executed": [], "payload_chars": [], "n_tools": []}

    c = ctrl.JarvisController()

    # 包装模型调用，记录每轮 tools 负载大小（token 代理）
    comp = c.client.chat.completions
    orig_create = comp.create
    async def wrapped_create(**kw):
        tools = kw.get("tools") or []
        rec["payload_chars"].append(len(json.dumps(tools, ensure_ascii=False)))
        rec["n_tools"].append(len(tools))
        return await orig_create(**kw)
    comp.create = wrapped_create

    # 拦截工具执行（模块级全局，chat 内按全局名引用）
    saved = ctrl._execute_tool
    ctrl._execute_tool = _fake_execute_factory(rec)
    try:
        async for _ in c.chat(case["prompt"]):
            pass
    except Exception as e:
        rec["error"] = repr(e)
    finally:
        ctrl._execute_tool = saved

    seq = _extract_calls(c.messages)
    tool_calls = [n for n in seq if n != ctrl.JarvisController.LOAD_TOOLS_NAME]
    load_calls = [n for n in seq if n == ctrl.JarvisController.LOAD_TOOLS_NAME]
    expected = set(case["expected_tools"])
    called = set(tool_calls)

    if expected:
        hit = expected <= called
        first_hit = bool(tool_calls) and tool_calls[0] in expected
    else:  # none 类：正确＝一个工具都没调
        hit = (len(tool_calls) == 0)
        first_hit = hit

    return {
        "hit": hit,
        "first_hit": first_hit,
        "hallucinated": any(n not in REGISTERED for n in tool_calls),
        "extra_tools": len(called - expected),
        "n_tool_calls": len(tool_calls),
        "n_load": len(load_calls),
        "rounds": len(rec["payload_chars"]),
        "mean_payload": (statistics.mean(rec["payload_chars"]) if rec["payload_chars"] else 0),
        "total_payload": sum(rec["payload_chars"]),
        "error": rec.get("error"),
    }


def _bootstrap_ci(paired, iters=5000):
    """对「每条用例 B 命中率 − A 命中率」做 bootstrap 95% CI。paired: list[(a,b)]"""
    if not paired:
        return (0.0, 0.0, 0.0)
    diffs_point = statistics.mean(b - a for a, b in paired)
    boots = []
    n = len(paired)
    for _ in range(iters):
        sample = [random.choice(paired) for _ in range(n)]
        boots.append(statistics.mean(b - a for a, b in sample))
    boots.sort()
    return (diffs_point, boots[int(0.025 * iters)], boots[int(0.975 * iters)])


async def main():
    print(f"ARMS={ARMS}  REPS={REPS}  cases={len(CASES)}  "
          f"(总调用≈{len(ARMS) * REPS * len(CASES)} 次模型)\n")
    results = {arm: {} for arm in ARMS}      # arm -> case_id -> list[rep records]

    for arm in ARMS:
        if arm == "C":
            _split_general_groups()          # 见文件末尾说明
        for case in CASES:
            recs = []
            for r in range(REPS):
                rec = await run_once(case, arm)
                recs.append(rec)
                print(f"[{arm}] {case['id']} rep{r+1}/{REPS} "
                      f"hit={rec['hit']} load={rec['n_load']} rounds={rec['rounds']}"
                      + (f" ERR={rec['error']}" if rec['error'] else ""))
            results[arm][case["id"]] = recs

    # ── 落盘 ──
    out_dir = Path(__file__).resolve().parent / "out"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    raw_path = out_dir / f"raw_{stamp}.json"
    raw_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 聚合表 ──
    def rate(arm, key):
        vals = [rec[key] for cid in results[arm] for rec in results[arm][cid]]
        return sum(vals) / len(vals) if vals else 0

    def mean(arm, key):
        vals = [rec[key] for cid in results[arm] for rec in results[arm][cid]]
        return statistics.mean(vals) if vals else 0

    print("\n================ 聚合对比 ================")
    hdr = f"{'指标':<22}" + "".join(f"{a:>12}" for a in ARMS)
    print(hdr)
    for label, key, kind in [
        ("正确工具率", "hit", "rate"),
        ("首工具命中率", "first_hit", "rate"),
        ("臆造工具率", "hallucinated", "rate"),
        ("平均额外工具数", "extra_tools", "mean"),
        ("平均load_tools轮", "n_load", "mean"),
        ("平均轮次", "rounds", "mean"),
        ("平均每轮工具字符", "mean_payload", "mean"),
        ("平均总工具字符", "total_payload", "mean"),
    ]:
        f = rate if kind == "rate" else mean
        row = f"{label:<22}" + "".join(f"{f(a, key):>12.3f}" for a in ARMS)
        print(row)

    # ── 主指标配对 CI（仅当同时有 A、B）──
    if "A" in ARMS and "B" in ARMS:
        paired = []
        for cid in results["A"]:
            a = statistics.mean(r["hit"] for r in results["A"][cid])
            b = statistics.mean(r["hit"] for r in results["B"][cid])
            paired.append((a, b))
        point, lo, hi = _bootstrap_ci(paired)
        print("\n主指标（正确工具率）B−A 配对差值：")
        print(f"   点估计 {point:+.3f}   95% CI [{lo:+.3f}, {hi:+.3f}]")
        verdict = ("B 显著更好" if lo > 0 else
                   "B 显著更差" if hi < 0 else
                   "差异不显著（看是否非劣 + 代价指标）")
        print("   判定：", verdict)

    print("\n明细已存：", raw_path)


# ── 可选 C 臂：把 general 拆成更细的组（只改内存里的 group 元数据，不写盘）──
def _split_general_groups():
    """演示：把 general 组按连接器拆细，看收益是否依赖更细分组。
    按你的真实工具名调整这里的映射；找不到的名字会被忽略。"""
    SPLIT = {
        "document": ["read_document"],
        "credentials": ["list_credentials", "reveal_credential", "ingest_credential_image"],
    }
    for new_group, names in SPLIT.items():
        for n in names:
            spec = registry._SPECS.get(n)
            if spec:
                spec.group = new_group


if __name__ == "__main__":
    asyncio.run(main())
