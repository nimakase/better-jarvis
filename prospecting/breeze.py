"""prospecting/breeze.py — 把 HubSpot 内置 AI「Breeze」当只读子 agent 驱动。

设计见 docs/客户循环与Breeze设计方案.md 第二节。要点:
  - Breeze widget 跑在一个【同源 iframe】(chatspot-widget-ui)内。定位 frame 的稳定办法是
    「找 contentDocument 里含 rte-content 的那个 frame」,别用 iframe 序号(序号会变)。
  - 完成判据用【事件锚点】而非定时等待:最新一条 assistant-message 内部冒出 actions 排
    (赞/踩/复制)才算答完。⚠ 不能用「actions 数≥msg 数」——assistant-message 计数含
    "Thinking/Counting" 中间态占位,只判【最后一条】是否挂 actions 排。
  - 两段式:先等出现新的答案气泡(count 增长)确认真开跑,再等它挂上 actions 排,
    避免误抓上一条旧答案。
  - 【只读】。任何写操作(改属性/建 Note/建 Task)一律走 UI 驱动,不经 Breeze。

用 Playwright 【sync】API(与 HubSpotBrowser 一致),入参只吃一个 page,与大类解耦。

选择器已于 2026-07-25 用浏览器实测确认;唯一没拿到稳定 data-test-id 的是顶栏
「Assistant」开关按钮(_OPEN_SIDEBAR_SELECTORS),首次在 Ned 本机跑时留意即可。
"""
from __future__ import annotations

import logging
import time
from typing import Any, List, Optional

# ── 已实测的稳定落点(语义化 data-test-id,不是哈希类名)──────────────
BREEZE_COMPOSER = 'div[contenteditable="true"][data-test-id="rte-content"]'
BREEZE_SEND_BUTTON = 'button[data-test-id="chat-send-button"]'
BREEZE_ANSWER = '[data-test-id="assistant-message"]'
BREEZE_ANSWER_ACTIONS = '[data-test-id="assistant-message-actions"]'
BREEZE_NEW_THREAD = '[data-test-id="new-thread-button"]'   # aria: Start a new chat

# 顶栏打开 Assistant 侧栏的按钮——没有稳定 data-test-id,用可访问名/文本兜底。
# 首次本机验证时若打不开,截图看一眼真实按钮再补一条更准的选择器。
_OPEN_SIDEBAR_SELECTORS = [
    'button[aria-label*="Assistant" i]',
    'button:has-text("Assistant")',
    '[data-test-id*="assistant" i]',
]

DEFAULT_TIMEOUT_S = 60          # Breeze 重查询可能 30-60s,给足
_FRAME_FIND_TIMEOUT_S = 15      # 找/等 Breeze frame
_GEN_START_TIMEOUT_S = 20       # 等「开始生成」(新气泡出现)
_POLL_S = 0.5


class BreezeError(RuntimeError):
    """Breeze 驱动失败(找不到 widget、打不开侧栏等)。"""


class BreezeTimeout(BreezeError):
    """等答案超时。附 partial 为超时时已渲染的部分文本。"""

    def __init__(self, message: str, partial: str = ""):
        super().__init__(message)
        self.partial = partial


def _log(logger: Optional[logging.Logger], level: str, msg: str, *args: Any) -> None:
    if logger is not None:
        getattr(logger, level)(msg, *args)


