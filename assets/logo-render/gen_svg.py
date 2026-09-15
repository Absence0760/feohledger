#!/usr/bin/env python3
"""Generate the FeohLedger brand mark: the shipped masters and the exploration.

The mark is feoh (ᚠ), the first rune of the Old English rune-row and the root
of "fee", written as a split Exchequer tally stick (docs/decisions.md §171).
Every concept from the exploration is kept here as geometry so the choice can
be revisited without re-deriving it; CHOSEN names the one that ships.

Run:
  python3 assets/logo-render/gen_svg.py            # masters + exploration
  python3 assets/logo-render/gen_svg.py --masters  # masters only

Masters are committed. assets/gen-icons.sh rasterises every platform icon from
them, and assets/check_icons.py fails CI when a committed master no longer
matches what this script produces:
  assets/icon.svg             full-bleed tile: iOS, apple-touch, web maskable
  assets/logo-mark.svg        rounded tile: in-app mark, favicon, Android legacy
  assets/icon-foreground.svg  Android adaptive foreground (66dp safe zone)
  assets/icon-background.svg  Android adaptive background
  assets/icon-monochrome.svg  Android 13+ themed icon
  assets/icon-notification.svg  Android notification small icon (white silhouette)
  frontend/static/logo-mark.svg, frontend/static/favicon.svg  (copies)

Exploration output is gitignored: assets/logo-render/svg/<concept>_<palette>.svg

Geometry lives in a 100x100 glyph space and is placed on a 100x100 tile by
translate(50 50) scale(s) translate(-cx -cy). Colour tokens are @fg@, @fg2@
and @cut@; @uid@ keeps mask and clip ids unique within a document.
"""
import argparse
import math
import os

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(BASE))
SVG_DIR = os.path.join(BASE, "svg")


def tan(deg):
    return math.tan(math.radians(deg))


def pts(points):
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def poly(points, tok="fg", extra=""):
    return f'<polygon points="{pts(points)}" fill="@{tok}@"{extra}/>'


def rect(x0, y0, x1, y1, tok="fg", extra=""):
    return (f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" '
            f'height="{y1 - y0:.2f}" fill="@{tok}@"{extra}/>')


def branch(root_top, t, x1, k, sx):
    """Parallelogram rising to the right from a stave whose right edge is sx.

    root_top is the top edge at x=sx. It starts 2 units inside the stave so the
    join has no seam, but not so far in that it pokes out of the stave's ends.
    """
    x0 = sx - 2
    top = lambda x: root_top - (x - sx) * k  # noqa: E731
    return [(x0, top(x0)), (x1, top(x1)), (x1, top(x1) + t), (x0, top(x0) + t)]


def rune(sx0=30, sx1=44, y0=10, y1=90, tip_x=76, t=15, gap=31, deg=40, tok="fg"):
    """The plain feoh: stave + two parallel branches cut vertically at tip_x."""
    k = tan(deg)
    r1 = y0 + (tip_x - sx1) * k
    return (rect(sx0, y0, sx1, y1, tok)
            + poly(branch(r1, t, tip_x, k, sx1), tok)
            + poly(branch(r1 + gap, t, tip_x, k, sx1), tok))


def mask(mid, shapes):
    return (f'<mask id="{mid}-@uid@" maskUnits="userSpaceOnUse" x="-100" y="-100" '
            f'width="300" height="300"><rect x="-100" y="-100" width="300" '
            f'height="300" fill="#fff"/>{shapes}</mask>')


def reach(c, extremes):
    """Farthest glyph-space distance from the placement centre."""
    return max(math.hypot(x - c[0], y - c[1]) for x, y in extremes)


# Each concept: geom (token template), c (glyph-space centre), s (tile scale),
# reach (farthest point from c, used to keep a mask's safe zone honest).
CONCEPTS = {}

# Futhorc: the rune itself, cut in the same slab geometry as Threkir's thorn.
CONCEPTS["futhorc"] = dict(geom=rune(), c=(53, 50), s=0.74)

# Tally (shipped): a split Exchequer tally stick. The foil carries the rune; the
# stock (longer, dimmer) carries the matching notches where the branches cross.
_k = tan(40)
_r1 = 8 + (77 - 45) * _k
_slots = "".join(
    '<polygon points="' + pts([
        (18, r + 7.5 - 2.6 - (18 - 45) * _k), (38, r + 7.5 - 2.6 - (38 - 45) * _k),
        (38, r + 7.5 + 2.6 - (38 - 45) * _k), (18, r + 7.5 + 2.6 - (18 - 45) * _k),
    ]) + '" fill="#000"/>'
    for r in (_r1, _r1 + 31))
CONCEPTS["tally"] = dict(
    geom=(mask("tm", _slots)
          + rect(24, 18, 33, 97, "fg2", ' mask="url(#tm-@uid@)"')
          + rect(36, 8, 45, 86)
          + poly(branch(_r1, 15, 77, _k, 45))
          + poly(branch(_r1 + 31, 15, 77, _k, 45))),
    c=(50.5, 52.5), s=0.70)
