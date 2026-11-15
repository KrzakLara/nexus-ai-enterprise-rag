"""ASGI middleware: request id, structured access log, HTTP metrics and security headers.

* X-Request-ID: accepted from the client when it is a safe token (8-64 of [A-Za-z0-9._-]),
  otherwise generated; echoed in the response and attached to every log line of the request.
* metrics use the matched ROUTE TEMPLATE (e.g. /api/documents/{document_id}), never the raw path.
* the access log records method, route, status, duration, tenant and user ids; never query
  strings, bodies or headers.
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders

from .logging import log_context
from .metrics import HTTP_DURATION, HTTP_IN_PROGRESS, HTTP_REQUESTS

log = logging.getLogger("nexus.http")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-site"),
)


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = supplied if _REQUEST_ID.match(supplied) else uuid.uuid4().hex
        ctx = {"request_id": request_id}
        token = log_context.set(ctx)
        start = time.perf_counter()
        status = {"code": 500}
        path = scope.get("path", "")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for name, value in SECURITY_HEADERS:
                    if name.decode() not in headers:
                        headers[name.decode()] = value.decode()
                if path.startswith("/api/auth") and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        HTTP_IN_PROGRESS.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            HTTP_IN_PROGRESS.dec()
            elapsed = time.perf_counter() - start
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope.get("method", "GET")
            HTTP_REQUESTS.labels(method, route, str(status["code"])).inc()
            HTTP_DURATION.labels(method, route).observe(elapsed)
            if not route.startswith("/api/health"):
                level = logging.WARNING if status["code"] >= 500 else logging.INFO
                log.log(level, "http_request", extra={
                    "event": "http_request", "method": method, "route": route,
                    "status": status["code"], "duration_ms": round(elapsed * 1000, 2),
                    "auth": ctx.get("auth")})
            log_context.reset(token)
