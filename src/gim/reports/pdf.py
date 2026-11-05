"""Professional PDF export (fpdf2, pure Python -- no external programs needed on Windows).

Uses the bundled DejaVu Sans fonts (Bitstream Vera licence, see fonts/LICENSE-DejaVu.txt)
so non-Latin names (Cyrillic, accents, etc.) render correctly.
"""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from gim.reports.render import build_blocks

FONT_DIR = Path(__file__).parent / "fonts"
NAVY = (16, 37, 66)
ACCENT = (178, 34, 52)
GREY = (90, 98, 110)
LIGHT = (236, 240, 245)
TYPE_COLOURS = {"ASSESSMENT": (31, 97, 141), "CONTEXT": (128, 96, 0), "ATTRIBUTED_CLAIM": (60, 60, 60),
                "REPORTED_FACT": (20, 20, 20)}


class ReportPDF(FPDF):
    def __init__(self, title: str, footer_note: str):
        super().__init__(format="A4")
        self.report_title = title
        self.footer_note = footer_note
        self.add_font("DejaVu", "", str(FONT_DIR / "DejaVuSans.ttf"))
        self.add_font("DejaVu", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
        self.add_font("DejaVu", "I", str(FONT_DIR / "DejaVuSans.ttf"))
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(18, 18, 18)

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("DejaVu", "", 7.5)
        self.set_text_color(*GREY)
        self.cell(0, 6, f"GIM · {self.report_title[:110]}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*LIGHT)
        self.line(18, self.get_y(), 192, self.get_y())
        self.ln(3)

    def footer(self):
        self.set_y(-12)
        self.set_font("DejaVu", "", 7)
        self.set_text_color(*GREY)
        self.cell(0, 5, f"{self.footer_note}   ·   page {self.page_no()}/{{nb}}", align="C")


def render_pdf(content: dict) -> bytes:
    status = content.get("status", "draft").upper()
    note = f"{status} — machine-assisted, analyst review required"
    if content.get("data_origin") != "live":
        note = "SYNTHETIC TEST DATA — NOT REAL INTELLIGENCE · " + note
    pdf = ReportPDF(content["title"], note)
    pdf.alias_nb_pages()
    pdf.add_page()
    w = pdf.epw

    for blk in build_blocks(content):
        k = blk.kind
        if k == "title":
            pdf.set_fill_color(*NAVY)
            pdf.rect(0, 0, 210, 42, style="F")
            pdf.set_xy(18, 12)
            pdf.set_font("DejaVu", "B", 8)
            pdf.set_text_color(200, 210, 225)
            pdf.cell(0, 5, "GLOBAL INTELLIGENCE MONITOR", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("DejaVu", "B", 15)
            pdf.set_text_color(255, 255, 255)
            pdf.multi_cell(w, 7, blk.text[:160], new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_y(max(pdf.get_y(), 44) + 2)
        elif k == "meta":
            pdf.set_font("DejaVu", "", 8)
            pdf.set_text_color(*GREY)
            for line in blk.data["lines"]:
                pdf.multi_cell(w, 4.5, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
        elif k == "banner":
            pdf.set_fill_color(253, 236, 236)
            pdf.set_text_color(*ACCENT)
            pdf.set_font("DejaVu", "B", 9)
            pdf.multi_cell(w, 6, "  " + blk.text, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
        elif k == "h1":
            pdf.ln(3)
            pdf.set_font("DejaVu", "B", 12)
            pdf.set_text_color(*NAVY)
            pdf.multi_cell(w, 7, blk.text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_draw_color(*ACCENT)
            pdf.set_line_width(0.5)
            pdf.line(18, pdf.get_y(), 48, pdf.get_y())
            pdf.set_line_width(0.2)
            pdf.ln(2)
        elif k == "h2":
            pdf.ln(2)
            pdf.set_fill_color(*LIGHT)
            pdf.set_font("DejaVu", "B", 10)
            pdf.set_text_color(*NAVY)
            pdf.multi_cell(w, 6, " " + blk.text, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)
        elif k == "h3":
            pdf.set_font("DejaVu", "B", 9)
            pdf.set_text_color(*NAVY)
            pdf.multi_cell(w, 5.5, blk.text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif k == "note":
            pdf.set_font("DejaVu", "", 7.5)
            pdf.set_text_color(*GREY)
            pdf.multi_cell(w, 4.2, blk.text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif k in ("claim", "bullet"):
            pdf.set_text_color(*TYPE_COLOURS.get(blk.data.get("type", ""), (20, 20, 20)))
            pdf.set_font("DejaVu", "", 9)
            label = blk.data.get("label")
            text = (f"{label}: " if label else "") + blk.text
            x = pdf.get_x()
            pdf.cell(4, 5, "•")
            pdf.set_x(x + 4)
            pdf.multi_cell(w - 4, 5, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(0.8)
        elif k == "table":
            pdf.set_font("DejaVu", "", 8)
            pdf.set_text_color(20, 20, 20)
            with pdf.table(col_widths=(35, 65), line_height=4.6, headings_style=FontFace(emphasis="BOLD", fill_color=LIGHT),
                           first_row_as_headings=True,
                           borders_layout="HORIZONTAL_LINES") as table:
                table.row([str(h) for h in blk.data["headers"]])
                for row in blk.data["rows"]:
                    table.row([str(c) for c in row])
        elif k == "source":
            pdf.set_font("DejaVu", "", 7.5)
            pdf.set_text_color(40, 40, 40)
            pdf.multi_cell(w, 4, blk.text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            url = blk.data.get("url", "")
            if url.startswith(("http://", "https://")):
                pdf.set_text_color(31, 97, 141)
                pdf.multi_cell(w, 4, url, link=url, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(0.8)
    return bytes(pdf.output())
