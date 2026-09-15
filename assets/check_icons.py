#!/usr/bin/env python3
"""Verify the committed brand icons without Inkscape or ImageMagick.

CI's guard for assets/gen-icons.sh (docs/decisions.md §171). It fails when:
  - an SVG master or web SVG copy differs from what logo-render/gen_svg.py
    produces now, i.e. the geometry, the palette or a copy was edited by hand;
  - a raster target is missing or not the pixel size its platform expects;
  - an icon that must be opaque carries alpha (App Store Connect rejects an iOS
    icon with any alpha channel, an indexed palette's tRNS chunk included);
  - favicon.ico is missing its 16, 32 or 48 px entry;
  - the Android adaptive icon definition is missing;
  - the web manifest names an icon that is not there at the size it declares.

What it cannot tell is whether a PNG was rendered from the current master; that
needs a rasteriser. The first check is what forces a re-render: a master edit
without running gen-icons.sh leaves the committed SVG stale, and that fails.

Run: python3 assets/check_icons.py   (pnpm check:icons)
"""
import importlib.util
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

ANDROID_RES = "mobile/android/app/src/main/res"
DENSITY = {"mdpi": 1.0, "hdpi": 1.5, "xhdpi": 2.0, "xxhdpi": 3.0, "xxxhdpi": 4.0}
IOS_SET = "mobile/ios/Runner/Assets.xcassets/AppIcon.appiconset"
WEB = "frontend/static"

# PNG colour types that cannot carry alpha, provided there is no tRNS chunk.
OPAQUE_COLOUR_TYPES = {0, 2, 3}


def load_generator():
    path = os.path.join(HERE, "logo-render", "gen_svg.py")
    spec = importlib.util.spec_from_file_location("gen_svg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png_info(path):
    """Return (width, height, colour_type, has_trns) from the chunk stream."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    colour_type = data[25]
    has_trns = False
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        if kind == b"tRNS":
            has_trns = True
        if kind == b"IEND":
            break
        pos += 12 + length
    return width, height, colour_type, has_trns


def ico_sizes(path):
    with open(path, "rb") as fh:
        data = fh.read()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    if reserved != 0 or kind != 1:
        raise ValueError("not an ICO")
    return {(data[6 + 16 * i] or 256, data[7 + 16 * i] or 256) for i in range(count)}


def ios_targets():
    """The iOS set as Xcode reads it: Contents.json, not whatever PNGs are on disk."""
    with open(os.path.join(REPO, IOS_SET, "Contents.json"), encoding="utf-8") as fh:
        images = json.load(fh)["images"]
    targets = []
    for image in images:
        side = float(image["size"].split("x")[0]) * float(image["scale"].rstrip("x"))
        # Only a bare file name is honoured, so an entry carrying a separator
        # cannot point the check outside the icon set.
        bare = os.path.basename(image["filename"])
        targets.append((IOS_SET + "/" + bare, round(side), True))
    return targets


def raster_targets():
    """(relative path, side in px, must be opaque) for every committed PNG."""
    targets = [
        (f"{WEB}/apple-touch-icon.png", 180, True),
        (f"{WEB}/icon-192.png", 192, False),
        (f"{WEB}/icon-512.png", 512, False),
        (f"{WEB}/icon-maskable-512.png", 512, True),
        ("mobile/assets/brand/logo_mark.png", 64, False),
        ("mobile/assets/brand/2.0x/logo_mark.png", 128, False),
        ("mobile/assets/brand/3.0x/logo_mark.png", 192, False),
    ]
    for density, scale in DENSITY.items():
        base = f"{ANDROID_RES}/mipmap-{density}"
        targets += [
            (f"{base}/ic_launcher.png", round(48 * scale), False),
            (f"{base}/ic_launcher_foreground.png", round(108 * scale), False),
            (f"{base}/ic_launcher_background.png", round(108 * scale), True),
            (f"{base}/ic_launcher_monochrome.png", round(108 * scale), False),
        ]
    return targets + ios_targets()


def main():
    errors = []
    checked = 0

    for rel, expected in load_generator().masters().items():
        checked += 1
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            errors.append(f"{rel}: missing; run pnpm gen:icons")
            continue
        with open(path, encoding="utf-8") as fh:
            if fh.read() != expected:
                errors.append(f"{rel}: differs from logo-render/gen_svg.py; run pnpm gen:icons")

    sizes = {}
    for rel, side, opaque in raster_targets():
        checked += 1
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            errors.append(f"{rel}: missing; run pnpm gen:icons")
            continue
        try:
            width, height, colour_type, has_trns = png_info(path)
        except ValueError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        sizes[rel] = (width, height)
        if (width, height) != (side, side):
            errors.append(f"{rel}: {width}x{height}, expected {side}x{side}")
        if opaque and (colour_type not in OPAQUE_COLOUR_TYPES or has_trns):
            errors.append(f"{rel}: has an alpha channel; it must be opaque")

    checked += 1
    ico = f"{WEB}/favicon.ico"
    try:
        missing = {(16, 16), (32, 32), (48, 48)} - ico_sizes(os.path.join(REPO, ico))
        if missing:
            errors.append(f"{ico}: no entry for {sorted(missing)}")
    except (OSError, ValueError) as exc:
        errors.append(f"{ico}: {exc}")

    checked += 1
    adaptive = f"{ANDROID_RES}/mipmap-anydpi-v26/ic_launcher.xml"
    if not os.path.exists(os.path.join(REPO, adaptive)):
        errors.append(f"{adaptive}: missing, so Android 8+ plates the legacy icon")

    manifest_rel = f"{WEB}/manifest.webmanifest"
    with open(os.path.join(REPO, manifest_rel), encoding="utf-8") as fh:
        manifest = json.load(fh)
    for icon in manifest.get("icons", []):
        checked += 1
        rel = f"{WEB}/" + icon["src"].removeprefix("./").removeprefix("/")
        declared = tuple(int(n) for n in icon["sizes"].split("x"))
        if rel not in sizes:
            errors.append(f"{manifest_rel}: icon {icon['src']} is not a checked raster")
        elif sizes[rel] != declared:
            errors.append(f"{manifest_rel}: {icon['src']} declares {icon['sizes']}, is {sizes[rel]}")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"check_icons: {len(errors)} problem(s)", file=sys.stderr)
        return 1
    print(f"check_icons: ok ({checked} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
