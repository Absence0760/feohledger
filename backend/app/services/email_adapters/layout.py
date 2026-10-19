"""The one HTML layout every transactional email goes out in.

Every adapter that can send HTML (SES, SMTP) wraps the message body in
:func:`render_email` — a complete, table-based, inline-styled document that
renders the same in Gmail, Outlook and Apple Mail: a 600px card on a neutral
background, the brand header (``branding.brand_email_html_header``), the body,
and a muted footer carrying the support link. A message that brings no HTML of
its own gets a body derived from its plaintext by :func:`text_to_html`, so no
email ever goes out as bare text and a new send site inherits the layout
without doing anything (``docs/decisions.md`` §280).

The building blocks (:func:`email_paragraph`, :func:`email_button`,
:func:`email_note`, :func:`email_fallback_link`, :func:`email_credentials`) are
what a hand-written body is made of, so a bespoke email and a derived one look
like the same product. Every one of them HTML-escapes what it is given: a body
is built from tenant- and vendor-supplied names, and none of them may become
markup.

**Only a URL the sender built becomes a link.** Text is never scanned for URLs:
a name or a rejection reason is free text, and "your account is locked, sign in
at https://…" typed into one must not become a clickable link inside a genuine
email. A hand-written body passes its URLs to :func:`email_button` /
:func:`email_fallback_link` explicitly; a derived body links only the URLs the
sender declared in ``EmailMessage.links``. Both refuse anything but an absolute
http(s) URL.
"""

from __future__ import annotations

import html
import re
from collections.abc import Collection
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from app.services.branding import BrandContext

# The palette. Light by default — most clients force a light canvas whatever
# the page asks for — with a dark override for the clients that honour
# prefers-color-scheme (Apple Mail, iOS Mail, Outlook for Mac).
_BG = "#f3f4f6"
_CARD = "#ffffff"
_TEXT = "#1f2937"
_MUTED = "#6b7280"
_BORDER = "#e5e7eb"
_PANEL = "#f9fafb"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

_DARK_CSS = (
    "@media (prefers-color-scheme: dark){"
    ".feoh-bg{background:#0f1115 !important}"
    ".feoh-card{background:#171a21 !important;border-color:#2a2f3a !important}"
    ".feoh-text{color:#e5e7eb !important}"
    ".feoh-muted{color:#9ca3af !important}"
    ".feoh-panel{background:#1f2430 !important;border-color:#2a2f3a !important}"
    ".feoh-rule{border-color:#2a2f3a !important}"
    "}"
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
# An indented "Label:   value" line — the credentials / details blocks the
# invite and reset emails write in their plaintext.
_KV_RE = re.compile(r"^\s+([^:\n]{1,40}):\s+(\S.*)$")


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _safe_href(url: str) -> str:
    """``url`` if it is an absolute http(s) URL; raises ``ValueError`` otherwise.

    Every href the layout emits passes through here, so no caller can put a
    ``javascript:`` / ``data:`` URL (or a relative one, which means nothing to a
    mail client) behind a button.
    """
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("email links must be absolute http(s) URLs")
    return raw


def _is_safe_href(url: str) -> bool:
    try:
        _safe_href(url)
    except ValueError:
        return False
    return True


def _readable_on(hex_color: str) -> str:
    """Black or white, whichever reads on ``hex_color`` (WCAG relative luminance)."""
    raw = (hex_color or "").lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    try:
        r, g, b = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return "#ffffff"

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    # Contrast against white vs against near-black; pick the larger.
    return "#ffffff" if (1.05 / (lum + 0.05)) >= ((lum + 0.05) / 0.0623) else "#111827"


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def email_paragraph(text: str) -> str:
    """One paragraph of escaped text, newlines as ``<br>``. URLs stay text."""
    inner = "<br>".join(_e(line) for line in (text or "").split("\n"))
    return (
        f'<p class="feoh-text" style="margin:0 0 16px;font-size:15px;line-height:1.6;'
        f'color:{_TEXT}">{inner}</p>'
    )


def email_note(text: str) -> str:
    """A small muted paragraph — expiry notes, "ignore this if" lines."""
    return (
        f'<p class="feoh-muted" style="margin:0 0 16px;font-size:13px;line-height:1.55;'
        f'color:{_MUTED}">{_e(text)}</p>'
    )


def email_button(url: str, label: str, brand: BrandContext) -> str:
    """A bulletproof call-to-action button in the brand accent.

    A table cell carries the colour (Outlook ignores padding and background on
    an ``<a>``), and the link fills it, so the whole button is the target.
    Raises ``ValueError`` for anything but an absolute http(s) ``url``.
    """
    href = _safe_href(url)
    accent = brand.accent_color
    fg = _readable_on(accent)
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:8px 0 24px"><tr>'
        f'<td align="center" bgcolor="{accent}" style="border-radius:8px;background:{accent}">'
        f'<a href="{_e(href)}" target="_blank" style="display:inline-block;padding:13px 26px;'
        f"font-family:{_FONT};font-size:15px;font-weight:600;line-height:1.2;color:{fg};"
        f'text-decoration:none;border-radius:8px">{_e(label)}</a>'
        "</td></tr></table>"
    )


