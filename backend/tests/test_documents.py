import base64
import json
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.auth import User, current_user
from app.main import app
from app.redaction import (
    MAX_PDF_BYTES,
    Record,
    RedactionService,
    Settings,
    get_redaction_service,
)

USER = User(str(uuid4()), str(uuid4()))
LANGUAGE_JOB_ID = str(uuid4())


def make_pdf(labels):
    writer = PdfWriter()
    for label in labels:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): DictionaryObject({
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                })
            })
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 700 Td ({label}) Tj ET".encode())
        page[NameObject("/Contents")] = stream
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def page_texts(pdf):
    return [page.extract_text() for page in PdfReader(BytesIO(pdf)).pages]


PDF = make_pdf(["synthetic test document"])
REDACTED_PDF = make_pdf(["redacted synthetic test document"])


@pytest.fixture
def workflow():
    settings = Settings(
        "https://language.example", "test-language-key",
        "testaccount", base64.b64encode(b"test-key-not-a-real-azure-secret").decode(),
        "originals", "redacted",
    )
    blobs = {}
    storage = Mock()
    state = {"status": "running", "requests": []}
    lease = Mock()
    state["lease"] = lease

    def get_blob_client(container, blob):
        key = (container, blob)
        client = Mock()
        client.url = f"{settings.storage_url}/{container}/{blob}"
        client.acquire_lease.return_value = lease
        if state.get("locked"):
            error = HttpResponseError(message="Lease in use")
            error.error_code = "LeaseAlreadyPresent"
            client.acquire_lease.side_effect = error

        def upload(data, **kwargs):
            blobs[key] = data.encode() if isinstance(data, str) else data

        def download():
            if key not in blobs:
                raise ResourceNotFoundError("Not found")
            return SimpleNamespace(readall=lambda: blobs[key])

        client.upload_blob.side_effect = upload
        client.download_blob.side_effect = download
        return client

    storage.get_blob_client.side_effect = get_blob_client
    def get_container_client(container):
        client = Mock()
        client.list_blobs.side_effect = lambda name_starts_with: [
            SimpleNamespace(name=name) for stored_container, name in list(blobs)
            if stored_container == container and name.startswith(name_starts_with)
        ]
        client.delete_blob.side_effect = lambda name: blobs.pop((container, name))
        return client

    storage.get_container_client.side_effect = get_container_client

    def language(request):
        state["requests"].append(request)
        if request.method == "POST":
            state["submitted"] = json.loads(request.content)
            return httpx.Response(202, headers={
                "operation-location": (
                    f"{settings.language_endpoint}/language/analyze-documents/jobs/"
                    f"{LANGUAGE_JOB_ID}?api-version=2026-05-01"
                )
            })
        if "response" in state:
            return httpx.Response(200, json=state["response"])
        if state["status"] == "running":
            return httpx.Response(200, json={"status": "running"})
        document_id = state["submitted"]["analysisInput"]["documents"][0]["id"]
        document = state["submitted"]["analysisInput"]["documents"][0]
        target = document["target"]["location"].split("?")[0]
        output = target.split("/redacted/")[1] + f"/{LANGUAGE_JOB_ID}/redacted.pdf"
        source = document["source"]["location"].split("?")[0].split("/originals/")[1]
        labels = page_texts(blobs[("originals", source)])
        blobs[("redacted", output)] = state.get(
            "output_pdf", make_pdf([f"redacted {label}" for label in labels])
        )
        return httpx.Response(200, json={
            "status": "succeeded",
            "tasks": {"failed": 0, "items": [{
                "kind": "PiiEntityRecognitionLROResults",
                "status": "succeeded",
                "results": {"errors": [], "documents": [{
                    "id": document_id,
                    "targets": [{
                        "location": state.get(
                            "output", f"{settings.storage_url}/redacted/{output}"
                        )
                    }],
                    "warnings": state.get("warnings", []),
                }]},
            }]},
        })

    with httpx.Client(transport=httpx.MockTransport(language)) as http:
        service = RedactionService(settings, storage, http)
        app.dependency_overrides[current_user] = lambda: USER
        app.dependency_overrides[get_redaction_service] = lambda: service
        try:
            with TestClient(app) as client:
                yield SimpleNamespace(
                    client=client, state=state, blobs=blobs, service=service, storage=storage
                )
        finally:
            app.dependency_overrides.clear()


