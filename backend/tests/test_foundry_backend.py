import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException

import app.foundry as foundry
from app.auth import User, current_user
from app.main import app
from app.redaction import get_redaction_service
from app.foundry_native import configure_existing_agent, native_definition
from foundry_poc import PocError
from test_documents import USER, workflow
from test_foundry_poc import pdf

ORIGINAL = pdf("synthetic original")
REDACTED = pdf("synthetic redacted")
PREVIEW = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


@pytest.fixture(params=["native", "toolbox"])
def backend(workflow, monkeypatch, request):
    mode = request.param
    state = {"calls": [], "failure": None, "cleanup_failure": False, "mode": mode}
    credential = Mock()
    credential.get_token.return_value.token = "synthetic-token"
    settings = foundry.FoundrySettings(
        workflow.service.settings,
        "https://example.services.ai.azure.com/api/projects/test",
        "sac-redaction",
        mode,
    )

    def handle(request):
        path = request.url.path
        state["calls"].append((request.method, path))
        if request.method == "DELETE":
            return httpx.Response(
                500 if state["cleanup_failure"] and "/containers/" in path else 204,
            )
        if path.endswith(f"/agents/{settings.agent_name}/versions"):
            definition = {
                "kind": "prompt", "model": "synthetic-model",
                "instructions": "Synthetic redaction instructions.",
            }
            return httpx.Response(200, json={"data": [{
                "version": "2",
                "definition": native_definition(definition) if mode == "native" else definition,
            }]})
        if path.endswith("/openai/v1/files"):
            assert request.method == "POST"
            assert ORIGINAL in request.content and b"assistants" in request.content
            return httpx.Response(200, json={"id": "uploaded_job"})
        if path.endswith("/containers"):
            return httpx.Response(200, json={"id": "cntr_job"})
        if path.endswith("/containers/cntr_job/files"):
            if request.method == "POST":
                assert ORIGINAL in request.content
                return httpx.Response(200, json={"path": "/mnt/data/document.pdf"})
            original_entry = {
                "id": "input", "source": "user",
                "path": "/mnt/data/uploaded_job-document.pdf" if mode == "native"
                else "/mnt/data/document.pdf",
            }
            return httpx.Response(200, json={"has_more": False, "data": [original_entry] + ([] if (
                state["failure"] == "missing_output"
            ) else [
                {"id": "output", "path": "/mnt/data/document_Redacted.pdf"},
                {"id": "preview", "path": "/mnt/data/preview.png"},
            ])})
        if path.endswith("/files/input/content"):
            return httpx.Response(
                200, content=b"modified original" if state["failure"] == "binding" else ORIGINAL,
            )
        if path.endswith("/files/output/content"):
            return httpx.Response(200, content=(
                ORIGINAL if state["failure"] == "unchanged" else REDACTED
            ))
        if path.endswith("/files/preview/content"):
            return httpx.Response(200, content=PREVIEW)
        if "/toolboxes/" in path and path.endswith("/versions"):
            assert json.loads(request.content)["tools"] == [
                {"type": "code_interpreter", "container": "cntr_job"},
            ]
            return httpx.Response(200, json={"version": "1"})
        if path.endswith("/mcp"):
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(200, json={"result": {"tools": [{"name": "execute"}]}})
            digest = "0" * 64 if state["failure"] == "binding" else hashlib.sha256(
                ORIGINAL
            ).hexdigest()
            return httpx.Response(200, json={"result": {"content": [{
                "type": "text", "text": (
                    f"BINDING_SHA256={digest}\nPYMUPDF_AVAILABLE=True"
                    + ("\ncntr_other_user" if state["failure"] == "foreign_citation" else "")
                ),
            }]}})
        if path.endswith("/agents"):
            return httpx.Response(200, json={})
        if path.endswith("/responses"):
            body = json.loads(request.content)
            if mode == "native":
                assert body["agent_reference"] == {
                    "type": "agent_reference", "name": settings.agent_name, "version": "2",
                }
                assert body["structured_inputs"] == {"analysis_file_id": "uploaded_job"}
                assert "/mnt/data/uploaded_job-document.pdf" in body["input"]
            failed = state["failure"] == "model"
            status = "failed" if failed else "completed"
            output = [] if mode != "native" or state["failure"] == "missing_output" else [{
                "content": [{"annotations": [
                    {"type": "container_file_citation", "container_id": "cntr_job",
                     "file_id": "output", "filename": "document_Redacted.pdf"},
                    {"type": "container_file_citation", "container_id": "cntr_job",
                     "file_id": "preview", "filename": "preview.png"},
                ]}],
            }]
            if state["failure"] == "foreign_citation":
                output = [{"content": [{"annotations": [{
                    "type": "container_file_citation", "container_id": "cntr_other_user",
                    "file_id": "foreign", "filename": "document_Redacted.pdf",
                }]}]}]
            events = [
                {"type": "response.created", "response": {"id": "resp_job", "status": "in_progress"}},
                {"type": "response.output_item.done",
                 "item": {"type": "mcp_call", "output": "cntr_other_user"}},
            ]
            if mode == "native":
                events.append({
                    "type": "response.output_item.added",
                    "item": {"type": "code_interpreter_call", "container_id": "cntr_job"},
                })
            events.append({
                "type": f"response.{status}",
                "response": {"id": "resp_job", "status": status, "output": output},
            })
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
            )
        pytest.fail(f"Unexpected Foundry call: {request.method} {path}")

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        def services(background_tasks):
            yield foundry.FoundryRedactionService(
                settings, workflow.storage, http, credential, background_tasks,
            )

        monkeypatch.setattr(foundry, "get_foundry_service", services)
        monkeypatch.delenv("REDACTION_PROVIDER", raising=False)
        app.dependency_overrides.pop(get_redaction_service)
        yield SimpleNamespace(
            client=workflow.client, state=state, workflow=workflow,
            service=next(services(BackgroundTasks())),
        )


