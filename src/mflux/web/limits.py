from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyTooLarge(HTTPException, MultiPartException):
    def __init__(self):
        HTTPException.__init__(self, 413, "Request body is too large")
        self.message = self.detail


class RequestBodyLimitMiddleware:
    JSON_LIMIT = 256 * 1024
    AUTH_LIMIT = 16 * 1024
    CHAT_LIMIT = 320000

    def __init__(self, app: ASGIApp, max_upload_mb: int):
        self.app = app
        self.upload_limit = (max_upload_mb + 1) * 1024 * 1024

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        limit = self.JSON_LIMIT
        if path.rstrip("/") == "/api/uploads":
            limit = self.upload_limit
        elif path.rstrip("/") in ("/api/login", "/api/setup"):
            limit = self.AUTH_LIMIT
        elif path.rstrip("/") == "/api/chat":
            limit = self.CHAT_LIMIT
        rejection = JSONResponse({"detail": "Request body is too large"}, status_code=413)
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                length = int(declared)
                if length < 0:
                    raise ValueError
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
                return
            if length > limit:
                await rejection(scope, receive, send)
                return

        received = 0
        exceeded = False
        rejected = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    # Multipart parsers close temporary files on MultiPartException.
                    raise RequestBodyTooLarge()
            return message

        async def limited_send(message: Message) -> None:
            nonlocal rejected, response_started
            if exceeded:
                if response_started:
                    raise RequestBodyTooLarge()
                # Multipart parsing may translate the exception to 400; preserve 413.
                if not rejected:
                    rejected = True
                    await rejection(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            if not rejected:
                await rejection(scope, receive, send)
