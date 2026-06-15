"""
本地证件 OCR 与字段解析

【隐私】整个流程在本机完成，照片与识别结果【不】发往任何云端服务。
依赖：rapidocr-onnxruntime（纯 CPU，无需 GPU）。未安装时给出清晰提示。

  pip install rapidocr-onnxruntime

【设计取舍】
  - CVV 不自动识别：卡背上的 CVV 只是三个裸数字、无标签，OCR 无法可靠区分，
    故 CVV 一律由用户在确认表单里手动填写。
  - 有效期不依赖标签词（有效期 / VALID THRU / GOOD THRU / Date of expiry 等
    用词中英混杂且不统一）：改为把所有日期候选抓出来，再按证件类型挑选。
  - 解析结果只作"预填"，最终由用户在本机确认表单核对/修改后才保存。
"""

import calendar
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── OCR 引擎（惰性单例，避免每次重新加载模型）────────────────────────────────

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise RuntimeError(
                "未安装本地 OCR 引擎。请在贾维斯所在的 Python 环境运行：\n"
                "    pip install rapidocr-onnxruntime\n"
                "（纯 CPU，无需 GPU；首次运行会自动加载内置模型）"
            ) from e
        # 调高检测分辨率（默认会把长边压到 ~736px，证件小字会糊）。
        # 不同版本参数名可能不同，失败则退回默认。
        try:
            _engine = RapidOCR(det_limit_side_len=1536, det_limit_type="max")
        except Exception:
            _engine = RapidOCR()
    return _engine


def _load_and_preprocess(image_path: str):
    """
    用 OpenCV 读图并做轻量增强，提升 OCR 命中率：
      - 用 imdecode + np.fromfile 读取（Windows 中文路径下 cv2.imread 会失败）
      - 分辨率偏低时放大（证件拍得小是识别差的主因）
    返回 ndarray；失败返回 None（调用方退回直接用路径）。
    """
    try:
        import cv2
        import numpy as np
        data = np.fromfile(image_path, dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        long_side = max(h, w)
        if long_side < 1600:
            scale = min(1600 / long_side, 3.0)
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        return img
    except Exception:
        return None


def ocr_lines(image_path: str) -> list[str]:
    """本机识别图片，返回文字行列表。开启方向校正；优先用预处理后的图。"""
    engine = _get_engine()
    img = _load_and_preprocess(image_path)
    target = img if img is not None else image_path
    try:
        result, _ = engine(target, use_cls=True)   # use_cls：文字方向校正，应对拍歪
    except TypeError:
        result, _ = engine(target)                 # 老版本不支持 use_cls
    if not result:
        return []
    return [item[1].strip() for item in result if item[1] and item[1].strip()]


# ── 基础正则 ──────────────────────────────────────────────────────────────────

_ID18_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_PASSPORT_CN_RE = re.compile(r"(?<![A-Z0-9])([EeGgDdSsPp]\d{8})(?![A-Z0-9])")
_LONGTERM_RE = re.compile(r"长\s*期|LONG\s*[- ]?TERM|PERMANENT", re.I)

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
)}


# ── 日期识别（多格式、中英文、不依赖标签词）──────────────────────────────────

def _valid(y: int, mo: int, d: Optional[int]) -> Optional[str]:
    if not (1 <= mo <= 12):
        return None
    if d is None:
        return f"{y:04d}-{mo:02d}"
    if not (1 <= d <= 31):
        return None
    try:
        datetime(y, mo, d)
    except ValueError:
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _h_year_first(m):
    return _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _h_day_enmonth(m):
    mo = _MONTHS.get(m.group(2)[:3].lower())
    return _valid(int(m.group(3)), mo, int(m.group(1))) if mo else None


def _h_enmonth_year(m):
    mo = _MONTHS.get(m.group(1)[:3].lower())
    return _valid(int(m.group(2)), mo, None) if mo else None


def _h_day_first(m):
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if a > 12 and b <= 12:
        return _valid(y, b, a)
    if b > 12 and a <= 12:
        return _valid(y, a, b)
    return _valid(y, b, a)  # 默认 日/月/年（中国大陆常见）


