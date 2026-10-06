"""HTTP response helpers.

Small, dependency-free builders for header values that interpolate
untrusted / attacker-influenced strings (AI-extracted or user-entered
fields), where a naive f-string is unsafe — plus ``detail_text``, which
reads an ``HTTPException.detail`` back as text whatever its shape.
"""

from __future__ import annotations

import re
from urllib.parse import quote

# Characters that break the `filename="..."` quoted-string form of a
# Content-Disposition header, or aren't safe to echo back in a header at
# all: the quote itself, the backslash escape character, the forward slash
# (a path separator — browsers strip path components from a
# `Content-Disposition` filename anyway, but callers were hand-stripping it
# before calling here, so the helper should own the property rather than leave
# each caller to remember), every ASCII control character (0x00-0x1F, 0x7F —
# includes CR/LF, though the ASGI server already rejects raw CRLF in header
# values before this ever runs), and anything outside printable ASCII
# (non-ASCII bytes in the legacy `filename=` parameter aren't reliably
# interpreted by every client).
_UNSAFE_FILENAME_CHARS = re.compile(r'["\\/]|[^\x20-\x7e]')


def content_disposition_attachment(filename: str) -> str:
    """Build a safe `Content-Disposition: attachment; filename=...` header
    value from an untrusted filename (e.g. a vendor-supplied / AI-extracted
    ``invoice_number``).

    A raw ``f'attachment; filename="{name}"'`` has two failure modes:

    1. A `"` (or `\\`) in ``name`` breaks the quoted-string syntax, so some
       HTTP clients save the file under a truncated or mangled name.
    2. Non-ASCII characters aren't valid in the legacy ``filename=`` form.

    Fixed per RFC 6266 / RFC 5987 by emitting both parameters: a sanitized
    ASCII ``filename=`` fallback (unsafe / non-ASCII characters replaced
    with ``_``) for clients that only understand the legacy form, and a
    percent-encoded UTF-8 ``filename*=`` for clients that support it — which
    always takes precedence when present, so the true characters still
    round-trip for any modern client.
    """
    ascii_fallback = _UNSAFE_FILENAME_CHARS.sub("_", filename) or "download"
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def detail_text(detail: object) -> str | None:
    """The human-readable text of an ``HTTPException.detail``, or None.

    ``detail`` is usually a string, but a refusal a client localizes is an
    OBJECT carrying a stable ``code`` beside its English ``message`` (the GL
    chart refusal, ``services/gl_chart.ChartRefusal.body``). A server-side
    catcher that turns a refusal into text — an escalation rationale, a bulk
    skip reason, an HTML page — reads it here, so it gets the sentence rather
    than a stringified dict or nothing at all.
    """
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str):
            return message
    return None
