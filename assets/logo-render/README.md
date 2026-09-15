# Brand mark

FeohLedger's mark is **feoh (ᚠ) written as a split tally stick**.

- **Feoh** is Old English for wealth (originally cattle) and the root of *fee*.
  It opens the Old English rune-row, which makes it a sibling to Threkir's
  thorn (þ).
- **The tally stick** is how England's Exchequer recorded a debt for seven
  centuries: notches cut across a hazel stick, which was then split lengthwise
  so each party held half. A debt was settled when the halves matched. The foil
  carries the rune; the longer, dimmer stock carries the matching notches.
  Matching a bill against its record is the product's core act.

The rationale, the alternatives and the white-label rule are in
[`docs/decisions.md` §171](../../docs/decisions.md).

## What is generated from what

`gen_svg.py` is the single source of the geometry and the palette. It writes the
committed SVG masters; `../gen-icons.sh` renders every platform icon from them.
Nothing downstream is edited by hand.

| Master | Used for |
|---|---|
| `assets/icon.svg` | Full-bleed tile: every iOS AppIcon size, `apple-touch-icon.png`, `icon-maskable-512.png` |
| `assets/logo-mark.svg` | Rounded tile: the in-app mark (web + mobile), `favicon.svg`/`.ico`, web manifest `any` icons, Android legacy `ic_launcher.png` |
| `assets/icon-foreground.svg` | Android adaptive foreground, glyph shrunk into the 66 dp safe zone |
| `assets/icon-background.svg` | Android adaptive background |
| `assets/icon-monochrome.svg` | Android 13+ themed icon |

```bash
# After changing gen_svg.py: rewrite the masters and re-render every icon.
# Needs python3, Inkscape and ImageMagick 7 (the opt-in asset-pipeline tools).
pnpm gen:icons

# What CI runs: masters match the generator, every raster exists at the right
# size, and nothing that must be opaque has alpha. Pure stdlib Python.
pnpm check:icons

# The exploration tiles (gitignored) -> assets/logo-render/svg/
python3 assets/logo-render/gen_svg.py
```

## The exploration

Nine treatments were compared side by side at 132, 64, 32 and 16 px, in three
palettes, inside the app's real surfaces. All nine are kept in `gen_svg.py` so
the choice can be revisited without re-deriving it. Change `CHOSEN` to swap the
mark; the palette and the pipeline follow.

| Concept | Idea | Why it didn't ship |
|---|---|---|
| **tally** | Split Exchequer tally stick | Shipped |
| futhorc | The bare rune, in Threkir's slab geometry | Carries the name but none of the product |
| total | The rune on a bookkeeper's double rule | The rules go soft below 32 px |
| credit | A T-account, strokes on the credit side | Reads as a plain T to anyone but an accountant |
| tick | Branches as auditor's tick marks | Reads closest to a K at small sizes |
| sceat | An early runic silver penny | Best launcher icon; a gold dot in a browser tab |
| quill | Shaft and nib as stave, vanes as branches | Illustrative rather than a mark |
| brand | A "rocking" cattle brand (feoh = cattle) | Charming, but the monoline loses weight small |
| dealt | Branches run off the tile (the rune poem's "deal it out") | The least like a letter |

| Palette | Ground → glyph | Note |
|---|---|---|
| **gilt** | navy `#24345C`→`#0A1124`, gold `#E7B95E` | Shipped |
| ledger | green `#1B7657`→`#093326`, cream `#F3EAD0` | |
| signal | blue `#7196FF`→violet `#6448E4`, white | Matches today's UI accent, which a tenant can override |
