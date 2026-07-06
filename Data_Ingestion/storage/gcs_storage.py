"""
Storage Layer
=============
Supports two modes controlled by LOCAL_MODE in .env:

  LOCAL_MODE=true  → LocalStorageService  (files saved to ./local_storage/)
  LOCAL_MODE=false → GCSStorageService    (Google Cloud Storage bucket)

Both implementations expose an identical public API.
Use get_storage_service() everywhere — it returns the correct implementation.

Vision analysis (VISION_ENABLED=true) runs during persist_all, before base64
data is cleared, so the LLM can see the image bytes.
"""

from __future__ import annotations
import base64
import json
import logging
import os
from pathlib import Path

from models.meta_schema import ParsedDocument, ImageElement

logger = logging.getLogger(__name__)

LOCAL_MODE       = os.environ.get("LOCAL_MODE",       "true").lower()  == "true"
DATABRICKS_MODE  = os.environ.get("DATABRICKS_MODE", "false").lower() == "true"

# Absolute path to Data_Ingestion/local_storage/ — consistent regardless of CWD.
_DEFAULT_LOCAL_STORAGE = Path(__file__).parent.parent / "local_storage"


# ─────────────────────────────────────────────────────────────────────────────
# Shared vision analysis — called by both storage implementations
# ─────────────────────────────────────────────────────────────────────────────

def _analyze_images(parsed_doc: ParsedDocument) -> ParsedDocument:
    """
    Run vision AI analysis on every image that still has base64_data.
    Populates ai_description, image_type, and key_elements on each ImageElement.
    Skips gracefully if VISION_ENABLED=false or if the LLM call fails.
    """
    try:
        from parsers.vision_analyzer import analyze_image, VISION_ENABLED
    except ImportError:
        logger.warning("vision_analyzer module not found — skipping image analysis")
        return parsed_doc

    if not VISION_ENABLED:
        return parsed_doc

    total   = len(parsed_doc.image_elements)
    success = 0

    for elem in parsed_doc.image_elements:
        if not elem.base64_data:
            continue
        try:
            result = analyze_image(elem.base64_data, elem.format)
            if result:
                elem.ai_description = result.get("description")
                elem.image_type     = result.get("image_type")
                elem.key_elements   = result.get("key_elements") or []
                # Set caption from key_elements if not already present
                if not elem.caption and elem.key_elements:
                    elem.caption = ", ".join(elem.key_elements[:3])
                success += 1
        except Exception as e:
            logger.warning("Vision analysis failed for %s: %s", elem.element_id, e)

    if total:
        logger.info("[VISION] Analyzed %d/%d images", success, total)

    return parsed_doc


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL STORAGE SERVICE  (development / testing)
# ─────────────────────────────────────────────────────────────────────────────