def _find_breeze_frame(page, timeout_s: float):
    """遍历 page.frames,返回内含 rte-content 的那个同源 frame;找不到返回 None。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for fr in page.frames:
            try:
                if fr.locator(BREEZE_COMPOSER).count() > 0:
                    return fr
            except Exception:
                # 跨域 frame 或还没加载好——跳过
                continue
        time.sleep(_POLL_S)
    return None


def _open_sidebar(page, logger) -> None:
    """点顶栏 Assistant 把侧栏调出来。已经开着就什么都不做(靠 frame 探测判断)。"""
    for sel in _OPEN_SIDEBAR_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=4000)
                _log(logger, "info", "breeze | opened sidebar via %s", sel)
                return
        except Exception as e:
            _log(logger, "debug", "breeze | open-sidebar selector 失败 %s: %s", sel, e)
            continue
    _log(logger, "warning",
         "breeze | 未能通过已知选择器打开侧栏;若 widget 已开着可忽略")


def _get_frame(page, logger):
    """拿到 Breeze frame:先探测,没有就尝试打开侧栏再探测。"""
    fr = _find_breeze_frame(page, timeout_s=3)
    if fr is not None:
        return fr
    _open_sidebar(page, logger)
    fr = _find_breeze_frame(page, timeout_s=_FRAME_FIND_TIMEOUT_S)
    if fr is None:
        raise BreezeError("找不到 Breeze widget 的 iframe(rte-content 未出现);"
                          "确认已登录且 Assistant 可用")
    return fr


def _new_conversation(fr, logger) -> None:
    """开一条新 Breeze 对话,避免跨问题的上下文污染。找不到按钮就静默跳过(降级不报错)。"""
    try:
        btn = fr.locator(BREEZE_NEW_THREAD)
        if btn.count() > 0:
            btn.first.click(timeout=4000)
            time.sleep(0.5)   # 等新对话渲染出干净的 composer
            _log(logger, "info", "breeze | 已开新对话")
            return
    except Exception as e:
        _log(logger, "debug", "breeze | 开新对话失败(忽略,沿用当前对话): %s", e)
    _log(logger, "debug", "breeze | 未找到新对话按钮,沿用当前对话")


def _parse_rows(answer_locator) -> List[dict]:
    """三态结构化解析:先表格、再列表、否则空(调用方回退用纯文本)。

    只做轻解析——拿不准就返回 [],让上层用 raw text 或丢给 structured.py 归一。
    """
    rows: List[dict] = []
    # ① 表格
    try:
        tables = answer_locator.locator("table")
        if tables.count() > 0:
            table = tables.first
            trs = table.locator("tbody tr")
            for i in range(trs.count()):
                cells = trs.nth(i).locator("td")
                vals = [cells.nth(j).inner_text().strip() for j in range(cells.count())]
                if any(vals):
                    rows.append({"cells": vals})
            if rows:
                return rows
    except Exception:
        pass
    # ② 列表
    try:
        lis = answer_locator.locator("li")
        for i in range(lis.count()):
            t = lis.nth(i).inner_text().strip()
            if t:
                rows.append({"text": t})
    except Exception:
        pass
    return rows


def ask(page, prompt: str, logger: Optional[logging.Logger] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S, fresh: bool = True) -> dict:
    """向 Breeze 提一个【只读】问题,返回 {"text": 答案纯文本, "rows": 轻结构化解析}。

    page: Playwright(sync)Page,通常传 HubSpotBrowser.page(需已登录、在 HubSpot 页面)。
    fresh: 默认每次开一条新对话,避免跨问题的长上下文污染;设 False 则续用当前对话。
    超时抛 BreezeTimeout(附 partial)。

    ⚠ 这是只读接口:prompt 里不要下达写指令(改属性/建记录);写走 UI 驱动。
    """
    if not prompt or not prompt.strip():
        raise BreezeError("空 prompt")

    fr = _get_frame(page, logger)

    # 默认开新对话:干净上下文。放在记基线之前,让基线反映新对话的真实起点。
    if fresh:
        _new_conversation(fr, logger)

    # 送信前记基线:已有多少条 assistant-message(含中间态占位)
    try:
        baseline = fr.locator(BREEZE_ANSWER).count()
    except Exception:
        baseline = 0

    composer = fr.locator(BREEZE_COMPOSER)
    composer.click(timeout=8000)
    composer.fill(prompt)
    # 优先点发送键;不行就回车兜底
    try:
        send = fr.locator(BREEZE_SEND_BUTTON)
        if send.count() > 0:
            send.click(timeout=4000)
        else:
            composer.press("Enter")
    except Exception:
        composer.press("Enter")
    _log(logger, "info", "breeze | 已发送 | prompt=%.60s", prompt.replace("\n", " "))

    # ── 第一段:等「开始生成」——新的 assistant-message 出现(count 超过基线)──
    gen_deadline = time.time() + _GEN_START_TIMEOUT_S
    started = False
    while time.time() < gen_deadline:
        try:
            if fr.locator(BREEZE_ANSWER).count() > baseline:
                started = True
                break
        except Exception:
            pass
        time.sleep(_POLL_S)
    if not started:
        _log(logger, "warning", "breeze | 未观测到生成开始(可能极快已完成),继续等完成锚点")

    # ── 第二段:等【最新一条】answer 内部挂上 actions 排 = 答完 ──
    deadline = time.time() + timeout_s
    last_answer = fr.locator(BREEZE_ANSWER).last
    while time.time() < deadline:
        try:
            last_answer = fr.locator(BREEZE_ANSWER).last
            actions = last_answer.locator(BREEZE_ANSWER_ACTIONS)
            text = (last_answer.inner_text() or "").strip()
            if actions.count() > 0 and text:
                rows = _parse_rows(last_answer)
                _log(logger, "info", "breeze | 完成 | 文本 %d 字 | 结构化 %d 行",
                     len(text), len(rows))
                return {"text": text, "rows": rows}
        except Exception as e:
            _log(logger, "debug", "breeze | 轮询中异常(忽略): %s", e)
        time.sleep(_POLL_S)

    # 超时:交回已渲染的部分
    partial = ""
    try:
        partial = (fr.locator(BREEZE_ANSWER).last.inner_text() or "").strip()
    except Exception:
        pass
    raise BreezeTimeout(f"Breeze 在 {timeout_s}s 内未出现完成锚点", partial=partial)
