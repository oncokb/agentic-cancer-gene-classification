"""Logging helpers shared by the API and benchmark CLI."""

from __future__ import annotations

import logging
import re
from typing import Any

_SECRET_QUERY_RE = re.compile(r"([?&](?:api_key|apikey|key|token)=)[^&\s\"]+")


def redact_url_credentials(value: str) -> str:
    """Redact common credential-bearing query parameters from log text."""
    return _SECRET_QUERY_RE.sub(r"\1<redacted>", value)


class SecretRedactionFilter(logging.Filter):
    """Best-effort filter for messages and %-format args before handlers emit."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_url_credentials(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_arg(value) for key, value in record.args.items()}
        return True


def _redact_arg(value: Any) -> Any:
    if isinstance(value, str):
        return redact_url_credentials(value)
    text = str(value)
    redacted = redact_url_credentials(text)
    if redacted != text:
        return redacted
    return value


def install_secret_redaction_filter() -> None:
    """Install the credential redaction filter on root handlers and noisy clients."""
    redaction_filter = SecretRedactionFilter()
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if not any(isinstance(existing, SecretRedactionFilter) for existing in handler.filters):
            handler.addFilter(redaction_filter)
    for logger_name in ("httpx", "httpcore"):
        target = logging.getLogger(logger_name)
        if not any(isinstance(existing, SecretRedactionFilter) for existing in target.filters):
            target.addFilter(redaction_filter)
