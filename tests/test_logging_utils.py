import logging

import httpx

from src.logging_utils import SecretRedactionFilter, redact_url_credentials


def test_redact_url_credentials_masks_ncbi_api_key():
    text = 'GET https://example.test/search?term=TP53&api_key=secret-token "200 OK"'

    assert redact_url_credentials(text) == (
        'GET https://example.test/search?term=TP53&api_key=<redacted> "200 OK"'
    )


def test_secret_redaction_filter_masks_log_record_args():
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: %s",
        args=("https://example.test/search?api_key=secret-token",),
        exc_info=None,
    )

    assert SecretRedactionFilter().filter(record) is True
    assert record.args == ("https://example.test/search?api_key=<redacted>",)


def test_secret_redaction_filter_masks_url_object_args():
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: %s",
        args=(httpx.URL("https://example.test/search?api_key=secret-token"),),
        exc_info=None,
    )

    assert SecretRedactionFilter().filter(record) is True
    assert record.args == ("https://example.test/search?api_key=<redacted>",)
