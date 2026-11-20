"""Native Files API binding for a retained, parameterized Foundry prompt agent."""

import argparse
import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from azure.identity import DefaultAzureCredential

from foundry_poc import AGENT_API, FoundryPoc, PocError, validate_project_endpoint

NATIVE_TOOL = {
    "type": "code_interpreter",
    "container": {"type": "auto", "file_ids": ["{{analysis_file_id}}"]},
}


def native_definition(
    source: dict[str, Any], *, replace_tools: bool = False,
) -> dict[str, Any]:
    if source.get("kind") != "prompt":
        raise PocError("The existing agent must be a prompt agent.")
    tools = source.get("tools", [])
    if tools and (len(tools) != 1 or tools[0].get("type") != "code_interpreter"):
        if not replace_tools:
            raise PocError(
                "The agent has other tools. This workflow requires native Code Interpreter "
                "only. Use --replace-tools to explicitly replace its tools in the new version."
            )
        tools = []
    definition = deepcopy(source)
    tool = deepcopy(tools[0] if tools else NATIVE_TOOL)
    container = tool.get("container")
    tool["container"] = {
        **(container if isinstance(container, dict) else {}),
        "type": "auto", "file_ids": ["{{analysis_file_id}}"],
    }
    definition["tools"] = [tool]
    inputs = definition.setdefault("structured_inputs", {})
    binding = inputs.setdefault("analysis_file_id", {})
    binding.setdefault("description", "Original file uploaded for this request.")
    binding.update(required=True, schema={"type": "string"})
    return definition


def configure_existing_agent(
    runner: FoundryPoc, agent_name: str, *, replace_tools: bool = False,
) -> tuple[dict[str, Any], bool]:
    url = f"{runner.project}/agents/{quote(agent_name, safe='')}/versions"
    versions = runner.request(
        "GET", url, params={"api-version": AGENT_API, "limit": 1, "order": "desc"},
    ).json()["data"]
    if not versions:
        raise PocError("The existing agent has no published version.")
    current = versions[0]
    definition = native_definition(current["definition"], replace_tools=replace_tools)
    if definition == current["definition"]:
        return current, False
    version = runner.request(
        "POST", url, params={"api-version": AGENT_API},
        json={"definition": definition},
    ).json()
    return version, True


class NativeFoundry(FoundryPoc):
    def run(self, source: Path, source_agent: str) -> dict[str, Any]:
        versions = self.request(
            "GET", f"{self.project}/agents/{quote(source_agent, safe='')}/versions",
            params={"api-version": AGENT_API, "limit": 1, "order": "desc"},
        ).json()["data"]
        if not versions:
            raise PocError("The retained native agent has no published version.")
        version = versions[0]
        definition = version["definition"]
        tools = definition.get("tools", [])
        container = tools[0].get("container") if len(tools) == 1 else None
        if (
            definition.get("kind") != "prompt" or len(tools) != 1
            or tools[0].get("type") != "code_interpreter"
            or not isinstance(container, dict) or container.get("type") != "auto"
            or container.get("file_ids") != ["{{analysis_file_id}}"]
            or not definition.get("structured_inputs", {}).get("analysis_file_id", {}).get(
                "required"
            )
        ):
            raise PocError("Configure this existing agent with runtime analysis_file_id binding.")
        original = source.read_bytes()
        with source.open("rb") as file:
            upload = self.request(
                "POST", f"{self.project}/openai/v1/files",
                data={"purpose": "assistants"},
                files={"file": (source.name, file, "application/pdf")},
            ).json()
        file_id = upload["id"]
        self.file_ids.add(file_id)
        self.resources_changed()
        mounted_path = f"/mnt/data/{file_id}-{source.name}"
        response = self.invoke(
            source_agent, version=version["version"],
            structured_inputs={"analysis_file_id": file_id},
            input_text=(
                f"Read the attached original PDF at {mounted_path}. Apply all redaction rules. "
                "Use only native Code Interpreter and PyMuPDF; explicitly fail if PyMuPDF is "
                "unavailable. Never modify the bound original. Infer the subject from its "
                "header if subject fields are placeholders. Do not print unmasked third-party "
                "details in chat. Preserve the PDF rather than reconstructing it as text or images. "
                f"Save the truly redacted PDF as /mnt/data/{source.stem}_Redacted.pdf, with "
                "cleared metadata and preview PNGs. Verify and return downloadable file "
                "citations. Do not stop after analysis. If unable, report failure."
            ),
        )
        if len(self.containers) != 1:
            raise PocError("Native execution did not identify one dedicated container.")
        container = next(iter(self.containers))
        originals = [
            item for item in self.container_files(container)
            if item["path"] == mounted_path and item.get("source") == "user"
        ]
        if len(originals) != 1:
            raise PocError("The original was not mounted in the native execution container.")
        bound = self.request(
            "GET", f"{self.project}/openai/v1/containers/{quote(container, safe='')}/files/"
            f"{quote(originals[0]['id'], safe='')}/content",
        ).content
        if hashlib.sha256(bound).digest() != hashlib.sha256(original).digest():
            raise PocError("The native container's original differs from the uploaded bytes.")
        outputs = self.download_outputs(source, original, response)
        return {
            "source_agent": source_agent, "source_version": version["version"],
            "model": definition["model"], "execution_mode": "native",
            "binding_sha256_verified": True, "shared_agent_modified": False, **outputs,
            "policy_status": "UNREVIEWED: model output is not certified safe for disclosure",
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure native file binding on a new version of an existing prompt agent.",
    )
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--agent", default="sac-redaction")
    parser.add_argument(
        "--replace-tools", action="store_true",
        help="Explicitly replace non-native tools with native Code Interpreter in the new version.",
    )
    args = parser.parse_args()
    try:
        endpoint = validate_project_endpoint(args.project_endpoint)
    except PocError as error:
        parser.error(str(error))
    with DefaultAzureCredential() as credential, httpx.Client(timeout=60) as client:
        runner = FoundryPoc(client, credential, endpoint, Path("."))
        version, changed = configure_existing_agent(
            runner, args.agent, replace_tools=args.replace_tools,
        )
        action = "Configured" if changed else "Already configured"
        print(f"{action}: existing agent {args.agent}, version {version['version']}. "
              "No separate agent created; instructions and model preserved.")


if __name__ == "__main__":
    main()