CONCEPTS["tally"]["reach"] = reach(CONCEPTS["tally"]["c"], [(24, 18), (24, 97), (36, 8), (77, 8), (77, 54)])

# Total: the double rule a bookkeeper draws under a final total.
_r1 = 6 + (76 - 44) * _k
CONCEPTS["total"] = dict(
    geom=(rect(30, 6, 44, 73)
          + poly(branch(_r1, 13, 76, _k, 44))
          + poly(branch(_r1 + 24, 13, 76, _k, 44))
          + rect(20, 79, 86, 87) + rect(20, 92, 86, 100)),
    c=(53, 53), s=0.68)

# Credit: a T-account. Payables are liabilities; they grow on the credit (right)
# side, so that is where the rune's strokes go.
_k34 = tan(34)
_r1 = 32 + (86 - 56) * _k34
CONCEPTS["credit"] = dict(
    geom=(rect(10, 8, 90, 18) + rect(44, 18, 56, 94)
          + poly(branch(_r1, 13, 86, _k34, 56))
          + poly(branch(_r1 + 26, 13, 86, _k34, 56))),
    c=(50, 50), s=0.68)

# Tick: the auditor's tick mark. Each branch is a vouched line.
CONCEPTS["tick"] = dict(
    geom=(rect(26, 10, 40, 90)
          + '<path d="M34 31 L49 46 L81 10 M34 59 L49 74 L81 38" fill="none" '
            'stroke="@fg@" stroke-width="12" stroke-linejoin="miter" '
            'stroke-miterlimit="4" stroke-linecap="butt"/>'),
    c=(55, 50), s=0.70)

# Sceat: an early silver penny; some were struck with runes.
_beads = "".join(
    f'<circle cx="{50 + 40.5 * math.cos(2 * math.pi * i / 36):.2f}" '
    f'cy="{50 + 40.5 * math.sin(2 * math.pi * i / 36):.2f}" r="1.75" fill="@cut@"/>'
    for i in range(36))
CONCEPTS["sceat"] = dict(
    geom=('<circle cx="50" cy="50" r="46" fill="@fg@"/>' + _beads
          + '<g transform="translate(50 50) scale(0.64) translate(-53 -50)">'
          + rune(tok="cut") + '</g>'),
    c=(50, 50), s=0.76)

# Quill: the ledger's pen. Shaft and split nib form the stave; two vanes the branches.
CONCEPTS["quill"] = dict(
    geom=(mask("qm", '<rect x="36.2" y="80" width="1.6" height="20" fill="#000"/>'
                     '<circle cx="37" cy="79" r="2.3" fill="#000"/>')
          + poly([(30, 6), (44, 6), (44, 78), (37, 99), (30, 78)],
                 extra=' mask="url(#qm-@uid@)"')
          + '<path d="M43 30 Q60 24 82 2 Q72 34 43 51 Z" fill="@fg@"/>'
          + '<path d="M43 57 Q60 51 82 29 Q72 61 43 78 Z" fill="@fg@"/>'),
    c=(56, 50.5), s=0.70)

# Brand: feoh first meant cattle (as Latin pecus gave pecunia, money). A
# rancher's "rocking" brand, the letter sitting on a curved bar.
CONCEPTS["brand"] = dict(
    geom=('<path d="M36 12 V74 M36 36 L70 12 M36 60 L70 36" fill="none" '
          'stroke="@fg@" stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/>'
          '<path d="M14 84 Q50 106 86 84" fill="none" stroke="@fg@" stroke-width="10" '
          'stroke-linecap="round"/>'),
    c=(50, 53), s=0.68)

# Dealt: the rune poem says wealth must be dealt out. The branches run off the
# tile, the way a payment leaves the building.
CONCEPTS["dealt"] = dict(
    geom=(rect(30, 12, 44, 92)
          + poly(branch(26, 15, 230, _k, 44))
          + poly(branch(26 + 31, 15, 230, _k, 44))),
    c=(63, 48), s=0.70, bleed=True)


PALETTES = {
    "gilt": dict(bg1="#24345C", bg2="#0A1124", fg="#E7B95E", fg2="#8F7440", cut="#0F1A33"),
    "ledger": dict(bg1="#1B7657", bg2="#093326", fg="#F3EAD0", fg2="#8DB7A2", cut="#0C4332"),
    "signal": dict(bg1="#7196FF", bg2="#6448E4", fg="#FFFFFF", fg2="#C6D2FF", cut="#4B55D6"),
}
MONOCHROME = dict(fg="#000000", fg2="#000000", cut="#000000")

CHOSEN = ("tally", "gilt")

# Corner radius of the rounded tile, as a percentage of its side.
RX = 22.5
# Web maskable icons keep a circle of radius 40% of the side; iOS masks less.
MASKABLE_SAFE_RADIUS = 40.0
# Android adaptive icons: a 108dp canvas of which only a 66dp circle is
# guaranteed visible, i.e. radius 33/108 = 30.56% of the side.
ADAPTIVE_SAFE_RADIUS = 30.5


