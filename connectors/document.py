"""
文档读取工具

支持格式：
  PDF    → pdfplumber 提取文字（含表格）
  DOCX   → python-docx 提取段落和表格
  XLSX   → openpyxl 提取所有 sheet
  PPTX   → python-pptx 提取每页文字
  TXT/MD → 直接读取
  CSV    → 转为 markdown 表格
  图片    → base64 发给视觉模型，返回描述/文字

使用方式（对贾维斯说）：
  "帮我读一下 C:/Users/Ned/Desktop/合同.pdf，总结主要条款"
  "读一下这个 Excel：C:/Users/Ned/Downloads/报表.xlsx"
"""

import base64
import csv
import io
import json
import os
from pathlib import Path
from typing import Optional

import config
from functools import partial
from core.registry import tool as _tool

# read_document 归入 fileio 组（与元工具 send_file_to_chat 同组：文件读入/发出）。
# 注意：与文档保险箱 doc_vault（group=documents）刻意分开——一个只读取，一个加密归档。
tool = partial(_tool, group="fileio")

# token 安全上限：超过此长度截断并提示
MAX_CHARS = 120_000  # ~8万 token，留足 context 给其他内容


def _truncate(text: str, path: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return (
        text[:MAX_CHARS]
        + f"\n\n[文档过长，已截断。原文件：{path}，"
        f"已读取约前 {MAX_CHARS} 字符，共约 {len(text)} 字符。"
        f"如需后续内容请告知页码或章节。]"
    )


# ── 各格式提取函数 ────────────────────────────────────────────────────────────

def _read_pdf(path: str, pages: Optional[str] = None) -> str:
    """
    pages: None=全部, "1"=第1页, "1-5"=第1到5页, "1,3,5"=指定页
    """
    import pdfplumber

    def parse_pages(spec: str, total: int) -> list[int]:
        result = set()
        for part in spec.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-")
                result.update(range(int(a) - 1, min(int(b), total)))
            else:
                result.add(int(part) - 1)
        return sorted(result)

    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        if pages:
            indices = parse_pages(pages, total)
        else:
            indices = list(range(total))

        parts = []
        for i in indices:
            page = pdf.pages[i]
            text = page.extract_text() or ""
            # 提取表格
            tables = page.extract_tables()
            table_text = ""
            for table in tables:
                rows = []
                for row in table:
                    rows.append(" | ".join(str(c or "").strip() for c in row))
                table_text += "\n" + "\n".join(rows)

            parts.append(f"--- 第 {i+1} 页 ---\n{text}{table_text}")

    return f"[PDF 文档，共 {total} 页，已读 {len(indices)} 页]\n\n" + "\n\n".join(parts)


def _read_docx(path: str) -> str:
    from docx import Document
    doc = Document(path)
    parts = []

    for para in doc.paragraphs:
        if para.text.strip():
            style = para.style.name
            prefix = ""
            if "Heading 1" in style:
                prefix = "# "
            elif "Heading 2" in style:
                prefix = "## "
            elif "Heading 3" in style:
                prefix = "### "
            parts.append(prefix + para.text)

    for table in doc.tables:
        rows = []
        for row in table.rows:
            rows.append(" | ".join(cell.text.strip() for cell in row.cells))
        parts.append("\n[表格]\n" + "\n".join(rows))

    return "[Word 文档]\n\n" + "\n\n".join(parts)


def _read_xlsx(path: str, sheet: Optional[str] = None) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)

    sheets = [sheet] if sheet and sheet in wb.sheetnames else wb.sheetnames
    parts = []

    for name in sheets:
        ws = wb[name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            if any(cell is not None for cell in row):
                rows.append(" | ".join(str(c) if c is not None else "" for c in row))
        parts.append(f"[Sheet: {name}]\n" + "\n".join(rows))

    return f"[Excel 文档，共 {len(wb.sheetnames)} 个 Sheet]\n\n" + "\n\n".join(parts)


def _read_pptx(path: str) -> str:
    from pptx import Presentation
    prs = Presentation(path)
    parts = []

    for i, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = para.text.strip()
                    if t:
                        texts.append(t)
        if texts:
            parts.append(f"--- 第 {i} 页 ---\n" + "\n".join(texts))

    return f"[PowerPoint，共 {len(prs.slides)} 页]\n\n" + "\n\n".join(parts)


def _read_csv(path: str) -> str:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        rows = [" | ".join(row) for row in reader]
    return "[CSV 文件]\n\n" + "\n".join(rows)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


async def _read_image(path: str) -> str:
    """把图片发给视觉模型，返回文字描述/OCR 结果。"""
    from openai import AsyncOpenAI

    ext = Path(path).suffix.lower().lstrip(".")
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}
    mime = mime_map.get(ext, "image/png")

    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    client = AsyncOpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
    )
    resp = await client.chat.completions.create(
        model=config.CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": "请提取并描述图片中的所有文字和内容，尽量完整。"}
            ]
        }]
    )
    return "[图片内容]\n\n" + resp.choices[0].message.content


