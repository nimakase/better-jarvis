"""
core/drift.py — 自我漂移感知（㉕ 的机械件之一 · git 是「我身上发生过什么」的事实源）

问题：贾维斯此前默认「只有我自己会改我」。但 Ned、外部工具（Cowork/Claude）、
编辑器都会从旁边改他——而他的世界观里没有「外部」这个概念。两个具体的洞：
  1) 自我认知过期：self_review 冷却判断读的是自己写的复盘文档，外部改动全盲；
  2) 更险：自我迭代的快照/回滚是「只有我在动」的假设，外部未提交改动会被误伤。

本模块提供只读的漂移检测（写入侧的和解闸在 self_iteration 里）：
  - workdir_dirty(paths=None)：工作树未提交改动清单（可限定路径）
  - external_commits_since_last_look()：上次看过之后新出现的提交，按作者/
    提交信息归因（self-iter: 前缀 = 自改，其余 = 外部手），并更新「已看过」状态
  - recently_touched(days)：近 N 天 git 里被【任何人】改过的文件（冷却判据）
  - provider()：world_state 感官——主线程每轮能看见「有外部改动待知晓」

全部 best-effort：git 不可用/不是仓库时一律安静返回空。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent

try:
    import config
    _STATE = config.DATA_DIR / "self_drift_state.json"
except Exception:
    _STATE = REPO / "data" / "self_drift_state.json"

SELF_COMMIT_PREFIX = "self-iter:"   # 自我迭代提交的识别前缀（见 self_iteration._commit）
_GIT_TIMEOUT = 5


def _git(args: list[str], repo: Optional[Path] = None) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(repo or REPO),
                           capture_output=True, text=True, timeout=_GIT_TIMEOUT)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# ── 工作树状态 ────────────────────────────────────────────────────────────────

def workdir_dirty(paths: Optional[list[str]] = None,
                  repo: Optional[Path] = None) -> list[str]:
    """未提交改动的文件清单（相对仓库根）。paths 给定时只看这些路径。"""
    args = ["status", "--porcelain", "--untracked-files=no"]
    if paths:
        args += ["--", *paths]
    out = _git(args, repo)
    files = []
    for line in out.splitlines():
        if len(line) > 3:
            files.append(line[3:].strip().strip('"'))
    return files


def head_sha(repo: Optional[Path] = None) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


# ── 外部提交归因 ──────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def external_commits_since_last_look(repo: Optional[Path] = None,
                                     update_state: bool = True) -> list[dict]:
    """上次看过之后的新提交里，【非自我迭代】的那些（= 别人对我做了什么）。

    每条：{sha, author, subject}。首次调用（无状态）只记基线不报旧账。
    """
    head = head_sha(repo)
    if not head:
        return []
    state = _load_state()
    last = state.get("last_seen_sha", "")
    if update_state:
        _save_state({**state, "last_seen_sha": head})
    if not last or last == head:
        return []
    out = _git(["log", "--format=%H%x01%an%x01%s", f"{last}..{head}"], repo)
    commits = []
    for line in out.splitlines():
        parts = line.split("\x01")
        if len(parts) != 3:
            continue
        sha, author, subject = parts
        if subject.startswith(SELF_COMMIT_PREFIX):
            continue   # 自我迭代自己的提交，不算外部
        commits.append({"sha": sha[:10], "author": author, "subject": subject[:80]})
    return commits


# ── 冷却判据（修 self_review 只读自家复盘的盲区）──────────────────────────────

def recently_touched(days: int = 3, repo: Optional[Path] = None) -> set[str]:
    """近 N 天被【任何人】（含外部手）改过的文件——反思冷却应对它们让路。"""
    out = _git(["log", f"--since={days} days ago", "--name-only", "--format="], repo)
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


# ── world_state 感官 ─────────────────────────────────────────────────────────

def provider() -> dict:
    """自我漂移读数（world_state 用）。干净时返回最小读数。"""
    d: dict = {}
    dirty = workdir_dirty()
    if dirty:
        d["未提交改动"] = f"{len(dirty)} 个文件"
    ext = external_commits_since_last_look()
    if ext:
        who = "、".join(sorted({c['author'] for c in ext}))
        d["外部改动"] = f"{len(ext)} 个新提交（{who}）——我被改过，可主动向用户确认"
    if not d:
        d["代码"] = "与上次一致"
    return d