def submit(workflow, pdf=PDF):
    response = workflow.client.post(
        "/api/documents", files={"file": ("sample.pdf", pdf, "application/pdf")}
    )
    assert response.status_code == 202
    document_id = response.json()["id"]
    assert workflow.client.get(f"/api/documents/{document_id}").json()["status"] == "running"
    return document_id


def test_upload_redact_store_and_download(workflow):
    document_id = submit(workflow)
    prefix = f"{USER.storage_prefix}/{document_id}"
    assert workflow.blobs[("originals", f"{prefix}/document.pdf")] == PDF
    payload = workflow.state["submitted"]
    document = payload["analysisInput"]["documents"][0]
    chunk_id = document["id"]
    assert document["target"]["kind"] == "AzureContainerFolder"
    assert document["target"]["location"].startswith(
        f"https://testaccount.blob.core.windows.net/redacted/{prefix}/chunks/{chunk_id}?"
    )
    assert document["source"]["location"].startswith(
        f"https://testaccount.blob.core.windows.net/originals/{prefix}/chunks/{chunk_id}.pdf?"
    )
    parameters = payload["tasks"][0]["parameters"]
    assert parameters["loggingOptOut"] is True
    assert parameters["piiCategories"] == ["All"]
    assert parameters["redactionPolicies"][0]["policyKind"] == "characterMask"

    assert workflow.client.get(f"/api/documents/{document_id}").json()["status"] == "running"
    assert workflow.client.get(f"/api/documents/{document_id}/download").status_code == 409
    workflow.state["status"] = "succeeded"
    result = workflow.client.get(f"/api/documents/{document_id}")
    assert result.status_code == 200
    assert result.json()["status"] == "succeeded"
    assert "location" not in result.text
    response = workflow.client.get(f"/api/documents/{document_id}/download")
    assert response.status_code == 200
    assert page_texts(response.content) == page_texts(REDACTED_PDF)
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert workflow.blobs[("originals", f"{prefix}/document.pdf")] == PDF

    requests = len(workflow.state["requests"])
    assert workflow.client.get(f"/api/documents/{document_id}").json()["status"] == "succeeded"
    assert len(workflow.state["requests"]) == requests


def test_other_users_cannot_read_status_or_download(workflow):
    document_id = submit(workflow)
    app.dependency_overrides[current_user] = lambda: User(USER.tenant_id, str(uuid4()))
    assert workflow.client.get(f"/api/documents/{document_id}").status_code == 404
    assert workflow.client.get(f"/api/documents/{document_id}/download").status_code == 404
    assert len(workflow.state["requests"]) == 1


def test_all_document_routes_require_sign_in(workflow):
    app.dependency_overrides.pop(current_user)
    document_id = str(uuid4())
    assert workflow.client.post(
        "/api/documents", files={"file": ("sample.pdf", PDF)}
    ).status_code == 401
    assert workflow.client.get(f"/api/documents/{document_id}").status_code == 401
    assert workflow.client.get(f"/api/documents/{document_id}/download").status_code == 401
    assert not workflow.blobs


@pytest.mark.parametrize("filename,data", [
    ("sample.txt", PDF), ("sample.pdf", b"not a pdf"), ("sample.pdf", b""),
])
def test_invalid_documents_are_not_stored(workflow, filename, data):
    response = workflow.client.post("/api/documents", files={"file": (filename, data)})
    assert response.status_code == 415
    assert not workflow.blobs