def upload(backend):
    response = backend.client.post(
        "/api/documents", files={"file": ("sample.pdf", ORIGINAL, "application/pdf")},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "running"
    return response.json()["id"]


def test_default_upload_uses_foundry_and_keeps_authenticated_download(backend, monkeypatch):
    actual_wait = foundry.wait
    first = True

    def wait_with_renewal(futures, timeout):
        nonlocal first
        if first:
            first = False
            return SimpleNamespace(done=set())
        return actual_wait(futures, timeout)

    monkeypatch.setattr(foundry, "wait", wait_with_renewal)
    document_id = upload(backend)
    result = backend.client.get(f"/api/documents/{document_id}").json()
    assert result["status"] == "succeeded"
    assert result["warning_count"] == 1
    assert "not been independently verified" in result["message"]
    response = backend.client.get(f"/api/documents/{document_id}/download")
    assert response.content == REDACTED
    assert response.headers["cache-control"] == "no-store"
    prefix = f"{USER.storage_prefix}/{document_id}"
    assert backend.workflow.blobs[("originals", f"{prefix}/document.pdf")] == ORIGINAL
    assert PREVIEW in backend.workflow.blobs.values()
    record = backend.service._load(USER, UUID(document_id))
    assert record.generation_report["binding_sha256_verified"] is True
    assert record.cleanup_pending is False
    deletions = [path for method, path in backend.state["calls"] if method == "DELETE"]
    assert len(deletions) == (3 if backend.state["mode"] == "native" else 4)
    if backend.state["mode"] == "native":
        assert record.generation_report["download_method"] == "response_file_citations"
        assert not any("/agents/" in path or "/toolboxes/" in path for path in deletions)
        assert not any(method == "POST" and (
            path.endswith("/agents") or "/toolboxes/" in path or path.endswith("/containers")
        ) for method, path in backend.state["calls"])
        assert record.resources.file_ids == {"uploaded_job"}
    backend.workflow.state["lease"].renew.assert_called()
    assert not backend.workflow.state["requests"]


def test_other_owners_cannot_poll_download_or_schedule_processing(backend):
    document_id = upload(backend)
    before = len(backend.state["calls"])
    app.dependency_overrides[current_user] = lambda: User(USER.tenant_id, str(uuid4()))
    assert backend.client.get(f"/api/documents/{document_id}").status_code == 404
    assert backend.client.get(f"/api/documents/{document_id}/download").status_code == 404
    assert len(backend.state["calls"]) == before


@pytest.mark.parametrize("failure", [
    "binding", "model", "missing_output", "unchanged", "foreign_citation",
])
def test_failed_generation_never_serves_original_and_cleans_owned_resources(backend, failure):
    backend.state["failure"] = failure
    document_id = upload(backend)
    result = backend.client.get(f"/api/documents/{document_id}").json()
    assert result["status"] == "failed"
    assert backend.client.get(f"/api/documents/{document_id}/download").status_code == 409
    assert not any(name.endswith("/redacted.pdf") for _, name in backend.workflow.blobs)
    assert any(method == "DELETE" and path.endswith("/containers/cntr_job")
               for method, path in backend.state["calls"])
    assert all("cntr_other_user" not in path for _, path in backend.state["calls"])


def test_status_retries_cleanup_without_reinvoking_model(backend):
    backend.state["cleanup_failure"] = True
    document_id = upload(backend)
    record = backend.service._load(USER, UUID(document_id))
    assert record.cleanup_pending
    assert "cleanup is pending" in record.message
    backend.state["cleanup_failure"] = False
    backend.client.get(f"/api/documents/{document_id}")
    record = backend.service._load(USER, UUID(document_id))
    assert not record.cleanup_pending
    assert record.warning_count == 1
    assert sum(method == "POST" and path.endswith("/responses")
               for method, path in backend.state["calls"]) == 1


def test_interrupted_job_cleans_persisted_resources_without_duplicate_generation(backend):
    job = backend.service.submit(USER, ORIGINAL)
    record = backend.service._load(USER, job.id)
    record.processing_started = True
    record.resources.containers.add("cntr_job")
    if backend.state["mode"] == "native":
        record.resources.file_ids.add("uploaded_job")
    backend.service._save(USER, record)
    backend.service.process(USER, job.id)
    record = backend.service._load(USER, job.id)
    assert record.status == "failed"
    assert "interrupted" in record.message
    expected = [
        ("DELETE", "/api/projects/test/openai/v1/containers/cntr_job"),
    ]
    if backend.state["mode"] == "native":
        expected.append(("DELETE", "/api/projects/test/openai/v1/files/uploaded_job"))
    assert backend.state["calls"] == expected


def test_duplicate_worker_does_not_run_while_job_is_leased(backend):
    job = backend.service.submit(USER, ORIGINAL)
    backend.workflow.state["locked"] = True
    backend.service.process(USER, job.id)
    assert backend.state["calls"] == []
    assert backend.service._load(USER, job.id).status == "running"


def test_language_setting_selects_retained_implementation(backend, monkeypatch):
    monkeypatch.setenv("REDACTION_PROVIDER", "language")
    monkeypatch.setattr(
        "app.redaction.get_language_redaction_service",
        lambda: iter([backend.workflow.service]),
    )
    document_id = upload(backend)
    assert backend.client.get(f"/api/documents/{document_id}").status_code == 200
    assert backend.workflow.state["requests"]
    assert not backend.state["calls"]


def test_foundry_settings_do_not_require_language_or_paired_keys(monkeypatch):
    monkeypatch.setenv(
        "AZURE_FOUNDRY_PROJECT_ENDPOINT",
        "https://example.services.ai.azure.com/api/projects/test",
    )
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "testaccount")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_KEY", "synthetic-storage-key")
    monkeypatch.setenv("AZURE_STORAGE_ORIGINALS_CONTAINER", "originals")
    monkeypatch.setenv("AZURE_STORAGE_REDACTED_CONTAINER", "redacted")
    monkeypatch.delenv("AZURE_LANGUAGE_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_LANGUAGE_KEY", raising=False)
    assert foundry.FoundrySettings.from_env().storage.account_key == "synthetic-storage-key"
    monkeypatch.setenv("AZURE_FOUNDRY_PROJECT_ENDPOINT", "https://untrusted.example")
    with pytest.raises(HTTPException) as error:
        foundry.FoundrySettings.from_env()
    assert error.value.status_code == 503