def fill(template, pal, uid):
    out = template.replace("@uid@", uid)
    for tok in ("fg2", "fg", "cut"):
        out = out.replace(f"@{tok}@", pal[tok])
    return out


def _svg(px, body, defs=""):
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
            f'viewBox="0 0 100 100">\n')
    return head + (f"<defs>\n{defs}</defs>\n" if defs else "") + body + "</svg>\n"


def _gradient(gid, pal):
    return (f'<linearGradient id="{gid}" x1="0" y1="0" x2="100" y2="100" '
            f'gradientUnits="userSpaceOnUse">\n'
            f'  <stop offset="0" stop-color="{pal["bg1"]}"/>\n'
            f'  <stop offset="1" stop-color="{pal["bg2"]}"/>\n'
            f'</linearGradient>\n')


def _glyph(name, pal, uid, scale):
    c = CONCEPTS[name]
    cx, cy = c["c"]
    return (f'<g transform="translate(50 50) scale({scale:.4g}) '
            f'translate({-cx:g} {-cy:g})">\n{fill(c["geom"], pal, uid)}\n</g>\n')


def tile_svg(name, pal, rx=RX, px=1024, uid="t"):
    """The glyph on its gradient tile. rx=0 gives the full-bleed square."""
    c = CONCEPTS[name]
    corner = f' rx="{rx:g}"' if rx else ""
    defs = _gradient(f"bg-{uid}", pal)
    glyph = _glyph(name, pal, uid, c["s"])
    if c.get("bleed"):
        defs += f'<clipPath id="cl-{uid}"><rect width="100" height="100"{corner}/></clipPath>\n'
        glyph = f'<g clip-path="url(#cl-{uid})">\n{glyph}</g>\n'
    body = f'<rect width="100" height="100"{corner} fill="url(#bg-{uid})"/>\n' + glyph
    return _svg(px, body, defs)


def background_svg(pal, px=1024):
    return _svg(px, '<rect width="100" height="100" fill="url(#bg-b)"/>\n', _gradient("bg-b", pal))


def foreground_svg(name, pal, px=1024, uid="f"):
    """The glyph alone on a transparent canvas, shrunk into the adaptive safe zone."""
    return _svg(px, _glyph(name, pal, uid, ADAPTIVE_SAFE_RADIUS / CONCEPTS[name]["reach"]))


# Android draws a notification's small icon from its alpha channel alone, so the
# colour tile renders as a solid white square. The icon is a white silhouette on
# a 24dp canvas with 2dp of padding: the glyph must fit the 20dp inside, a half
# side of 50 * 20 / 24 units.
SILHOUETTE = dict(fg="#FFFFFF", fg2="#FFFFFF", cut="#FFFFFF")
NOTIFICATION_LIVE_HALF = 50 * 20 / 24


def notification_svg(name, px=1024, uid="n"):
    """White glyph sized so its farthest point stays inside the live area."""
    scale = NOTIFICATION_LIVE_HALF / CONCEPTS[name]["reach"]
    return _svg(px, _glyph(name, SILHOUETTE, uid, scale))


def masters():
    """Relative path -> content for every committed master."""
    name, pname = CHOSEN
    pal = PALETTES[pname]
    concept = CONCEPTS[name]
    if concept["reach"] * concept["s"] > MASKABLE_SAFE_RADIUS:
        raise SystemExit(f"{name}: glyph leaves the maskable safe zone at scale {concept['s']}")
    mark = tile_svg(name, pal, rx=RX, uid="mark")
    return {
        "assets/icon.svg": tile_svg(name, pal, rx=0, uid="icon"),
        "assets/logo-mark.svg": mark,
        "assets/icon-foreground.svg": foreground_svg(name, pal, uid="fg"),
        "assets/icon-background.svg": background_svg(pal),
        "assets/icon-monochrome.svg": foreground_svg(name, MONOCHROME, uid="mono"),
        "assets/icon-notification.svg": notification_svg(name, uid="note"),
        "frontend/static/logo-mark.svg": mark,
        "frontend/static/favicon.svg": mark,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--masters", action="store_true", help="write the masters only")
    args = parser.parse_args()

    for rel, content in masters().items():
        with open(os.path.join(REPO, rel), "w", encoding="utf-8") as fh:
            fh.write(content)
    print(f"wrote {len(masters())} masters")

    if not args.masters:
        os.makedirs(SVG_DIR, exist_ok=True)
        for name in CONCEPTS:
            for pname, pal in PALETTES.items():
                with open(os.path.join(SVG_DIR, f"{name}_{pname}.svg"), "w", encoding="utf-8") as fh:
                    fh.write(tile_svg(name, pal))
        print(f"wrote {len(CONCEPTS) * len(PALETTES)} exploration tiles to {SVG_DIR}")


if __name__ == "__main__":
    main()