# (正则, 处理函数) 按优先级排列：越具体/越完整的越靠前；命中后会"消费"该片段，
# 防止后面的模式在同一段文字里重复或错位匹配（如年在前的日期被日在前模式误抓）。
_DATE_PATTERNS = [
    (re.compile(r"((?:19|20)\d{2})\s*[.\-/年]\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})"), _h_year_first),
    (re.compile(r"(\d{1,2})\s*[ \-./]\s*([A-Za-z]{3,9})\.?\s*[ \-./,]\s*((?:19|20)\d{2})"), _h_day_enmonth),
    (re.compile(r"(?<!\d)(\d{1,2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*((?:19|20)\d{2})(?!\d)"), _h_day_first),
    (re.compile(r"(?<![A-Za-z])([A-Za-z]{3,9})\.?\s*[ \-./,]\s*((?:19|20)\d{2})(?!\d)"), _h_enmonth_year),
]


def collect_full_dates(text: str) -> list[str]:
    """
    抓出文本里所有"完整日期"（含年月日，或英文月份+年），归一化为
    YYYY-MM-DD（无日时 YYYY-MM）。覆盖中英文多种写法，匹配后消费片段避免重叠。
    """
    work = list(text)
    found: list[str] = []
    for pattern, handler in _DATE_PATTERNS:
        spans = []
        for m in pattern.finditer("".join(work)):
            v = handler(m)
            if v:
                found.append(v)
                spans.append((m.start(), m.end()))
        for s, e in spans:
            for i in range(s, e):
                work[i] = " "
    # 去重保序
    seen, out = set(), []
    for d in found:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def collect_mmyy(text: str) -> list[str]:
    """银行卡有效期 MM/YY 或 MM/YYYY，归一化为 YYYY-MM-DD（当月最后一天）。"""
    out = []
    # MM/YYYY 优先
    for m in re.finditer(r"(?<!\d)(0[1-9]|1[0-2])\s*/\s*(20\d{2})(?!\d)", text):
        mo, y = int(m.group(1)), int(m.group(2))
        out.append(f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}")
    # MM/YY
    for m in re.finditer(r"(?<!\d)(0[1-9]|1[0-2])\s*/\s*(\d{2})(?!\d)", text):
        mo, y = int(m.group(1)), 2000 + int(m.group(2))
        out.append(f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}")
    # 去重
    seen, res = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            res.append(d)
    return res


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


# ── 各类型解析 ────────────────────────────────────────────────────────────────

_BANK_KEYWORDS = {
    "招商": "招商银行", "工商": "中国工商银行", "建设": "中国建设银行",
    "农业": "中国农业银行", "中国银行": "中国银行", "交通": "交通银行",
    "邮政": "中国邮政储蓄银行", "民生": "民生银行", "兴业": "兴业银行",
    "浦发": "浦发银行", "中信": "中信银行", "光大": "光大银行",
    "平安": "平安银行", "广发": "广发银行", "华夏": "华夏银行",
    "VISA": "VISA", "MASTERCARD": "MasterCard", "UNIONPAY": "银联",
}


def parse_bank_card(lines: list[str]) -> dict:
    text = " ".join(lines)
    fields: dict = {}

    # 卡号：13-19 位（允许空格/连字符），Luhn 校验优先
    best = None
    for c in re.findall(r"(?:\d[ \-]?){13,19}", text):
        digits = re.sub(r"\D", "", c)
        if 13 <= len(digits) <= 19:
            if _luhn_ok(digits):
                best = digits
                break
            if best is None:
                best = digits
    if best:
        fields["card_number"] = best

    # 有效期：卡上是 MM/YY；若多个取最晚
    mmyy = collect_mmyy(text)
    if mmyy:
        fields["expiry"] = max(mmyy)

    # 发卡行
    up = text.upper()
    for kw, name in _BANK_KEYWORDS.items():
        if kw in text or kw in up:
            fields["bank"] = name
            break

    # 注意：CVV 不自动识别（裸三位数字无法可靠区分），交由用户手填。
    return fields


