"""Phase 4D: object keys, local / S3 storage, upload validation, MinIO integration.

MinIO tests run when TEST_S3_ENDPOINT_URL is set (e.g. http://127.0.0.1:9000) with
TEST_S3_ACCESS_KEY / TEST_S3_SECRET_KEY; each run uses its own disposable bucket.
"""
import io
import os
import time
import uuid
import zipfile
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

from backend.app.config import Settings
from backend.app.models import Document, DocumentVersion
from backend.app.storage import (IntegrityError, InvalidObjectKey, LocalObjectStore, ObjectMissing,
                                 S3ObjectStore, StorageUnavailable, check_key, object_key, sha256_hex)
from backend.app.uploads import UploadRejected, validate_upload

from .fixtures import KESTREL_PAGES, make_docx, make_pdf

S3 = os.getenv("TEST_S3_ENDPOINT_URL", "")
DOC = "3f2b9c1e-1111-4222-8333-444455556666"


# ----------------------------------------------------------------------------- keys

def test_object_keys_are_server_generated_and_strictly_validated():
    assert object_key("northstar", DOC, 3) == f"tenants/northstar/documents/{DOC}/v3/original"
    for tenant, doc, version in (("../etc", DOC, 1), ("northstar", "../../x", 1), ("Northstar", DOC, 1),
                                 ("northstar", DOC, 0), ("north star", DOC, 1)):
        with pytest.raises(InvalidObjectKey):
            object_key(tenant, doc, version)
    for key in ("../secret", "tenants/northstar/documents/x/v1/original", "/etc/passwd", "a/../../b",
                f"tenants/northstar/documents/{DOC}/v1/original/../../x", "C:\\windows\\x.txt"):
        with pytest.raises(InvalidObjectKey):
            check_key(key, allow_legacy=True)
    assert check_key("d_hr.txt", allow_legacy=True)  # v0.3 local keys stay readable


def test_local_store_refuses_traversal_and_writes_atomically(tmp_path):
    store = LocalObjectStore(tmp_path / "root")
    key = object_key("northstar", DOC, 1)
    data = b"original bytes"
    store.put(key, data, "text/plain", sha256_hex(data))
    assert store.get(key) == data and store.head(key).size == len(data)
    assert not list((tmp_path / "root").rglob(".upload-*"))  # no temp files left behind
    with pytest.raises(IntegrityError):
        store.put(key, data, "text/plain", "0" * 64)
    (tmp_path / "secret.txt").write_text("outside")
    for evil in ("../secret.txt", "..\\secret.txt"):
        with pytest.raises(InvalidObjectKey):
            store.get(evil)
    assert store.delete(key) is True and store.delete(key) is False  # idempotent
    with pytest.raises(ObjectMissing):
        store.get(key)


# ----------------------------------------------------------------------------- upload validation

CFG = Settings()