class LocalStorageService:
    """
    Mirrors GCSStorageService using the local filesystem.
    Layout under ./local_storage/ exactly mirrors GCS blob paths.

      local_storage/documents/{doc_id}/source/{filename}
      local_storage/documents/{doc_id}/images/img_XXXX.{ext}
      local_storage/documents/{doc_id}/tables/tbl_XXXX.csv
      local_storage/documents/{doc_id}/meta.json
      local_storage/cosmos/{doc_id}.json        ← GCS index simulation
    """

    def __init__(self, base_dir: str | None = None):
        # Default to an absolute path so storage works regardless of which
        # directory the server is started from.
        self.base = Path(base_dir) if base_dir else _DEFAULT_LOCAL_STORAGE
        (self.base / "documents").mkdir(parents=True, exist_ok=True)
        (self.base / "cosmos").mkdir(parents=True, exist_ok=True)
        logger.info("[LOCAL] Storage root: %s", self.base.resolve())

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _doc_dir(self, doc_id: str) -> Path:
        p = self.base / "documents" / doc_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _local_url(self, rel_path: str) -> str:
        return "file://" + str((self.base / rel_path).resolve())

    def _write(self, rel_path: str, data: bytes) -> str:
        full = self.base / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return self._local_url(rel_path)

    # ── Public API ────────────────────────────────────────────────────────────

    def upload_source_file(self, document_id: str, file_path: Path) -> str:
        rel = f"documents/{document_id}/source/{file_path.name}"
        url = self._write(rel, file_path.read_bytes())
        logger.info("[LOCAL] Source saved: %s", rel)
        return url

    def save_images(self, parsed_doc: ParsedDocument) -> ParsedDocument:
        doc_id = parsed_doc.document_id
        for elem in parsed_doc.image_elements:
            if not elem.base64_data:
                continue
            raw = base64.b64decode(elem.base64_data)
            rel = f"documents/{doc_id}/images/{elem.element_id}.{elem.format}"
            url = self._write(rel, raw)
            elem.blob_url    = url
            elem.local_path  = str(self.base / rel)
            elem.base64_data = None   # clear — base64 in meta.json bloats it
            logger.info("[LOCAL] Image saved: %s", rel)
        return parsed_doc

    def save_tables(self, parsed_doc: ParsedDocument) -> ParsedDocument:
        doc_id = parsed_doc.document_id
        for elem in parsed_doc.table_elements:
            if not elem.csv_data:
                continue
            rel = f"documents/{doc_id}/tables/{elem.element_id}.csv"
            url = self._write(rel, elem.csv_data.encode("utf-8"))
            elem.blob_url = url
            logger.info("[LOCAL] Table saved: %s", rel)
        return parsed_doc

    def save_meta_json(self, parsed_doc: ParsedDocument) -> str:
        doc_id = parsed_doc.document_id
        rel    = f"documents/{doc_id}/meta.json"
        data   = parsed_doc.model_dump_json(indent=2).encode("utf-8")
        url    = self._write(rel, data)
        logger.info("[LOCAL] Meta JSON saved: %s", rel)
        return url

    def save_to_cosmos(self, parsed_doc: ParsedDocument) -> None:
        record = _build_index_record(parsed_doc)
        path   = self.base / "cosmos" / f"{parsed_doc.document_id}.json"
        path.write_text(json.dumps(record, indent=2, default=str))
        logger.info("[LOCAL] Index saved: %s", path)

    def get_meta_json(self, document_id: str) -> dict:
        path = self.base / "documents" / document_id / "meta.json"
        if not path.exists():
            raise FileNotFoundError(f"meta.json not found for document_id={document_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def get_document_index(self, document_id: str) -> dict:
        path = self.base / "cosmos" / f"{document_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Index record not found for document_id={document_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def persist_all(self, parsed_doc: ParsedDocument, source_file: Path) -> ParsedDocument:
        parsed_doc.blob_base_path = f"local_storage/documents/{parsed_doc.document_id}/"
        self.upload_source_file(parsed_doc.document_id, source_file)
        # Vision analysis BEFORE save_images clears base64_data
        parsed_doc = _analyze_images(parsed_doc)
        parsed_doc.rebuild_summary()   # refresh stats after vision (has_workflows etc.)
        self.save_images(parsed_doc)
        self.save_tables(parsed_doc)
        self.save_meta_json(parsed_doc)
        self.save_to_cosmos(parsed_doc)
        logger.info("[LOCAL] All files saved under: %s", parsed_doc.blob_base_path)
        return parsed_doc


# ─────────────────────────────────────────────────────────────────────────────
# GCS STORAGE SERVICE  (production — Google Cloud Storage)
# ─────────────────────────────────────────────────────────────────────────────

class GCSStorageService:
    """
    Production GCP implementation using Google Cloud Storage.
    Requires: GCS_BUCKET_NAME in environment.
    Authentication via Application Default Credentials (service account key.json,
    Workload Identity, or GOOGLE_APPLICATION_CREDENTIALS env var).

    Blob layout mirrors LocalStorageService:
      documents/{doc_id}/source/{filename}
      documents/{doc_id}/images/img_XXXX.{ext}
      documents/{doc_id}/tables/tbl_XXXX.csv
      documents/{doc_id}/meta.json
      cosmos/{doc_id}.json                      ← document index
      outputs/{job_id}/{filename}               ← exported documents
    """

    def __init__(self):
        from google.cloud import storage as gcs

        bucket_name = os.environ.get("GCS_BUCKET_NAME", "")
        if not bucket_name:
            raise EnvironmentError(
                "GCS_BUCKET_NAME environment variable is not set. "
                "Set it to your GCS bucket name in Data_Ingestion/.env"
            )

        self._client     = gcs.Client()
        self._bucket     = self._client.bucket(bucket_name)
        self._bucket_name = bucket_name
        logger.info("[GCS] Storage bucket: gs://%s", bucket_name)

    def _upload_blob(
        self, blob_path: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        blob = self._bucket.blob(blob_path)
        blob.upload_from_string(data, content_type=content_type)
        url = f"gs://{self._bucket_name}/{blob_path}"
        logger.info("[GCS] Uploaded: %s", url)
        return url

    def _download_blob(self, blob_path: str) -> bytes:
        blob = self._bucket.blob(blob_path)
        return blob.download_as_bytes()

    def upload_source_file(self, document_id: str, file_path: Path) -> str:
        return self._upload_blob(
            f"documents/{document_id}/source/{file_path.name}",
            file_path.read_bytes(),
        )

    def save_images(self, parsed_doc: ParsedDocument) -> ParsedDocument:
        for elem in parsed_doc.image_elements:
            if not elem.base64_data:
                continue
            raw = base64.b64decode(elem.base64_data)
            url = self._upload_blob(
                f"documents/{parsed_doc.document_id}/images/{elem.element_id}.{elem.format}",
                raw, f"image/{elem.format}",
            )
            elem.blob_url    = url
            elem.base64_data = None
        return parsed_doc

    def save_tables(self, parsed_doc: ParsedDocument) -> ParsedDocument:
        for elem in parsed_doc.table_elements:
            if not elem.csv_data:
                continue
            url = self._upload_blob(
                f"documents/{parsed_doc.document_id}/tables/{elem.element_id}.csv",
                elem.csv_data.encode("utf-8"), "text/csv",
            )
            elem.blob_url = url
        return parsed_doc

    def save_meta_json(self, parsed_doc: ParsedDocument) -> str:
        return self._upload_blob(
            f"documents/{parsed_doc.document_id}/meta.json",
            parsed_doc.model_dump_json(indent=2).encode("utf-8"),
            "application/json",
        )

    def save_to_cosmos(self, parsed_doc: ParsedDocument) -> None:
        """Store the document index as a JSON blob in GCS (replaces Cosmos DB)."""
        record = _build_index_record(parsed_doc)
        self._upload_blob(
            f"cosmos/{parsed_doc.document_id}.json",
            json.dumps(record, indent=2, default=str).encode("utf-8"),
            "application/json",
        )

    def get_meta_json(self, document_id: str) -> dict:
        data = self._download_blob(f"documents/{document_id}/meta.json")
        return json.loads(data)

    def get_document_index(self, document_id: str) -> dict:
        data = self._download_blob(f"cosmos/{document_id}.json")
        return json.loads(data)

    def persist_all(self, parsed_doc: ParsedDocument, source_file: Path) -> ParsedDocument:
        parsed_doc.blob_base_path = f"documents/{parsed_doc.document_id}/"
        self.upload_source_file(parsed_doc.document_id, source_file)
        # Vision analysis BEFORE save_images clears base64_data
        parsed_doc = _analyze_images(parsed_doc)
        parsed_doc.rebuild_summary()
        self.save_images(parsed_doc)
        self.save_tables(parsed_doc)
        self.save_meta_json(parsed_doc)
        self.save_to_cosmos(parsed_doc)
        return parsed_doc


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_index_record(parsed_doc: ParsedDocument) -> dict:
    """Lightweight index record written to GCS (replaces Cosmos DB)."""
    return {
        "id":               parsed_doc.document_id,
        "document_id":      parsed_doc.document_id,
        "source_filename":  parsed_doc.source_filename,
        "file_type":        parsed_doc.file_type,
        "upload_timestamp": parsed_doc.upload_timestamp.isoformat(),
        "parsed_at":        parsed_doc.parsed_at.isoformat() if parsed_doc.parsed_at else None,
        "blob_base_path":   parsed_doc.blob_base_path,
        "status":           parsed_doc.status,
        "summary":          parsed_doc.summary.model_dump(),
        "user_inputs":      parsed_doc.user_inputs.model_dump() if parsed_doc.user_inputs else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

_storage_instance: "LocalStorageService | GCSStorageService | None" = None


def get_storage_service() -> "LocalStorageService | GCSStorageService":
    """Return the module-level singleton storage service (created once, reused)."""
    global _storage_instance
    if _storage_instance is None:
        if DATABRICKS_MODE:
            logger.info("[STORAGE] Using DatabricksVolumeStorageService (DATABRICKS_MODE=true)")
            from storage.databricks_volume_storage import DatabricksVolumeStorageService
            _storage_instance = DatabricksVolumeStorageService()
        elif LOCAL_MODE:
            logger.info("[STORAGE] Using LocalStorageService (LOCAL_MODE=true)")
            _storage_instance = LocalStorageService()
        else:
            logger.info("[STORAGE] Using GCSStorageService (LOCAL_MODE=false)")
            _storage_instance = GCSStorageService()
    return _storage_instance
