"""Text that ReportLab can actually draw — the one escaper every PDF uses.

Every PDF this backend renders (remittance advice, the SOX audit report, the
analytics reports, the 1099 working copies) sets its text in ReportLab's
built-in Helvetica. That is a standard Type 1 font with WinAnsi (cp1252)
encoding: it has no glyph for anything outside Latin-1 plus a few typographic
marks. A vendor called ``Ōsaka Seimitsu Kōgyō 株式会社`` came out as
``■saka Seimitsu K■gy■ ■■■■`` — on the remittance advice that is emailed to that
very supplier. Nothing had caught it because every seeded vendor name was
ASCII (#575 § 2).

:func:`pdf_markup` is the fix, and the only place it lives. It escapes a value
for ReportLab's inline-XML ``Paragraph`` markup (what each module's private
``_escape`` used to do) and wraps every run Helvetica cannot draw in a
``<font>`` that can:

* **CJK** (Han, kana, CJK punctuation, full-width forms) → ``HeiseiKakuGo-W5``,
  and **Hangul** → ``HYGothic-Medium``. Both are ReportLab's built-in Adobe CID
  fonts: nothing is bundled, and every mainstream viewer (Acrobat, Preview,
  pdf.js, pdfium) supplies the glyphs. Han characters are drawn with the
  Japanese face; it covers the common Chinese repertoire as well.
* **everything else** (Latin Extended, Greek, Cyrillic, …) → DejaVu Sans,
  bundled under ``app/assets/fonts/`` (Bitstream Vera / DejaVu licence, see
  ``DejaVu-LICENSE.txt``) and subset-embedded, so the file is self-contained.

Runs Helvetica CAN draw are left alone, so a document whose text is all Latin-1
renders exactly as it did before this module existed.

Only ``Paragraph`` text can carry the markup. A bare string in a ``Table`` cell
is drawn in the cell's single font with no markup at all, so a cell holding a
tenant- or vendor-supplied value must be a ``Paragraph`` of ``pdf_markup(...)``.
"""

from __future__ import annotations

import threading
from pathlib import Path

from reportlab.lib.fonts import addMapping
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"

#: Bundled TrueType face for every non-CJK character Helvetica lacks.
UNICODE_FONT = "DejaVuSans"
UNICODE_FONT_BOLD = "DejaVuSans-Bold"
#: ReportLab's built-in CID faces (not embedded; supplied by the viewer).
CJK_FONT = "HeiseiKakuGo-W5"
HANGUL_FONT = "HYGothic-Medium"

_lock = threading.Lock()
_registered = False


def ensure_fonts() -> None:
    """Register the fallback faces with ReportLab once per process (thread-safe:
    renders run in `asyncio.to_thread` workers)."""
    global _registered
    if _registered:
        return
    with _lock:
        if _registered:
            return
        pdfmetrics.registerFont(TTFont(UNICODE_FONT, str(FONT_DIR / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont(UNICODE_FONT_BOLD, str(FONT_DIR / "DejaVuSans-Bold.ttf")))
        # `<b>` inside a `<font name=…>` resolves through the family mapping.
        for bold in (0, 1):
            for italic in (0, 1):
                addMapping(UNICODE_FONT, bold, italic, UNICODE_FONT_BOLD if bold else UNICODE_FONT)
        for cid in (CJK_FONT, HANGUL_FONT):
            pdfmetrics.registerFont(UnicodeCIDFont(cid))
            for bold in (0, 1):
                for italic in (0, 1):
                    addMapping(cid, bold, italic, cid)
        _registered = True


def _helvetica_can_draw(ch: str) -> bool:
    try:
        ch.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def _is_hangul(cp: int) -> bool:
    return 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F


def _is_cjk(cp: int) -> bool:
    return (
        0x3000 <= cp <= 0x30FF  # CJK punctuation, hiragana, katakana
        or 0x3400 <= cp <= 0x4DBF  # CJK extension A
        or 0x4E00 <= cp <= 0x9FFF  # CJK unified ideographs
        or 0xF900 <= cp <= 0xFAFF  # CJK compatibility ideographs
        or 0xFF00 <= cp <= 0xFFEF  # half-width / full-width forms
    )


def font_for(ch: str) -> str | None:
    """The fallback face for ``ch``, or ``None`` when Helvetica draws it."""
    if _helvetica_can_draw(ch):
        return None
    cp = ord(ch)
    if _is_hangul(cp):
        return HANGUL_FONT
    if _is_cjk(cp):
        return CJK_FONT
    return UNICODE_FONT


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pdf_markup(value: object) -> str:
    """``value`` as ReportLab ``Paragraph`` markup that draws every character.

    Escapes ``& < >`` (so a vendor named ``<Manufacturing>`` cannot break the
    document) and wraps each run Helvetica has no glyph for in a ``<font>``
    that does. ``None`` renders as an empty string.
    """
    s = "" if value is None else str(value)
    out: list[str] = []
    run: list[str] = []
    run_font: str | None = None

    def flush() -> None:
        if not run:
            return
        text = _escape("".join(run))
        out.append(f'<font name="{run_font}">{text}</font>' if run_font else text)
        run.clear()

    for ch in s:
        f = font_for(ch)
        if f != run_font:
            flush()
            run_font = f
        run.append(ch)
    flush()

    if any('<font name="' in part for part in out):
        ensure_fonts()
    return "".join(out)
