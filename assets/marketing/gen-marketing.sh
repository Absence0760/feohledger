#!/usr/bin/env bash
#
# Regenerate the marketing page's generated assets. Deterministic and
# idempotent on one machine: re-running with no source change rewrites
# byte-identical files (a different GPU backend changes the noise, not the
# image — see tally_scene.py).
#
#   1. Blender renders the split tally ornament (tally_scene.py) with Cycles on
#      the GPU, once per page theme — `dark` for the navy palette, `light` for
#      the light one (docs/decisions.md §306) — then ImageMagick trims the
#      transparent margin, downsamples it to the width the page serves, and
#      encodes it as WebP. The two lightings share geometry, camera and depth
#      of field, so both trim to the same frame and the page swaps them with
#      no layout change.
#   2. The grain overlay is written as an SVG feTurbulence filter — a few hundred
#      bytes that tile without a seam at any size.
#
# Requires blender, magick (ImageMagick 7). None of it runs in CI; the committed
# results are what ship, exactly like assets/gen-icons.sh. Rationale and the
# reason the ornament is a render rather than a drawing: docs/decisions.md §180.
#
# Usage: gen-marketing.sh [all|dark|light]   (default: all)
#   Naming one variant re-renders only that ornament (and rewrites the grain),
#   so retuning one lighting leaves the other's committed bytes untouched.
#
set -euo pipefail

VARIANTS="${1:-all}"
case "$VARIANTS" in
  all) VARIANTS="dark light" ;;
  dark|light) ;;
  *) echo "usage: $0 [all|dark|light]" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT_DIR="$REPO_ROOT/frontend/static/marketing"
RENDER_DIR="$SCRIPT_DIR/render"

# $BLENDER, else the workstation's ~/.local/bin build, else whatever is on PATH
# (Homebrew's cask on a Mac).
if [ -z "${BLENDER:-}" ]; then
  if [ -x "$HOME/.local/bin/blender" ]; then BLENDER="$HOME/.local/bin/blender"
  else BLENDER="$(command -v blender || true)"; fi
fi

command -v magick >/dev/null 2>&1 || { echo "error: 'magick' not on PATH" >&2; exit 1; }
[ -n "$BLENDER" ] && [ -x "$BLENDER" ] || { echo "error: blender not found (set \$BLENDER)" >&2; exit 1; }

mkdir -p "$OUT_DIR"

# variant -> output basename. Dark keeps the original name so nothing that
# already references it moves.
out_name() { if [ "$1" = dark ]; then echo tally-split; else echo "tally-split-$1"; fi; }

for variant in $VARIANTS; do
name="$(out_name "$variant")"
echo "[render] split tally ornament, $variant lighting (Cycles)"
TALLY_VARIANT="$variant" "$BLENDER" -b --python "$SCRIPT_DIR/tally_scene.py" >/dev/null

echo "[web] downsampling $name"
# -trim removes the transparent margin the render frames with, so the page
# controls the object's size rather than inheriting Blender's framing; the 2%
# border puts a little back so the drop-off at the edges isn't a hard cut.
# -strip drops the timestamp chunk, which is the only thing that would make two
# renders of identical pixels differ on disk.
# WebP, not PNG: the ornament is a continuous-tone render with soft gradients,
# which is the case PNG is worst at — the same pixels are 243 KB as PNG and
# 32 KB here, on a page every cold visitor loads. Nothing needs a fallback;
# WebP has been in every shipping browser since 2020.
magick "$RENDER_DIR/$name.png" \
  -trim +repage \
  -bordercolor none -border 2% \
  -resize 1100x \
  -strip \
  -quality 86 -define webp:method=6 \
  "$OUT_DIR/$name.webp"
done

echo "[web] grain overlay"
# An SVG filter rather than a noise bitmap. A raster tile that is fine enough not
# to read as a repeat costs ~50 KB, because noise is the one thing PNG cannot
# compress; feTurbulence is ~400 bytes, resolution-independent, and has no tile
# seam to hide. `fractalNoise` (not the default `turbulence`) is the one that
# looks like film grain rather than smoke.
#
# The opacity is baked into the filter's own alpha, not applied as a CSS
# `opacity` on the element — `src/lib/a11y/opacityAudit.test.ts` reports every
# at-rest fade in the tree and this one would have to be argued into its
# allowlist for no gain.
cat > "$OUT_DIR/grain.svg" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" width="220" height="220" viewBox="0 0 220 220">
  <filter id="g" x="0" y="0" width="100%" height="100%">
    <feTurbulence type="fractalNoise" baseFrequency="0.82" numOctaves="3" seed="11" stitchTiles="stitch"/>
    <feColorMatrix type="saturate" values="0"/>
    <feComponentTransfer><feFuncA type="linear" slope="0.2"/></feComponentTransfer>
  </filter>
  <rect width="220" height="220" filter="url(#g)"/>
</svg>
SVG

echo
echo "wrote:"
for variant in $VARIANTS; do
  name="$(out_name "$variant")"
  printf '  %-44s %s\n' "frontend/static/marketing/$name.webp" \
    "$(magick identify -format '%wx%h, %b' "$OUT_DIR/$name.webp")"
done
# The page gives both <source>s one width/height; a variant that trimmed to a
# different frame would shift the layout on a theme switch.
if [ -f "$OUT_DIR/tally-split.webp" ] && [ -f "$OUT_DIR/tally-split-light.webp" ]; then
  a="$(magick identify -format '%wx%h' "$OUT_DIR/tally-split.webp")"
  b="$(magick identify -format '%wx%h' "$OUT_DIR/tally-split-light.webp")"
  [ "$a" = "$b" ] || { echo "error: ornament variants differ in size ($a vs $b)" >&2; exit 1; }
fi
printf '  %-44s %s\n' "frontend/static/marketing/grain.svg" \
  "$(wc -c < "$OUT_DIR/grain.svg" | tr -d ' ') bytes"