# ── 主工具函数 ────────────────────────────────────────────────────────────────

@tool(
    "read_document",
    (
        "读取本地文件并提取文字内容。支持 PDF、Word(.docx)、Excel(.xlsx)、"
        "PowerPoint(.pptx)、CSV、TXT、Markdown，以及 JPG/PNG 等图片（视觉识别）。"
        "用户要读取、查看、分析、总结某个文件时使用。"
        "【只读取、不保存】——若用户是要把保单/合同等存档归类，改用 ingest_document_file。"
        "path 必须是文件的完整路径。"
    ),
    {
        "type": "object",
        "properties": {
            "path":  {"type": "string", "description": "文件完整路径，如 C:/Users/Ned/Desktop/合同.pdf"},
            "pages": {"type": "string", "description": "仅 PDF 有效：指定页码范围，如 '1-5' 或 '1,3,5'，留空=全部"},
            "sheet": {"type": "string", "description": "仅 Excel 有效：指定 Sheet 名称，留空=所有 Sheet"},
        },
        "required": ["path"],
    },
)
async def read_document(path: str, pages: str = "", sheet: str = "") -> str:
    """
    读取文档并返回文本内容。
    path:  文件的完整路径
    pages: 仅 PDF 有效，指定页码，如 "1-5" 或 "1,3,5"，留空=全部
    sheet: 仅 XLSX 有效，指定 Sheet 名称，留空=所有 Sheet
    """
    path = path.strip().strip('"').strip("'")

    if not os.path.exists(path):
        return f"文件不存在：{path}\n请确认路径正确（Windows 路径示例：C:/Users/Ned/Desktop/文件.pdf）"

    ext = Path(path).suffix.lower()
    file_size_mb = os.path.getsize(path) / 1024 / 1024

    if file_size_mb > 50:
        return f"文件过大（{file_size_mb:.1f} MB），超过 50MB 限制，请提供更小的文件或指定具体页码范围。"

    try:
        if ext == ".pdf":
            text = _read_pdf(path, pages or None)
        elif ext in (".docx", ".doc"):
            text = _read_docx(path)
        elif ext in (".xlsx", ".xls", ".xlsm"):
            text = _read_xlsx(path, sheet or None)
        elif ext in (".pptx", ".ppt"):
            text = _read_pptx(path)
        elif ext == ".csv":
            text = _read_csv(path)
        elif ext in (".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml"):
            text = _read_text(path)
        elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            text = await _read_image(path)
        else:
            return (
                f"不支持的文件格式：{ext}\n"
                f"支持的格式：PDF、DOCX、XLSX、PPTX、CSV、TXT、MD、JPG、PNG 等图片"
            )

        return _truncate(text, path)

    except ImportError as e:
        pkg = str(e).split("'")[1] if "'" in str(e) else str(e)
        return f"缺少依赖库 {pkg}，请运行：pip install {pkg}"
    except Exception as e:
        return f"读取文件出错：{type(e).__name__}: {e}"
