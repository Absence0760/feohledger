# Help centre — how it works and how to write for it

The in-app help lives at `/help` (signed-in, every role) and reaches into the
rest of the app in three ways: a **Help & guides** row in the sidebar footer, a
**How this page works** link in every `PageHeader`, and **ⓘ HelpTips** beside
the labels that need explaining. Reasoning: `docs/decisions.md` §244.

## What is where

| Surface | Route / file |
|---|---|
| Landing — start-here path per role (`?role=` picks one; default is the reader's most senior role), the invoice-lifecycle walkthrough, concept guides, reference cards | `routes/help/+page.svelte` |
| One guide | `routes/help/guides/[id]/+page.svelte` |
| Glossary (topic filter `?topic=`, entries anchored `#<id>`) | `routes/help/glossary/+page.svelte` |
| Page by page — every sidebar page + the account pages, marked when the reader's role can't open them | `routes/help/pages/+page.svelte` |
| Search (`?q=`, live from the shell's search box) | `routes/help/search/+page.svelte` |
| Shell — search box + contents column (folds behind a button under 900px) | `routes/help/+layout.svelte`, `components/help/HelpNav.svelte` |

Content, all under `src/lib/help/`:

| File | Holds |
|---|---|
| `types.ts` | The shapes, the inline-markup grammar, `DIAGRAM_IDS` |
| `guides/{start,invoices,payments,vendors,setup,concepts}.ts` | The guides, by area |
| `glossary.ts` | Glossary ids, categories, the long English entries, aliases |
| `pages.ts` | `PAGE_HELP` — one line + a guide per sidebar href |
| `lifecycle.ts` | The invoice-lifecycle stages |
| `content.ts` | Joins it all; search; `guidesForTerm` |
| `routeHelp.ts`, `terms.ts` | The light lookups `PageHeader` / `HelpTip` use (see *Bundles* below) |

## Translation: two halves

- **Catalogued** (all six locales, `help.*` keys): the help centre's chrome,
  every glossary term's name (`help.term.<id>`) and one-line definition
  (`help.term.<id>.short` — the HelpTip text, ≤ 160 chars), and every word
  inside a diagram (`help.diagram.<id>.*`).
- **English prose**: guide titles, summaries and bodies, glossary long text,
  page-directory lines, lifecycle stage text. Under another UI locale it is
  rendered with `lang="en"` (WCAG 3.1.2) beneath `EnglishNotice`, which says so
  in the reader's language. Translating the prose is a tracked follow-up
  (`docs/followups.md`), not a gap to paper over by machine-translating inline.

Inside the English prose, **name UI with `{ui:key}`, never by typing the label**:
it renders the catalogue label in the reader's language (bold, with the UI's
`lang`), and a renamed or deleted key fails `content.test.ts`. A key whose value
has a `{placeholder}` can't be used (it would render the braces) — write the
label in `**bold**` instead.

## Inline markup

```
**Label**                     bold — a label with no catalogue key
*stress*                      emphasis
{ui:invoices.status.approved} the catalogue label, bold, in the reader's language
[[three-way-match]]           glossary link, labelled with the term's (translated) name
[[void|voided]]               glossary link with its own label
[[guide:run-payments|label]]  link to another guide
[[page:/payments]]            link into the app, labelled with its sidebar label
[[page:/profile|label]]       the same for a page off the sidebar (label required)
```

Rendered by `components/help/RichText.svelte` as elements — never `{@html}`.

Blocks: `p`, `steps` (numbered things to do), `list`, `note` (`tip` / `caution`
— a control that will stop you, saying *why* it exists / `role` — who may do
this), `lifecycle` (the walkthrough), `diagram` (`{ id, caption }`).

## Adding things

- **A guide** — add it to the right `guides/*.ts`, with `roles` (who it's for;
  drives the landing page's per-role task list), `route` (the page the work
  happens on — its "Open …" button is shown only if the reader's role can open
  it, via `nav.ts::canSee`), `terms` and `related`. Its id is its URL: never
  rename one that has shipped.
- **A glossary term** — an entry in `glossary.ts` **and** `help.term.<id>` +
  `help.term.<id>.short` in all six locale files. Add the id to some guide's
  `terms` so the term's HelpTip has further reading (the test requires it for
  every tipped term).
- **A sidebar page** — `PAGE_HELP` must gain its href in the same change
  (`content.test.ts` enumerates `NAV`); that row is also what lights up the
  page's "How this page works" link.
- **An invoice status** — place it in a `lifecycle.ts` stage (or as a branch);
  the test enumerates `INVOICE_STATUSES`.
- **A diagram** — a component in `components/help/diagrams/`, an id in
  `DIAGRAM_IDS`, a branch in `Diagram.svelte`, and a `diagram` block in at least
  one guide. Inline SVG coloured from the `app.css` tokens (it follows the
  tenant's accent), every word from the catalogue, legible at 320px.

## HelpTips in the app

`<HelpTip term="three-way-match" />` — an ⓘ toggletip (click / Enter / Space,
Escape closes, a polite live region announces it) showing the term's name and
one-liner, a glossary link, and up to three guides that list the term in their
`terms`. Placement rules (`docs/ui-patterns.md` § Contextual help):

- Beside the text, never inside a `<label>`, `<button>`, `<a>`, `<legend>` or a
  sortable header — it would change that element's accessible name.
- For a heading, wrap heading + tip in a row; leave the heading's own text alone.
- A handful per page where the "what does this mean?" is real — not every label.
- A component that renders its own title can take a `helpTerm` prop and forward
  it (`ui/ApprovalChainProgress.svelte`); the test reads those call sites.

`term` must be a literal id, or an expression whose possible results are
literal ids (the invoice modal picks two-, three- or four-way by match type).

## Bundles

`PageHeader` and `HelpTip` render on ordinary pages, so neither may import
`content.ts` (which pulls every guide): `PageHeader` reads `routeHelp.ts` (the
small page directory only), and `HelpTip` needs only catalogue keys
(`terms.ts`), fetching the guide index with a dynamic `import()` the first time
any tip opens. `content.test.ts` § bundle boundaries pins both.

The glossary, help-route and diagram **strings** are their own lazy catalogue
slice too (`src/lib/i18n/locales/help/<locale>.ts`, decisions §261): the /help
layout and each `HelpTip` call `ensureHelpCatalogue()` on mount, and English
shows until the reader's slice lands. Add a new `help.*` key there, in all six
locales — only `help.pageLink` and `help.tip.*` stay in the main catalogue.

## Tests

- `src/lib/help/content.test.ts` — every reference in every piece of prose
  resolves (`{ui:}` keys exist and take no params; terms, guides and routes
  exist; unlabelled page links are sidebar entries); glossary complete in all
  six locales; every `<HelpTip>` term exists and has further reading; the page
  directory covers the sidebar; every invoice status is in the lifecycle;
  every diagram is drawn and used; the bundle boundaries; search.
- `src/lib/help/inline.test.ts` — the markup parser, the page-help lookup, anchors.
- `tests-e2e/help/help-centre.spec.ts` — the pages render, search, the
  role-chips, a guide's open-page button, the PageHeader link, a HelpTip by
  keyboard. `tests-e2e/a11y/reflow.spec.ts` picks the `/help` routes up off
  disk.
