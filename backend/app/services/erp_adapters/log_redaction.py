"""Keep ERP credentials that travel in a URL query string out of the logs.

Some ERPs take credentials in the query string: SYSPRO's operator password and
session id, Sage Business Cloud Accounting (South Africa)'s ``apikey``. httpx
logs every request URL at INFO (``HTTP Request: GET <url> "HTTP/1.1 200 OK"``)
and the app's root logger runs at INFO, so without this every call would write
the credential to stdout and on to the log shipper.

An adapter calls :func:`redact_query_strings_containing` once, at import, with
a marker that identifies its URLs (a path fragment, or a query-parameter name
such as ``"apikey="``). One filter on the ``httpx`` logger then replaces the
query of any logged URL containing a registered marker with ``?[redacted]``.
Matching is case-insensitive. Not an adapter itself — it registers nothing.
"""

from __future__ import annotations

import logging
import re

REDACTED = "?[redacted]"

#: A query string: from ``?`` up to whitespace, a quote or an angle bracket —
#: wherever a URL ends inside a log message.
_QUERY_RE = re.compile(r"\?[^\s\"'<>]*")

_markers: set[str] = set()


def redact_url_query(value: object) -> object:
    """``value`` with every URL query replaced, when it mentions a registered marker.

    Anything that mentions no marker is returned unchanged (and un-stringified),
    so ordinary log arguments keep their type for ``%d`` and friends.
    """
    text = str(value)
    lowered = text.lower()
    if "?" not in text or not any(marker in lowered for marker in _markers):
        return value
    return _QUERY_RE.sub(REDACTED, text)


class ErpQueryRedactor(logging.Filter):
    """Strip the query string from any registered ERP URL in a log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_url_query(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_url_query(v) for k, v in record.args.items()}
        record.msg = redact_url_query(record.msg)
        return True


def redact_query_strings_containing(marker: str) -> None:
    """Register ``marker`` and make sure the ``httpx`` logger carries the filter."""
    _markers.add(marker.lower())
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, ErpQueryRedactor) for f in httpx_logger.filters):
        httpx_logger.addFilter(ErpQueryRedactor())
