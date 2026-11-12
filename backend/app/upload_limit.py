from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.redaction import MAX_PDF_BYTES


class UploadLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        # Allow a small multipart envelope, but bound chunked requests as well.
        limit = MAX_PDF_BYTES + 65_536
        headers = dict(scope["headers"])
        length = headers.get(b"content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length."}, 400)(scope, receive, send)
                return
            if size < 0 or size > limit:
                await JSONResponse({"detail": "Upload exceeds the 10 MB PDF limit."}, 413)(
                    scope, receive, send
                )
                return
        total = 0

        async def bounded_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise HTTPException(413, "Upload exceeds the 10 MB PDF limit.")
            return message

        await self.app(scope, bounded_receive, send)
