"""Legacy toolbox runner and shared Foundry transport, download, and validation helpers."""

import argparse
import hashlib
import json
import logging
import re
import sys
from collections.abc import Callable
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from azure.core.exceptions import AzureError
from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential
from pypdf import PdfReader

logger = logging.getLogger(__name__)
AGENT_API = "2025-11-15-preview"


class PocError(Exception):
    pass


def validate_project_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip().rstrip("/")
    try:
        url = urlsplit(endpoint)
    except ValueError:
        raise PocError("Invalid Azure Foundry project endpoint.") from None
    if (
        url.scheme != "https" or not url.hostname
        or not url.hostname.endswith(".services.ai.azure.com")
        or not re.fullmatch(r"/api/projects/[^/]+", url.path)
        or url.query or url.fragment or url.netloc != url.hostname
    ):
        raise PocError("Use an HTTPS Azure Foundry project endpoint without a query string.")
    return endpoint


def decode_mcp(response: httpx.Response) -> dict[str, Any]:
    if "text/event-stream" not in response.headers.get("content-type", ""):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            message = json.loads(line[5:].strip())
            if "result" in message or "error" in message:
                return message
    raise PocError("MCP returned no JSON-RPC result.")


def file_citations(response: dict[str, Any]) -> list[dict[str, str]]:
    citations = {}
    for item in response.get("output", []):
        for content in item.get("content", []):
            for annotation in content.get("annotations", []):
                if annotation.get("type") == "container_file_citation":
                    key = (annotation["container_id"], annotation["file_id"])
                    citations[key] = annotation
    return list(citations.values())


def validate_pdf(original: bytes, generated: bytes) -> dict[str, Any]:
    if not generated.startswith(b"%PDF-"):
        raise PocError("Generated PDF does not have a PDF signature.")
    if hashlib.sha256(original).digest() == hashlib.sha256(generated).digest():
        raise PocError("The agent returned the unchanged original.")
    before = PdfReader(BytesIO(original))
    after = PdfReader(BytesIO(generated))
    if len(before.pages) != len(after.pages):
        raise PocError("Generated PDF page count differs from the original.")
    if after.is_encrypted:
        raise PocError("Generated PDF is encrypted and cannot be verified.")
    if any((after.metadata or {}).values()):
        raise PocError("Generated PDF metadata was not cleared.")
    if any(
        list(a.mediabox) != list(b.mediabox) or a.rotation != b.rotation
        for a, b in zip(before.pages, after.pages)
    ):
        raise PocError("Generated PDF page geometry differs from the original.")
    before_text = [page.extract_text() or "" for page in before.pages]
    after_text = [page.extract_text() or "" for page in after.pages]
    if before_text == after_text:
        raise PocError("Extracted text is unchanged; true redaction was not demonstrated.")
    if not any(text.strip() for text in after_text):
        raise PocError("Generated PDF has no extractable text; preservation cannot be checked.")
    return {
        "page_count": len(after.pages),
        "text_changed": True,
        "metadata_cleared": True,
        "page_geometry_preserved": True,
        "full_policy_verification": "not_performed_requires_independent_review",
    }


