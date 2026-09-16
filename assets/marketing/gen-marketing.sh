#!/usr/bin/env bash
#
# Regenerate the marketing page's two generated assets. Deterministic and
# idempotent: re-running with no source change rewrites byte-identical files.
#
#   1. Blender renders the split tally ornament (tally_scene.py) with Cycles on
#      the GPU, then ImageMagick trims the transparent margin, downsamples it to
#      the width the page serves, and encodes it as WebP.
#   2. The grain overlay is written as an SVG feTurbulence filter — a few hundred
#      bytes that tile without a seam at any size.
#
# Requires blender, magick (ImageMagick 7). None of it runs in CI; the committed
# results are what ship, exactly like assets/gen-icons.sh. Rationale and the
# reason the ornament is a render rather than a drawing: docs/decisions.md §180.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT_DIR="$REPO_ROOT/frontend/static/marketing"
RENDER="$SCRIPT_DIR/render/tally-split.png"

BLENDER="${BLENDER:-$HOME/.local/bin/blender}"

command -v magick >/dev/null 2>&1 || { echo "error: 'magick' not on PATH" >&2; exit 1; }
[ -x "$BLENDER" ] || { echo "error: blender not found at $BLENDER (set \$BLENDER)" >&2; exit 1; }

mkdir -p "$OUT_DIR"

echo "[render] split tally ornament (Cycles / OPTIX)"
"$BLENDER" -b --python "$SCRIPT_DIR/tally_scene.py" >/dev/null

echo "[web] downsampling"
# -trim removes the transparent margin the render frames with, so the page
# controls the object's size rather than inheriting Blender's framing; the 2%
# border puts a little back so the drop-off at the edges isn't a hard cut.
# -strip drops the timestamp chunk, which is the only thing that would make two
# renders of identical pixels differ on disk.
# WebP, not PNG: the ornament is a continuous-tone render with soft gradients,
# which is the case PNG is worst at — the same pixels are 243 KB as PNG and
# 32 KB here, on a page every cold visitor loads. Nothing needs a fallback;
# WebP has been in every shipping browser since 2020.
magick "$RENDER" \
  -trim +repage \
  -bordercolor none -border 2% \
  -resize 1100x \
  -strip \
  -quality 86 -define webp:method=6 \
  "$OUT_DIR/tally-split.webp"

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
printf '  %-44s %s\n' "frontend/static/marketing/tally-split.webp" \
  "$(magick identify -format '%wx%h, %b' "$OUT_DIR/tally-split.webp")"
printf '  %-44s %s\n' "frontend/static/marketing/grain.svg" \
  "$(wc -c < "$OUT_DIR/grain.svg" | tr -d ' ') bytes"
