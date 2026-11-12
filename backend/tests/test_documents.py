import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import pytest
from azure.core.exceptions import ResourceNotFoundError
from fastapi.testclient import TestClient

from app.auth import User, current_user
from app.main import app
from app.redaction import (
    MAX_PDF_BYTES,
    RedactionService,
    Settings,
    get_redaction_service,
)

USER = User(str(uuid4()), str(uuid4()))
LANGUAGE_JOB_ID = str(uuid4())
PDF = b"%PDF-1.7\nsynthetic test document"
REDACTED_PDF = b"%PDF-1.7\nredacted synthetic document"


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

    def get_blob_client(container, blob):
        key = (container, blob)
        client = Mock()
        client.url = f"{settings.storage_url}/{container}/{blob}"

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
        output = f"{USER.storage_prefix}/{document_id}/{LANGUAGE_JOB_ID}/redacted.pdf"
        blobs[("redacted", output)] = REDACTED_PDF
        return httpx.Response(200, json={
            "status": "succeeded",
            "tasks": {"failed": 0, "items": [{
                "kind": "PiiEntityRecognitionLROResults",
                "status": "succeeded",
                "results": {"errors": [], "documents": [{
                    "id": document_id,
                    "target": [{
                        "location": state.get(
                            "output", f"{settings.storage_url}/redacted/{output}"
                        )
                    }],
                    "warnings": [],
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


def submit(workflow):
    response = workflow.client.post(
        "/api/documents", files={"file": ("sample.pdf", PDF, "application/pdf")}
    )
    assert response.status_code == 202
    return response.json()["id"]


def test_upload_redact_store_and_download(workflow):
    document_id = submit(workflow)
    prefix = f"{USER.storage_prefix}/{document_id}"
    assert workflow.blobs[("originals", f"{prefix}/document.pdf")] == PDF
    payload = workflow.state["submitted"]
    document = payload["analysisInput"]["documents"][0]
    assert document["target"]["kind"] == "AzureContainerFolder"
    assert document["target"]["location"].startswith(
        f"https://testaccount.blob.core.windows.net/redacted/{prefix}?"
    )
    assert document["source"]["location"].startswith(
        f"https://testaccount.blob.core.windows.net/originals/{prefix}/document.pdf?"
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
    assert response.content == REDACTED_PDF
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
