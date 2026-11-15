"""ASGI middleware: request id, structured access log, HTTP metrics and security headers.

* X-Request-ID: accepted from the client when it is a safe token (8-64 of [A-Za-z0-9._-]),
  otherwise generated; echoed in the response and attached to every log line of the request.
* metrics use the matched ROUTE TEMPLATE (e.g. /api/documents/{document_id}), never the raw path.
* the access log records method, route, status, duration, tenant and user ids; never query
  strings, bodies or headers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders

from .logging import log_context
from .metrics import HTTP_DURATION, HTTP_IN_PROGRESS, HTTP_REQUESTS, REQUEST_DEADLINE_EXCEEDED

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


class RequestDeadlineMiddleware:
    """Answer 503 + Retry-After when a request has not STARTED its response within `seconds`.

    Protects clients from dependencies that hang instead of failing: a database that is frozen but
    still accepts TCP (found by the resilience drill: requests waited until the client gave up),
    a stalled object store, an IdP that never answers. Synchronous handlers cannot be interrupted,
    so the abandoned handler keeps its worker thread until the dependency answers; its late
    response is discarded. A response that has started (e.g. a streaming download) is never cut.
    Retried writes stay safe through Idempotency-Key (uploads, deletions) and duplicate-job
    detection. `seconds <= 0` disables the deadline; liveness is exempt.
    """

    def __init__(self, app, seconds: float, exempt_paths: tuple[str, ...] = ("/api/health/live",)):
        self.app, self.seconds, self.exempt = app, seconds, exempt_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.seconds <= 0 or scope.get("path", "") in self.exempt:
            await self.app(scope, receive, send)
            return
        started = asyncio.Event()
        state = {"abandoned": False}

        async def guarded_send(message):
            if state["abandoned"]:
                return  # late answer of a request that already received its 503
            if message["type"] == "http.response.start":
                started.set()
            await send(message)

        handler = asyncio.ensure_future(self.app(scope, receive, guarded_send))
        waiter = asyncio.ensure_future(started.wait())
        try:
            done, _ = await asyncio.wait({handler, waiter}, timeout=self.seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if done:
            await handler  # finished, or the response is under way: complete it normally
            return
        state["abandoned"] = True
        handler.add_done_callback(_consume_result)
        route = getattr(scope.get("route"), "path", None) or "unmatched"
        REQUEST_DEADLINE_EXCEEDED.inc()
        log.warning("request_deadline_exceeded", extra={"event": "request_deadline_exceeded", "route": route,
                                                        "deadline_seconds": self.seconds})
        body = json.dumps({"detail": "The service could not answer in time (a dependency may be "
                                     "unavailable); please retry."}).encode()
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"application/json"), (b"retry-after", b"5"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def _consume_result(task: asyncio.Future) -> None:
    """Retrieve the abandoned handler's outcome so asyncio does not report it as unhandled."""
    if not task.cancelled():
        task.exception()
