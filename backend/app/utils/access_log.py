"""Keep credentials that arrive in a request's query string out of the access log.

uvicorn's access logger writes every request line, query string included:
``"GET /api/erp/oauth/callback?code=…&state=…&realmId=… HTTP/1.1" 302``. On
the ERP OAuth callback that query is an authorization code, the signed
``state`` and the customer's QuickBooks company id — and the access log is
shipped off the box like any other log line. The provider chooses the
callback's shape, so the only fix is not to log the query.

:func:`install` attaches :class:`AccessLogQueryRedactor` to the
``uvicorn.access`` logger; ``app/main.py`` calls it where it configures app
logging, so it holds under ``python main.py`` and the production uvicorn alike
(uvicorn configures its loggers before it imports the app, and a logger's
filters survive that). Only paths in :data:`SENSITIVE_QUERY_PATHS` are
redacted: every other access line keeps its query, which is what makes the log
useful for debugging list filters and pagination.
"""

from __future__ import annotations

import logging

ACCESS_LOGGER = "uvicorn.access"

#: Request paths whose query string carries a credential. Exact paths, no
#: prefixes: a new GET route that takes a secret in its query joins here.
SENSITIVE_QUERY_PATHS: frozenset[str] = frozenset(
    {
        # Provider redirect: ?code=…&state=…&realmId=… (services/erp_oauth).
        "/api/erp/oauth/callback",
    }
)

REDACTED_QUERY = "?[redacted]"


def redact_path(full_path: object) -> object:
    """``full_path`` with its query replaced when the path is sensitive."""
    if not isinstance(full_path, str) or "?" not in full_path:
        return full_path
    path = full_path.split("?", 1)[0]
    if path.rstrip("/") in SENSITIVE_QUERY_PATHS:
        return path + REDACTED_QUERY
    return full_path


class AccessLogQueryRedactor(logging.Filter):
    """Strip the query from a sensitive path in a uvicorn access record.

    uvicorn logs ``'%s - "%s %s HTTP/%s" %d'`` with args ``(client, method,
    full_path, http_version, status)``. Every string argument is checked, so a
    change in the argument order cannot silently let the query through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_path(a) for a in record.args)
        return True


def install() -> None:
    """Attach the redactor to ``uvicorn.access`` (idempotent)."""
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(f, AccessLogQueryRedactor) for f in logger.filters):
        logger.addFilter(AccessLogQueryRedactor())