def test_protocol_switch_does_not_process_existing_job_with_wrong_service(backend, monkeypatch):
    document_id = upload(backend)
    before = len(backend.state["calls"])
    monkeypatch.setenv("REDACTION_PROVIDER", "language")
    monkeypatch.setattr(
        "app.redaction.get_language_redaction_service",
        lambda: iter([backend.workflow.service]),
    )
    assert backend.client.get(f"/api/documents/{document_id}").status_code == 409
    assert backend.client.get(f"/api/documents/{document_id}/download").status_code == 409
    assert len(backend.state["calls"]) == before
    assert not backend.workflow.state["requests"]


def test_queued_job_keeps_its_agent_and_execution_mode_after_configuration_changes(backend):
    job = backend.service.submit(USER, ORIGINAL)
    backend.service.foundry = replace(
        backend.service.foundry, agent_name="different-agent",
        execution_mode="toolbox" if backend.state["mode"] == "native" else "native",
    )
    backend.service.process(USER, job.id)
    record = backend.service._load(USER, job.id)
    assert record.status == "succeeded"
    assert record.execution_mode == backend.state["mode"]


def test_setup_versions_existing_agent_without_copying_or_changing_its_prompt():
    original = {
        "kind": "prompt", "model": "customer-model", "instructions": "Customer redaction rules.",
        "temperature": 0.2,
        "tools": [{"type": "code_interpreter", "container": {
            "type": "auto", "file_ids": [], "memory_limit": "4g",
        }}],
        "structured_inputs": {"department": {
            "required": False, "schema": {"type": "string"},
        }},
    }
    runner = Mock(project="https://example.services.ai.azure.com/api/projects/test")
    runner.request.side_effect = [
        httpx.Response(200, json={"data": [{"version": "3", "definition": original}]}),
        httpx.Response(200, json={"version": "4"}),
    ]
    version, changed = configure_existing_agent(runner, "customer-agent")
    assert changed and version["version"] == "4"
    method, url = runner.request.call_args.args
    assert method == "POST"
    assert url.endswith("/agents/customer-agent/versions")
    definition = runner.request.call_args.kwargs["json"]["definition"]
    assert definition["instructions"] == original["instructions"]
    assert definition["model"] == original["model"]
    assert definition["temperature"] == original["temperature"]
    assert definition["structured_inputs"]["department"] == original["structured_inputs"]["department"]
    assert definition["tools"][0]["container"]["memory_limit"] == "4g"
    assert definition["tools"][0]["container"]["file_ids"] == ["{{analysis_file_id}}"]
    assert original["tools"][0]["container"]["file_ids"] == []


def test_existing_agent_setup_is_idempotent():
    definition = native_definition({
        "kind": "prompt", "model": "customer-model", "instructions": "Customer rules.",
    })
    runner = Mock(project="https://example.services.ai.azure.com/api/projects/test")
    runner.request.return_value = httpx.Response(
        200, json={"data": [{"version": "4", "definition": definition}]},
    )
    version, changed = configure_existing_agent(runner, "customer-agent")
    assert not changed and version["version"] == "4"
    runner.request.assert_called_once()


def test_setup_does_not_silently_replace_existing_tools():
    original = {
        "kind": "prompt", "model": "customer-model", "instructions": "Customer rules.",
        "tools": [{"type": "mcp", "server_label": "existing", "server_url": "https://example.test"}],
    }
    with pytest.raises(PocError, match="--replace-tools"):
        native_definition(original)
    definition = native_definition(original, replace_tools=True)
    assert definition["tools"][0]["type"] == "code_interpreter"
    assert definition["instructions"] == original["instructions"]
