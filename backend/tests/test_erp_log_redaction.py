"""ERP query-string credentials stay out of every logger that writes a URL.

``httpx`` logs each request URL at INFO; httpcore has a DEBUG trace logger per
module. A logger's filters only see records logged on that logger itself, so
the redactor has to sit on each one by name.
"""

from __future__ import annotations

import http.server
import logging
import re
import threading
from pathlib import Path

import httpcore
import httpx
import pytest

from app.services.erp_adapters import (
    log_redaction,
    sage_accounting_za,  # noqa: F401 — registers "apikey="
)


def test_every_httpcore_logger_is_covered():
    """Drift guard: a logger a newer httpcore adds must join REDACTED_LOGGERS."""
    root = Path(httpcore.__file__).parent
    names = set()
    for source in root.rglob("*.py"):
        names |= set(re.findall(r'getLogger\(\s*"([^"]+)"\s*\)', source.read_text()))
    assert names, "httpcore no longer names its loggers literally; update this guard"
    assert names <= set(log_redaction.REDACTED_LOGGERS), names - set(log_redaction.REDACTED_LOGGERS)


@pytest.mark.parametrize("name", log_redaction.REDACTED_LOGGERS)
def test_each_logger_redacts_a_registered_url(name, caplog):
    log_redaction.install()
    target = (
        "URL(scheme=b'https', host=b'za.example', port=None, "
        "target=b'/api/2.0.0/Company/Get?apikey=KEY-SECRET&companyid=1')"
    )
    with caplog.at_level(logging.DEBUG, logger=name):
        logging.getLogger(name).debug("send_request_headers.started url=%s", target)
        logging.getLogger(name).debug(f"preformatted url={target}")
    assert "KEY-SECRET" not in caplog.text
    assert log_redaction.REDACTED in caplog.text


def test_a_real_request_at_debug_logs_no_credential(caplog):
    """End to end: a live request, every httpx/httpcore logger at DEBUG."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib hook name
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with caplog.at_level(logging.DEBUG):
            for name in log_redaction.REDACTED_LOGGERS:
                logging.getLogger(name).setLevel(logging.DEBUG)
            httpx.get(f"http://127.0.0.1:{server.server_port}/x?apikey=KEY-SECRET")
    finally:
        server.shutdown()
        for name in log_redaction.REDACTED_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
    assert "HTTP Request" in caplog.text
    assert "KEY-SECRET" not in caplog.text


def test_the_app_installs_it_without_waiting_for_an_adapter_import():
    import app.main  # noqa: F401

    for name in log_redaction.REDACTED_LOGGERS:
        filters = logging.getLogger(name).filters
        assert sum(isinstance(f, log_redaction.ErpQueryRedactor) for f in filters) == 1