@pytest.mark.parametrize("size,expected_status", [
    (MAX_PDF_BYTES, 202), (MAX_PDF_BYTES + 1, 413),
])
def test_exact_pdf_size_limit(workflow, size, expected_status):
    service = Mock()
    service.submit.return_value = {"id": str(uuid4()), "status": "running"}
    app.dependency_overrides[get_redaction_service] = lambda: service
    pdf = b"%PDF-" + b"0" * (size - 5)
    response = workflow.client.post("/api/documents", files={"file": ("sample.pdf", pdf)})
    assert response.status_code == expected_status
    assert service.submit.called is (expected_status == 202)


def test_http_upload_limit_rejects_large_multipart_envelope(workflow):
    response = workflow.client.post(
        "/api/documents", content=b"x", headers={"Content-Length": str(MAX_PDF_BYTES + 100_000)}
    )
    assert response.status_code == 413
    assert not workflow.blobs


def test_chunked_upload_limit(workflow):
    def body():
        yield b"--boundary\r\n"
        yield b"x" * (MAX_PDF_BYTES + 70_000)

    response = workflow.client.post(
        "/api/documents", content=body(),
        headers={"Content-Type": "multipart/form-data; boundary=boundary"},
    )
    assert response.status_code == 413
    assert not workflow.blobs


def test_language_failure_is_not_downloadable(workflow):
    document_id = submit(workflow)
    workflow.state["response"] = {"status": "failed", "errors": [{"message": "sensitive"}]}
    response = workflow.client.get(f"/api/documents/{document_id}")
    assert response.json()["status"] == "failed"
    assert "sensitive" not in response.text
    assert workflow.client.get(f"/api/documents/{document_id}/download").status_code == 409


