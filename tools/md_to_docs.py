"""Конвертер INSTRUKCIYA.md -> DOCX (форматированный) и TXT (плоский).

Поддерживает: заголовки, списки, таблицы, блоки кода, **жирный**, `код`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm

MONO = "Consolas"
BODY = "Calibri"


def _shade(cell, hex_color: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def _add_runs(par, text: str, base_size: int = 11, mono: bool = False) -> None:
    """Разбирает **жирный**, `код`, *курсив* внутри строки."""
    tokens = re.split(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)", text)
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("**") and tok.endswith("**"):
            r = par.add_run(tok[2:-2])
            r.bold = True
        elif tok.startswith("`") and tok.endswith("`"):
            r = par.add_run(tok[1:-1])
            r.font.name = MONO
            r.font.size = Pt(base_size - 1)
            r.font.color.rgb = RGBColor(0xB0, 0x30, 0x60)
        elif tok.startswith("*") and tok.endswith("*") and len(tok) > 2:
            r = par.add_run(tok[1:-1])
            r.italic = True
        else:
            r = par.add_run(tok)
        if not (tok.startswith("`") and tok.endswith("`")):
            r.font.name = MONO if mono else BODY
            r.font.size = Pt(base_size)


def convert(md_path: str, docx_path: str, txt_path: str) -> None:
    md = Path(md_path).read_text(encoding="utf-8")
    lines = md.split("\n")

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = BODY
    st.font.size = Pt(11)
    for s in doc.sections:
        s.left_margin = s.right_margin = Cm(2)
        s.top_margin = s.bottom_margin = Cm(2)

    txt: list[str] = []
    i = 0
    in_code = False
    code_buf: list[str] = []

    def flush_code():
        nonlocal code_buf
        if not code_buf:
            return
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.5)
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after = Pt(8)
        pPr = p._p.get_or_add_pPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:fill"), "F4F4F4")
        pPr.append(shd)
        for n, cl in enumerate(code_buf):
            r = p.add_run(cl)
            r.font.name = MONO
            r.font.size = Pt(9)
            if n < len(code_buf) - 1:
                r.add_break()
        txt.extend(code_buf)
        txt.append("")
        code_buf = []

    while i < len(lines):
        ln = lines[i]

        # блок кода
        if ln.strip().startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(ln)
            i += 1
            continue

        # таблица
        if ln.strip().startswith("|") and i + 1 < len(lines) and re.match(
                r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            body: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            table = doc.add_table(rows=1, cols=len(header))
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            hdr = table.rows[0].cells
            for j, h in enumerate(header):
                hdr[j].text = ""
                p = hdr[j].paragraphs[0]
                _add_runs(p, h, base_size=10)
                for r in p.runs:
                    r.bold = True
                _shade(hdr[j], "E8EEF7")
            for row in body:
                cells = table.add_row().cells
                for j, val in enumerate(row[:len(header)]):
                    cells[j].text = ""
                    _add_runs(cells[j].paragraphs[0], val, base_size=10)
            doc.add_paragraph()
            clean = lambda v: re.sub(r"[*`]", "", v)
            txt.append(" | ".join(clean(h) for h in header))
            for row in body:
                txt.append(" | ".join(clean(c) for c in row))
            txt.append("")
            continue

        # заголовки
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            p = doc.add_heading(level=min(level, 4))
            _add_runs(p, text, base_size={1: 18, 2: 14, 3: 12}.get(level, 11))
            for r in p.runs:
                r.font.name = BODY
                if level <= 2:
                    r.font.color.rgb = RGBColor(0x1F, 0x3B, 0x63)
            plain = re.sub(r"[*`]", "", text)
            txt.append("")
            txt.append(plain.upper() if level == 1 else plain)
            txt.append("-" * min(len(text), 60) if level <= 2 else "")
            i += 1
            continue

        # горизонтальная линия
        if ln.strip() in ("---", "***", "___"):
            doc.add_paragraph()
            txt.append("=" * 70)
            i += 1
            continue

        # списки
        m = re.match(r"^(\s*)([-*·]|\d+\.)\s+(.*)$", ln)
        if m:
            indent = len(m.group(1)) // 2
            text = m.group(3)
            style = "List Bullet" if m.group(2) in "-*·" else "List Number"
            p = doc.add_paragraph(style=style)
            p.paragraph_format.left_indent = Cm(0.6 + 0.6 * indent)
            p.paragraph_format.space_after = Pt(2)
            _add_runs(p, text)
            bullet = "•" if m.group(2) in "-*·" else m.group(2)
            txt.append("  " * indent + f"{bullet} " + re.sub(r"[*`]", "", text))
            i += 1
            continue

        # пустая строка
        if not ln.strip():
            i += 1
            continue

        # обычный абзац
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(6)
        _add_runs(p, ln.strip())
        txt.append(re.sub(r"[*`]", "", ln.strip()))
        i += 1

    # нумерация страниц в подвале
    for section in doc.sections:
        footer_p = section.footer.paragraphs[0]
        footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = footer_p.add_run()
        fld = OxmlElement("w:fldSimple")
        fld.set(qn("w:instr"), "PAGE")
        r._r.addnext(fld)

    doc.save(docx_path)
    Path(txt_path).write_text("\n".join(txt), encoding="utf-8")
    print(f"DOCX: {docx_path}")
    print(f"TXT:  {txt_path}")


if __name__ == "__main__":
    convert(sys.argv[1], sys.argv[2], sys.argv[3])
