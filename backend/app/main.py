import os
import logging
from typing import Literal
from uuid import UUID

import httpx
from azure.core.exceptions import AzureError
from fastapi import Depends, FastAPI, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import User, current_user
from app.redaction import (
    MAX_PDF_BYTES,
    Job,
    RedactionError,
    RedactionService,
    get_redaction_service,
)
from app.upload_limit import UploadLimitMiddleware

logger = logging.getLogger(__name__)
app = FastAPI(title="AI Redaction API", version="0.1.0")
app.add_middleware(UploadLimitMiddleware)

allowed_origins = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS", "http://localhost:4200"
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.exception_handler(AzureError)
@app.exception_handler(httpx.HTTPError)
@app.exception_handler(RedactionError)
async def upstream_error_handler(request, error: Exception):
    # Upstream exception messages can include SAS URLs, keys, or document content.
    logger.error("Azure operation failed (%s).", type(error).__name__)
    return JSONResponse(
        status_code=502,
        content={"detail": "Azure processing or storage failed. Contact the administrator or retry."},
        headers={"Cache-Control": "no-store"},
    )


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="ai-redaction-api")


@app.post("/api/documents", response_model=Job, status_code=202)
def upload_document(
    file: UploadFile,
    response: Response,
    user: User = Depends(current_user),
    service: RedactionService = Depends(get_redaction_service),
) -> Job:
    response.headers["Cache-Control"] = "no-store"
    try:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise HTTPException(415, "Choose a PDF document.")
        if file.size is not None and file.size > MAX_PDF_BYTES:
            raise HTTPException(413, "PDFs must be 10 MB or smaller.")
        pdf = file.file.read(MAX_PDF_BYTES + 1)
        if len(pdf) > MAX_PDF_BYTES:
            raise HTTPException(413, "PDFs must be 10 MB or smaller.")
        if not pdf.startswith(b"%PDF-"):
            raise HTTPException(415, "The uploaded file is not a PDF.")
        return service.submit(user, pdf)
    finally:
        file.file.close()


@app.get("/api/documents/{document_id}", response_model=Job)
def document_status(
    document_id: UUID,
    response: Response,
    user: User = Depends(current_user),
    service: RedactionService = Depends(get_redaction_service),
) -> Job:
    response.headers["Cache-Control"] = "no-store"
    return service.status(user, document_id)


@app.get("/api/documents/{document_id}/download")
def download_document(
    document_id: UUID,
    user: User = Depends(current_user),
    service: RedactionService = Depends(get_redaction_service),
) -> Response:
    return Response(
        service.download(user, document_id),
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="redacted.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
