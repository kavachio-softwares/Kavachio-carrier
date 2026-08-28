import os
import csv
import glob


def _ensure_tessdata_prefix() -> None:
    """Point PyMuPDF at Tesseract's language data so image/scanned PDFs can be
    OCR'd. MuPDF reads TESSDATA_PREFIX at import time and raises
    "No OCR support: TESSDATA_PREFIX not set" if it's missing — so this MUST run
    before ``import fitz`` below. If the deployment already sets the var we
    leave it alone; otherwise we probe the common tessdata locations. OCR is a
    best-effort fallback, so a miss here is non-fatal (extraction still works
    for text-layer PDFs)."""
    if os.environ.get("TESSDATA_PREFIX"):
        return
    candidates = [
        "/opt/homebrew/share/tessdata",          # macOS (Homebrew, Apple silicon)
        "/usr/local/share/tessdata",             # macOS (Homebrew, Intel) / manual
        "/usr/share/tesseract-ocr/5/tessdata",   # Debian/Ubuntu tesseract 5.x
        "/usr/share/tesseract-ocr/4.00/tessdata",# Debian/Ubuntu tesseract 4.x
        "/usr/share/tessdata",                   # other Linux distros
    ]
    for path in candidates:
        if glob.glob(os.path.join(path, "*.traineddata")):
            os.environ["TESSDATA_PREFIX"] = path
            break


_ensure_tessdata_prefix()

import fitz
import docx
import openpyxl

from PyPDF2 import PdfReader
from contract_upload_services.constants import FIELD_TYPE_MAPPING


def extract_form_fields_new(pdf_path):

    pypdf2_fields = {}

    try:
        with open(pdf_path, 'rb') as file:

            reader = PdfReader(file)
            form_fields = reader.get_fields()

            if form_fields:
                for field_name, field_obj in form_fields.items():

                    field_dict = field_obj.get_object()
                    value = ""

                    if '/V' in field_dict:
                        value = field_dict['/V']

                        if hasattr(value, 'get_object'):
                            value = value.get_object()

                    pypdf2_fields[field_name] = str(value) if value else ""

    except Exception as e:
        print("PyPDF2 error:", e)

    doc = fitz.open(pdf_path)
    fields = []

    for page_num in range(len(doc)):

        page = doc.load_page(page_num)
        widgets = page.widgets()

        if not widgets:
            continue

        for widget in widgets:

            field_name = widget.field_name
            field_type = FIELD_TYPE_MAPPING.get(widget.field_type, "text")

            local_id = f"kollabRadioId_{field_name}_{page_num+1}_{widget.rect}"

            element_id = local_id if widget.field_type == 5 else None

            value = widget.field_value or ""

            if field_type == "radio" and field_name in pypdf2_fields:
                value = pypdf2_fields[field_name]

            fields.append({
                "field_name": field_name,
                "field_value": value,
                "field_type": field_type,
                "page": page_num + 1,
                "position": {
                    "x1": widget.rect.x0,
                    "y1": widget.rect.y0,
                    "x2": widget.rect.x1,
                    "y2": widget.rect.y1
                },
                "element_id": element_id
            })

    doc.close()

    return fields