def _zip(entries: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize("filename, data, status, code", [
    ("notes.html", b"<script>alert(1)</script>", 415, "unsupported_type"),
    ("image.svg", b"<svg onload=alert(1)>", 415, "unsupported_type"),
    ("run.exe", b"MZ\x90\x00", 415, "unsupported_type"),
    (".pdf", b"%PDF-1.4", 415, "unsupported_type"),
    ("fake.pdf", b"<html><body>not a pdf</body></html>", 422, "content_mismatch"),
    ("fake.docx", b"%PDF-1.4 pretending to be word", 422, "content_mismatch"),
    ("zip.docx", _zip({"payload.bin": b"x"}), 422, "content_mismatch"),
    ("binary.txt", b"MZ\x00\x00\x00binary", 422, "content_mismatch"),
    ("latin1.txt", "Café".encode("cp1252"), 422, "content_mismatch"),
    ("empty.txt", b"", 400, "empty"),
])
def test_unsafe_and_mislabelled_uploads_are_rejected_before_storage(filename, data, status, code):
    with pytest.raises(UploadRejected) as err:
        validate_upload(filename, data, CFG)
    assert (err.value.status, err.value.code) == (status, code)


def test_oversized_uploads_and_zip_bombs_are_rejected():
    with pytest.raises(UploadRejected) as err:
        validate_upload("big.txt", b"a" * (CFG.max_upload_bytes + 1), CFG)
    assert err.value.status == 413
    bomb = _zip({"[Content_Types].xml": b"<x/>", "word/document.xml": b"<w/>",
                 "word/media/huge.bin": b"\0" * (70 * 1024 * 1024)})
    assert len(bomb) < CFG.max_upload_bytes  # small on the wire
    with pytest.raises(UploadRejected) as err:
        validate_upload("bomb.docx", bomb, CFG)
    assert err.value.status == 422 and err.value.code == "archive_limits"
    traversal = _zip({"[Content_Types].xml": b"<x/>", "word/document.xml": b"<w/>", "../../evil.sh": b"x"})
    with pytest.raises(UploadRejected) as err:
        validate_upload("slip.docx", traversal, CFG)
    assert err.value.code == "archive_limits"


def test_valid_uploads_get_a_server_side_content_type_and_a_safe_name():
    pdf = validate_upload("..\\..\\Quarterly\x00 report.pdf", make_pdf(KESTREL_PAGES), CFG)
    assert pdf.filename == "Quarterly report.pdf" and pdf.content_type == "application/pdf"
    data = make_docx(["Notice period is 45 days."])
    docx = validate_upload("terms.docx", data, CFG)
    assert docx.format == "docx" and docx.sha256 == sha256_hex(data) and docx.size == len(data)
    text = validate_upload("note.md", "# Naslov\nčćž".encode("utf-8"), CFG)
    assert text.content_type.startswith("text/markdown")


# ----------------------------------------------------------------------------- MinIO / S3

@pytest.fixture(scope="module")
def s3_settings():
    if not S3:
        pytest.skip("TEST_S3_ENDPOINT_URL not set (MinIO / S3 integration tests)")
    cfg = replace(Settings(), storage_backend="s3", s3_endpoint_url=S3, s3_public_endpoint_url=S3,
                  s3_bucket=f"nexus-test-{uuid.uuid4().hex[:10]}", s3_region="us-east-1",
                  s3_access_key_id=os.getenv("TEST_S3_ACCESS_KEY", "minio-test-admin"),
                  s3_secret_access_key=os.getenv("TEST_S3_SECRET_KEY", "minio-test-admin-secret"),
                  s3_force_path_style=True, presigned_downloads=True, presigned_ttl_seconds=60)
    store = S3ObjectStore(cfg)
    store.ensure_bucket()
    yield cfg
    for obj in store.list_keys(""):
        store.client.delete_object(Bucket=cfg.s3_bucket, Key=obj.key)
    store.client.delete_bucket(Bucket=cfg.s3_bucket)


pytest_minio = pytest.mark.minio


@pytest_minio
def test_s3_roundtrip_checksums_and_idempotent_delete(s3_settings):
    store = S3ObjectStore(s3_settings)
    key = object_key("northstar", str(uuid.uuid4()), 1)
    data = b"%PDF-1.4 synthetic"
    stored = store.put(key, data, "application/pdf", sha256_hex(data), {"document-id": "x"})
    assert stored.size == len(data)
    assert store.get(key) == data and b"".join(store.stream(key)) == data
    head = store.head(key)
    assert head.sha256 == sha256_hex(data) and head.content_type == "application/pdf"
    with pytest.raises(IntegrityError):  # local pre-check
        store.put(key, data, "application/pdf", "0" * 64)
    assert store.delete(key) is True
    assert store.head(key) is None
    store.delete(key)  # deleting again is fine
    with pytest.raises(ObjectMissing):
        store.get(key)


@pytest_minio
def test_server_side_checksum_rejects_corruption_in_transit(s3_settings):
    store = S3ObjectStore(s3_settings)
    key = object_key("northstar", str(uuid.uuid4()), 1)
    import base64
    import hashlib
    with pytest.raises(Exception):  # noqa: B017 - MinIO refuses a body that does not match its checksum
        store.client.put_object(Bucket=s3_settings.s3_bucket, Key=key, Body=b"tampered", ChecksumAlgorithm="SHA256",
                                ChecksumSHA256=base64.b64encode(hashlib.sha256(b"original").digest()).decode())
    assert store.head(key) is None


@pytest_minio
def test_bucket_is_private_and_presigned_urls_are_short_lived_and_tamper_proof(s3_settings):
    store = S3ObjectStore(s3_settings)
    key = object_key("otherco", str(uuid.uuid4()), 1)
    store.put(key, b"secret", "text/plain", sha256_hex(b"secret"))
    anonymous = httpx.get(f"{S3}/{s3_settings.s3_bucket}/{key}")
    assert anonymous.status_code == 403  # no public access
    url = store.presigned_get(key, "budget.txt", "text/plain", ttl=60)
    ok = httpx.get(url)
    assert ok.status_code == 200 and ok.content == b"secret"
    assert 'attachment; filename="budget.txt"' in ok.headers["content-disposition"]
    tampered = url.replace("X-Amz-Signature=", "X-Amz-Signature=0")
    assert httpx.get(tampered).status_code == 403
    other_key = url.replace(key, object_key("otherco", str(uuid.uuid4()), 1))
    assert httpx.get(other_key).status_code == 403  # a signature is bound to its key
    short = store.presigned_get(key, "budget.txt", "text/plain", ttl=5)
    time.sleep(6)
    assert httpx.get(short).status_code == 403  # expired
    store.delete(key)
    assert httpx.get(url).status_code == 404  # deleted object: the old URL is useless


@pytest_minio
def test_unreachable_storage_is_reported_as_unavailable(s3_settings):
    dead = S3ObjectStore(replace(s3_settings, s3_endpoint_url="http://127.0.0.1:9", s3_max_attempts=1,
                                 s3_connect_timeout=0.5))
    with pytest.raises(StorageUnavailable):
        dead.put(object_key("northstar", DOC, 1), b"x", "text/plain", sha256_hex(b"x"))
    assert dead.healthy() is False


@pytest_minio
def test_api_upload_download_presign_and_delete_with_minio(scenario, s3_settings, monkeypatch):
    from backend.app import main

    client, token, factory = scenario
    monkeypatch.setattr(main, "settings", replace(main.settings, **{
        k: getattr(s3_settings, k) for k in ("storage_backend", "s3_endpoint_url", "s3_public_endpoint_url",
                                             "s3_bucket", "s3_region", "s3_access_key_id", "s3_secret_access_key",
                                             "s3_force_path_style", "presigned_downloads", "presigned_ttl_seconds")}))
    finance = token("finance@northstar.demo")
    pdf = make_pdf(KESTREL_PAGES)
    up = client.post("/api/documents", headers=finance, files={"file": ("kestrel.pdf", pdf, "application/pdf")},
                     data={"groups": "finance"})
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]
    with factory() as db:
        doc = db.get(Document, doc_id)
        version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc_id))
        assert (doc.storage_backend, doc.storage_key) == ("s3", f"tenants/northstar/documents/{doc_id}/v1/original")
        assert version.sha256 == sha256_hex(pdf) and version.state == "active" and doc.lifecycle_state == "ready"
    store = S3ObjectStore(s3_settings)
    assert store.get(doc.storage_key) == pdf
    source = client.get(f"/api/documents/{doc_id}/source", headers=finance)
    assert source.status_code == 200 and source.content == pdf
    assert source.headers["x-content-type-options"] == "nosniff" and "attachment" in source.headers["content-disposition"]
    presigned = client.get(f"/api/documents/{doc_id}/download-url", headers=finance)
    assert presigned.status_code == 200 and httpx.get(presigned.json()["url"]).content == pdf
    for outsider in ("hr@northstar.demo", "finance@otherco.demo"):  # authorization before signing
        h = token(outsider)
        assert client.get(f"/api/documents/{doc_id}/download-url", headers=h).status_code == 404
        assert client.get(f"/api/documents/{doc_id}/source", headers=h).status_code == 404
    deleted = client.delete(f"/api/documents/{doc_id}", headers=finance)
    assert deleted.status_code == 202 and deleted.json()["purge_job"]["status"] == "completed"
    assert store.head(doc.storage_key) is None  # purged from the bucket
    assert httpx.get(presigned.json()["url"]).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/source", headers=finance).status_code == 404


@pytest_minio
def test_storage_outage_at_upload_stores_nothing(scenario, s3_settings, monkeypatch):
    from backend.app import main

    client, token, factory = scenario
    monkeypatch.setattr(main, "settings", replace(main.settings, storage_backend="s3", s3_endpoint_url="http://127.0.0.1:9",
                                                  s3_bucket="nowhere", s3_access_key_id="x", s3_secret_access_key="y",
                                                  s3_max_attempts=1, s3_connect_timeout=0.5))
    r = client.post("/api/documents", headers=token("finance@northstar.demo"),
                    files={"file": ("a.txt", b"Atlas renewal terms.", "text/plain")}, data={"groups": "finance"})
    assert r.status_code == 503 and "nothing was stored" in r.json()["detail"]
    with factory() as db:
        assert db.query(Document).count() == 3  # only the seeded documents
