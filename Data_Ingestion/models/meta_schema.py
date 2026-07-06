"""
Meta Source Schema
==================
The ParsedDocument is the single source of truth for all extracted content.
Every text block, image, and table gets a unique element_id and ref string
so LLM prompts and document templates can cite specific elements by reference.

Reference format:  text#txt_001  |  image#img_001  |  table#tbl_001
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, Field
import uuid


# ─────────────────────────────────────────────────────────────────────────────
# Element Models
# ─────────────────────────────────────────────────────────────────────────────

class SourceLocation(BaseModel):
    """Where in the source document this element came from."""
    page:        Optional[int]   = None   # PDF / Word / PPT page number
    slide:       Optional[int]   = None   # PPT slide number
    sheet_name:  Optional[str]   = None   # Excel sheet name
    order:       int             = 0      # Sequential order within the page/slide


class TextElement(BaseModel):
    element_id:    str
    ref:           str                     # "text#txt_001" — used in LLM prompts
    type:          Literal["text"] = "text"
    source_location: SourceLocation
    heading_level: Optional[int]  = None   # None=body, 1=H1 … 6=H6
    is_title:      bool           = False
    is_bullet:     bool           = False
    content:       str
    word_count:    int            = 0

    @classmethod
    def build(cls, idx: int, content: str, loc: SourceLocation, **kw) -> "TextElement":
        eid = f"txt_{idx:04d}"
        return cls(
            element_id      = eid,
            ref             = f"text#{eid}",
            source_location = loc,
            content         = content.strip(),
            word_count      = len(content.split()),
            **kw,
        )


class ImageElement(BaseModel):
    element_id:    str
    ref:           str                     # "image#img_001"
    type:          Literal["image"] = "image"
    source_location: SourceLocation
    caption:       Optional[str]   = None
    # Storage references — at least one will be populated after persist
    blob_url:      Optional[str]   = None  # GCS URL (production, gs://...)
    local_path:    Optional[str]   = None  # Local filesystem path (dev/staging)
    base64_data:   Optional[str]   = None  # Inline base64 (cleared after save)
    width:         Optional[int]   = None
    height:        Optional[int]   = None
    format:        str             = "png"
    size_bytes:    Optional[int]   = None
    # Vision AI analysis — populated by vision_analyzer during persist_all
    ai_description: Optional[str]  = None  # LLM-generated description
    image_type:     Optional[str]  = None  # workflow_flowchart | architecture_diagram |
                                           # chart_graph | table_screenshot | ui_screenshot |
                                           # photo | logo_icon | other
    key_elements:   List[str]      = Field(default_factory=list)  # identified components

    @classmethod
    def build(cls, idx: int, loc: SourceLocation, **kw) -> "ImageElement":
        eid = f"img_{idx:04d}"
        return cls(element_id=eid, ref=f"image#{eid}", source_location=loc, **kw)


class TableElement(BaseModel):
    element_id:    str
    ref:           str                     # "table#tbl_001"
    type:          Literal["table"] = "table"
    source_location: SourceLocation
    caption:       Optional[str]   = None
    headers:       List[str]       = Field(default_factory=list)
    rows:          List[List[str]] = Field(default_factory=list)
    row_count:     int             = 0
    col_count:     int             = 0
    markdown:      str             = ""   # Rendered as Markdown table
    csv_data:      str             = ""   # Comma-separated (for download / LLM)
    blob_url:      Optional[str]   = None  # GCS URL of .csv file (gs://...)

    @classmethod
    def build(cls, idx: int, loc: SourceLocation,
              headers: list, rows: list, **kw) -> "TableElement":
        eid = f"tbl_{idx:04d}"
        markdown = _to_markdown_table(headers, rows)
        csv_data = _to_csv(headers, rows)
        return cls(
            element_id      = eid,
            ref             = f"table#{eid}",
            source_location = loc,
            headers         = headers,
            rows            = rows,
            row_count       = len(rows),
            col_count       = len(headers),
            markdown        = markdown,
            csv_data        = csv_data,
            **kw,
        )


# ─────────────────────────────────────────────────────────────────────────────
# User Input Model  (what the user fills in via the UI)
# ─────────────────────────────────────────────────────────────────────────────

class UserInputData(BaseModel):
    """
    All the fields the user provides in addition to the uploaded document.
    These are passed to the LLM as context when generating the output document.
    """
    project_name:           str
    document_type:          str    # BRD | Scope Doc | Proposal | SOW | Tech Spec | Custom
    output_format:          str    # Word | PDF | Markdown
    template_id:            Optional[str]   = None   # references a stored template

    # Context fields
    stakeholders:           Optional[str]   = None   # "CTO, PM, Dev Team"
    project_description:    Optional[str]   = None
    target_audience:        Optional[str]   = None
    business_problem:       Optional[str]   = None
    expected_outcome:       Optional[str]   = None

    # Document generation control
    sections_to_include:    Optional[List[str]] = None   # override template sections
    generation_mode:        str = "complete"  # "complete" | "section_by_section"
    additional_instructions: Optional[str]  = None   # free-text LLM instruction
    language:               str = "English"


# ─────────────────────────────────────────────────────────────────────────────
# Top-level Parsed Document (the meta source)
# ─────────────────────────────────────────────────────────────────────────────

class DocumentSummary(BaseModel):
    total_text_elements:  int = 0
    total_images:         int = 0
    total_tables:         int = 0
    estimated_words:      int = 0
    page_count:           Optional[int] = None
    slide_count:          Optional[int] = None
    sheet_names:          List[str]     = Field(default_factory=list)
    # Vision analysis stats
    has_workflows:        bool = False
    has_architecture:     bool = False
    has_charts:           bool = False
    images_analyzed:      int  = 0


class ParsedDocument(BaseModel):
    """
    The complete meta source for a parsed document.
    Saved as a JSON file to GCS and indexed as cosmos/{document_id}.json.

    Reference guide for LLM prompts:
      - To cite a text block:  {ref: text#txt_0001}
      - To embed an image:     {ref: image#img_0001}
      - To include a table:    {ref: table#tbl_0001}
    """
    document_id:      str           = Field(default_factory=lambda: str(uuid.uuid4()))
    source_filename:  str
    file_type:        str           # pdf | docx | pptx | xlsx
    upload_timestamp: datetime      = Field(default_factory=datetime.utcnow)
    parsed_at:        Optional[datetime] = None
    status:           str           = "ready"  # parsing | ready | error

    # Storage path — set after upload to GCS
    blob_base_path:   Optional[str] = None   # e.g. "documents/{document_id}/"

    # Extracted elements
    text_elements:    List[TextElement]   = Field(default_factory=list)
    image_elements:   List[ImageElement]  = Field(default_factory=list)
    table_elements:   List[TableElement]  = Field(default_factory=list)

    # User-provided context
    user_inputs:      Optional[UserInputData] = None

    # Quick statistics
    summary:          DocumentSummary = Field(default_factory=DocumentSummary)

    def rebuild_summary(self):
        analyzed = sum(1 for e in self.image_elements if e.ai_description)
        types    = {e.image_type for e in self.image_elements if e.image_type}
        self.summary = DocumentSummary(
            total_text_elements = len(self.text_elements),
            total_images        = len(self.image_elements),
            total_tables        = len(self.table_elements),
            estimated_words     = sum(e.word_count for e in self.text_elements),
            page_count          = self.summary.page_count,
            slide_count         = self.summary.slide_count,
            sheet_names         = self.summary.sheet_names,
            has_workflows       = "workflow_flowchart" in types,
            has_architecture    = "architecture_diagram" in types,
            has_charts          = "chart_graph" in types,
            images_analyzed     = analyzed,
        )

    def all_elements_by_page(self) -> list:
        """Return all elements sorted by page then order — useful for LLM context building."""
        all_elems = (
            [(e.source_location.page or 0, e.source_location.order, e)
             for e in self.text_elements]
            + [(e.source_location.page or 0, e.source_location.order, e)
               for e in self.image_elements]
            + [(e.source_location.page or 0, e.source_location.order, e)
               for e in self.table_elements]
        )
        return [e for _, _, e in sorted(all_elems)]

    def to_llm_context(self, max_chars: int = 80_000) -> str:
        """
        Build a compact text representation for the LLM.
        Uses ai_description for images when available (richer than caption alone).
        Stays within max_chars to avoid exceeding context limits.
        """
        parts = []
        for elem in self.all_elements_by_page():
            if isinstance(elem, TextElement):
                prefix = f"{'#' * elem.heading_level} " if elem.heading_level else ""
                parts.append(f"{prefix}{elem.content}  <!-- {elem.ref} -->")

            elif isinstance(elem, ImageElement):
                # Prefer AI description over raw caption
                if elem.ai_description:
                    img_type = f" [{elem.image_type}]" if elem.image_type else ""
                    desc = elem.ai_description
                    if elem.key_elements:
                        desc += f" Key components: {', '.join(elem.key_elements)}."
                    parts.append(
                        f"[IMAGE{img_type}: {desc}]  <!-- {elem.ref} -->"
                    )
                else:
                    cap = elem.caption or "image"
                    parts.append(f"[IMAGE: {cap}]  <!-- {elem.ref} -->")

            elif isinstance(elem, TableElement):
                cap = f"\n**{elem.caption}**\n" if elem.caption else ""
                parts.append(f"{cap}{elem.markdown}  <!-- {elem.ref} -->")

        text = "\n\n".join(parts)
        return text[:max_chars]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_markdown_table(headers: list, rows: list) -> str:
    if not headers:
        return ""
    sep  = "| " + " | ".join(["---"] * len(headers)) + " |"
    head = "| " + " | ".join(str(h) for h in headers) + " |"
    body = "\n".join(
        "| " + " | ".join(str(c) for c in row) + " |"
        for row in rows
    )
    return f"{head}\n{sep}\n{body}"


def _to_csv(headers: list, rows: list) -> str:
    import csv, io
    buf = io.StringIO()
    w   = csv.writer(buf)
    if headers:
        w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue()
