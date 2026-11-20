import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import httpx
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.storage.blob import (
    BlobSasPermissions,
    BlobLeaseClient,
    BlobServiceClient,
    ContainerSasPermissions,
    ContentSettings,
    generate_blob_sas,
    generate_container_sas,
)
from fastapi import BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, ValidationError
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

from app.auth import User

logger = logging.getLogger(__name__)
MAX_PDF_BYTES = 10_000_000
API_VERSION = "2026-05-01"
CHUNK_TEXT_ELEMENTS = 90_000


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
    def from_env(cls, *, require_language: bool = True) -> "Settings":
        names = [
            "AZURE_LANGUAGE_ENDPOINT",
            "AZURE_LANGUAGE_KEY",
            "AZURE_STORAGE_ACCOUNT_NAME",
            "AZURE_STORAGE_ACCOUNT_KEY",
            "AZURE_STORAGE_ORIGINALS_CONTAINER",
            "AZURE_STORAGE_REDACTED_CONTAINER",
        ]
        values = [os.environ.get(name, "").strip() for name in names]
        required = [0, 2, 4, 5] if require_language else [2, 4, 5]
        if any(not values[index] for index in required):
            logger.error("Missing Azure app settings: %s", ", ".join(
                names[index] for index in required if not values[index]
            ))
            raise HTTPException(503, "Backend Azure settings are not configured.")
        endpoint, language_key, account, account_key, originals, redacted = values
        if require_language and bool(language_key) != bool(account_key):
            raise HTTPException(503, "Configure both Azure keys or use managed identity without keys.")
        url = urlsplit(endpoint)
        if (
            (require_language and (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path.rstrip("/")
            ))
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


class Chunk(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    start: int
    end: int
    language_job_id: UUID | None = None
    status: Literal["running", "succeeded", "failed"] = "running"
    redacted_blob: str | None = None
    warning_count: int = 0


class Record(BaseModel):
    id: UUID
    provider: Literal["language", "foundry"] = "language"
    language_job_id: UUID | None = None
    status: Literal["running", "succeeded", "failed"] = "running"
    redacted_blob: str | None = None
    warning_count: int = 0
    page_count: int | None = None
    chunks: list[Chunk] = Field(default_factory=list)
    message: str | None = None
    cleanup_pending: bool = False


class Location(BaseModel):
    location: str


class DocumentResult(BaseModel):
    id: str
    targets: list[Location]
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

    def _save(self, user: User, record: Record, lease: BlobLeaseClient | None = None) -> None:
        self._record_blob(user, record.id).upload_blob(
            record.model_dump_json(),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
            lease=lease,
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
        if record.provider != "language":
            raise HTTPException(409, "This document requires the Foundry redaction backend.")
        if not record.chunks and record.language_job_id:
            record.chunks = [Chunk(
                id=record.id, start=0, end=0, language_job_id=record.language_job_id
            )]
        return record

    def submit(self, user: User, pdf: bytes) -> Job:
        reader = self._reader(pdf, uploaded=True)
        ranges = []
        start = 0
        size = 0
        for index, page in enumerate(reader.pages):
            try:
                # UTF-16 units deliberately overestimate many Unicode text elements.
                estimate = len((page.extract_text() or "").encode("utf-16-le")) // 2
            except (PyPdfError, ValueError, RecursionError):
                raise HTTPException(415, "PDF text could not be read.") from None
            if index > start and size + estimate > CHUNK_TEXT_ELEMENTS:
                ranges.append(Chunk(start=start, end=index))
                start, size = index, 0
            size += estimate
        ranges.append(Chunk(start=start, end=len(reader.pages)))
        document_id = uuid4()
        prefix = f"{user.storage_prefix}/{document_id}"
        source_name = f"{prefix}/document.pdf"
        source = self.storage.get_blob_client(self.settings.originals_container, source_name)
        source.upload_blob(
            pdf, overwrite=False, content_settings=ContentSettings(content_type="application/pdf")
        )
        record = Record(id=document_id, page_count=len(reader.pages), chunks=ranges)
        self._save(user, record)
        return self._job(record)

    @staticmethod
    def _reader(pdf: bytes, uploaded: bool = False) -> PdfReader:
        try:
            reader = PdfReader(BytesIO(pdf))
            if reader.is_encrypted or not reader.pages:
                raise ValueError("Encrypted or empty PDF")
            return reader
        except (PyPdfError, ValueError, RecursionError):
            if uploaded:
                raise HTTPException(415, "Choose a readable, unencrypted PDF with pages.") from None
            raise RedactionError("Stored PDF could not be read.") from None

    def _submit_chunk(self, user: User, record: Record, chunk: Chunk) -> UUID:
        prefix = f"{user.storage_prefix}/{record.id}"
        original = self.storage.get_blob_client(
            self.settings.originals_container, f"{prefix}/document.pdf"
        ).download_blob().readall()
        reader = self._reader(original)
        writer = PdfWriter()
        for page in reader.pages[chunk.start:chunk.end]:
            writer.add_page(page)
        buffer = BytesIO()
        writer.write(buffer)
        source_name = f"{prefix}/chunks/{chunk.id}.pdf"
        source = self.storage.get_blob_client(self.settings.originals_container, source_name)
        source.upload_blob(
            buffer.getvalue(), overwrite=True,
            content_settings=ContentSettings(content_type="application/pdf"),
        )
        source_location = source.url
        target_location = (
            f"{self.settings.storage_url}/{self.settings.redacted_container}/"
            f"{prefix}/chunks/{chunk.id}"
        )
        now = datetime.now(timezone.utc)
        sas_options = {
            "account_name": self.settings.account_name,
            "account_key": self.settings.account_key,
            "start": now - timedelta(minutes=5),
            "expiry": now + timedelta(hours=1),
            "protocol": "https",
        }
        if self.settings.account_key:
            source_sas = generate_blob_sas(
                container_name=self.settings.originals_container, blob_name=source_name,
                permission=BlobSasPermissions(read=True), **sas_options,
            )
            target_sas = generate_container_sas(
                container_name=self.settings.redacted_container,
                permission=ContainerSasPermissions(write=True, create=True, list=True),
                **sas_options,
            )
            source_location += f"?{source_sas}"
            target_location += f"?{target_sas}"
        response = self.http.post(
            f"{self.settings.language_endpoint}/language/analyze-documents/jobs",
            params={"api-version": API_VERSION},
            json={
                "analysisInput": {
                    "documents": [{
                        "id": str(chunk.id),
                        "language": "en",
                        "source": {"kind": "AzureBlob", "location": source_location},
                        "target": {
                            "kind": "AzureContainerFolder",
                            "location": target_location,
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
        return language_job_id

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
        if record.status != "running" and not record.cleanup_pending:
            return self._job(record)
        blob = self._record_blob(user, document_id)
        try:
            lease = blob.acquire_lease(lease_duration=60)
        except HttpResponseError as error:
            if getattr(error, "error_code", None) != "LeaseAlreadyPresent":
                raise
            return self._job(self._load(user, document_id))
        try:
            record = self._load(user, document_id)
            if record.status == "running":
                chunk = next((c for c in record.chunks if c.status == "running"), None)
                if chunk is None:
                    self._merge(user, record)
                elif chunk.language_job_id is None:
                    chunk.language_job_id = self._submit_chunk(user, record, chunk)
                else:
                    self._poll_chunk(user, record, chunk)
                if record.status != "running" and record.page_count is not None:
                    record.cleanup_pending = True
                lease.renew()
                self._save(user, record, lease)
            if record.cleanup_pending:
                self._cleanup(user, record, lease)
                record.cleanup_pending = False
                self._save(user, record, lease)
        finally:
            lease.release()
        return self._job(record)

    @staticmethod
    def _job(record: Record) -> Job:
        message = record.message
        if record.status == "failed" and not message:
            message = "Redaction failed or the job expired. Try another PDF or contact the administrator."
        elif record.status == "running":
            completed = sum(c.status == "succeeded" for c in record.chunks)
            message = f"Redacted {completed} of {len(record.chunks)} parts. Keep this page open."
        return Job(
            id=record.id,
            status=record.status,
            warning_count=record.warning_count,
            message=message,
        )

    def _poll_chunk(self, user: User, record: Record, chunk: Chunk) -> None:
        response = self.http.get(
            f"{self.settings.language_endpoint}/language/analyze-documents/jobs/"
            f"{chunk.language_job_id}", params={"api-version": API_VERSION},
        )
        if response.status_code == 404:
            record.status = "failed"
            return
        response.raise_for_status()
        try:
            result = LanguageResult.model_validate(response.json())
        except (ValidationError, ValueError):
            raise RedactionError("Language returned an invalid status response.") from None
        if result.status in ("failed", "cancelled", "rejected") or result.errors:
            if result.status == "cancelled" or not self._retry_size(record, chunk, result.errors):
                record.status = "failed"
            return
        if result.status in ("notStarted", "running", "cancelling"):
            return
        if result.status != "succeeded":
            raise RedactionError("Language returned an unknown job status.")
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
            if not self._retry_size(record, chunk, task_result.errors):
                record.status = "failed"
            return
        documents = task_result.documents
        if len(documents) != 1 or documents[0].id != str(chunk.id):
            raise RedactionError("Language returned unexpected document results.")
        outputs = [
            target.location for target in documents[0].targets
            if urlsplit(target.location).path.lower().endswith(".pdf")
        ]
        if len(outputs) != 1:
            raise RedactionError("Language did not return one redacted PDF.")
        chunk.redacted_blob = self._output_blob(outputs[0], user, record.id)
        if record.page_count is not None:
            expected = f"{user.storage_prefix}/{record.id}/chunks/{chunk.id}/"
            if not chunk.redacted_blob.startswith(expected):
                raise RedactionError("Language returned another chunk's output.")
        chunk.warning_count = len(documents[0].warnings)
        chunk.status = "succeeded"
        if all(c.status == "succeeded" for c in record.chunks):
            self._merge(user, record)

    def _retry_size(self, record: Record, chunk: Chunk, errors: list[dict]) -> bool:
        if record.page_count is None or not self._size_error(errors):
            return False
        if chunk.end - chunk.start == 1:
            record.status = "failed"
            record.message = (
                f"Page {chunk.start + 1} exceeds Azure's document text limit "
                "and cannot be split automatically."
            )
        else:
            middle = (chunk.start + chunk.end) // 2
            index = record.chunks.index(chunk)
            record.chunks[index:index + 1] = [
                Chunk(start=chunk.start, end=middle),
                Chunk(start=middle, end=chunk.end),
            ]
        return True

    @staticmethod
    def _size_error(errors: list[dict]) -> bool:
        def too_large(value: object) -> bool:
            if isinstance(value, dict):
                message = value.get("message", "")
                if (
                    value.get("code") == "InvalidDocument" and isinstance(message, str)
                    and "text elements" in message and "too large" in message
                ):
                    return True
                return any(too_large(child) for child in value.values())
            if isinstance(value, list):
                return any(too_large(child) for child in value)
            return False

        return bool(errors) and all(too_large(error) for error in errors)

    def _merge(self, user: User, record: Record) -> None:
        if not record.chunks or any(
            c.status != "succeeded" or not c.redacted_blob for c in record.chunks
        ):
            raise RedactionError("Cannot assemble incomplete redaction.")
        if record.page_count is None:
            record.redacted_blob = record.chunks[0].redacted_blob
        else:
            writer = PdfWriter()
            next_page = 0
            for chunk in record.chunks:
                if chunk.start != next_page or chunk.end <= chunk.start:
                    raise RedactionError("Stored chunk page ranges are invalid.")
                location = (
                    f"{self.settings.storage_url}/{self.settings.redacted_container}/"
                    f"{chunk.redacted_blob}"
                )
                blob_name = self._output_blob(location, user, record.id)
                data = self.storage.get_blob_client(
                    self.settings.redacted_container, blob_name
                ).download_blob().readall()
                reader = self._reader(data)
                if len(reader.pages) != chunk.end - chunk.start:
                    raise RedactionError("Redacted chunk page count does not match the original.")
                for page in reader.pages:
                    writer.add_page(page)
                next_page = chunk.end
            if next_page != record.page_count or len(writer.pages) != record.page_count:
                raise RedactionError("Redacted document page count does not match the original.")
            buffer = BytesIO()
            writer.write(buffer)
            record.redacted_blob = f"{user.storage_prefix}/{record.id}/redacted.pdf"
            self.storage.get_blob_client(
                self.settings.redacted_container, record.redacted_blob
            ).upload_blob(
                buffer.getvalue(), overwrite=True,
                content_settings=ContentSettings(content_type="application/pdf"),
            )
        record.warning_count = sum(c.warning_count for c in record.chunks)
        record.status = "succeeded"

    def _cleanup(self, user: User, record: Record, lease: BlobLeaseClient) -> None:
        prefix = f"{user.storage_prefix}/{record.id}/chunks/"
        for container in (
            self.settings.originals_container, self.settings.redacted_container
        ):
            client = self.storage.get_container_client(container)
            for blob in client.list_blobs(name_starts_with=prefix):
                lease.renew()
                try:
                    client.delete_blob(blob.name)
                except ResourceNotFoundError:
                    # A previous cleanup attempt may already have removed this blob.
                    continue

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


def get_redaction_service(background_tasks: BackgroundTasks) -> Iterator[RedactionService]:
    provider = os.environ.get("REDACTION_PROVIDER", "foundry").strip().lower()
    if provider == "foundry":
        from app.foundry import get_foundry_service

        yield from get_foundry_service(background_tasks)
    elif provider == "language":
        yield from get_language_redaction_service()
    else:
        logger.error("Invalid REDACTION_PROVIDER app setting.")
        raise HTTPException(503, "Configure REDACTION_PROVIDER as foundry or language.")


def get_language_redaction_service() -> Iterator[RedactionService]:
    settings = Settings.from_env()
    with (
        DefaultAzureCredential() as credential,
        BlobServiceClient(
            settings.storage_url, credential=settings.account_key or credential,
            logging_enable=False,
        ) as storage,
        httpx.Client(
            auth=LanguageAuth(settings, credential),
            timeout=30,
            follow_redirects=False,
        ) as http,
    ):
        yield RedactionService(settings, storage, http)


class LanguageAuth(httpx.Auth):
    def __init__(self, settings: Settings, credential: DefaultAzureCredential):
        self.key = settings.language_key
        self.token = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )

    def auth_flow(self, request: httpx.Request):
        if self.key:
            request.headers["Ocp-Apim-Subscription-Key"] = self.key
        else:
            request.headers["Authorization"] = f"Bearer {self.token()}"
        yield request
