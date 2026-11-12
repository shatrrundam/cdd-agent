"""Builds the Word (.docx) CDD report from the agent output."""

from __future__ import annotations

import io

from docx import Document
from docx.enum.section import WD_ORIENT  # noqa: F401  (kept for users who want landscape tables)
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

NAVY = RGBColor(0x0B, 0x2A, 0x4A)
TEAL = RGBColor(0x1F, 0x6F, 0x8B)
GREY = RGBColor(0x59, 0x59, 0x59)
FONT = "Calibri"

SECTION_TITLES = {
    "market": "1. Market analysis",
    "competition": "2. Competitive landscape",
    "customer": "3. Customer analysis",
    "target": "4. Target business analysis",
}
VERDICT_COLOURS = {"Supports": "2E7D32", "Partially supports": "B26A00", "Does not support": "B71C1C"}
SEVERITY_COLOURS = {"High": "F8D7DA", "Medium": "FFF3CD", "Low": "E2EFDA"}


# ---------- low-level formatting ----------

def _shade(cell, hex_fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def _cell_text(cell, text, bold=False, size=9, colour=None):
    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    run = p.add_run(str(text) if text is not None else "")
    run.bold = bold
    run.font.size = Pt(size)
    if colour:
        run.font.color.rgb = colour


def _repeat_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    tr_pr.append(el)


def _setup(doc):
    st = doc.styles["Normal"]
    st.font.name = FONT
    st.font.size = Pt(10.5)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    st.paragraph_format.space_after = Pt(6)
    st.paragraph_format.line_spacing = 1.15
    for name, size, colour in (("Heading 1", 18, NAVY), ("Heading 2", 13, TEAL), ("Heading 3", 11, NAVY)):
        h = doc.styles[name]
        h.font.name = FONT
        h.font.size = Pt(size)
        h.font.bold = True
        h.font.color.rgb = colour
        h.paragraph_format.space_before = Pt(14 if name == "Heading 1" else 10)
        h.paragraph_format.space_after = Pt(6)
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Cm(2.2)
        s.left_margin = s.right_margin = Cm(2.2)


def _heading(doc, text, level):
    h = doc.add_heading(text, level=level)
    for r in h.runs:
        r.font.name = FONT
    return h


def _bullets(doc, items):
    for it in items or []:
        if it:
            doc.add_paragraph(str(it), style="List Bullet")


def _label(doc, label, value):
    p = doc.add_paragraph()
    r = p.add_run(f"{label}: ")
    r.bold = True
    p.add_run(str(value))
    return p


def _table(doc, columns, rows, widths=None, header_fill="0B2A4A"):
    if not columns:
        return
    t = doc.add_table(rows=1, cols=len(columns))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, c in enumerate(columns):
        cell = t.rows[0].cells[i]
        _cell_text(cell, c, bold=True, colour=RGBColor(0xFF, 0xFF, 0xFF))
        _shade(cell, header_fill)
    _repeat_header(t.rows[0])
    for r_i, row in enumerate(rows or []):
        cells = t.add_row().cells
        row = list(row) + [""] * (len(columns) - len(row))
        for i, val in enumerate(row[:len(columns)]):
            _cell_text(cells[i], val)
            if r_i % 2:
                _shade(cells[i], "F2F5F8")
    if widths:
        _set_widths(t, widths)
    doc.add_paragraph()
    return t


def _set_widths(t, widths):
    """Fixed column widths that Word and LibreOffice both respect."""
    t.autofit = False
    tbl_pr = t._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)
    for i, gc in enumerate(t._tbl.tblGrid.findall(qn("w:gridCol"))):
        if i < len(widths):
            gc.set(qn("w:w"), str(int(widths[i] * 567)))
    for row in t.rows:
        for i, w in enumerate(widths):
            row.cells[i].width = Cm(w)


# ---------- report ----------