def email_fallback_link(intro: str, url: str) -> str:
    """The "button not working? paste this" line, with the raw URL beneath it.

    Raises ``ValueError`` for anything but an absolute http(s) ``url``.
    """
    href = _safe_href(url)
    return (
        f'<p class="feoh-muted" style="margin:0 0 16px;font-size:12px;line-height:1.55;'
        f'color:{_MUTED}">{_e(intro)}<br>'
        f'<a href="{_e(href)}" style="color:{_MUTED};word-break:break-all">{_e(href)}</a></p>'
    )


def email_credentials(
    rows: list[tuple[str, str]], *, mono: bool = True, links: Collection[str] = ()
) -> str:
    """A set-off panel of label / value rows (sign-in URL, email, password).

    Values are escaped text. A value is a link only when it is exactly one of
    ``links`` — the URLs the sender built — and an http(s) URL.
    """
    cells = []
    for i, (label, value) in enumerate(rows):
        top = "" if i == 0 else f"border-top:1px solid {_BORDER};"
        value_font = f"font-family:{_MONO};" if mono else ""
        shown = (
            f'<a href="{_e(value)}" style="color:inherit;text-decoration:underline">{_e(value)}</a>'
            if value in links and _is_safe_href(value)
            else _e(value)
        )
        cells.append(
            "<tr>"
            f'<td class="feoh-muted feoh-rule" style="{top}padding:10px 16px;font-size:13px;'
            f'color:{_MUTED};white-space:nowrap;vertical-align:top;width:1%">{_e(label)}</td>'
            f'<td class="feoh-text feoh-rule" style="{top}padding:10px 16px 10px 0;'
            f"{value_font}font-size:14px;color:{_TEXT};word-break:break-all;"
            f'vertical-align:top">{shown}</td>'
            "</tr>"
        )
    return (
        '<table role="presentation" class="feoh-panel" cellpadding="0" cellspacing="0" '
        f'border="0" width="100%" style="margin:4px 0 20px;background:{_PANEL};'
        f'border:1px solid {_BORDER};border-radius:8px;border-collapse:separate">'
        + "".join(cells)
        + "</table>"
    )


def email_preformatted(text: str) -> str:
    """A monospace block for tabular plaintext (a scheduled report's CSV)."""
    return (
        f'<pre class="feoh-panel feoh-text" style="margin:0 0 16px;padding:12px 14px;'
        f"background:{_PANEL};border:1px solid {_BORDER};border-radius:8px;"
        f"font-family:{_MONO};font-size:12px;line-height:1.5;color:{_TEXT};"
        f'white-space:pre-wrap;word-break:break-word">{_e(text)}</pre>'
    )


# ---------------------------------------------------------------------------
# Plaintext → HTML
# ---------------------------------------------------------------------------


def _is_url(line: str) -> bool:
    match = _URL_RE.fullmatch(line.strip())
    return match is not None


