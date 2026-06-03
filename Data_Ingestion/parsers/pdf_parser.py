"""
PDF Parser — PyMuPDF (fitz)
============================
Extracts from each page:
  - Text blocks (assembled at block level, not span level)
  - Images  (deduplicated by xref; tiny icons/bullets filtered out)
  - Tables  (PyMuPDF find_tables; text inside table rects excluded from text blocks)

Production considerations:
  - One TextElement per logical block (not per span/run) — prevents hundreds of fragments
  - Images deduplicated across pages by xref ID
  - Images below MIN_IMAGE_DIM × MIN_IMAGE_DIM or MIN_IMAGE_BYTES are skipped
  - Text that overlaps a detected table rect is excluded to avoid duplication
  - Per-element try/except so a bad page never aborts the whole document
"""

from __future__ import annotations
import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import fitz  # pip install pymupdf

from models.meta_schema import (
    ParsedDocument, TextElement, ImageElement, TableElement, SourceLocation,
)
from parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)

# Images smaller than these thresholds are decorations/icons — skip them
MIN_IMAGE_DIM   = 60    # pixels (width AND height must exceed this)
MIN_IMAGE_BYTES = 4096  # 4 KB


class PDFParser(BaseParser):

    def parse(self, file_path: str | Path) -> ParsedDocument:
        file_path = Path(file_path)
        doc_meta  = self._base_doc(file_path, "pdf")

        try:
            pdf = fitz.open(str(file_path))
        except Exception as e:
            raise RuntimeError(f"Cannot open PDF '{file_path.name}': {e}") from e

        doc_meta.summary.page_count = pdf.page_count
        seen_xrefs: set[int] = set()   # dedup images that repeat across pages

        for page_num, page in enumerate(pdf, start=1):
            order = 0

            # ── 1. Find tables first — we need their bounding boxes to exclude
            #       overlapping text blocks from being double-counted ────────────
            page_tables: list[tuple[fitz.Rect, list, list]] = []
            try:
                found  = page.find_tables()
                for tbl in found.tables:
                    try:
                        raw_data = tbl.extract()   # list[list[str|None]]
                        if not raw_data or len(raw_data) < 2:
                            continue
                        headers = [str(c or "").strip() for c in raw_data[0]]
                        rows    = [[str(c or "").strip() for c in r] for r in raw_data[1:]]
                        if any(h for h in headers):   # at least one non-empty header
                            page_tables.append((fitz.Rect(tbl.bbox), headers, rows))
                    except Exception as e:
                        logger.warning("Table data extraction failed (page %d): %s", page_num, e)
            except Exception as e:
                logger.warning("Table detection failed (page %d): %s", page_num, e)

            table_rects = [t[0] for t in page_tables]

            # ── 2. Text blocks ────────────────────────────────────────────────
            try:
                page_dict = page.get_text(
                    "dict",
                    flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES,
                )
                for block in page_dict.get("blocks", []):
                    if block.get("type") != 0:   # 0 = text block
                        continue

                    # Skip blocks whose centre point falls inside any table rect
                    bx0, by0, bx1, by1 = block["bbox"]
                    cx = (bx0 + bx1) / 2
                    cy = (by0 + by1) / 2
                    if any(tr.x0 <= cx <= tr.x1 and tr.y0 <= cy <= tr.y1
                           for tr in table_rects):
                        continue

                    # Assemble all spans across all lines into one block text.
                    # Track dominant font size for heading detection.
                    lines_text:  list[str]   = []
                    max_size:    float       = 0.0
                    dominant_sz: float       = 11.0

                    for line in block.get("lines", []):
                        span_parts: list[str] = []
                        for span in line.get("spans", []):
                            txt = span.get("text", "").strip()
                            if txt:
                                span_parts.append(txt)
                            sz = span.get("size", 11.0)
                            if sz > max_size:
                                max_size    = sz
                                dominant_sz = sz
                        if span_parts:
                            lines_text.append(" ".join(span_parts))

                    raw = self._clean(" ".join(lines_text))
                    if not raw or len(raw) < 3:
                        continue

                    h_level = self._is_heading_size(dominant_sz)

                    elem = TextElement.build(
                        idx           = self._next_text_idx(),
                        content       = raw,
                        loc           = SourceLocation(page=page_num, order=order),
                        heading_level = h_level,
                    )
                    doc_meta.text_elements.append(elem)
                    order += 1

            except Exception as e:
                logger.warning("Text extraction failed (page %d): %s", page_num, e)

            # ── 3. Images ─────────────────────────────────────────────────────
            try:
                for img_info in page.get_images(full=True):
                    xref = img_info[0]
                    if xref in seen_xrefs:
                        continue   # same image appeared on an earlier page

                    try:
                        base_img = pdf.extract_image(xref)
                        if not base_img:
                            continue

                        img_bytes = base_img["image"]
                        w = base_img.get("width",  0)
                        h = base_img.get("height", 0)

                        # Filter decorative/tiny images
                        if (len(img_bytes) < MIN_IMAGE_BYTES
                                or w < MIN_IMAGE_DIM
                                or h < MIN_IMAGE_DIM):
                            continue

                        seen_xrefs.add(xref)

                        ext     = base_img.get("ext", "png").lower()
                        b64     = base64.b64encode(img_bytes).decode("utf-8")
                        caption = _find_caption(page, img_info)

                        elem = ImageElement.build(
                            idx         = self._next_img_idx(),
                            loc         = SourceLocation(page=page_num, order=order),
                            base64_data = b64,
                            width       = w,
                            height      = h,
                            format      = ext,
                            size_bytes  = len(img_bytes),
                            caption     = caption,
                        )
                        doc_meta.image_elements.append(elem)
                        order += 1

                    except Exception as e:
                        logger.warning(
                            "Image extraction failed (xref=%d, page=%d): %s",
                            xref, page_num, e,
                        )

            except Exception as e:
                logger.warning("Image enumeration failed (page %d): %s", page_num, e)

            # ── 4. Emit table elements ─────────────────────────────────────────
            for tbl_rect, headers, rows in page_tables:
                try:
                    elem = TableElement.build(
                        idx     = self._next_tbl_idx(),
                        loc     = SourceLocation(page=page_num, order=order),
                        headers = headers,
                        rows    = rows,
                    )
                    doc_meta.table_elements.append(elem)
                    order += 1
                except Exception as e:
                    logger.warning("TableElement build failed (page %d): %s", page_num, e)

        pdf.close()
        doc_meta.parsed_at = datetime.utcnow()
        doc_meta.rebuild_summary()
        return doc_meta


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_caption(page: fitz.Page, img_info: tuple) -> Optional[str]:
    """
    Heuristic: look for a short text block just below or just above the image.
    Returns the first candidate text under 200 characters, or None.
    """
    try:
        bbox = page.get_image_bbox(img_info)
        if not bbox:
            return None

        # Search 40 pt below, then 40 pt above if nothing found
        for search_rect in (
            fitz.Rect(bbox.x0, bbox.y1,      bbox.x1, bbox.y1 + 40),
            fitz.Rect(bbox.x0, bbox.y0 - 40, bbox.x1, bbox.y0),
        ):
            text = page.get_textbox(search_rect).strip()
            if text and len(text) < 200:
                return text

    except Exception:
        pass
    return None
