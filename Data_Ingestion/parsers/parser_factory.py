"""
Parser Factory
==============
Routes an uploaded file to the correct parser by extension.
Returns a ParsedDocument ready to be saved to GCS.
"""

from pathlib import Path
from models.meta_schema import ParsedDocument
from parsers.pdf_parser   import PDFParser
from parsers.docx_parser  import DOCXParser
from parsers.pptx_parser  import PPTXParser
from parsers.excel_parser import ExcelParser

# Extension → parser class
_PARSERS = {
    ".pdf":  PDFParser,
    ".docx": DOCXParser,
    ".doc":  DOCXParser,    # older Word — python-docx handles most .doc files
    ".pptx": PPTXParser,
    ".ppt":  PPTXParser,
    ".xlsx": ExcelParser,
    ".xls":  ExcelParser,
}

SUPPORTED_EXTENSIONS = list(_PARSERS.keys())


def parse_document(file_path: str | Path) -> ParsedDocument:
    """
    Parse any supported document. Auto-selects the right parser.

    Args:
        file_path: Path to the uploaded file on disk.

    Returns:
        ParsedDocument with all extracted text, images, tables and metadata.

    Raises:
        ValueError: If the file extension is not supported.
        RuntimeError: If parsing fails for any reason.
    """
    # Canonicalize the path (resolves '..' and symlinks) before any file access —
    # path-traversal hardening (CWE-22).
    path = Path(file_path).resolve()
    ext  = path.suffix.lower()

    if ext not in _PARSERS:
        raise ValueError(
            f"Unsupported file type '{ext}'. "
            f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
        )

    if not path.is_file():
        raise ValueError(f"File not found or not a regular file: {path}")

    parser = _PARSERS[ext]()

    try:
        return parser.parse(path)
    except Exception as e:
        raise RuntimeError(f"Parsing '{path.name}' failed: {type(e).__name__}: {e}") from e