def test_document_level_failure_is_not_reported_as_success(workflow):
    document_id = submit(workflow)
    workflow.state["response"] = {
        "status": "succeeded",
        "tasks": {"failed": 0, "items": [{
            "kind": "PiiEntityRecognitionLROResults",
            "status": "succeeded",
            "results": {"documents": [], "errors": [{"message": "sensitive"}]},
        }]},
    }
    response = workflow.client.get(f"/api/documents/{document_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert "sensitive" not in response.text
    assert workflow.client.get(f"/api/documents/{document_id}/download").status_code == 409


@pytest.mark.parametrize("location", [
    "https://other.example/private/redacted.pdf?secret",
    "https://testaccount.blob.core.windows.net/redacted/another-user/redacted.pdf",
    "https://testaccount.blob.core.windows.net/redacted/../originals/document.pdf",
])
def test_untrusted_output_locations_are_never_downloaded(workflow, location):
    document_id = submit(workflow)
    workflow.state["status"] = "succeeded"
    workflow.state["output"] = location
    response = workflow.client.get(f"/api/documents/{document_id}")
    assert response.status_code == 502
    assert location not in response.text
    assert workflow.client.get(f"/api/documents/{document_id}/download").status_code == 409


def test_storage_errors_do_not_expose_secrets(workflow):
    workflow.storage.get_blob_client.side_effect = ResourceNotFoundError(
        "sensitive storage credential and SAS URL"
    )
    response = workflow.client.post("/api/documents", files={"file": ("sample.pdf", PDF)})
    assert response.status_code == 502
    assert "sensitive" not in response.text


def finish(workflow, document_id):
    workflow.state["status"] = "succeeded"
    for _ in range(30):
        response = workflow.client.get(f"/api/documents/{document_id}")
        assert response.status_code == 200
        if response.json()["status"] != "running":
            return response.json()
    pytest.fail("Job did not reach a terminal state")


def test_chunks_survive_restart_and_merge_in_page_order(workflow, monkeypatch):
    monkeypatch.setattr("app.redaction.CHUNK_TEXT_ELEMENTS", 12)
    original = make_pdf(["first-page", "second-page", "third-page"])
    document_id = submit(workflow, original)
    record = workflow.service._load(USER, UUID(document_id))
    assert [(c.start, c.end) for c in record.chunks] == [(0, 1), (1, 2), (2, 3)]
    restarted = RedactionService(
        workflow.service.settings, workflow.storage, workflow.service.http
    )
    app.dependency_overrides[get_redaction_service] = lambda: restarted
    workflow.state["warnings"] = [{"code": "test-warning"}]
    result = finish(workflow, document_id)
    assert result["status"] == "succeeded"
    assert result["warning_count"] == 3
    output = workflow.client.get(f"/api/documents/{document_id}/download")
    assert page_texts(output.content) == [
        "redacted first-page", "redacted second-page", "redacted third-page"
    ]
    assert sum(r.method == "POST" for r in workflow.state["requests"]) == 3
    assert not any("/chunks/" in name for _, name in workflow.blobs)


def size_error(workflow):
    chunk_id = workflow.state["submitted"]["analysisInput"]["documents"][0]["id"]
    return {
        "status": "succeeded",
        "tasks": {"items": [{
            "kind": "PiiEntityRecognitionLROResults", "status": "succeeded",
            "results": {"errors": [{
                "id": chunk_id,
                "error": {"code": "InvalidRequest", "details": [{
                    "code": "InvalidDocument",
                    "message": "A document was too large. Document contains 160874 text elements."
                }]}
            }]}
        }]}
    }


@pytest.mark.parametrize("top_level", [False, True])
def test_azure_size_error_splits_only_failed_range_and_retries(workflow, top_level):
    document_id = submit(workflow, make_pdf(["first", "second", "third", "fourth"]))
    error_response = size_error(workflow)
    if top_level:
        error_response = {
            "status": "failed",
            "errors": error_response["tasks"]["items"][0]["results"]["errors"],
        }
    workflow.state["response"] = error_response
    result = workflow.client.get(f"/api/documents/{document_id}")
    assert result.json()["status"] == "running"
    record = workflow.service._load(USER, UUID(document_id))
    assert [(c.start, c.end) for c in record.chunks] == [(0, 2), (2, 4)]
    assert all(c.language_job_id is None for c in record.chunks)
    del workflow.state["response"]
    assert finish(workflow, document_id)["status"] == "succeeded"
    output = workflow.client.get(f"/api/documents/{document_id}/download")
    assert page_texts(output.content) == [
        "redacted first", "redacted second", "redacted third", "redacted fourth"
    ]


def test_single_oversized_page_fails_without_retry_or_download(workflow):
    document_id = submit(workflow)
    workflow.state["response"] = size_error(workflow)
    result = workflow.client.get(f"/api/documents/{document_id}").json()
    assert result["status"] == "failed"
    assert "Page 1" in result["message"]
    assert workflow.client.get(f"/api/documents/{document_id}/download").status_code == 409
    workflow.client.get(f"/api/documents/{document_id}")
    assert sum(r.method == "POST" for r in workflow.state["requests"]) == 1


def test_failed_later_chunk_never_exposes_partial_pdf(workflow, monkeypatch):
    monkeypatch.setattr("app.redaction.CHUNK_TEXT_ELEMENTS", 6)
    document_id = submit(workflow, make_pdf(["first", "second"]))
    workflow.state["status"] = "succeeded"
    workflow.client.get(f"/api/documents/{document_id}")
    workflow.client.get(f"/api/documents/{document_id}")
    workflow.state["response"] = {"status": "failed"}
    assert finish(workflow, document_id)["status"] == "failed"
    assert workflow.client.get(f"/api/documents/{document_id}/download").status_code == 409
    assert not any(name.endswith("/redacted.pdf") and "/chunks/" not in name
                   for container, name in workflow.blobs if container == "redacted")


def test_output_page_count_mismatch_blocks_download(workflow):
    document_id = submit(workflow, make_pdf(["one", "two"]))
    workflow.state["status"] = "succeeded"
    workflow.state["output_pdf"] = REDACTED_PDF
    assert workflow.client.get(f"/api/documents/{document_id}").status_code == 502
    assert workflow.client.get(f"/api/documents/{document_id}/download").status_code == 409


def test_concurrent_poll_does_not_duplicate_submission(workflow):
    document_id = submit(workflow)
    workflow.state["locked"] = True
    before = len(workflow.state["requests"])
    assert workflow.client.get(f"/api/documents/{document_id}").json()["status"] == "running"
    assert len(workflow.state["requests"]) == before


def test_lease_errors_other_than_contention_are_reported(workflow):
    document_id = submit(workflow)
    original_get_client = workflow.storage.get_blob_client.side_effect

    def failing_lease(container, blob):
        client = original_get_client(container, blob)
        client.acquire_lease.side_effect = HttpResponseError("sensitive credential")
        return client

    workflow.storage.get_blob_client.side_effect = failing_lease
    response = workflow.client.get(f"/api/documents/{document_id}")
    assert response.status_code == 502
    assert "sensitive" not in response.text


def test_managed_identity_requests_do_not_use_sas(workflow):
    workflow.service.settings = replace(
        workflow.service.settings, language_key="", account_key=""
    )
    submit(workflow)
    document = workflow.state["submitted"]["analysisInput"]["documents"][0]
    assert "?" not in document["source"]["location"]
    assert "?" not in document["target"]["location"]


def test_legacy_job_records_remain_pollable(workflow):
    document_id = uuid4()
    prefix = f"{USER.storage_prefix}/{document_id}"
    record = Record(id=document_id, language_job_id=UUID(LANGUAGE_JOB_ID))
    workflow.blobs[("originals", f"{prefix}/job.json")] = record.model_dump_json().encode()
    workflow.blobs[("redacted", f"{prefix}/old.pdf")] = REDACTED_PDF
    workflow.state["response"] = {
        "status": "succeeded", "tasks": {"items": [{
            "kind": "PiiEntityRecognitionLROResults", "status": "succeeded",
            "results": {"documents": [{
                "id": str(document_id),
                "targets": [{"location": f"{workflow.service.settings.storage_url}/redacted/{prefix}/old.pdf"}]
            }]}
        }]}
    }
    assert finish(workflow, document_id)["status"] == "succeeded"
    assert workflow.client.get(f"/api/documents/{document_id}/download").content == REDACTED_PDF


def test_unreadable_pdf_is_rejected_before_storage(workflow):
    response = workflow.client.post(
        "/api/documents", files={"file": ("broken.pdf", b"%PDF-not really a document")}
    )
    assert response.status_code == 415
    assert not workflow.blobs


def test_cleanup_failure_is_persisted_and_retried(workflow):
    document_id = submit(workflow)
    workflow.state["status"] = "succeeded"
    original_container_client = workflow.storage.get_container_client.side_effect
    workflow.storage.get_container_client.side_effect = HttpResponseError("Cleanup unavailable")
    assert workflow.client.get(f"/api/documents/{document_id}").status_code == 502
    record = workflow.service._load(USER, UUID(document_id))
    assert record.status == "succeeded"
    assert record.cleanup_pending is True
    before = len(workflow.state["requests"])
    workflow.storage.get_container_client.side_effect = original_container_client
    assert workflow.client.get(f"/api/documents/{document_id}").json()["status"] == "succeeded"
    assert len(workflow.state["requests"]) == before
    assert not workflow.service._load(USER, UUID(document_id)).cleanup_pending
    assert not any("/chunks/" in name for _, name in workflow.blobs)


@pytest.mark.parametrize("second_page_size,expected_ranges", [
    (30_000, [(0, 2)]),
    (30_001, [(0, 1), (1, 2)]),
])
def test_real_text_budget_boundary(workflow, second_page_size, expected_ranges):
    document_id = submit(workflow, make_pdf(["x" * 60_000, "y" * second_page_size]))
    record = workflow.service._load(USER, UUID(document_id))
    assert [(c.start, c.end) for c in record.chunks] == expected_ranges


def test_upload_returns_persisted_id_before_azure_submission(workflow):
    response = workflow.client.post(
        "/api/documents", files={"file": ("sample.pdf", PDF, "application/pdf")}
    )
    assert response.status_code == 202
    document_id = UUID(response.json()["id"])
    assert workflow.service._load(USER, document_id).chunks[0].language_job_id is None
    assert not workflow.state["requests"]
