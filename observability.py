"""Logging setup: request-id correlation, optional JSON output, secret scrubbing."""

import contextvars
import json
import logging
import os
import re
from datetime import datetime, timezone

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# Patterns that must never reach a log sink.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"fc-[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token)(=|:\s*|\"\s*:\s*\")([^\s&\"',]{4,})"),
    re.compile(r"postgres(ql)?://([^:]+):([^@]+)@"),
]


def scrub(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub("sk-ant-***", text)
    text = _SECRET_PATTERNS[1].sub("fc-***", text)
    text = _SECRET_PATTERNS[2].sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = _SECRET_PATTERNS[3].sub(lambda m: f"postgres{m.group(1) or ''}://{m.group(2)}:***@", text)
    return text


class ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        record.msg = scrub(message)
        record.args = ()
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    def formatException(self, ei) -> str:
        return scrub(super().formatException(ei))


_configured = False


def configure_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(getattr(logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO))
    handler = logging.StreamHandler()
    if os.getenv("LOG_FORMAT", "plain").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(PlainFormatter("%(levelname)s [%(request_id)s] %(name)s: %(message)s"))
    handler.addFilter(ScrubFilter())
    root.handlers = [handler]
    _configured = True
