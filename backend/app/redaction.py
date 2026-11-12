import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import httpx
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContainerSasPermissions,
    ContentSettings,
    generate_blob_sas,
    generate_container_sas,
)
from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError

from app.auth import User

logger = logging.getLogger(__name__)
MAX_PDF_BYTES = 10_000_000
API_VERSION = "2026-05-01"


class RedactionError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    language_endpoint: str
    language_key: str = field(repr=False)
    account_name: str
    account_key: str = field(repr=False)
    originals_container: str
    redacted_container: str

    @property
    def storage_url(self) -> str:
        return f"https://{self.account_name}.blob.core.windows.net"

    @classmethod
    def from_env(cls) -> "Settings":
        names = [
            "AZURE_LANGUAGE_ENDPOINT",
            "AZURE_LANGUAGE_KEY",
            "AZURE_STORAGE_ACCOUNT_NAME",
            "AZURE_STORAGE_ACCOUNT_KEY",
            "AZURE_STORAGE_ORIGINALS_CONTAINER",
            "AZURE_STORAGE_REDACTED_CONTAINER",
        ]
        values = [os.environ.get(name, "").strip() for name in names]
        if not all(values):
            logger.error("Missing Azure app settings: %s", ", ".join(
                name for name, value in zip(names, values) if not value
            ))
            raise HTTPException(503, "Backend Azure settings are not configured.")
        endpoint, language_key, account, account_key, originals, redacted = values
        url = urlsplit(endpoint)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path.rstrip("/")
            or not re.fullmatch(r"[a-z0-9]{3,24}", account)
            or originals == redacted
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61})[a-z0-9]", c)
                   or "--" in c for c in (originals, redacted))
        ):
            logger.error("Invalid Azure endpoint, storage account, or container app settings.")
            raise HTTPException(503, "Backend Azure settings are invalid.")
        return cls(endpoint.rstrip("/"), language_key, account, account_key, originals, redacted)


class Job(BaseModel):
    id: UUID
    status: Literal["running", "succeeded", "failed"]
    message: str | None = None
    warning_count: int = 0


class Record(BaseModel):
    id: UUID
    language_job_id: UUID
    status: Literal["running", "succeeded", "failed"] = "running"
    redacted_blob: str | None = None
    warning_count: int = 0


class Location(BaseModel):
    location: str


class DocumentResult(BaseModel):
    id: str
    target: list[Location]
    warnings: list[dict] = Field(default_factory=list)


class TaskResults(BaseModel):
    documents: list[DocumentResult] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)


class Task(BaseModel):
    kind: str
    status: str
    results: TaskResults | None = None


class Tasks(BaseModel):
    items: list[Task] = Field(default_factory=list)
    failed: int = 0


class LanguageResult(BaseModel):
    status: str
    errors: list[dict] = Field(default_factory=list)
    tasks: Tasks | None = None


