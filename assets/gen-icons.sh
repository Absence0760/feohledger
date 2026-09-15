#!/usr/bin/env bash
#
# Regenerate every FeohLedger icon from the brand-mark masters. Deterministic,
# idempotent, safe to re-run.
#
#   1. logo-render/gen_svg.py --masters rewrites the SVG masters
#      (assets/icon*.svg, assets/logo-mark.svg, assets/og-image.svg) and the web
#      SVG copies.
#   2. Inkscape renders each master at every committed pixel size; ImageMagick
#      flattens what must be opaque and packs favicon.ico.
#
# Requires python3, inkscape and ImageMagick 7 (magick). None of that runs in
# CI: assets/check_icons.py (pnpm check:icons) verifies the committed result
# with the standard library alone. Rationale: docs/decisions.md §171.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ICON="$SCRIPT_DIR/icon.svg"
MARK="$SCRIPT_DIR/logo-mark.svg"
FOREGROUND="$SCRIPT_DIR/icon-foreground.svg"
BACKGROUND="$SCRIPT_DIR/icon-background.svg"
MONOCHROME="$SCRIPT_DIR/icon-monochrome.svg"
NOTIFICATION="$SCRIPT_DIR/icon-notification.svg"
OG_CARD="$SCRIPT_DIR/og-image.svg"

# The tile's darker gradient stop. The tile is already opaque, so flattening onto
# it only drops the alpha channel; the colour never shows.
OPAQUE_BG="#0A1124"

for tool in python3 inkscape magick; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: '$tool' not on PATH" >&2; exit 1; }
done

# render <svg> <size> <out>: PNG at size x size with alpha kept. -strip drops the
# text and date chunks, so re-rendering an unchanged master is byte-identical.
render() {
  local svg="$1" size="$2" out="$3"
  mkdir -p "$(dirname "$out")"
  inkscape "$svg" -w "$size" -h "$size" -o "$out" >/dev/null 2>&1
  magick "$out" -strip "$out"
  echo "  ${out#"$REPO_ROOT"/} (${size}px)"
}

# render_opaque <svg> <width> <height> <out>: flattened and forced to 8-bit RGB
# (PNG colour type 2). Without the explicit colour type ImageMagick may
# palette-optimise a small icon into an indexed PNG that still carries a tRNS
# chunk, and App Store Connect rejects any icon with alpha, indexed or not.
render_opaque() {
  local svg="$1" width="$2" height="$3" out="$4"
  mkdir -p "$(dirname "$out")"
  inkscape "$svg" -w "$width" -h "$height" -o "$out" >/dev/null 2>&1
  magick "$out" -background "$OPAQUE_BG" -alpha remove -alpha off -strip \
    -define png:color-type=2 "$out"
  echo "  ${out#"$REPO_ROOT"/} (${width}x${height}px, opaque)"
}

echo "[masters]"
python3 "$SCRIPT_DIR/logo-render/gen_svg.py" --masters

echo "[web]"
WEB="$REPO_ROOT/frontend/static"
render_opaque "$ICON" 180 180 "$WEB/apple-touch-icon.png"
render "$MARK" 192 "$WEB/icon-192.png"
render "$MARK" 512 "$WEB/icon-512.png"
render_opaque "$ICON" 512 512 "$WEB/icon-maskable-512.png"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for size in 16 32 48; do
  inkscape "$MARK" -w "$size" -h "$size" -o "$TMP/$size.png" >/dev/null 2>&1
done
magick "$TMP/16.png" "$TMP/32.png" "$TMP/48.png" -strip "$WEB/favicon.ico"
echo "  frontend/static/favicon.ico (16/32/48px)"
# The mark in outbound email headers, served from the frontend root. Email
# clients do not render SVG, hence a PNG.
render "$MARK" 96 "$WEB/email-mark.png"
# The link-preview card crawlers fetch from app.html's og:image.
render_opaque "$OG_CARD" 1200 630 "$WEB/og-image.png"

echo "[backend: PDF mark]"
# Embedded in branded PDFs with no network fetch.
render "$MARK" 256 "$REPO_ROOT/backend/app/assets/brand/logo-mark.png"

echo "[mobile: in-app mark]"
BRAND="$REPO_ROOT/mobile/assets/brand"
render "$MARK" 64 "$BRAND/logo_mark.png"
render "$MARK" 128 "$BRAND/2.0x/logo_mark.png"
render "$MARK" 192 "$BRAND/3.0x/logo_mark.png"

echo "[mobile: android]"
RES="$REPO_ROOT/mobile/android/app/src/main/res"
declare -A LEGACY=([mdpi]=48 [hdpi]=72 [xhdpi]=96 [xxhdpi]=144 [xxxhdpi]=192)
declare -A ADAPTIVE=([mdpi]=108 [hdpi]=162 [xhdpi]=216 [xxhdpi]=324 [xxxhdpi]=432)
for density in mdpi hdpi xhdpi xxhdpi xxxhdpi; do
  render "$MARK" "${LEGACY[$density]}" "$RES/mipmap-$density/ic_launcher.png"
  render "$FOREGROUND" "${ADAPTIVE[$density]}" "$RES/mipmap-$density/ic_launcher_foreground.png"
  render_opaque "$BACKGROUND" "${ADAPTIVE[$density]}" "${ADAPTIVE[$density]}" "$RES/mipmap-$density/ic_launcher_background.png"
  render "$MONOCHROME" "${ADAPTIVE[$density]}" "$RES/mipmap-$density/ic_launcher_monochrome.png"
done

echo "[mobile: android notification]"
# Status-bar icons are drawn from alpha alone, so this is the white silhouette,
# never the tile (which would render as a solid square). 24dp per density.
declare -A NOTIFICATION_PX=([mdpi]=24 [hdpi]=36 [xhdpi]=48 [xxhdpi]=72 [xxxhdpi]=96)
for density in mdpi hdpi xhdpi xxhdpi xxxhdpi; do
  render "$NOTIFICATION" "${NOTIFICATION_PX[$density]}" "$RES/drawable-$density/ic_notification.png"
done

echo "[mobile: launch screens]"
# The native launch screen shows the same 64dp mark the Flutter splash does,
# so the hand-off from the OS to Flutter doesn't jump.
declare -A LAUNCH_PX=([mdpi]=64 [hdpi]=96 [xhdpi]=128 [xxhdpi]=192 [xxxhdpi]=256)
for density in mdpi hdpi xhdpi xxhdpi xxxhdpi; do
  render "$MARK" "${LAUNCH_PX[$density]}" "$RES/drawable-$density/launch_mark.png"
done
LAUNCH_SET="$REPO_ROOT/mobile/ios/Runner/Assets.xcassets/LaunchImage.imageset"
render "$MARK" 64 "$LAUNCH_SET/LaunchImage.png"
render "$MARK" 128 "$LAUNCH_SET/LaunchImage@2x.png"
render "$MARK" 192 "$LAUNCH_SET/LaunchImage@3x.png"

echo "[mobile: ios]"
IOS="$REPO_ROOT/mobile/ios/Runner/Assets.xcassets/AppIcon.appiconset"
# Sizes come from Contents.json, the file Xcode reads, not from whatever PNGs
# happen to be on disk.
while read -r filename px; do
  render_opaque "$ICON" "$px" "$px" "$IOS/$filename"
done < <(python3 - "$IOS/Contents.json" <<'PY'
import json, sys
for image in json.load(open(sys.argv[1]))["images"]:
    side = float(image["size"].split("x")[0]) * float(image["scale"].rstrip("x"))
    print(image["filename"], round(side))
PY
)

echo "Done. Verify with: pnpm check:icons"
