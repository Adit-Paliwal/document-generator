"""
Databricks Unity Catalog Volume Storage (production — Azure Databricks)
=======================================================================
Activated when  DATABRICKS_MODE=true  in environment.
Replaces GCSStorageService with no changes to callers.

Requirements:
  pip install databricks-sdk>=0.72.0

Environment variables (auto-set inside Databricks Apps, or set manually):
  DATABRICKS_HOST          — https://<workspace>.azuredatabricks.net
  DATABRICKS_TOKEN         — personal access token (PAT)
  DATABRICKS_VOLUME_PATH   — e.g. /Volumes/intellidraft/files

Volume layout (mirrors GCSStorageService):
  /Volumes/.../documents/{doc_id}/source/{filename}
  /Volumes/.../documents/{doc_id}/images/{element_id}.{ext}
  /Volumes/.../documents/{doc_id}/tables/{element_id}.csv
  /Volumes/.../documents/{doc_id}/meta.json
  /Volumes/.../cosmos/{doc_id}.json
  /Volumes/.../outputs/{job_id}/{filename}   ← exported DOCX / PDF
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from pathlib import Path

from models.meta_schema import ParsedDocument

logger = logging.getLogger(__name__)

_VOLUME_PATH = os.environ.get("DATABRICKS_VOLUME_PATH", "/Volumes/intellidraft/files")


class DatabricksVolumeStorageService:
    """
    Unity Catalog Volume storage — same public API as GCSStorageService.
    WorkspaceClient() auto-authenticates inside Databricks Apps using the
    app's service principal. For local dev, set DATABRICKS_HOST + DATABRICKS_TOKEN.
    """

    def __init__(self, volume_path: str | None = None):
        from databricks.sdk import WorkspaceClient

        self._client = WorkspaceClient()
        self._base   = (volume_path or _VOLUME_PATH).rstrip("/")
        logger.info("[DBX-VOL] Volume root: %s", self._base)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _full(self, *rel: str) -> str:
        return self._base + "/" + "/".join(rel)

    def _upload(self, *rel: str, data: bytes) -> str:
        path = self._full(*rel)
        self._client.files.upload(path, io.BytesIO(data), overwrite=True)
        logger.debug("[DBX-VOL] Uploaded: %s (%d bytes)", path, len(data))
        return path

    def _download(self, *rel: str) -> bytes:
        path = self._full(*rel)
        resp = self._client.files.download(path)
        with resp.contents as f:
            return f.read()

    # ── Public API ────────────────────────────────────────────────────────────

    def upload_source_file(self, document_id: str, file_path: Path) -> str:
        return self._upload(
            "documents", document_id, "source", file_path.name,
            data=file_path.read_bytes(),
        )

    def save_images(self, parsed_doc: ParsedDocument) -> ParsedDocument:
        for elem in parsed_doc.image_elements:
            if not elem.base64_data:
                continue
            raw = base64.b64decode(elem.base64_data)
            url = self._upload(
                "documents", parsed_doc.document_id,
                "images", f"{elem.element_id}.{elem.format}",
                data=raw,
            )
            elem.blob_url    = url
            elem.base64_data = None
        return parsed_doc

    def save_tables(self, parsed_doc: ParsedDocument) -> ParsedDocument:
        for elem in parsed_doc.table_elements:
            if not elem.csv_data:
                continue
            url = self._upload(
                "documents", parsed_doc.document_id,
                "tables", f"{elem.element_id}.csv",
                data=elem.csv_data.encode("utf-8"),
            )
            elem.blob_url = url
        return parsed_doc

    def save_meta_json(self, parsed_doc: ParsedDocument) -> str:
        return self._upload(
            "documents", parsed_doc.document_id, "meta.json",
            data=parsed_doc.model_dump_json(indent=2).encode("utf-8"),
        )

    def save_to_cosmos(self, parsed_doc: ParsedDocument) -> None:
        from storage.gcs_storage import _build_index_record
        record = _build_index_record(parsed_doc)
        self._upload(
            "cosmos", f"{parsed_doc.document_id}.json",
            data=json.dumps(record, indent=2, default=str).encode("utf-8"),
        )

    def get_meta_json(self, document_id: str) -> dict:
        data = self._download("documents", document_id, "meta.json")
        return json.loads(data)

    def get_document_index(self, document_id: str) -> dict:
        data = self._download("cosmos", f"{document_id}.json")
        return json.loads(data)

    def persist_all(self, parsed_doc: ParsedDocument, source_file: Path) -> ParsedDocument:
        from storage.gcs_storage import _analyze_images   # local import — avoids circular
        parsed_doc.blob_base_path = f"documents/{parsed_doc.document_id}/"
        self.upload_source_file(parsed_doc.document_id, source_file)
        parsed_doc = _analyze_images(parsed_doc)
        parsed_doc.rebuild_summary()
        self.save_images(parsed_doc)
        self.save_tables(parsed_doc)
        self.save_meta_json(parsed_doc)
        self.save_to_cosmos(parsed_doc)
        logger.info("[DBX-VOL] All files saved under: %s", parsed_doc.blob_base_path)
        return parsed_doc