def build_report(res: dict) -> bytes:
    doc = Document()
    _setup(doc)
    s = res.get("summary", {})
    secs = res.get("sections", {})

    # Cover
    for _ in range(5):
        doc.add_paragraph()
    p = doc.add_paragraph()
    r = p.add_run("Commercial Due Diligence")
    r.font.size, r.font.color.rgb, r.bold = Pt(14), TEAL, True
    p = doc.add_paragraph()
    r = p.add_run(res.get("company", ""))
    r.font.size, r.font.color.rgb, r.bold = Pt(30), NAVY, True
    p = doc.add_paragraph()
    r = p.add_run("High-level outside-in review from public sources")
    r.font.size, r.font.color.rgb = Pt(13), GREY
    doc.add_paragraph()
    _label(doc, "Context", res.get("context") or "Listed company")
    _label(doc, "Perspective", res.get("lens", ""))
    _label(doc, "Date", res.get("date", ""))
    _label(doc, "Prepared by", "CDD research agent (AI-assisted desk research)")
    doc.add_paragraph()
    p = doc.add_paragraph()
    r = p.add_run("Draft for discussion. Based only on publicly available information; not verified with "
                  "management, customers or the data room. Not investment advice.")
    r.italic, r.font.size, r.font.color.rgb = True, Pt(9), GREY
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # Executive summary
    _heading(doc, "Executive summary", 1)
    verdict = s.get("verdict", "Not assessed")
    t = doc.add_table(rows=1, cols=2)
    t.style = "Table Grid"
    _cell_text(t.rows[0].cells[0], "Growth story", bold=True, size=10, colour=RGBColor(0xFF, 0xFF, 0xFF))
    _shade(t.rows[0].cells[0], VERDICT_COLOURS.get(verdict, "595959"))
    _cell_text(t.rows[0].cells[1], f"{verdict}. {s.get('verdict_reason', '')}", size=10)
    _set_widths(t, [3.5, 13.1])
    doc.add_paragraph()
    _bullets(doc, s.get("executive_summary"))

    # Section scorecard
    _heading(doc, "Section scorecard", 2)
    rows = []
    for k in ("market", "competition", "customer", "target"):
        sec = secs.get(k, {})
        rows.append([SECTION_TITLES[k][3:], sec.get("headline", ""), sec.get("confidence", "")])
    _table(doc, ["Section", "Headline", "Confidence"], rows, widths=[3.5, 10.6, 2.5])

    # Red flags
    flags = s.get("top_red_flags") or []
    if flags:
        _heading(doc, "Key red flags", 2)
        t = _table(doc, ["Issue", "Section", "Severity", "Why it matters"],
                   [[f.get("issue"), f.get("section"), f.get("severity"), f.get("why_it_matters")] for f in flags],
                   widths=[5.0, 2.4, 2.0, 7.2])
        if t is not None:
            for i, f in enumerate(flags, start=1):
                fill = SEVERITY_COLOURS.get(f.get("severity"))
                if fill:
                    _shade(t.rows[i].cells[2], fill)

    if s.get("value_creation_levers"):
        _heading(doc, "Value creation levers", 2)
        _bullets(doc, s["value_creation_levers"])

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # Company snapshot: the shared facts every section builds on, printed once
    fb = res.get("fact_base") or {}
    if fb:
        _heading(doc, "Company snapshot", 1)
        if fb.get("description"):
            doc.add_paragraph(fb["description"])
        for label, key in (("Listing", "listing"), ("Market cap", "market_cap"),
                           ("Fiscal year end", "fiscal_year_end"), ("Markets served", "market_definition"),
                           ("Geographies", "geographies")):
            if fb.get(key):
                _label(doc, label, fb[key])
        segs = fb.get("segments") or []
        if segs:
            _heading(doc, "Business segments", 2)
            _table(doc, ["Segment", "Revenue share", "Description"],
                   [[s.get("segment"), s.get("revenue_share"), s.get("description")] for s in segs],
                   widths=[4.0, 3.0, 9.6])
        fin = fb.get("financials") or {}
        if isinstance(fin, dict) and fin.get("columns"):
            unit = f" ({fb['currency_unit']})" if fb.get("currency_unit") else ""
            _heading(doc, f"Financial summary{unit}", 2)
            _table(doc, fin["columns"], fin.get("rows") or [])
        if fb.get("peers"):
            _label(doc, "Peer set used in this report", ", ".join(fb["peers"]))
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # Detailed sections
    for k in ("market", "competition", "customer", "target"):
        sec = secs.get(k)
        if not sec:
            continue
        _heading(doc, SECTION_TITLES[k], 1)
        p = doc.add_paragraph()
        r = p.add_run(sec.get("headline", ""))
        r.bold, r.font.size, r.font.color.rgb = True, Pt(11.5), NAVY
        meta = sec.get("_meta", {})
        p = doc.add_paragraph()
        r = p.add_run(f"Confidence: {sec.get('confidence', 'n/a')}. {sec.get('confidence_reason', '')}  "
                      f"({meta.get('searches', 0)} web searches, {len(sec.get('sources') or [])} sources)")
        r.italic, r.font.size, r.font.color.rgb = True, Pt(9), GREY

        if sec.get("key_findings"):
            _heading(doc, "Key findings", 2)
            _bullets(doc, sec["key_findings"])

        tbl = sec.get("table")
        if isinstance(tbl, dict) and tbl.get("columns"):
            _heading(doc, tbl.get("title") or "Data", 2)
            _table(doc, tbl["columns"], tbl.get("rows") or [])

        for part in sec.get("analysis") or []:
            _heading(doc, part.get("heading", ""), 3)
            for para in str(part.get("body", "")).split("\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())

        if sec.get("red_flags"):
            _heading(doc, "Red flags", 2)
            _bullets(doc, sec["red_flags"])
        if sec.get("data_gaps"):
            _heading(doc, "Data gaps", 2)
            _bullets(doc, sec["data_gaps"])
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # Questions and next steps
    _heading(doc, "Questions for management", 1)
    for i, q in enumerate(s.get("questions_for_management") or [], start=1):
        doc.add_paragraph(str(q), style="List Number")
    if s.get("next_steps"):
        _heading(doc, "Recommended next steps for full CDD", 2)
        _bullets(doc, s["next_steps"])

    # Sources
    _heading(doc, "Appendix A: Sources", 1)
    p = doc.add_paragraph()
    r = p.add_run("Only sources returned by live web search are listed; any source the model named but "
                  "did not actually retrieve was removed.")
    r.italic, r.font.size = True, Pt(9)
    groups = [("Company snapshot", fb.get("sources") or [])] + \
             [(SECTION_TITLES[k][3:], (secs.get(k) or {}).get("sources") or [])
              for k in ("market", "competition", "customer", "target")]
    for title, srcs in groups:
        if not srcs:
            continue
        _heading(doc, title, 3)
        for src in srcs:
            para = doc.add_paragraph(style="List Bullet")
            r = para.add_run(f"{src.get('title', '')}  ")
            r.font.size = Pt(9)
            r = para.add_run(src.get("url", ""))
            r.font.size, r.font.color.rgb = Pt(8.5), TEAL

    # Methodology
    _heading(doc, "Appendix B: Methodology and limitations", 1)
    doc.add_paragraph(
        "A snapshot agent first established the company's segments, headline financials and peer set. Four "
        "research agents then ran in parallel, one per CDD workstream (market, competition, customers, target), "
        "each given the snapshot as established fact and a strict list of topics it owns, so that each section "
        "researches new ground rather than restating the same numbers. All agents used live web search and "
        "prioritised company filings, investor materials, exchange disclosures, regulator statistics and listed "
        "peer reports. An editor pass then removed points repeated across sections "
        f"({res.get('duplicates_removed', 0)} removed in this run), and a synthesis step wrote the executive "
        "summary without adding new data.")
    _bullets(doc, [
        "Customer analysis relies on public proxies (disclosures, reviews, complaints data), not interviews or surveys.",
        "Paywalled sources such as broker research, Gartner, Statista or Capital IQ were not accessible.",
        "Market sizes from public sources often conflict; ranges are shown where they do.",
        "Figures were not reconciled to audited accounts line by line; verify key numbers before use.",
        f"Model: {res.get('model', '')}. Run date: {res.get('date', '')}.",
    ])

    # Footer page numbers
    for section in doc.sections:
        fp = section.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        run = fp.add_run(f"{res.get('company', '')} | CDD draft | Page ")
        run.font.size, run.font.color.rgb = Pt(8), GREY
        _add_page_field(fp)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _add_page_field(paragraph):
    run = paragraph.add_run()
    run.font.size = Pt(8)
    for tag, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if tag:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), tag)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)
