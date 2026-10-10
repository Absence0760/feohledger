"""The uvicorn access log never records the ERP OAuth callback's query.

The provider redirects to ``/api/erp/oauth/callback?code=…&state=…&realmId=…``
and uvicorn's access logger writes the full request line; the authorization
code, the signed state and the customer's company id must not reach it.
"""

from __future__ import annotations

import logging

import pytest

from app.utils import access_log

CALLBACK = "/api/erp/oauth/callback"
QUERY = "?code=AUTHCODE-SECRET&state=STATE-SECRET&realmId=REALM-SECRET"


def _access_record(full_path: str) -> logging.LogRecord:
    """A record shaped exactly as uvicorn's h11/httptools protocols log one."""
    return logging.LogRecord(
        name=access_log.ACCESS_LOGGER,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("203.0.113.9:5000", "GET", full_path, "1.1", 302),
        exc_info=None,
    )


def test_callback_query_is_stripped():
    record = _access_record(CALLBACK + QUERY)
    assert access_log.AccessLogQueryRedactor().filter(record) is True
    line = record.getMessage()
    assert "SECRET" not in line
    assert f"GET {CALLBACK}?[redacted] HTTP/1.1" in line


def test_trailing_slash_variant_is_stripped_too():
    record = _access_record(CALLBACK + "/" + QUERY)
    access_log.AccessLogQueryRedactor().filter(record)
    assert "SECRET" not in record.getMessage()


@pytest.mark.parametrize(
    "path",
    [
        "/api/invoices?status=approved&page=2",
        "/api/erp/oauth/callbackx?code=1",
        CALLBACK,
    ],
)
def test_other_paths_keep_their_query(path):
    record = _access_record(path)
    access_log.AccessLogQueryRedactor().filter(record)
    assert path in record.getMessage()


def test_installed_on_the_uvicorn_access_logger_by_the_app(caplog):
    """``app.main`` installs it at import; a real record through the real logger
    comes out redacted."""
    import app.main  # noqa: F401 — the import is what installs the filter

    logger = logging.getLogger(access_log.ACCESS_LOGGER)
    assert any(isinstance(f, access_log.AccessLogQueryRedactor) for f in logger.filters)
    access_log.install()  # idempotent
    assert sum(isinstance(f, access_log.AccessLogQueryRedactor) for f in logger.filters) == 1

    with caplog.at_level(logging.INFO, logger=access_log.ACCESS_LOGGER):
        logger.info(
            '%s - "%s %s HTTP/%s" %d', "203.0.113.9:5000", "GET", CALLBACK + QUERY, "1.1", 302
        )
    assert CALLBACK in caplog.text
    assert "SECRET" not in caplog.text
