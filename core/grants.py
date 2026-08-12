"""
core/grants.py — 权限授权记录（谁批准了哪个模块能碰哪些敏感权限）

配合 core/permission_scan.py（算"这段代码实际碰了什么"）使用。一条授权记录 =
{module, granted_permissions, code_hash, approved_by, approved_at}——按【模块】
（源文件的 dotted path，如 "connectors.consolidation_tools" / "skills.weather_query"）
为单位而不是按单个工具名，因为权限footprint天然是文件级的：同一个文件里注册的
多个工具共享同一份 import，分别审批没有意义、只会让 Ned 把同一份代码读好几遍。

判断"这个模块现在还信不信"不再看它的 origin（builtin/skill），只看两条：
  ① 它当前代码的权限footprint（permission_scan.scan_file）是否是 granted_permissions
     的子集；
  ② code_hash 是否等于批准时那一版——只要代码变了（不管权限变多还是变少）都要求
     重新审批。"变少"也可能是误删了某个必要的安全检查，不能只单向放行，所以不做
     "更严格就自动放行"这种智能判断，一律交回人工。

覆盖判定见 is_covered()。2026-08-12（步骤⑤一期）先只建立记录与判定机制本身，
不接任何拦截闸——先让 Ned 用 scripts/audit_permission_grants.py 看到全貌、走完
至少一轮审批。同日（⑤二期）在 Ned 完成首轮审批确认后接入：check_tool() 是运行
时拦截闸的全部逻辑，core/controller.py 的 JarvisController.chat() 在执行每个工具前调用
它（受 config.PERMISSION_ENFORCEMENT 总开关控制，默认开）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from core.memory import _get_conn


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS permission_grants (
                module          TEXT PRIMARY KEY,
                permissions     TEXT NOT NULL,   -- JSON 数组
                code_hash       TEXT NOT NULL,
                source_path     TEXT NOT NULL DEFAULT '',
                approved_by     TEXT NOT NULL,
                approved_at     TEXT NOT NULL,
                note            TEXT NOT NULL DEFAULT ''
            );
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def code_hash(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()[:16]


def grant(module: str, permissions, code: str, approved_by: str,
          source_path: str = "", note: str = "") -> dict:
    """记一条授权：approved_by 批准了 module 当前这版代码可以碰 permissions 这些权限点。
    对同一 module 重复调用是"更新"（新审批覆盖旧的），不是追加。"""
    perms = sorted(set(permissions))
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO permission_grants (module, permissions, code_hash, source_path,"
            " approved_by, approved_at, note) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(module) DO UPDATE SET permissions=excluded.permissions,"
            " code_hash=excluded.code_hash, source_path=excluded.source_path,"
            " approved_by=excluded.approved_by, approved_at=excluded.approved_at,"
            " note=excluded.note",
            (module, json.dumps(perms, ensure_ascii=False), code_hash(code),
             source_path, approved_by, _now(), note),
        )
    return {"ok": True, "module": module, "permissions": perms}


def get_grant(module: str) -> "dict | None":
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM permission_grants WHERE module = ?", (module,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["permissions"] = json.loads(d["permissions"])
    return d


def list_grants() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM permission_grants ORDER BY module").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["permissions"] = json.loads(d["permissions"])
        out.append(d)
    return out


def revoke(module: str) -> dict:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM permission_grants WHERE module = ?", (module,))
    return {"ok": cur.rowcount > 0}


def is_covered(module: str, detected_permissions, code: str) -> tuple[bool, str]:
    """当前代码的权限footprint是否被已批准的授权覆盖。返回 (covered, reason)。"""
    g = get_grant(module)
    if not g:
        return False, "未审批：这个模块还没有任何授权记录"
    if g["code_hash"] != code_hash(code):
        return False, "代码已变化（hash 不匹配批准时那版），需要重新审批"
    missing = set(detected_permissions) - set(g["permissions"])
    if missing:
        return False, f"当前代码用到了未被授权的权限点：{', '.join(sorted(missing))}"
    return True, "ok"


init_db()


def audit_registry() -> list[dict]:
    """遍历 core.registry 里【当前已注册的全部工具】（不分 connectors/prospecting/
    tool_builder 元工具/skills，origin 在这里不重要），按"源文件所在模块"分组
    （同一文件多个工具共享一份权限footprint），逐组算出：现在的权限footprint、
    是否被已有授权覆盖、若曾审批过但代码变了则额外算出"相比上次批准新增了哪些
    权限点"方便 Ned 只看增量。只读，不改任何状态。

    需要在【已完成 core.registry.discover_connectors() + tool_builder 元工具注册 +
    load_all_active_skills()】之后调用（即完整 app 装配完成后），否则看到的只是
    部分注册表。scripts/audit_permission_grants.py 负责把这套装配跑起来。
    """
    import inspect
    from pathlib import Path as _Path
    from core import registry as _registry
    from core import permission_scan as _scan

    by_module: dict[str, list[str]] = {}
    for spec in _registry.iter_specs():
        mod_name = getattr(spec.handler, "__module__", None) or "?"
        by_module.setdefault(mod_name, []).append(spec.name)

    # 每个模块名取一个代表性 handler 来定位源文件——直接对函数对象调用
    # inspect.getsourcefile()，不经 sys.modules/__import__ 按名找模块。
    # 原因（真被沙盒测试炸出来过）：skills/ 里的技能是靠
    # importlib.util.spec_from_file_location + exec_module 动态加载的（见
    # tool_builder.activate_skill），这种加载方式【不会】把模块注册进
    # sys.modules，之后单靠模块名反查是找不到的；但函数对象自带 __code__.co_filename，
    # inspect.getsourcefile 直接从函数对象就能拿到真实文件路径，不依赖 sys.modules。
    representative: dict[str, object] = {}
    for spec in _registry.iter_specs():
        mod_name = getattr(spec.handler, "__module__", None) or "?"
        representative.setdefault(mod_name, spec.handler)

    out: list[dict] = []
    for mod_name, tool_names in sorted(by_module.items()):
        path = ""
        code = ""
        try:
            path = inspect.getsourcefile(representative[mod_name]) or ""
            if path:
                code = _Path(path).read_text(encoding="utf-8")
        except Exception as e:
            out.append({
                "module": mod_name, "tools": sorted(tool_names), "source_path": "",
                "detected_permissions": [], "covered": False,
                "reason": f"读源码失败：{type(e).__name__}: {e}",
                "newly_added": [],
            })
            continue

        detected = _scan.scan_code(code) if code else set()
        covered, reason = is_covered(mod_name, detected, code)
        existing = get_grant(mod_name)
        newly_added = sorted(detected - set(existing["permissions"])) if existing else sorted(detected)
        out.append({
            "module": mod_name,
            "tools": sorted(tool_names),
            "source_path": path,
            "detected_permissions": sorted(detected),
            "covered": covered,
            "reason": reason,
            "newly_added": newly_added,
        })
    return out


# ── 二期：运行时拦截闸（供 core/controller.py 调用）───────────────────────────
# 权限管理工具自身必须永远可调——否则一旦 connectors/permission_tools.py 这份
# 代码将来改了一个字符（code_hash 变化）而还没来得及重新批准，Ned 会被这道闸
# 锁在外面、连"批准"这个动作本身都执行不了（自锁）。这四个是纯治理动作
# （审计只读、批准/撤销/查看都是 core.grants 自己的表），不碰任何业务权限，
# 因此豁免检查——真正把关的是"谁能触发这几个工具"这件事本身（真人对话）。
ALWAYS_ALLOWED_TOOLS = frozenset({
    "audit_permission_grants", "approve_permission_grant",
    "revoke_permission_grant", "list_permission_grants",
})


def check_tool(tool_name: str) -> "tuple[bool, str]":
    """运行时权限闸的全部判断逻辑（controller 只调这一个函数 + 十几行胶水）。

    在【工具即将被执行前】按工具名找到它所在模块，用与 audit_registry 完全一致
    的方式（handler 函数对象 → inspect.getsourcefile，不经 sys.modules）拿到
    当前代码，判断是否被已批准的授权覆盖。找不到工具本身（未注册/拼写错）不是
    本闸职责，交给 _execute_tool 现有的"未知工具"分支处理，这里放行。
    """
    from core import registry as _registry

    if tool_name in ALWAYS_ALLOWED_TOOLS:
        return True, ""

    spec = next((s for s in _registry.iter_specs() if s.name == tool_name), None)
    if spec is None:
        return True, ""

    import inspect
    from pathlib import Path as _Path
    from core import permission_scan as _scan

    mod_name = getattr(spec.handler, "__module__", None) or "?"
    try:
        path = inspect.getsourcefile(spec.handler) or ""
        code = _Path(path).read_text(encoding="utf-8") if path else ""
    except Exception as e:
        return False, (f"权限闸拦截：读不到工具 {tool_name}（模块 {mod_name}）的源码"
                        f"（{type(e).__name__}: {e}），出于安全默认拒绝执行。"
                        f"用 audit_permission_grants 排查。")

    detected = _scan.scan_code(code) if code else set()
    covered, reason = is_covered(mod_name, detected, code)
    if covered:
        return True, ""
    return False, (f"权限闸拦截：工具 {tool_name}（模块 {mod_name}）尚未获得覆盖当前代码的授权"
                    f"——{reason}。请用 audit_permission_grants 查看详情，确认代码安全后用 "
                    f'approve_permission_grant(module="{mod_name}") 批准，再重试这次调用。'
                    f'（应急开关：JARVIS_PERMISSION_ENFORCEMENT=0 可整体关闭本闸。）')
