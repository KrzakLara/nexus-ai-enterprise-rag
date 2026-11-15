"""Structured JSON logging with mandatory redaction.

Every record is one JSON object: timestamp (UTC, ms), severity, service, environment, logger,
message, the request / job context (request_id, tenant_id, user_id, job_id, job_type, operation),
the active trace / span id, and the structured `fields` of the event.

What is never logged (enforced in two layers):
  1. call sites log identifiers, counts, durations and error CATEGORIES, never document text,
     extracted values, prompts, tokens or passwords;
  2. this formatter redacts known secret shapes from every message and field (bearer / JWT
     tokens, `password=...` style pairs, OpenAI / AWS keys) and DROPS fields whose names denote
     content or credentials (content, text, prompt, excerpt, value, token, password, ...).
backend/tests/test_observability.py feeds a canary document through upload, ask and extraction
and asserts the canary never reaches the log output.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import traceback
from datetime import datetime, timezone

# One mutable dict per request / job. The dict object is shared by every context copy (FastAPI runs
# sync dependencies and routes in different thread-pool calls), so fields added by the auth
# dependency are visible in the route and in the access log.
log_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar("nexus_log_context", default=None)

_SECRET_PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|client_secret|api[_-]?key|access[_-]?token|"
                r"refresh[_-]?token|id[_-]?token|authorization|cookie|session[_-]?token|csrf[_-]?token|"
                r"code_verifier|logout_token)(\"?'?\s*[:=]\s*\"?'?)([^\s\"',;&}]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), "[REDACTED_KEY]"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?i)(X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)=[^&\s]+"), r"\1=[REDACTED]"),
]
# Field names whose VALUES must never be emitted (content or credentials).
_DROP_KEYS = frozenset({
    "password", "passwd", "secret", "client_secret", "token", "access_token", "refresh_token",
    "id_token", "authorization", "cookie", "api_key", "code", "code_verifier", "csrf_token",
    "content", "text", "body", "prompt", "messages", "excerpt", "page_text", "value", "values",
    "evidence", "answer", "question", "document_text", "snippet", "raw", "old_value", "new_value",
})
_STANDARD = frozenset(vars(logging.LogRecord("x", 0, "", 0, "", (), None)).keys()) | {"message", "asctime"}


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_fields(value, depth: int = 0):
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if str(k).lower() in _DROP_KEYS else redact_fields(v, depth + 1))
                for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [redact_fields(v, depth + 1) for v in list(value)[:50]]
    if isinstance(value, str):
        return redact(value[:500])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value)[:200])


def _trace_ids() -> dict:
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
    except Exception:  # noqa: BLE001 - logging must never fail because of tracing
        pass
    return {}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str):
        super().__init__()
        self.service, self.environment = service, environment

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "severity": record.levelname,
            "service": self.service,
            "environment": self.environment,
            "logger": record.name,
            "message": redact(record.getMessage())[:2000],
        }
        ctx = log_context.get()
        if ctx:
            payload.update({k: v for k, v in ctx.items() if v is not None})
        payload.update(_trace_ids())
        extra = {k: v for k, v in record.__dict__.items() if k not in _STANDARD and not k.startswith("_")}
        if extra:
            payload["fields"] = redact_fields(extra)
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc, tb = record.exc_info
            payload["error"] = {
                "type": exc_type.__name__,
                "message": redact(str(exc))[:500],
                "stack": redact("".join(traceback.format_tb(tb))[-4000:]),
            }
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = log_context.get() or {}
        prefix = " ".join(f"{k}={v}" for k, v in ctx.items() if v is not None)
        base = f"{record.levelname:<7} {record.name}: {redact(record.getMessage())}"
        if record.exc_info and record.exc_info[0] is not None:
            base += f" [{record.exc_info[0].__name__}: {redact(str(record.exc_info[1]))[:300]}]"
        return f"{base} {prefix}".rstrip()


_configured = False


def configure_logging(service: str, environment: str, level: str = "INFO", fmt: str = "json",
                      stream=None) -> None:
    """Idempotent; replaces root handlers and routes uvicorn's loggers through the same handler."""
    global _configured
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter(service, environment) if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False  # replaced by the structured http_request event
    for noisy in ("httpx", "httpx2", "httpcore", "botocore", "boto3", "urllib3", "s3transfer",
                  "sentence_transformers", "transformers", "huggingface_hub", "pdfminer", "PIL",
                  "alembic", "alembic.runtime.migration"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def bind(**fields) -> None:
    """Add fields to the current request / job log context (no-op outside one)."""
    ctx = log_context.get()
    if ctx is not None:
        ctx.update({k: v for k, v in fields.items() if v is not None})


def new_context(**fields) -> contextvars.Token:
    return log_context.set({k: v for k, v in fields.items() if v is not None})


def event(logger: logging.Logger, name: str, level: int = logging.INFO, **fields) -> None:
    """Log a named structured event: message = name, fields = identifiers / counts / durations."""
    logger.log(level, name, extra={"event": name, **fields})