class RedactionService:
    def __init__(self, settings: Settings, storage: BlobServiceClient, http: httpx.Client):
        self.settings = settings
        self.storage = storage
        self.http = http

    def _record_blob(self, user: User, document_id: UUID):
        return self.storage.get_blob_client(
            self.settings.originals_container, f"{user.storage_prefix}/{document_id}/job.json"
        )

    def _save(self, user: User, record: Record) -> None:
        self._record_blob(user, record.id).upload_blob(
            record.model_dump_json(),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
        )

    def _load(self, user: User, document_id: UUID) -> Record:
        try:
            data = self._record_blob(user, document_id).download_blob().readall()
        except ResourceNotFoundError:
            raise HTTPException(404, "Document not found.") from None
        try:
            record = Record.model_validate_json(data)
        except ValidationError:
            raise RedactionError("Stored job record is invalid.") from None
        if record.id != document_id:
            raise RedactionError("Stored job identity is invalid.")
        return record

    def submit(self, user: User, pdf: bytes) -> Job:
        document_id = uuid4()
        prefix = f"{user.storage_prefix}/{document_id}"
        source_name = f"{prefix}/document.pdf"
        source = self.storage.get_blob_client(self.settings.originals_container, source_name)
        source.upload_blob(
            pdf, overwrite=False, content_settings=ContentSettings(content_type="application/pdf")
        )
        now = datetime.now(timezone.utc)
        sas_options = {
            "account_name": self.settings.account_name,
            "account_key": self.settings.account_key,
            "start": now - timedelta(minutes=5),
            "expiry": now + timedelta(hours=1),
            "protocol": "https",
        }
        source_sas = generate_blob_sas(
            container_name=self.settings.originals_container,
            blob_name=source_name,
            permission=BlobSasPermissions(read=True),
            **sas_options,
        )
        target_sas = generate_container_sas(
            container_name=self.settings.redacted_container,
            permission=ContainerSasPermissions(write=True, create=True, list=True),
            **sas_options,
        )
        response = self.http.post(
            f"{self.settings.language_endpoint}/language/analyze-documents/jobs",
            params={"api-version": API_VERSION},
            json={
                "analysisInput": {
                    "documents": [{
                        "id": str(document_id),
                        "language": "en",
                        "source": {"kind": "AzureBlob", "location": f"{source.url}?{source_sas}"},
                        "target": {
                            "kind": "AzureContainerFolder",
                            "location": (
                                f"{self.settings.storage_url}/{self.settings.redacted_container}/"
                                f"{prefix}?{target_sas}"
                            ),
                        },
                    }],
                },
                "tasks": [{
                    "kind": "PiiEntityRecognition",
                    "taskName": "redact",
                    "parameters": {
                        "piiCategories": ["All"],
                        "loggingOptOut": True,
                        "redactionPolicies": [{
                            "policyKind": "characterMask",
                            "redactionCharacter": "*",
                            "isDefault": True,
                        }],
                    },
                }],
            },
        )
        response.raise_for_status()
        if response.status_code != 202:
            raise RedactionError("Language did not accept the redaction job.")
        operation = urlsplit(response.headers.get("operation-location", ""))
        expected = urlsplit(self.settings.language_endpoint)
        path_prefix = "/language/analyze-documents/jobs/"
        if (
            operation.scheme != expected.scheme
            or operation.netloc != expected.netloc
            or not operation.path.startswith(path_prefix)
        ):
            raise RedactionError("Language returned an invalid operation location.")
        try:
            language_job_id = UUID(operation.path.removeprefix(path_prefix))
        except ValueError:
            raise RedactionError("Language returned an invalid job identity.") from None
        record = Record(id=document_id, language_job_id=language_job_id)
        self._save(user, record)
        return Job(id=document_id, status="running")

    def _output_blob(self, location: str, user: User, document_id: UUID) -> str:
        url = urlsplit(location)
        storage = urlsplit(self.settings.storage_url)
        path = unquote(url.path)
        root = f"/{self.settings.redacted_container}/"
        prefix = f"{root}{user.storage_prefix}/{document_id}/"
        if (
            url.scheme != "https"
            or url.netloc != storage.netloc
            or not path.startswith(prefix)
            or not path.lower().endswith(".pdf")
            or any(part in (".", "..") for part in path.split("/"))
        ):
            raise RedactionError("Language returned an unexpected output location.")
        return path.removeprefix(root)

    def status(self, user: User, document_id: UUID) -> Job:
        record = self._load(user, document_id)
        if record.status == "running":
            response = self.http.get(
                f"{self.settings.language_endpoint}/language/analyze-documents/jobs/"
                f"{record.language_job_id}",
                params={"api-version": API_VERSION},
            )
            if response.status_code == 404:
                record.status = "failed"
            else:
                response.raise_for_status()
                try:
                    result = LanguageResult.model_validate(response.json())
                except (ValidationError, ValueError):
                    raise RedactionError("Language returned an invalid status response.") from None
                if result.status in ("failed", "cancelled", "rejected") or result.errors:
                    record.status = "failed"
                elif result.status == "succeeded":
                    tasks = result.tasks
                    if (
                        tasks is None or tasks.failed or len(tasks.items) != 1
                        or tasks.items[0].kind != "PiiEntityRecognitionLROResults"
                        or tasks.items[0].status != "succeeded"
                        or tasks.items[0].results is None
                    ):
                        raise RedactionError("Language returned incomplete task results.")
                    task_result = tasks.items[0].results
                    if task_result.errors:
                        record.status = "failed"
                    else:
                        documents = task_result.documents
                        if len(documents) != 1 or documents[0].id != str(document_id):
                            raise RedactionError("Language returned unexpected document results.")
                        outputs = [
                            target.location for target in documents[0].target
                            if urlsplit(target.location).path.lower().endswith(".pdf")
                        ]
                        if len(outputs) != 1:
                            raise RedactionError("Language did not return one redacted PDF.")
                        record.redacted_blob = self._output_blob(outputs[0], user, document_id)
                        record.warning_count = len(documents[0].warnings)
                        record.status = "succeeded"
                elif result.status not in ("notStarted", "running", "cancelling"):
                    raise RedactionError("Language returned an unknown job status.")
            if record.status != "running":
                self._save(user, record)
        return Job(
            id=record.id,
            status=record.status,
            warning_count=record.warning_count,
            message=(
                "Redaction failed or the job expired. Try another PDF or contact the administrator."
                if record.status == "failed" else None
            ),
        )

    def download(self, user: User, document_id: UUID) -> bytes:
        record = self._load(user, document_id)
        if record.status != "succeeded" or not record.redacted_blob:
            raise HTTPException(409, "The redacted document is not ready.")
        # Revalidate persisted paths before reading with the account credential.
        blob_name = self._output_blob(
            f"{self.settings.storage_url}/{self.settings.redacted_container}/{record.redacted_blob}",
            user, document_id,
        )
        try:
            return self.storage.get_blob_client(
                self.settings.redacted_container, blob_name
            ).download_blob().readall()
        except ResourceNotFoundError:
            raise HTTPException(404, "The redacted document is no longer available.") from None


def get_redaction_service() -> Iterator[RedactionService]:
    settings = Settings.from_env()
    with (
        BlobServiceClient(
            settings.storage_url, credential=settings.account_key, logging_enable=False
        ) as storage,
        httpx.Client(
            headers={"Ocp-Apim-Subscription-Key": settings.language_key},
            timeout=30,
            follow_redirects=False,
        ) as http,
    ):
        yield RedactionService(settings, storage, http)