def parse_id_card(lines: list[str]) -> dict:
    text = " ".join(lines)
    fields: dict = {}

    m = _ID18_RE.search(text.replace(" ", ""))
    if m:
        idn = m.group(1).upper()
        fields["id_number"] = idn
        try:
            fields["birth_date"] = f"{idn[6:10]}-{idn[10:12]}-{idn[12:14]}"
        except Exception:
            pass

    # 姓名
    for ln in lines:
        mm = re.search(r"姓\s*名\s*[:：]?\s*([一-龥·]{2,6})", ln)
        if mm:
            fields["name"] = mm.group(1)
            break
    if "name" not in fields:
        for ln in lines:
            if re.fullmatch(r"[一-龥·]{2,4}", ln):
                fields["name"] = ln
                break

    # 住址
    addr = [re.sub(r".*住\s*址\s*[:：]?", "", ln).strip() for ln in lines if "住址" in ln or "住 址" in ln]
    addr = [a for a in addr if a]
    if addr:
        fields["address"] = " ".join(addr)

    # 签发机关
    for ln in lines:
        mm = re.search(r"签发机关\s*[:：]?\s*(.+)", ln)
        if mm and mm.group(1).strip():
            fields["authority"] = mm.group(1).strip()
            break

    # 有效期：先抠掉身份证号（号里内嵌出生日期），再抓日期
    search_text = text
    if fields.get("id_number"):
        search_text = search_text.replace(fields["id_number"], " ")
    search_text = re.sub(r"\d{15,}", " ", search_text)

    if _LONGTERM_RE.search(search_text):
        # "2015.03.07-长期"
        dates = collect_full_dates(search_text)
        if dates:
            fields["issue_date"] = min(dates)
        fields["expiry"] = "长期"
    else:
        dates = collect_full_dates(search_text)
        if len(dates) >= 2:
            fields["issue_date"] = min(dates)
            fields["expiry"] = max(dates)
        elif len(dates) == 1:
            fields["expiry"] = dates[0]

    return fields


def parse_passport(lines: list[str]) -> dict:
    text = " ".join(lines)
    fields: dict = {}

    m = _PASSPORT_CN_RE.search(text.replace(" ", "").upper())
    if m:
        fields["passport_number"] = m.group(1).upper()

    # 姓名（英文护照含拼音；这里尽力取中文名）
    for ln in lines:
        if re.fullmatch(r"[一-龥·]{2,6}", ln):
            fields["name"] = ln
            break

    dates = collect_full_dates(text)
    if dates:
        fields["issue_date"] = min(dates)
        fields["expiry"] = max(dates)   # 护照日期多，最晚的通常是有效期至
    return fields


_PARSERS = {
    "bank_card": parse_bank_card,
    "id_card":   parse_id_card,
    "passport":  parse_passport,
}


def _auto_detect(lines: list[str]) -> str:
    text = " ".join(lines)
    flat = text.replace(" ", "")
    if _ID18_RE.search(flat) or "居民身份证" in flat:
        return "id_card"
    if "护照" in flat or "PASSPORT" in text.upper() or _PASSPORT_CN_RE.search(flat.upper()):
        return "passport"
    if re.search(r"(?:\d[ \-]?){13,19}", text) or "银行" in flat or "UNIONPAY" in text.upper():
        return "bank_card"
    return "other"


def _parse_lines(lines: list[str], cred_type: str = "auto") -> dict:
    """从（可能来自多张图合并的）OCR 文字行解析字段。"""
    if not lines:
        return {"cred_type": cred_type if cred_type != "auto" else "other",
                "fields": {}, "expiry": None, "raw_lines": []}

    ctype = cred_type if cred_type in _PARSERS else _auto_detect(lines)
    parser = _PARSERS.get(ctype)
    fields = parser(lines) if parser else {}

    expiry = fields.get("expiry")  # 已是 YYYY-MM-DD 或 "长期"
    fields["_raw_lines"] = lines
    return {"cred_type": ctype, "fields": fields, "expiry": expiry, "raw_lines": lines}


def extract(image_path: str, cred_type: str = "auto") -> dict:
    """
    单张图：本机识别 + 解析。返回：
      {cred_type, fields(真实值，仅本机), expiry(YYYY-MM-DD|长期|None), raw_lines}
    出错抛 RuntimeError（含安装提示）。
    """
    p = Path(image_path)
    if not p.exists():
        raise RuntimeError(f"图片不存在：{image_path}")
    return _parse_lines(ocr_lines(str(p)), cred_type)


def extract_multi(image_paths: list[str], cred_type: str = "auto") -> dict:
    """
    多张图（如证件正反面）：分别识别后【合并所有文字行】再统一解析，
    使正面（号码/姓名）与背面（有效期/签发机关等）的信息能合到一条记录里。
    """
    all_lines: list[str] = []
    for path in image_paths:
        p = Path(path)
        if not p.exists():
            continue
        all_lines.extend(ocr_lines(str(p)))
    result = _parse_lines(all_lines, cred_type)
    result["image_count"] = len([p for p in image_paths if Path(p).exists()])
    return result
