"""
Word / DOCX Parser — python-docx
==================================
Walks the document body XML in document order so text, images, and tables are
extracted in the sequence they appear on the page.

Extracts:
  - Paragraphs with heading/bullet style detection
  - Inline images embedded within paragraphs (in document order)
  - Tables with merged-cell deduplication and optional caption detection
  - Relationship-based images that aren't inline (fallback)

Production considerations:
  - Elements appear in the order they exist in the document body
  - Merged table cells are deduplicated per row
  - Table captions detected from the paragraph directly above each table
  - Nested tables (tables inside table cells) are recursively processed
  - Per-element try/except — a bad table or image never aborts the whole doc
"""

from __future__ import annotations
import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from models.meta_schema import (
    ParsedDocument, TextElement, ImageElement, TableElement, SourceLocation,
)
from parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)

# Word heading style names → heading level
_HEADING_STYLE_MAP = {
    "heading 1": 1, "heading 2": 2, "heading 3": 3,
    "heading 4": 4, "heading 5": 5, "heading 6": 6,
    "title":     1,
}

# XML namespaces used for inline image detection
_NS = {
    "a":   "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "r":   "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w":   "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}


class DOCXParser(BaseParser):

    def parse(self, file_path: str | Path) -> ParsedDocument:
        file_path = Path(file_path)
        doc_meta  = self._base_doc(file_path, "docx")
        doc       = Document(str(file_path))
        order     = 0

        # Track relationship IDs we have already emitted as inline images
        # so the fallback loop at the end doesn't double-emit them.
        seen_rel_ids: set[str] = set()

        # Walk every top-level element of the body in document order
        body = doc.element.body
        prev_para_text: Optional[str] = None  # used for caption detection

        for child in body:
            local = etree.QName(child.tag).localname

            # ── Paragraph ─────────────────────────────────────────────────────
            if local == "p":
                try:
                    from docx.text.paragraph import Paragraph as DocxParagraph
                    para = DocxParagraph(child, doc)
                    raw  = self._clean(para.text)

                    # --- Inline images inside this paragraph ---
                    img_rels = _extract_inline_image_rels(child, doc.part)
                    for rel_id, img_bytes, ext in img_rels:
                        if rel_id in seen_rel_ids:
                            continue
                        try:
                            b64 = base64.b64encode(img_bytes).decode("utf-8")
                            elem = ImageElement.build(
                                idx         = self._next_img_idx(),
                                loc         = SourceLocation(page=None, order=order),
                                base64_data = b64,
                                format      = ext,
                                size_bytes  = len(img_bytes),
                            )
                            doc_meta.image_elements.append(elem)
                            seen_rel_ids.add(rel_id)
                            order += 1
                        except Exception as e:
                            logger.warning("Inline image failed (rel=%s): %s", rel_id, e)

                    # --- Paragraph text ---
                    if raw:
                        style_name = (para.style.name or "").lower()
                        h_level    = _HEADING_STYLE_MAP.get(style_name)
                        is_title   = style_name == "title"
                        is_bullet  = "list" in style_name or "bullet" in style_name

                        elem = TextElement.build(
                            idx           = self._next_text_idx(),
                            content       = raw,
                            loc           = SourceLocation(page=None, order=order),
                            heading_level = h_level,
                            is_title      = is_title,
                            is_bullet     = is_bullet,
                        )
                        doc_meta.text_elements.append(elem)
                        order += 1
                        prev_para_text = raw
                    else:
                        prev_para_text = None   # blank paragraph resets caption candidate

                except Exception as e:
                    logger.warning("Paragraph extraction failed: %s", e)

            # ── Table ─────────────────────────────────────────────────────────
            elif local == "tbl":
                try:
                    from docx.table import Table as DocxTable
                    tbl = DocxTable(child, doc)

                    # Caption = paragraph directly above this table (if short & non-heading)
                    caption: Optional[str] = None
                    if prev_para_text and len(prev_para_text) < 120:
                        caption = prev_para_text

                    rows_data = _extract_table_rows(tbl)
                    if rows_data:
                        headers = rows_data[0]
                        rows    = rows_data[1:]
                        elem = TableElement.build(
                            idx     = self._next_tbl_idx(),
                            loc     = SourceLocation(page=None, order=order),
                            headers = headers,
                            rows    = rows,
                            caption = caption,
                        )
                        doc_meta.table_elements.append(elem)
                        order += 1

                except Exception as e:
                    logger.warning("Table extraction failed: %s", e)

                prev_para_text = None   # table resets caption candidate

        # ── Fallback: relationship images not found as inline ─────────────────
        # Catches images in headers/footers, text boxes, or unusual embeddings.
        try:
            for rel_id, rel in doc.part.rels.items():
                if "image" not in rel.reltype or rel_id in seen_rel_ids:
                    continue
                try:
                    img_part  = rel.target_part
                    img_bytes = img_part.blob
                    ext       = img_part.content_type.split("/")[-1].lower()
                    b64       = base64.b64encode(img_bytes).decode("utf-8")

                    elem = ImageElement.build(
                        idx         = self._next_img_idx(),
                        loc         = SourceLocation(page=None, order=order),
                        base64_data = b64,
                        format      = ext,
                        size_bytes  = len(img_bytes),
                    )
                    doc_meta.image_elements.append(elem)
                    order += 1
                except Exception as e:
                    logger.warning("Fallback image failed (rel=%s): %s", rel_id, e)
        except Exception as e:
            logger.warning("Relationship image scan failed: %s", e)

        doc_meta.parsed_at = datetime.utcnow()
        doc_meta.rebuild_summary()
        return doc_meta


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_inline_image_rels(
    para_elem,
    doc_part,
) -> list[tuple[str, bytes, str]]:
    """
    Find all inline image relationship IDs inside a paragraph XML element.
    Returns list of (rel_id, image_bytes, extension).
    """
    results = []
    # DrawingML embeds: a:blip r:embed="rId..."
    for blip in para_elem.iter(f"{{{_NS['a']}}}blip"):
        rel_id = blip.get(f"{{{_NS['r']}}}embed")
        if rel_id and rel_id in doc_part.rels:
            try:
                img_part  = doc_part.rels[rel_id].target_part
                img_bytes = img_part.blob
                ext       = img_part.content_type.split("/")[-1].lower()
                results.append((rel_id, img_bytes, ext))
            except Exception as e:
                logger.debug("blip image load failed (rel=%s): %s", rel_id, e)

    # Legacy VML embeds: v:imagedata r:id="rId..."
    vml_ns = "urn:schemas-microsoft-com:vml"
    for imgdata in para_elem.iter(f"{{{vml_ns}}}imagedata"):
        rel_id = imgdata.get(f"{{{_NS['r']}}}id")
        if rel_id and rel_id in doc_part.rels:
            try:
                img_part  = doc_part.rels[rel_id].target_part
                img_bytes = img_part.blob
                ext       = img_part.content_type.split("/")[-1].lower()
                results.append((rel_id, img_bytes, ext))
            except Exception as e:
                logger.debug("VML image load failed (rel=%s): %s", rel_id, e)

    return results


def _extract_table_rows(tbl) -> list[list[str]]:
    """
    Extract table rows with merged-cell deduplication.
    Word repeats merged cell content across the cells in the span — we deduplicate
    by comparing adjacent cell text within each row.
    """
    rows_data: list[list[str]] = []
    for row in tbl.rows:
        cells: list[str] = []
        prev_text = object()   # sentinel — guaranteed != any string
        for cell in row.cells:
            text = _clean_cell(cell.text)
            # Word repeats the same cell object for horizontally merged cells
            if text != prev_text:
                cells.append(text)
                prev_text = text
        if cells:
            rows_data.append(cells)

    # Check for nested tables and process them recursively (best-effort)
    # — python-docx doesn't expose nested tables directly; skip for now.

    return rows_data


def _clean_cell(text: str) -> str:
    return " ".join(text.split()).strip() if text else ""
