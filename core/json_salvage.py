"""
截断容忍的 JSON 数组解析 —— 联网大批量生成的通用保底。

背景：模型被要求"只输出一个 JSON 数组"时，一旦触到 max_tokens 就会在半路断掉。
朴素写法（find("[") … rfind("]") … json.loads）在这种情况下**整批归零**：
rfind("]") 会命中某个内层数组（如 "components": ["MCU"]）的收尾方括号，
切出来的片段依然非法，except 里一 return []，前面已经完整生成的几十条全丢。

这套解析按括号深度逐个抢救完整的顶层 {...} 对象，只丢弃被截断的最后一个。
于是"输出被切断"从**整批失败**降级为**少最后一条**。

原先这段逻辑只长在信号采集那条路上（intel/workflow_defs._parse_signal_array），
潜客生成那条路是朴素写法、踩同一个坑。提到这里两边共用，避免再分叉。

纯标准库。
"""
from __future__ import annotations

import json


def salvage_json_array(text: str) -> list:
    """从模型输出里抽出 JSON 数组；容忍前后说明文字与【尾部截断】。

    1) 先试整体解析（正常情况，最快路径）
    2) 失败则按括号深度逐个提取完整的顶层对象，跳过被截断的尾巴

    永不抛错；实在捞不到返回 []。
    """
    if not text:
        return []
    start = text.find("[")
    if start < 0:
        return []
    body = text[start:]

    # 1) 正常情况：整体就是合法数组
    end = body.rfind("]")
    if end > 0:
        try:
            data = json.loads(body[: end + 1])
            if isinstance(data, list):
                return data
        except Exception:
            pass

    # 2) 抢救：逐个提取完整的顶层对象
    #    需要自己跟踪字符串状态与转义，否则 rationale 里的 { } " 会把深度算错
    objs: list = []
    depth = 0
    in_str = False
    esc = False
    obj_start = None
    for i, ch in enumerate(body):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    objs.append(json.loads(body[obj_start:i + 1]))
                except Exception:
                    pass
                obj_start = None
    return objs


def strip_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏（各解析入口共用，别再各写一份正则）。"""
    import re
    m = re.search(r"```(?:json)?\s*(.+?)```", text or "", re.DOTALL)
    return m.group(1).strip() if m else (text or "").strip()


def salvage_json_objects(text: str) -> list:
    """从模型输出里抽出全部【顶层 JSON 对象】（不在数组里也行），容忍前后噪声与截断。

    与 salvage_json_array 同一套括号深度 + 字符串状态跟踪；对象版此前散落在
    spawn.parse_contract / doc_vault._parse_json 各写一份（后者还是朴素正则），
    统一到这里。永不抛错；捞不到返回 []。
    """
    text = strip_fence(text)
    if not text:
        return []
    objs: list = []
    depth = 0
    in_str = False
    esc = False
    obj_start = None
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start is not None:
                    try:
                        obj = json.loads(text[obj_start:i + 1])
                        if isinstance(obj, dict):
                            objs.append(obj)
                    except Exception:
                        pass
                    obj_start = None
    return objs


def looks_truncated(text: str) -> bool:
    """输出是否像被 max_tokens 切断（用于日志/告警，不影响解析结果）。

    判据：找得到 '['，但配不出收尾的 ']'——即数组从未合法闭合。
    """
    if not text:
        return False
    start = text.find("[")
    if start < 0:
        return False
    body = text[start:]
    depth = 0
    in_str = False
    esc = False
    for ch in body:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return False      # 数组已合法闭合
    return True
