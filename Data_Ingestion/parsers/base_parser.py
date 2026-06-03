"""
Abstract base parser — all document parsers inherit from this.
"""

from __future__ import annotations
import abc
import logging
from datetime import datetime
from pathlib import Path

from models.meta_schema import ParsedDocument

logger = logging.getLogger(__name__)


class BaseParser(abc.ABC):
    """
    Every parser must implement `parse()` and return a ParsedDocument.
    Counters (text_idx, img_idx, tbl_idx) are passed to element builders
    to guarantee globally unique IDs across the whole document.
    """

    def __init__(self):
        self.text_idx: int = 0
        self.img_idx:  int = 0
        self.tbl_idx:  int = 0

    # ── Must implement ────────────────────────────────────────────────────────

    @abc.abstractmethod
    def parse(self, file_path: str | Path) -> ParsedDocument:
        """Parse the file and return a fully populated ParsedDocument."""

    # ── Helpers shared across parsers ─────────────────────────────────────────

    def _next_text_idx(self) -> int:
        self.text_idx += 1
        return self.text_idx

    def _next_img_idx(self) -> int:
        self.img_idx += 1
        return self.img_idx

    def _next_tbl_idx(self) -> int:
        self.tbl_idx += 1
        return self.tbl_idx

    def _base_doc(self, file_path: Path, file_type: str) -> ParsedDocument:
        return ParsedDocument(
            source_filename  = file_path.name,
            file_type        = file_type,
            upload_timestamp = datetime.utcnow(),
        )

    @staticmethod
    def _clean(text: str) -> str:
        """Strip whitespace and normalise line breaks."""
        return " ".join(text.split()).strip() if text else ""

    @staticmethod
    def _is_heading_size(size: float, body_size: float = 11.0) -> int | None:
        """Heuristic: map font size to heading level (1–3) or None for body text."""
        if size >= body_size * 2.0:
            return 1
        if size >= body_size * 1.5:
            return 2
        if size >= body_size * 1.2:
            return 3
        return None