def text_to_html(
    body_text: str,
    brand: BrandContext,
    *,
    cta_label: str,
    links: Collection[str] = (),
    fallback_intro: str = "",
) -> str:
    """Derive an HTML body from a plaintext one.

    Blocks are split on blank lines, then each is read by its shape:

    - a line that is only a URL **the sender declared in** ``links`` becomes the
      call-to-action button; when the line above it is a lead-in ending in ``:``
      ("View the conversation and reply:"), that lead-in is the button's label,
      otherwise ``cta_label`` is;
    - a block of indented ``Label: value`` lines becomes the credentials panel
      (a value that is a declared link is linked, nothing else is);
    - a block of four or more lines is tabular data and stays monospace;
    - anything else is a paragraph.

    Any other URL — one inside a sentence, or on its own line but not declared —
    stays inert text: it may have arrived inside an interpolated name.

    With ``fallback_intro`` set, each button's raw URL is repeated at the end
    under that line, for a client that will not follow the button.

    Everything is escaped. The plaintext itself is never altered: it still goes
    out as the message's text part.
    """
    parts: list[str] = []
    buttons: list[str] = []

    def button(url: str, label: str) -> None:
        parts.append(email_button(url, label, brand))
        if url not in buttons:
            buttons.append(url)

    declared = {u.strip() for u in links if _is_safe_href(u)}

    def is_link(line: str) -> bool:
        return line.strip() in declared and _is_url(line)

    for raw_block in re.split(r"\n\s*\n", (body_text or "").strip("\n")):
        lines = [ln for ln in raw_block.split("\n") if ln.strip()]
        if not lines:
            continue
        if len(lines) == 1 and is_link(lines[0]):
            button(lines[0].strip(), cta_label)
            continue
        if len(lines) >= 2 and is_link(lines[-1]) and not any(_is_url(x) for x in lines[:-1]):
            lead = lines[:-1]
            label_line = lead[-1].strip()
            if label_line.endswith(":") and len(label_line) <= 80:
                if len(lead) > 1:
                    parts.append(email_paragraph("\n".join(x.strip() for x in lead[:-1])))
                button(lines[-1].strip(), label_line[:-1].strip())
            else:
                parts.append(email_paragraph("\n".join(x.strip() for x in lead)))
                button(lines[-1].strip(), cta_label)
            continue
        kv = [_KV_RE.match(ln) for ln in lines]
        if all(kv):
            rows = [(m.group(1).strip(), m.group(2).strip()) for m in kv]
            parts.append(email_credentials(rows, links=declared))
            continue
        if len(lines) >= 4:
            parts.append(email_preformatted("\n".join(lines)))
            continue
        parts.append(email_paragraph("\n".join(x.strip() for x in lines)))
    if fallback_intro:
        parts.extend(email_fallback_link(fallback_intro, url) for url in buttons)
    return "".join(parts)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def render_email(
    *,
    brand: BrandContext,
    body_html: str,
    public_url: str | None,
    preheader: str = "",
    lang: str = "en",
) -> str:
    """Wrap ``body_html`` in the full branded document."""
    from app.services.branding import brand_email_footer_html, brand_email_html_header

    header = brand_email_html_header(brand, public_url=public_url)
    footer = brand_email_footer_html(brand)
    name = _e(brand.product_name)
    # The preheader is hidden text the inbox shows as the preview; the trailing
    # zero-width run stops clients pulling body text in after it. It is never
    # derived from the body (see ``EmailMessage.preheader``).
    pre = (
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all">'
        f"{_e(preheader)}" + "&#8204;&nbsp;" * 40 + "</div>"
        if preheader
        else ""
    )
    return (
        "<!DOCTYPE html>"
        f'<html lang="{_e(lang)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<title>{name}</title>"
        f"<style>{_DARK_CSS}</style></head>"
        f'<body class="feoh-bg" style="margin:0;padding:0;background:{_BG};'
        '-webkit-text-size-adjust:100%">'
        f"{pre}"
        '<table role="presentation" class="feoh-bg" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0" style="background:{_BG}"><tr>'
        '<td align="center" style="padding:32px 16px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="max-width:600px;font-family:{_FONT}">'
        f'<tr><td style="padding:0 4px 16px">{header}</td></tr>'
        '<tr><td class="feoh-card" style="background:'
        f'{_CARD};border:1px solid {_BORDER};border-radius:12px;padding:32px 32px 16px">'
        f"{body_html}</td></tr>"
        f'<tr><td class="feoh-muted" style="padding:20px 8px 0;font-size:12px;'
        f'line-height:1.6;color:{_MUTED};text-align:center">{footer}'
        f'<div style="margin-top:4px">{name}</div></td></tr>'
        "</table></td></tr></table></body></html>"
    )
