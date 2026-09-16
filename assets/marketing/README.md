# Marketing page assets

Two generated files, both written by `gen-marketing.sh` and both committed under
`frontend/static/marketing/`. Neither is drawn by hand and neither is edited
after generation — change the generator and re-run it.

```bash
pnpm gen:marketing      # = bash assets/marketing/gen-marketing.sh; needs blender + ImageMagick 7
```

| Output | Source | What it is |
|---|---|---|
| `frontend/static/marketing/tally-split.webp` | `tally_scene.py` (Blender / Cycles) | The split Exchequer tally, rendered in gold |
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

## Regenerating

`gen-marketing.sh` is deterministic: same scene, same seed, same bytes. It is
**not** run in CI — same call as `gen-icons.sh`, because CI has no GPU — so the
committed WebP is what ships. Re-run it whenever `tally_scene.py` changes, and
commit the result in the same change.
