# Marketing page assets

Three generated files, all written by `gen-marketing.sh` and all committed under
`frontend/static/marketing/`. Neither is drawn by hand and neither is edited
after generation — change the generator and re-run it.

```bash
pnpm gen:marketing            # = bash assets/marketing/gen-marketing.sh; needs blender + ImageMagick 7
pnpm gen:marketing light      # re-render one lighting only (dark | light | all, default all)
```

| Output | Source | What it is |
|---|---|---|
| `frontend/static/marketing/tally-split.webp` | `tally_scene.py`, `TALLY_VARIANT=dark` | The split Exchequer tally in gold, lit for the navy palette |
| `frontend/static/marketing/tally-split-light.webp` | `tally_scene.py`, `TALLY_VARIANT=light` | The same object and frame, lit for the light palette |
| `frontend/static/marketing/grain.svg` | `gen-marketing.sh` heredoc | An `feTurbulence` grain overlay |

## The ornament is the brand argument, not decoration

The mark is feoh written as a split tally stick ([`docs/decisions.md`
§173](../../docs/decisions.md), and `../logo-render/README.md` for the mark
itself). This renders the object the mark abstracts.

The Exchequer recorded a debt by cutting notches across a hazel stick and then
splitting the stick lengthwise, so each party held half. The debt was settled
when the halves matched. That is the act an accounts-payable system performs —
an invoice against a purchase order, a payment against a statement — so the
ornament states the product's thesis rather than filling space beside it. The
two halves in the render carry the **same** notch set, because in life they were
one blank notched before it was split; `NOTCHES` in `tally_scene.py` is
therefore one list consumed twice, never a per-half seed.

## Why a render and not a drawing

Everything else in `assets/` is flat vector geometry, because an icon has to
survive being 16 px wide. This does not: it appears once, large, on the marketing
page, where the product's claim is that it handles money carefully. A lit object
with real depth of field says that in a way a flat illustration of the same
shape does not — and Blender with Cycles on the GPU is already the workstation's
tool for it (root `CLAUDE.md` § Design & graphics workflow).

`tally_scene.py` derives the geometry from constants at the top of the file
rather than shipping a `.blend`: the scene is a diff, the notch positions are
readable, and a change to `LENGTH` re-frames the camera instead of cropping the
object. Four things in it are worth not re-deriving, each recorded as a comment
at the point it bit:

- a BOOLEAN modifier copies the **cutter's** material slots, so the stick came
  out owning an empty slot that every face pointed at — three renders in
  Blender's default grey;
- `from_pydata` takes face winding on trust, and an inward normal makes an EXACT
  boolean remove nothing while reporting success — the notches were *in* the
  mesh at the right depths and the face rendered smooth;
- at metallic 1 there is no diffuse term at all, so the object is purely an image
  of its environment, and in an empty world the notch walls reflect the void;
- an 85 mm lens 0.6 m from a 24 cm object is near-macro, where f/5.6 puts every
  notch inside the blur.

## Two lightings, one frame

The page follows the visitor's theme ([`docs/decisions.md`
§306](../../docs/decisions.md)), so the ornament is rendered once per palette.
The dark lighting was composed for navy: a cool blue fill and rim keep the
shadow side off black. On a light ground that lighting fails the other way —
the faces reflecting the softbox come out near-white cream, the same value as
the page, and the near edges of the stick dissolve. The light variant
(`LIGHTING["light"]` in `tally_scene.py`) dims the softbox, takes ~0.45 stop
off the exposure, makes the fill warm and weak and drops the rim, so every face
stays in the gold range and the underside reads as shadow.

Everything else — geometry, camera, depth of field, film — is shared, so both
renders trim to the same 1100 × 460 frame and the page swaps the file without
moving the layout. `gen-marketing.sh` refuses to finish if the two disagree,
and `frontend/src/lib/marketing/ornament.test.ts` checks the committed files.
The page picks the file from `theme.resolved` (`src/lib/marketing/ornament.ts`),
not a `<picture>` media query, which would follow the OS and ignore a stored
choice. The CSS drop shadow under it comes from the `--drop-shade` and `--gilt`
tokens, so it is ink on light and black on navy.

The grain overlay is shared: a grey `feTurbulence` tile composited with
`mix-blend-mode: overlay` reads as grain on either ground.

## Regenerating

`gen-marketing.sh` is deterministic on one machine: same scene, same seed, same
bytes. The scene renders on OptiX where it exists (the NVIDIA workstation the
dark render came from), on Metal on Apple silicon, and on the CPU otherwise;
the backend changes the noise, not the image, but not the bytes either — which
is why a single lighting can be re-rendered on its own and the other left as
committed. It is
**not** run in CI — same call as `gen-icons.sh`, because CI has no GPU — so the
committed WebP is what ships. Re-run it whenever `tally_scene.py` changes, and
commit the result in the same change.
