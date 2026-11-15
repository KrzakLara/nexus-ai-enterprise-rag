"""Synchronous upload validation, BEFORE anything is stored (Phase 4D).

Checks, in order: filename -> allowed extension (415), empty (400), size limit (413), content
sniffing against the extension (magic bytes / Office Open XML structure, 422), archive limits for
DOCX / XLSX (declared uncompressed size, entry count, compression ratio, path traversal names;
422), text files must be UTF-8 without NUL bytes (422). Deep parsing (pdfplumber / python-docx /
openpyxl / Tesseract) happens later in the worker; a file that passes these checks but cannot be
parsed ends as a failed job (queue mode) or HTTP 422 (inline mode).

The stored Content-Type is derived from the validated format, never from the client header.
"""
from __future__ import annotations

import re
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from .config import Settings
from .docintel.parsing import UnsupportedFormat, detect_format
from .storage import sha256_hex

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}
SUPPORTED_MESSAGE = "Supported: PDF (text layer or scanned), DOCX, XLSX, TXT, MD"
MAX_RATIO = 250  # a single DOCX / XLSX member inflating more than this is treated as a zip bomb


class UploadRejected(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


@dataclass(frozen=True)
class ValidatedUpload:
    filename: str
    extension: str
    format: str
    content_type: str
    sha256: str
    size: int


def safe_filename(raw: str | None) -> str:
    name = Path((raw or "").replace("\\", "/")).name
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:255]


def _check_archive(data: bytes, cfg: Settings) -> None:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        raise UploadRejected(422, "Document could not be parsed: corrupt archive", "malformed") from None
    infos = archive.infolist()
    if len(infos) > cfg.max_archive_entries:
        raise UploadRejected(422, "Document could not be parsed: archive has too many entries", "archive_limits")
    total = 0
    for info in infos:
        if info.filename.startswith(("/", "\\")) or ".." in Path(info.filename).parts:
            raise UploadRejected(422, "Document could not be parsed: unsafe archive member name", "archive_limits")
        total += info.file_size
        if info.compress_size and info.file_size > 1_000_000 and info.file_size / info.compress_size > MAX_RATIO:
            raise UploadRejected(422, "Document could not be parsed: suspicious compression ratio", "archive_limits")
    if total > cfg.max_uncompressed_bytes:
        raise UploadRejected(422, "Document could not be parsed: archive expands beyond the size limit",
                             "archive_limits")


def validate_upload(raw_filename: str | None, data: bytes, cfg: Settings) -> ValidatedUpload:
    filename = safe_filename(raw_filename)
    extension = Path(filename).suffix.lower()
    if extension not in {".pdf", ".docx", ".xlsx", ".txt", ".md"} or not Path(filename).stem:
        raise UploadRejected(415, SUPPORTED_MESSAGE, "unsupported_type")
    if not data:
        raise UploadRejected(400, "The uploaded file is empty", "empty")
    if len(data) > cfg.max_upload_bytes:
        raise UploadRejected(413, f"Upload exceeds {cfg.max_upload_bytes // (1024 * 1024)} MB", "too_large")
    try:
        fmt = detect_format(filename, data)
    except UnsupportedFormat as exc:
        raise UploadRejected(415, str(exc), "unsupported_type") from None
    except ValueError as exc:
        raise UploadRejected(422, f"Document could not be parsed: {str(exc)[:180]}", "content_mismatch") from None
    if fmt in ("docx", "xlsx"):
        _check_archive(data, cfg)
    if fmt in ("txt", "md"):
        if b"\x00" in data:
            raise UploadRejected(422, "Document could not be parsed: binary content in a text file", "content_mismatch")
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise UploadRejected(422, "Document could not be parsed: text files must be UTF-8", "content_mismatch") from None
    return ValidatedUpload(filename=filename, extension=extension, format=fmt,
                           content_type=CONTENT_TYPES[fmt], sha256=sha256_hex(data), size=len(data))
