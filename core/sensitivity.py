"""
文件敏感度判定 —— 决定一份文件能否走云端处理（如 OpenRouter 云端 OCR）。

用于「本地优先、云端兜底」的读取路由：只有被判定为【非敏感】的文件才允许上云；
【敏感】一律留在本机；【存疑】交由上层去问用户（fail-safe，绝不擅自上云）。

判定分三档，返回 (bucket, reason)：
  - "sensitive"      明确敏感（证件/银行/医疗/法律个人文件等）→ 绝不上云
  - "non_sensitive"  明确非敏感（说明书/规格书/白皮书等公开技术资料）→ 可上云
  - "uncertain"      拿不准 → 上层应询问用户

判定顺序（前者命中即短路，越确定越靠前）：
  1) 硬规则·敏感：位于本机加密保险箱目录 / 文件名含敏感词 / 局部文字里出现
     身份证号·银行卡号（Luhn）·护照号等强特征。
  2) 硬规则·非敏感：文件名像公开/技术资料。
  3) 轻模型语义判断（可关）：仅在硬规则都没命中时兜底，且被要求"拿不准就 uncertain"。
"""

import re
from pathlib import Path
from typing import Optional

import config

# ── 关键词表 ──────────────────────────────────────────────────────────────────

# 命中即判【敏感】。覆盖证件、金融、医疗、法律/个人隐私等。
_SENSITIVE_KW = [
    # 证件类
    "身份证", "护照", "户口", "驾驶证", "行驶证", "结婚证", "房产证", "出生证",
    "社保", "医保", "signature", "id card", "passport",
    # 金融类
    "银行", "银行卡", "信用卡", "借记卡", "储蓄卡", "对账单", "流水", "征信",
    "工资", "薪资", "薪酬", "报税", "纳税", "完税", "税单", "invoice", "payslip",
    "salary", "bank", "credit card", "statement",
    # 医疗类
    "病历", "诊断", "处方", "化验", "体检", "medical", "diagnosis",
    # 法律/个人/合同类
    "合同", "协议", "契约", "offer", "录用", "劳动", "保单", "保险单",
    "contract", "agreement", "insurance",
    # 显式标注
    "机密", "绝密", "confidential", "私密", "个人隐私", "内部资料",
]

# 命中且未命中任何敏感词，才判【非敏感】。保守，只放明显的公开/技术资料。
_NONSENSITIVE_KW = [
    "说明书", "使用手册", "用户手册", "操作手册", "产品手册", "规格书", "数据手册",
    "白皮书", "技术文档", "datasheet", "spec", "specification", "manual",
    "whitepaper", "brochure", "catalog", "catalogue", "论文", "paper",
    "公开", "宣传册", "介绍",
]

# ── 内容强特征（局部文字里出现即判敏感）────────────────────────────────────────

_ID18_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")            # 大陆身份证 18 位
_PASSPORT_CN_RE = re.compile(r"(?<![A-Za-z0-9])([EeGgDdSsPp]\d{8})(?![A-Za-z0-9])")


def _luhn_ok(num: str) -> bool:
    digits = [int(c) for c in num if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _content_signal(text: str) -> Optional[str]:
    """局部文字里若出现证件号/银行卡号等强特征，返回原因；否则 None。"""
    if not text:
        return None
    flat = text.replace(" ", "")
    if _ID18_RE.search(flat):
        return "内容中出现疑似身份证号"
    if _PASSPORT_CN_RE.search(flat.upper()):
        return "内容中出现疑似护照号"
    for cand in re.findall(r"(?:\d[ \-]?){13,19}", text):
        if _luhn_ok(re.sub(r"\D", "", cand)):
            return "内容中出现疑似银行卡号（Luhn 校验通过）"
    return None


# ── 轻模型语义判断（兜底，可关）──────────────────────────────────────────────

_LLM_PROMPT = (
    "你是文件敏感度判定器。仅根据给出的文件名（可能还有少量正文片段），"
    "判断这份文件是否涉及【个人隐私/证件/金融账户/医疗/法律个人文件】等敏感信息，"
    "从而决定它能否被上传到第三方云端服务处理。"
    "只输出一个词：sensitive（明显敏感）/ non_sensitive（明显是公开或技术资料）/ "
    "uncertain（拿不准）。拿不准时必须输出 uncertain，不要猜。"
)


async def _llm_classify(name: str, snippet: str = "") -> str:
    try:
        from core.llm import get_client
        client = get_client()   # 有界超时（core/llm 单一构建点）
        user = f"文件名：{name}"
        if snippet.strip():
            user += f"\n正文片段：{snippet[:500]}"
        resp = await client.chat.completions.create(
            model=config.CLAUDE_MODEL_LIGHT,
            max_tokens=8,
            timeout=20,
            messages=[
                {"role": "system", "content": _LLM_PROMPT},
                {"role": "user", "content": user},
            ],
        )
        out = (resp.choices[0].message.content or "").strip().lower()
        for b in ("non_sensitive", "sensitive", "uncertain"):
            if b in out:
                return b
    except Exception:
        pass
    return "uncertain"


# ── 主入口 ────────────────────────────────────────────────────────────────────

def _kw_hit(haystack: str, words: list) -> Optional[str]:
    for w in words:
        if w.lower() in haystack:
            return w
    return None


async def classify(path: str, partial_text: str = "", use_llm: Optional[bool] = None) -> tuple[str, str]:
    """判定文件敏感度，返回 (bucket, reason)。bucket ∈ {sensitive, non_sensitive, uncertain}。"""
    p = Path(path)
    name = p.name
    hay = f"{name} {p.parent}".lower()

    # 1) 硬规则·敏感
    try:
        if config.DATA_DIR in p.resolve().parents:
            return "sensitive", "位于本机加密保险箱数据目录"
    except Exception:
        pass
    hit = _kw_hit(hay, _SENSITIVE_KW)
    if hit:
        return "sensitive", f"文件名/路径含敏感词「{hit}」"
    sig = _content_signal(partial_text)
    if sig:
        return "sensitive", sig

    # 2) 硬规则·非敏感
    hit = _kw_hit(hay, _NONSENSITIVE_KW)
    if hit:
        return "non_sensitive", f"文件名像公开/技术资料（含「{hit}」）"

    # 3) 轻模型兜底（可关）；关或失败一律 uncertain（fail-safe）
    if config.SENSITIVITY_LLM if use_llm is None else use_llm:
        b = await _llm_classify(name, partial_text)
        reason = {
            "sensitive": "模型判断涉及敏感信息",
            "non_sensitive": "模型判断为公开/非敏感资料",
            "uncertain": "无明显信号，模型也拿不准",
        }.get(b, "拿不准")
        return b, reason
    return "uncertain", "无明显敏感/非敏感信号"
