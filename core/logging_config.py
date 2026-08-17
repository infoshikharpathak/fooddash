from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE_PATH = Path("/tmp/fooddash.log")

_EXTRA_FIELDS = ("request_id", "endpoint", "user_id", "duration_ms", "error_type")


class JSONFormatter(logging.Formatter):
    """One JSON object per line — the contract LogScribe reads from."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service_name": getattr(record, "service_name", record.name),
            "level": record.levelname,
            "message": record.getMessage(),
        }

        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if record.exc_info:
            payload["stack_trace"] = self.formatException(record.exc_info)
            payload.setdefault("error_type", record.exc_info[0].__name__)

        return json.dumps(payload)


class ServiceLoggerAdapter(logging.LoggerAdapter):
    """Injects service_name into every record via a per-call extra dict rather than
    global mutable state — safe under concurrent requests from different routers
    sharing one process, where a shared LogRecordFactory would race and clobber
    the service attribution between interleaved async calls."""

    def process(self, msg, kwargs):
        extra = kwargs.get("extra", {})
        kwargs["extra"] = {**self.extra, **extra}
        return msg, kwargs


_configured = False


def _configure_root() -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger("fooddash")
    root.setLevel(logging.INFO)

    formatter = JSONFormatter()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    file_handler = logging.FileHandler(LOG_FILE_PATH)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    root.propagate = False
    _configured = True


def get_logger(service_name: str) -> ServiceLoggerAdapter:
    _configure_root()
    return ServiceLoggerAdapter(logging.getLogger("fooddash"), {"service_name": service_name})