class FoundryPoc:
    def __init__(
        self, client: httpx.Client, credential: TokenCredential,
        project: str, output_dir: Path,
        *, on_resources_changed: Callable[[], None] | None = None,
    ):
        self.client = client
        self.credential = credential
        self.project = project.rstrip("/")
        self.output_dir = output_dir
        self.toolbox: str | None = None
        self.agent: str | None = None
        self.response_ids: set[str] = set()
        self.containers: set[str] = set()
        self.file_ids: set[str] = set()
        self.on_resources_changed = on_resources_changed

    def resources_changed(self) -> None:
        if self.on_resources_changed:
            self.on_resources_changed()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = (
            "Bearer " + self.credential.get_token("https://ai.azure.com/.default").token
        )
        response = self.client.request(method, url, headers=headers, **kwargs)
        if not response.is_success:
            request_id = response.headers.get("apim-request-id", "unavailable")
            raise PocError(
                f"{method} {urlsplit(url).path}: HTTP {response.status_code}; "
                f"request ID {request_id}. Response body omitted to protect document data."
            )
        return response

    def mcp(self, endpoint: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.request(
            "POST", endpoint,
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        message = decode_mcp(response)
        if "error" in message:
            raise PocError(f"MCP {method} failed; error code {message['error'].get('code')}.")
        result = message["result"]
        if result.get("isError"):
            raise PocError(f"MCP {method} returned a tool execution error.")
        return result

    def run(self, source: Path, source_agent: str) -> dict[str, Any]:
        original = source.read_bytes()
        expected_pages = len(PdfReader(BytesIO(original)).pages)
        source_hash = hashlib.sha256(original).hexdigest()
        versions = self.request(
            "GET", f"{self.project}/agents/{quote(source_agent, safe='')}/versions",
            params={"api-version": AGENT_API, "limit": 1, "order": "desc"},
        ).json()["data"]
        if not versions:
            raise PocError("Source agent has no published version.")
        source_version = versions[0]
        definition = source_version["definition"]
        if definition.get("kind") != "prompt":
            raise PocError("This POC requires a prompt agent.")

        job = uuid4().hex[:12]
        container = self.request(
            "POST", f"{self.project}/openai/v1/containers",
            json={"name": f"redaction-poc-{job}"},
        ).json()
        container_id = container["id"]
        self.containers.add(container_id)
        self.resources_changed()
        with source.open("rb") as file:
            upload = self.request(
                "POST", f"{self.project}/openai/v1/containers/{container_id}/files",
                files={"file": (source.name, file, "application/pdf")},
            ).json()
        mounted_path = upload["path"]
        logger.info("Original PDF uploaded: %s bytes, %s pages.", len(original), expected_pages)

        toolbox_name = f"redaction-poc-tools-{job}"
        toolbox = self.request(
            "POST", f"{self.project}/toolboxes/{toolbox_name}/versions",
            params={"api-version": "v1"},
            json={
                "description": "Temporary single-document redaction POC; no web search.",
                "tools": [{
                    "type": "code_interpreter",
                    "container": container_id,
                }],
            },
        ).json()
        self.toolbox = toolbox_name
        self.resources_changed()
        mcp_url = (
            f"{self.project}/toolboxes/{toolbox_name}/versions/{toolbox['version']}"
            "/mcp?api-version=v1"
        )
        tools = self.mcp(mcp_url, "tools/list", {})["tools"]
        if len(tools) != 1:
            raise PocError("Job toolbox does not expose exactly one execution tool.")
        tool_name = tools[0]["name"]
        probe_code = (
            "from pathlib import Path\n"
            "import hashlib, importlib.util\n"
            f"p = Path({mounted_path!r})\n"
            "assert p.is_file(), 'Bound original is missing'\n"
            "print('BINDING_SHA256=' + hashlib.sha256(p.read_bytes()).hexdigest())\n"
            "print('PYMUPDF_AVAILABLE=' + str(importlib.util.find_spec('fitz') is not None))\n"
        )
        probe = self.mcp(
            mcp_url, "tools/call",
            {"name": tool_name, "arguments": {"code": probe_code}},
        )
        probe_text = json.dumps(probe)
        digest = re.search(r"BINDING_SHA256=([a-f0-9]{64})", probe_text)
        if not digest or digest.group(1) != source_hash:
            raise PocError("Toolbox binding did not expose a byte-identical original PDF.")
        if "PYMUPDF_AVAILABLE=True" not in probe_text:
            raise PocError("Code Interpreter lacks PyMuPDF, required for this redaction POC.")
        logger.info("File binding verified: sandbox SHA-256 matches the original.")

        instructions = definition["instructions"] + (
            "\n\nExecution requirements for this job: Use only the provided Code Interpreter. "
            "Never search the web or send document data to an external service. Infer the subject "
            "from the original PDF header if subject fields are placeholders. Do not output "
            "unmasked third-party details in chat. The original is preloaded at "
            f"{mounted_path}. Use this job's provided container only. "
            "Always reread the bound original as necessary. "
            "Complete final redaction, verification and file saving within the same invocation. "
            f"Save the final PDF as /mnt/data/{source.stem}_Redacted.pdf. "
            "Do not replace the PDF with reconstructed text or images. If blocked, explicitly "
            "report failure. Return actual file citations for the final PDF and preview PNGs."
        )
        agent_name = f"redaction-poc-{job}"
        self.request(
            "POST", f"{self.project}/agents",
            params={"api-version": AGENT_API},
            json={
                "name": agent_name,
                "definition": {
                    "kind": "prompt", "model": definition["model"],
                    "instructions": instructions,
                    "tools": [{
                        "type": "mcp", "server_label": "document_executor",
                        "server_url": mcp_url, "require_approval": "never",
                    }],
                },
            },
        ).json()
        self.agent = agent_name
        self.resources_changed()
        logger.info("Invoking isolated copy of %s version %s.", source_agent, source_version["version"])
        response = self.invoke(agent_name)
        outputs = self.download_outputs(source, original, response)
        return {
            "source_agent": source_agent, "source_version": source_version["version"],
            "model": definition["model"], "binding_sha256_verified": True,
            "shared_agent_modified": False, **outputs,
            "policy_status": "UNREVIEWED: model output is not certified safe for disclosure",
        }

    def download_outputs(
        self, source: Path, original: bytes, response: dict[str, Any],
    ) -> dict[str, Any]:
        citations = file_citations(response)
        download_method = "response_file_citations"
        if not any(item["filename"].lower().endswith(".pdf") for item in citations):
            logger.warning("No PDF citation returned; checking this job's container files.")
            citations = self.generated_files(source)
            download_method = "authenticated_job_container_listing"
        if sum(item["filename"].lower().endswith(".pdf") for item in citations) != 1:
            raise PocError("Agent must generate exactly one downloadable PDF.")

        generated = []
        for index, citation in enumerate(citations):
            if citation["container_id"] not in self.containers:
                raise PocError("Output citation references a container not owned by this job.")
            suffix = Path(citation["filename"]).suffix.lower()
            if suffix not in {".pdf", ".png"}:
                continue
            if suffix == ".pdf" and PurePosixPath(citation["filename"]).name.casefold() != (
                f"{source.stem}_Redacted.pdf".casefold()
            ):
                raise PocError("Output citation does not identify the expected redacted PDF.")
            container = citation["container_id"]
            content = self.request(
                "GET",
                f"{self.project}/openai/v1/containers/{quote(container, safe='')}/files/"
                f"{quote(citation['file_id'], safe='')}/content",
            ).content
            if suffix == ".pdf":
                checks = validate_pdf(original, content)
                name = f"{source.stem}_Redacted.pdf"
            else:
                if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise PocError("Generated preview does not have a PNG signature.")
                checks = {"png_signature_valid": True}
                name = f"{source.stem}_Preview_{index + 1}.png"
            destination = self.output_dir / name
            with destination.open("xb") as file:
                file.write(content)
            generated.append({"file": name, "bytes": len(content), "checks": checks})
        return {"generated": generated, "download_method": download_method}

    def generated_files(self, source: Path) -> list[dict[str, str]]:
        result = []
        expected = f"{source.stem}_Redacted.pdf".casefold()
        for container in self.containers:
            for file in self.container_files(container):
                name = PurePosixPath(file["path"]).name
                if name.casefold() == expected or name.lower().endswith(".png"):
                    result.append({
                        "filename": name, "container_id": container, "file_id": file["id"],
                    })
        return result

    def container_files(self, container: str) -> list[dict[str, Any]]:
        result = []
        after = None
        while True:
            params = {"limit": 100}
            if after:
                params["after"] = after
            page = self.request(
                "GET", f"{self.project}/openai/v1/containers/{container}/files",
                params=params,
            ).json()
            result.extend(page["data"])
            if not page.get("has_more"):
                return result
            if not page.get("data"):
                raise PocError("Container pagination claimed more files but returned none.")
            after = page["data"][-1]["id"]

    def track_execution(self, item: dict[str, Any]) -> None:
        # Native tool metadata is authoritative; never discover IDs in model text.
        if item.get("type") == "code_interpreter_call" and item.get("container_id"):
            container = item["container_id"]
            if container not in self.containers:
                self.containers.add(container)
                self.resources_changed()

    def invoke(
        self, agent_name: str, *, version: str | None = None,
        structured_inputs: dict[str, str] | None = None, input_text: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": "Bearer "
            + self.credential.get_token("https://ai.azure.com/.default").token,
        }
        payload = {
            "agent_reference": {"type": "agent_reference", "name": agent_name},
            "input": input_text or (
                "Read the bound original PDF. Apply all of your redaction rules, create the "
                "truly redacted PDF with cleared metadata and previews, verify it, and return "
                "downloadable file citations. Do not stop after analysis. If unable, report failure."
            ),
            "stream": True,
            "max_output_tokens": 32000,
        }
        if version is not None:
            payload["agent_reference"]["version"] = version
        if structured_inputs is not None:
            payload["structured_inputs"] = structured_inputs
        final = None
        with self.client.stream(
            "POST", f"{self.project}/openai/v1/responses", headers=headers, json=payload,
        ) as response:
            if not response.is_success:
                response.read()
                raise PocError(f"Agent invocation failed: HTTP {response.status_code}.")
            for line in response.iter_lines():
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                event = json.loads(line[5:].strip())
                kind = event.get("type")
                if kind in {"response.created", "response.completed",
                            "response.failed", "response.incomplete"}:
                    result = event["response"]
                    if result.get("id"):
                        self.response_ids.add(result["id"])
                        self.resources_changed()
                    for item in result.get("output", []):
                        self.track_execution(item)
                    logger.info("Agent status: %s.", result.get("status"))
                    if kind != "response.created":
                        final = result
                elif kind in {"response.output_item.added", "response.output_item.done"}:
                    item = event.get("item", {})
                    self.track_execution(item)
                    if kind == "response.output_item.done":
                        logger.info("Completed output item: %s.", item.get("type"))
                elif kind == "error":
                    raise PocError("Agent stream reported an error; sensitive body omitted.")
        if final is None or final.get("status") != "completed":
            raise PocError("Agent did not complete successfully.")
        return final

    def cleanup(self) -> list[str]:
        failures = []
        urls = [
            f"{self.project}/openai/v1/responses/{quote(item, safe='')}"
            for item in self.response_ids
        ]
        if self.agent:
            urls.append(f"{self.project}/agents/{self.agent}?api-version={AGENT_API}")
        if self.toolbox:
            urls.append(f"{self.project}/toolboxes/{self.toolbox}?api-version=v1")
        urls.extend(
            f"{self.project}/openai/v1/containers/{quote(item, safe='')}"
            for item in self.containers
        )
        urls.extend(
            f"{self.project}/openai/v1/files/{quote(item, safe='')}"
            for item in self.file_ids
        )
        for url in urls:
            try:
                response = self.client.delete(url, headers={
                    "Authorization": "Bearer "
                    + self.credential.get_token("https://ai.azure.com/.default").token,
                })
                if response.status_code not in {200, 202, 204, 404, 410}:
                    failures.append(f"{urlsplit(url).path}: HTTP {response.status_code}")
            except (httpx.HTTPError, AzureError) as error:
                failures.append(f"{urlsplit(url).path}: {type(error).__name__}")
        return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--agent", default="sac-redaction")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        args.project_endpoint = validate_project_endpoint(args.project_endpoint)
    except PocError as error:
        parser.error(str(error))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if (args.output_dir / f"{args.input.stem}_Redacted.pdf").exists():
        parser.error("Output PDF already exists; choose an empty output directory.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    report: dict[str, Any] = {"status": "failed"}
    exit_code = 1
    with AzureCliCredential() as credential, httpx.Client(
        timeout=httpx.Timeout(1800, connect=30),
    ) as client:
        poc = FoundryPoc(
            client, credential, args.project_endpoint, args.output_dir,
        )
        try:
            report.update(poc.run(args.input, args.agent))
            report["status"] = "generated_unreviewed"
            exit_code = 0
        except (PocError, httpx.HTTPError, AzureError) as error:
            logger.error("%s", str(error) if isinstance(error, PocError) else type(error).__name__)
            report["error"] = str(error) if isinstance(error, PocError) else type(error).__name__
        finally:
            cleanup_errors = poc.cleanup()
            report["cleanup_errors"] = cleanup_errors
            if cleanup_errors:
                logger.error("Remote cleanup was incomplete: %s", "; ".join(cleanup_errors))
                exit_code = 1
            (args.output_dir / "foundry-poc-result.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8",
            )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