def _layout_text(page) -> str:
    """Reconstruct a page's text in true visual reading order.

    PyMuPDF's default ``get_text("text")`` emits text block-by-block in storage
    order. When a value lives in its own block — e.g. a bold revision printed
    mid-line — it is torn away from the words beside it: a ``$3,000,000``
    correction rendered right after ``APD Terminal Limit = $1,`` ends up in a
    different part of the stream, so the clause the LLM reads loses the real
    number and keeps only the ``$1,`` fragment.

    Here we rebuild lines from the word geometry instead: words are grouped into
    visual lines by their vertical centre and ordered left-to-right, so
    horizontally adjacent fragments from *different* blocks are rejoined on the
    same line. Falls back to plain extraction when a page has no positioned
    words (e.g. a scanned image page). Never raises — the caller must always get
    text back."""
    try:
        words = page.get_text("words")   # (x0, y0, x1, y1, word, block, line, word_no)
    except Exception:
        words = []
    if not words:
        return page.get_text("text")

    heights = sorted(w[3] - w[1] for w in words if w[3] > w[1])
    med_h = heights[len(heights) // 2] if heights else 8.0
    tol = max(2.0, med_h * 0.6)          # same-line vertical tolerance

    # (vertical centre, left edge, text) sorted top-to-bottom then left-to-right.
    items = sorted(((w[1] + w[3]) / 2.0, w[0], w[4]) for w in words)

    lines: list[list[tuple[float, str]]] = []
    anchor = None
    for yc, x0, text in items:
        if anchor is None or yc - anchor > tol:   # start of a new visual line
            lines.append([])
            anchor = yc
        lines[-1].append((x0, text))

    out = []
    for ln in lines:
        ln.sort(key=lambda t: t[0])               # left-to-right within the line
        out.append(" ".join(t[1] for t in ln))
    return "\n".join(out)


def _page_text_with_ocr_fallback(page) -> str:
    """Extract a page's text, falling back to OCR for scanned/image pages.

    ``_layout_text`` reads the PDF's embedded text layer. A scanned page has no
    text objects, so it comes back effectively empty. In that case we render the
    page and OCR it via PyMuPDF's built-in Tesseract bridge (``get_textpage_ocr``),
    so image-only contracts still yield readable text instead of tripping the
    INSUFFICIENT_TEXT gate.

    OCR requires the Tesseract binary + language data on the host; if it's
    unavailable or fails, we degrade gracefully to whatever the text layer gave
    us. Never raises — the caller must always get a string back."""
    text = _layout_text(page)
    if len(text.strip()) >= 20:          # has a real text layer → no OCR needed
        return text
    try:
        tp = page.get_textpage_ocr(flags=0, dpi=300, full=True)
        ocr_text = page.get_text("text", textpage=tp)
        if len(ocr_text.strip()) > len(text.strip()):
            return ocr_text
    except Exception:
        pass                             # Tesseract missing/failed → keep text layer
    return text


def extract_pdf_text(pdf_path):

    doc = fitz.open(pdf_path)
    pages = []

    for page_num in range(len(doc)):

        page = doc.load_page(page_num)

        pages.append({
            "page": page_num + 1,
            # Layout-aware reading order so mid-line revision fragments in their
            # own block (a bold "$3,000,000" beside "$1,") stay on their line.
            # Falls back to OCR when the page has no text layer (scanned image).
            "text": _page_text_with_ocr_fallback(page),
            "blocks": page.get_text("blocks"),
            # Structured tables (grid of cells) detected on the page. Plain text
            # extraction flattens a table into a scrambled run where columns blur
            # together; capturing the real grid lets us feed the LLM clean
            # rows/columns so per-row association (e.g. reinsurer ↔ its limit) is
            # exact. Best-effort — never fails page extraction.
            "tables": _extract_page_tables(page),
        })

    doc.close()

    return pages


def _extract_page_tables(page) -> list[list[list[str]]]:
    """Return each detected table on the page as a list of rows (each a list of
    cell strings). Empty rows/cols are trimmed. Robust to older PyMuPDF and to
    pages with no tables."""
    out: list[list[list[str]]] = []
    try:
        finder = page.find_tables()
    except Exception:
        return out
    for t in getattr(finder, "tables", []):
        try:
            raw = t.extract()
        except Exception:
            continue
        rows = [[(c or "").replace("\n", " ").strip() for c in row] for row in raw]
        rows = [r for r in rows if any(cell for cell in r)]   # drop blank rows
        if len(rows) >= 2:      # a header + at least one data row
            out.append(rows)
    return out


def clean_text(text):

    return "\n".join(
        line.strip()
        for line in text.splitlines()
        if line.strip() and len(line.strip()) >= 2
    )


def get_page_info(pdf_path):

    doc = fitz.open(pdf_path)

    dimensions = []
    offsets = []

    for page_num in range(len(doc)):

        page = doc.load_page(page_num)
        rect = page.rect

        dimensions.append({
            "width": rect.width,
            "height": rect.height
        })

        offsets.append({
            "xMin": rect.x0,
            "yMin": rect.y0,
            "xMax": rect.x1,
            "yMax": rect.y1
        })

    doc.close()

    return dimensions, offsets


def extract_excel_data(file_path):

    wb = openpyxl.load_workbook(file_path, data_only=True)

    return [
        {
            "sheet_name": s.title,
            "tables": [list(r) for r in s.iter_rows(values_only=True)]
        }
        for s in wb.worksheets
    ]


def extract_csv_data(file_path):

    with open(file_path, newline='', encoding='utf-8') as f:
        return [{
            "sheet_name": "CSV",
            "tables": list(csv.reader(f))
        }]


def extract_docx_data(file_path):

    document = docx.Document(file_path)

    text = [
        p.text for p in document.paragraphs
        if p.text.strip()
    ]

    tables = [
        [[c.text for c in r.cells] for r in t.rows]
        for t in document.tables
    ]

    return [{
        "text": "\n".join(text),
        "tables": tables
    }]


def convert_doc_to_docx(path):

    os.system(f"soffice --headless --convert-to docx '{path}'")

    return path + "x"


def _docx_data_to_pages(data):
    """Normalize extract_docx_data() output ([{text, tables}]) into the
    page-shaped list the extraction pipeline (build_llm_context /
    _assign_clause_pages) consumes. `tables` is already a list-of-tables,
    which matches the per-page table shape, so it passes through unchanged."""
    return [
        {
            "page": i,
            "text": d.get("text", "") or "",
            "tables": d.get("tables", []) or [],
        }
        for i, d in enumerate(data, start=1)
    ]


def _sheet_data_to_pages(data):
    """Normalize spreadsheet output ([{sheet_name, tables}]) into pages. Each
    sheet's `tables` is a SINGLE grid (list of rows), so it's wrapped in a list
    to match the per-page list-of-tables shape; each sheet becomes one page."""
    pages = []
    for i, sheet in enumerate(data, start=1):
        grid = sheet.get("tables") or []
        pages.append({
            "page": i,
            "text": "",
            "tables": [grid] if grid else [],
            "sheet_name": sheet.get("sheet_name"),
        })
    return pages


def extract_document_data(file_path):

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":

        dimensions, offsets = get_page_info(file_path)

        return {
            "type": "pdf",
            "formData": extract_form_fields_new(file_path),
            "pages": extract_pdf_text(file_path),
            "dimension": dimensions,
            "offset": offsets
        }

    elif ext in [".xlsx", ".xls"]:
        data = extract_excel_data(file_path)
        return {
            "type": "excel",
            "data": data,
            "pages": _sheet_data_to_pages(data),
        }

    elif ext == ".csv":
        data = extract_csv_data(file_path)
        return {
            "type": "csv",
            "data": data,
            "pages": _sheet_data_to_pages(data),
        }

    elif ext == ".docx":
        data = extract_docx_data(file_path)
        return {
            "type": "docx",
            "data": data,
            "pages": _docx_data_to_pages(data),
        }

    elif ext == ".doc":
        data = extract_docx_data(convert_doc_to_docx(file_path))
        return {
            "type": "doc",
            "data": data,
            "pages": _docx_data_to_pages(data),
        }

    else:
        raise ValueError("Unsupported file type")
