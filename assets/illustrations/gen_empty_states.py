#!/usr/bin/env python3
"""Generate the empty-state illustrations: geometry in, a typed data module out.

    python3 assets/illustrations/gen_empty_states.py            # write the module
    python3 assets/illustrations/gen_empty_states.py --check    # fail if stale
    python3 assets/illustrations/gen_empty_states.py --preview  # + PNG contact sheet

Why a data module and not SVG files. The art has to follow the theme — muted
strokes from the palette, and the ACCENT from the tenant's own brand, which a
white-label tenant may override at runtime. An `<img src=*.svg>` cannot see a
CSS custom property; inline SVG can. And the frontend has no `{@html}` anywhere
by design (frontend/CLAUDE.md § Stack), so the markup cannot arrive as a
string. So the geometry lives here, is emitted as plain data, and
`ui/EmptyState.svelte` renders each shape as a real element whose class names a
TONE. Colour is decided in that component's CSS, never in this file.

Tones:
  line         outline strokes, in the muted palette
  paper        a card-like surface fill, with a line stroke
  soft         a faint ground fill, no stroke
  accent       strokes in the tenant accent
  accent-soft  a tint of the tenant accent, no stroke

Every illustration sits on the same 160 × 120 canvas with the same ground
ellipse, so a page that swaps one for another does not change height.

`--preview` substitutes a fixed palette, writes one SVG per illustration and a
contact sheet under assets/illustrations/preview/ (gitignored), and renders
them with Inkscape and ImageMagick. It is for looking at, not for shipping.

`--check` is what CI runs (stdlib only): the committed module must be exactly
what this file produces, so the geometry cannot be edited in one place and not
the other.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
MODULE = os.path.join(REPO, "frontend", "src", "lib", "components", "ui", "emptyStateArt.generated.ts")
PREVIEW = os.path.join(HERE, "preview")

W, H = 160, 120


def rect(x, y, w, h, tone, rx=6):
    return {"el": "rect", "tone": tone, "attrs": {"x": x, "y": y, "width": w, "height": h, "rx": rx}}


def circle(cx, cy, r, tone):
    return {"el": "circle", "tone": tone, "attrs": {"cx": cx, "cy": cy, "r": r}}


def path(d, tone):
    return {"el": "path", "tone": tone, "attrs": {"d": d}}


def ground():
    """The shared shadow every illustration stands on."""
    return path("M26 104 C 26 97, 134 97, 134 104 C 134 111, 26 111, 26 104 Z", "soft")


def sparkle(cx, cy, s=4):
    return path(f"M{cx} {cy - s} V{cy + s} M{cx - s} {cy} H{cx + s}", "accent")


def text_lines(x, y, widths, gap=8):
    return [path(f"M{x} {y + i * gap} H{x + w}", "line") for i, w in enumerate(widths)]


def check(cx, cy, s=6):
    return path(f"M{cx - s} {cy} L{cx - s / 3:.1f} {cy + s * 0.66:.1f} L{cx + s} {cy - s * 0.66:.1f}", "accent")


ART = {
    # A stack of bills with the top one vouched.
    "invoices": [
        ground(),
        rect(40, 20, 58, 74, "paper"),
        rect(56, 28, 62, 76, "paper"),
        *text_lines(66, 44, [30, 40, 24]),
        path("M66 72 H108", "line"),
        path("M92 84 H108", "line"),
        circle(116, 90, 13, "accent-soft"),
        circle(116, 90, 13, "accent"),
        check(116, 90),
        sparkle(34, 34),
    ],
    # A storefront: who you buy from.
    "vendors": [
        ground(),
        rect(44, 50, 72, 52, "paper", rx=4),
        path("M38 50 L46 28 H114 L122 50 Z", "paper"),
        path("M58 28 L54 50 M72 28 L70 50 M88 28 L90 50 M102 28 L106 50", "line"),
        rect(70, 72, 20, 30, "accent-soft", rx=3),
        path("M70 102 V75 a3 3 0 0 1 3 -3 H87 a3 3 0 0 1 3 3 V102", "accent"),
        rect(52, 62, 12, 10, "line", rx=2),
        rect(96, 62, 12, 10, "line", rx=2),
        sparkle(128, 30),
    ],
    # A card and a stack of coins.
    "payments": [
        ground(),
        rect(28, 34, 76, 50, "paper", rx=7),
        path("M28 48 H104", "line"),
        rect(38, 58, 16, 12, "accent-soft", rx=2),
        rect(38, 58, 16, 12, "accent", rx=2),
        *text_lines(62, 66, [28, 18], gap=8),
        circle(116, 88, 16, "paper"),
        circle(116, 80, 16, "paper"),
        circle(116, 80, 9, "accent-soft"),
        circle(116, 80, 9, "accent"),
        sparkle(128, 42),
    ],
    # An inbox tray with nothing waiting.
    "inbox": [
        ground(),
        path("M34 66 L50 36 H110 L126 66 V96 a4 4 0 0 1 -4 4 H38 a4 4 0 0 1 -4 -4 Z", "paper"),
        path("M34 66 H62 a4 4 0 0 1 4 4 v2 a6 6 0 0 0 6 6 H88 a6 6 0 0 0 6 -6 v-2 a4 4 0 0 1 4 -4 H126", "line"),
        path("M80 16 V44 M70 34 L80 44 L90 34", "accent"),
        sparkle(30, 30),
        sparkle(132, 26, 3),
    ],
    # A report: bars with a trend drawn through them.
    "chart": [
        ground(),
        rect(30, 22, 100, 78, "paper"),
        rect(44, 70, 12, 20, "soft", rx=2),
        rect(62, 58, 12, 32, "soft", rx=2),
        rect(80, 48, 12, 42, "accent-soft", rx=2),
        rect(98, 38, 12, 52, "soft", rx=2),
        path("M44 90 H116", "line"),
        path("M50 64 L68 52 L86 42 L104 32", "accent"),
        circle(104, 32, 3, "accent"),
        sparkle(136, 20),
    ],
    # A shield with a tick: nothing needs attention.
    "shield": [
        ground(),
        path("M80 16 L112 28 V56 C112 78, 98 92, 80 100 C62 92, 48 78, 48 56 V28 Z", "paper"),
        path("M80 26 L103 35 V56 C103 72, 93 83, 80 90 C67 83, 57 72, 57 56 V35 Z", "accent-soft"),
        check(80, 58, 11),
        sparkle(38, 34),
        sparkle(124, 76, 3),
    ],
    # Steps joined into a flow.
    "workflow": [
        ground(),
        rect(22, 30, 36, 24, "paper"),
        rect(102, 30, 36, 24, "paper"),
        rect(62, 70, 36, 24, "accent-soft"),
        rect(62, 70, 36, 24, "accent"),
        path("M58 42 C 70 42, 70 82, 62 82", "line"),
        path("M98 82 C 90 82, 90 42, 102 42", "line"),
        *text_lines(30, 42, [20]),
        *text_lines(110, 42, [20]),
        path("M72 82 H88", "accent"),
        sparkle(80, 22),
    ],
    # Documents on file, and a lens over them.
    "documents": [
        ground(),
        rect(34, 22, 56, 72, "paper"),
        *text_lines(44, 38, [36, 28, 34, 20]),
        circle(98, 70, 18, "accent-soft"),
        circle(98, 70, 18, "accent"),
        path("M111 83 L124 96", "accent"),
        sparkle(126, 30),
    ],
}


def module_text():
    lines = [
        "// GENERATED by assets/illustrations/gen_empty_states.py — do not edit by hand.",
        "// Change the geometry there and run `pnpm gen:illustrations`; `pnpm check:illustrations`",
        "// fails CI when this file and the generator disagree.",
        "",
        "export type ArtTone = 'line' | 'paper' | 'soft' | 'accent' | 'accent-soft';",
        "",
        "export interface ArtShape {",
        "\tel: 'rect' | 'circle' | 'path';",
        "\ttone: ArtTone;",
        "\tattrs: Record<string, string | number>;",
        "}",
        "",
        f"export const ART_VIEWBOX = '0 0 {W} {H}';",
        "",
        # The key union is written out rather than inferred with `keyof typeof`:
        # annotating the object with it keeps every shape typed as ArtShape, where
        # inference would give each illustration its own narrow literal type.
        "export type EmptyStateArt = " + " | ".join(f"'{name}'" for name in ART) + ";",
        "",
        "export const EMPTY_STATE_ART: Record<EmptyStateArt, ArtShape[]> = {",
    ]
    for name, shapes in ART.items():
        lines.append(f"\t{name}: [")
        for shape in shapes:
            lines.append("\t\t" + json.dumps(shape, separators=(", ", ": ")) + ",")
        lines.append("\t],")
    lines += [
        "};",
        "",
    ]
    return "\n".join(lines)


PREVIEW_TONES = {
    "line": 'fill="none" stroke="#8a8fa0" stroke-width="2"',
    "paper": 'fill="#1f2230" stroke="#8a8fa0" stroke-width="2"',
    "soft": 'fill="#232634"',
    "accent": 'fill="none" stroke="#638cff" stroke-width="2.4"',
    "accent-soft": 'fill="#263155"',
}


def preview():
    for tool in ("inkscape", "magick"):
        if not shutil.which(tool):
            sys.exit(f"--preview needs {tool} on PATH")
    os.makedirs(PREVIEW, exist_ok=True)
    pngs = []
    for name, shapes in ART.items():
        body = []
        for shape in shapes:
            attrs = " ".join(f'{k}="{v}"' for k, v in shape["attrs"].items())
            body.append(
                f'<{shape["el"]} {attrs} {PREVIEW_TONES[shape["tone"]]} '
                'stroke-linecap="round" stroke-linejoin="round"/>'
            )
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * 2}" height="{H * 2}" '
            f'viewBox="0 0 {W} {H}"><rect width="{W}" height="{H}" fill="#181a23"/>'
            + "".join(body) + "</svg>\n"
        )
        svg_path = os.path.join(PREVIEW, f"{name}.svg")
        png_path = os.path.join(PREVIEW, f"{name}.png")
        with open(svg_path, "w") as f:
            f.write(svg)
        subprocess.run(["inkscape", svg_path, "-o", png_path], check=True, capture_output=True)
        pngs.append(png_path)
    sheet = os.path.join(PREVIEW, "contact-sheet.png")
    subprocess.run(
        ["magick", "montage", *pngs, "-tile", "4x", "-geometry", "+8+8", "-background", "#0f1117", sheet],
        check=True,
    )
    print(f"wrote {sheet}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--preview", action="store_true")
    # Where the committed module lives. Overridable only so the guard's own
    # test can point `--check` at a deliberately broken copy.
    parser.add_argument("--module", default=MODULE)
    args = parser.parse_args()
    module = args.module
    text = module_text()
    if args.check:
        try:
            with open(module) as f:
                committed = f.read()
        except FileNotFoundError:
            committed = None
        if committed != text:
            print(f"{module} is stale: run `pnpm gen:illustrations`", file=sys.stderr)
            return 1
        print("empty-state illustrations match the generator")
        return 0
    with open(module, "w") as f:
        f.write(text)
    print(f"wrote {os.path.relpath(module, REPO)} ({len(ART)} illustrations)")
    if args.preview:
        preview()
    return 0


if __name__ == "__main__":
    sys.exit(main())
