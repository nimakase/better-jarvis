"""
core/capability_watch.py — 模型能力变化检测（任务 #19）

core/model_capabilities.py 的文档里早留了这句话："定时轮询模型目录、自动发现
能力变化是姊妹机制……本模块只负责声明与查询"——这就是那个姊妹机制。

背景：贾维斯会因为主模型缺某项能力而搭"临时通路"（如 core/vision.py 的
describe_image：主模型不支持视觉时的图片理解兜底，见 [[core/model_routing]]
任务 #20）。这类通路一旦模型自己获得了对应能力（如迁移到原生视觉的 DeepSeek
v4 之后），继续绕远路调用就是纯浪费——但没人会主动想起去检查。

设计：
  - 状态落盘（DATA_DIR/capability_watch_state.json）：记上一次检查时的
    模型字符串 + 其能力快照（布尔字段）。
  - check_drift()：拿当前 config.CLAUDE_MODEL 的能力（core/model_capabilities）
    跟上次快照比对，算出哪些布尔能力【新获得】、哪些【失去了】，然后在
    registry 里找 capability_workaround 命中"新获得能力"的工具——这些工具
    的存在理由可能已经不成立了，值得人工复核（不自动删除/停用，只是提示）。
  - 首次运行（没有历史快照）不报"变化"——没有基线可比，只记录当前状态当基线。
  - 只比较布尔字段（online_search/vision 这类"有/没有"），context_window
    这种数值字段不参与 gained/lost 判定（数值变化不是"获得/失去某项能力"）。
  - "定时"这部分不在本模块重新发明调度器——接入方式是把
    check_capability_drift 工具挂一个 core.schedule 定时任务（用户决定要不要、
    多久查一次），本模块只提供可重复调用、幂等安全的检测原语。
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path


def _state_path() -> Path:
    import config
    return config.DATA_DIR / "capability_watch_state.json"


def _load_state() -> "dict | None":
    p = _state_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_state(state: dict) -> None:
    try:
        _state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _caps_bool_dict(caps) -> dict:
    """只取布尔字段（notes 是文本说明，context_window 是数值，都不参与 gained/lost）。"""
    return {f.name: getattr(caps, f.name) for f in dataclasses.fields(caps)
            if f.type in ("bool", bool) or isinstance(getattr(caps, f.name), bool)}


def check_drift() -> dict:
    """核心检测。返回：
    {first_run, changed, model, prev_model, gained: [能力名,...], lost: [...],
     workaround_tools_to_review: [{"tool":..., "capability":...}, ...]}
    绝不抛异常（内部读写失败一律降级为"当作首次运行"，不阻断调用方）。"""
    import config
    from core import model_capabilities as mc

    current_model = config.CLAUDE_MODEL
    current_dict = _caps_bool_dict(mc.capabilities_of(current_model))

    state = _load_state()
    is_first_run = state is None
    prev_caps = (state or {}).get("caps") or {}
    prev_model = (state or {}).get("model")

    if is_first_run:
        gained, lost = [], []
    else:
        gained = sorted(k for k, v in current_dict.items() if v and not prev_caps.get(k))
        lost = sorted(k for k, v in current_dict.items() if (not v) and prev_caps.get(k))

    workaround_tools: list[dict] = []
    if gained:
        try:
            from core import registry
            for spec in registry.iter_specs():
                if spec.capability_workaround and spec.capability_workaround in gained:
                    workaround_tools.append({"tool": spec.name, "capability": spec.capability_workaround})
        except Exception:
            pass

    _save_state({"model": current_model, "caps": current_dict})

    return {
        "first_run": is_first_run,
        "changed": bool(gained or lost),
        "model": current_model,
        "prev_model": prev_model,
        "gained": gained,
        "lost": lost,
        "workaround_tools_to_review": workaround_tools,
    }


def describe_drift(result: dict) -> str:
    """把 check_drift() 的结果渲成人可读文案。"""
    if result["first_run"]:
        return f"首次检测，已记录基线（当前模型：{result['model']}）。"
    if not result["changed"]:
        return f"模型能力无变化（当前：{result['model']}）。"
    lines = [f"检测到模型能力变化：{result.get('prev_model') or '?'} → {result['model']}"]
    if result["gained"]:
        lines.append("新获得能力：" + "、".join(result["gained"]))
    if result["lost"]:
        lines.append("失去能力：" + "、".join(result["lost"]))
    if result["workaround_tools_to_review"]:
        lines.append("以下工具是为对应能力搭的临时通路，现在可能不再需要，建议复核：")
        for w in result["workaround_tools_to_review"]:
            lines.append(f"  - {w['tool']}（原为绕过缺失的「{w['capability']}」能力）")
    return "\n".join(lines)
