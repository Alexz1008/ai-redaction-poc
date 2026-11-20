import hashlib
import json
from io import BytesIO
from unittest.mock import Mock

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from foundry_poc import FoundryPoc, PocError, decode_mcp, file_citations, validate_pdf


@pytest.fixture
def credential():
    value = Mock()
    value.get_token.return_value.token = "synthetic-token"
    return value


def pdf(text):
    writer = PdfWriter()
    writer.metadata = None
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }),
        }),
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = stream
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


@pytest.mark.parametrize("generated_output", [False, True])
def test_direct_container_binding_precedes_model_invocation(
    tmp_path, credential, generated_output,
):
    original = pdf("synthetic original")
    source = tmp_path / "input.pdf"
    source.write_bytes(original)
    calls = []

    def handle(request):
        body = json.loads(request.content) if request.headers.get(
            "content-type", ""
        ).startswith("application/json") else None
        path = request.url.path
        calls.append((request.method, path))
        if path.endswith("/agents/source/versions"):
            return httpx.Response(200, json={"data": [{
                "version": "2",
                "definition": {"kind": "prompt", "model": "test-model", "instructions": "rules"},
            }]})
        if path.endswith("/containers"):
            return httpx.Response(200, json={"id": "cntr_job"})
        if path.endswith("/containers/cntr_job/files"):
            if request.method == "POST":
                assert original in request.content
                return httpx.Response(200, json={"path": "/mnt/data/input.pdf"})
            return httpx.Response(200, json={"data": [{
                "id": "file_output", "path": "/mnt/data/input_Redacted.pdf",
            }] if generated_output else [], "has_more": False})
        if path.endswith("/files/file_output/content"):
            return httpx.Response(200, content=pdf("synthetic redacted"))
        if "/toolboxes/" in path and path.endswith("/versions"):
            assert body["tools"] == [{"type": "code_interpreter", "container": "cntr_job"}]
            return httpx.Response(200, json={"version": "1"})
        if path.endswith("/mcp"):
            if body["method"] == "tools/list":
                return httpx.Response(200, json={"result": {"tools": [{"name": "execute"}]}})
            assert "/mnt/data/input.pdf" in body["params"]["arguments"]["code"]
            return httpx.Response(200, json={"result": {"content": [{
                "type": "text",
                "text": (
                    "BINDING_SHA256=" + hashlib.sha256(original).hexdigest()
                    + "\nPYMUPDF_AVAILABLE=True"
                ),
            }]}})
        if path.endswith("/agents"):
            assert body["definition"]["model"] == "test-model"
            assert [tool["type"] for tool in body["definition"]["tools"]] == ["mcp"]
            assert "web_search" not in json.dumps(body)
            return httpx.Response(200, json={"name": body["name"]})
        pytest.fail(f"Unexpected request: {request.method} {path}")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        poc = FoundryPoc(client, credential, "https://example.services.ai.azure.com/api/projects/test",
                         tmp_path)
        poc.invoke = Mock(return_value={"status": "completed", "output": []})
        if generated_output:
            report = poc.run(source, "source")
            assert report["download_method"] == "authenticated_job_container_listing"
            assert (tmp_path / "input_Redacted.pdf").read_bytes() == pdf("synthetic redacted")
            assert report["generated"][0]["checks"]["metadata_cleared"]
        else:
            with pytest.raises(PocError, match="exactly one downloadable PDF"):
                poc.run(source, "source")
        poc.invoke.assert_called_once()
        assert any(path.endswith("/containers/cntr_job/files") for _, path in calls)
        assert all("/agents/source" not in path for method, path in calls if method != "GET")


def test_binding_hash_mismatch_never_invokes_model(tmp_path, credential):
    source = tmp_path / "input.pdf"
    source.write_bytes(pdf("synthetic original"))
    with httpx.Client() as client:
        poc = FoundryPoc(client, credential, "https://example.services.ai.azure.com/api/projects/test",
                         tmp_path)
        poc.request = Mock(side_effect=[
            httpx.Response(200, json={"data": [{
                "version": "1",
                "definition": {"kind": "prompt", "model": "test-model", "instructions": "rules"},
            }]}),
            httpx.Response(200, json={"id": "cntr_job"}),
            httpx.Response(200, json={"path": "/mnt/data/input.pdf"}),
            httpx.Response(200, json={"version": "1"}),
        ])
        poc.mcp = Mock(side_effect=[
            {"tools": [{"name": "execute"}]},
            {"content": [{"text": "BINDING_SHA256=" + "0" * 64}]},
        ])
        poc.invoke = Mock()
        with pytest.raises(PocError, match="byte-identical original"):
            poc.run(source, "source")
        poc.invoke.assert_not_called()


def test_generated_pdf_checks_reject_non_redaction_and_label_unreviewed():
    original = pdf("synthetic original")
    with pytest.raises(PocError, match="unchanged original"):
        validate_pdf(original, original)
    with pytest.raises(PocError, match="PDF signature"):
        validate_pdf(original, b"not a PDF")
    checks = validate_pdf(original, pdf("synthetic redacted"))
    assert checks["full_policy_verification"] == "not_performed_requires_independent_review"


def test_citations_and_mcp_sse():
    annotation = {
        "type": "container_file_citation", "container_id": "cntr_job",
        "file_id": "file_job", "filename": "../../redacted.pdf",
    }
    response = {"output": [{"content": [{"annotations": [annotation, annotation]}]}]}
    assert file_citations(response) == [annotation]
    rpc = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    stream = httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        text=f": heartbeat\n\nevent: message\ndata: {json.dumps(rpc)}\n\n",
    )
    assert decode_mcp(stream) == rpc


def test_container_download_discovery_excludes_original_and_follows_pagination(
    tmp_path, credential,
):
    with httpx.Client() as client:
        poc = FoundryPoc(client, credential, "https://example.services.ai.azure.com/api/projects/test",
                         tmp_path)
        poc.containers.add("cntr_job")
        poc.request = Mock(side_effect=[
            httpx.Response(200, json={"data": [
                {"id": "input", "path": "/mnt/data/input.pdf"},
            ], "has_more": True}),
            httpx.Response(200, json={"data": [
                {"id": "output", "path": "/mnt/data/input_Redacted.pdf"},
                {"id": "preview", "path": "/mnt/data/preview.png"},
            ], "has_more": False}),
        ])
        files = poc.generated_files(tmp_path / "input.pdf")
    assert [file["file_id"] for file in files] == ["output", "preview"]
    assert poc.request.call_args.kwargs["params"]["after"] == "input"


def test_cleanup_attempts_remaining_resources_after_a_failure(tmp_path, credential):
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(500 if "/responses/" in request.url.path else 204)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        poc = FoundryPoc(client, credential, "https://example.services.ai.azure.com/api/projects/test",
                         tmp_path)
        poc.response_ids.add("response_job")
        poc.agent = "job-agent"
        poc.toolbox = "job-tools"
        poc.containers.add("cntr_job")
        errors = poc.cleanup()
    assert len(errors) == 1
    assert len(paths) == 4
    assert any(path.endswith("/containers/cntr_job") for path in paths)
