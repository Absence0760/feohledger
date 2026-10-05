<script lang="ts">
	import type { InvoiceStatus } from '#lib/types/invoice.ts';
	import { invoiceStatusLabelKey } from '#lib/types/invoice.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	let { status }: { status: InvoiceStatus } = $props();

	// $derived so the badge re-renders when the locale changes. A status this
	// build doesn't know renders its RAW value — visible and searchable —
	// rather than a blank pill; `status` is typed against the union, but the
	// assistant's tool results carry a status straight off the wire.
	let label = $derived.by(() => {
		const key = invoiceStatusLabelKey(status);
		return key ? m(key) : status;
	});
</script>

<span class="badge {status}">{label}</span>

<style>
	.badge {
		display: inline-block;
		padding: 3px 10px;
		border-radius: 12px;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		white-space: nowrap;
	}

	/* WCAG 1.4.3 — these badges are small but bold uppercase, so they are held
	   to the normal-text bar (≥4.5:1), not the large-text one.

	   This component worked out the lifted-text answer for the tinted-badge
	   recipe before the palette had a name for it, and carried the resulting
	   hexes as private literals. They are now the `--<tone>-tint` /
	   `--<tone>-on-tint` pairs in app.css — same values, one owner — so a tone
	   is recalibrated in one place instead of here and in the 20-odd badges
	   that copied these numbers. See `frontend/CLAUDE.md` § Colour tokens and
	   contrast, and decisions.md §30.

	   Two drifts went with the move: `.done` and `.ready_for_review` had two
	   different greens (a 15% tint of #1fa86a vs of #32c882) that composite
	   within a few units of each other, so the difference was accidental
	   rather than meaningful; and `.failed` / `.pending` gain a little margin
	   (4.74→5.36 and 4.87→5.50) by taking the tokens' text. */
	.new {
		background: var(--accent-tint);
		color: var(--accent-on-tint);
	}

	.pending {
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	.ready_for_review {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	.failed {
		background: var(--danger-tint);
		color: var(--danger-on-tint);
	}

	/* The one tone with no palette entry: purple carries no semantic meaning
	   the other four share — it exists to make "handed to the ERP" scannable
	   mid-pipeline — so naming it would add a token with one caller. Kept as
	   a literal deliberately, and measured like any other: #a585f5 on the 15%
	   tint composites to 5.59:1. */
	.sent_to_erp {
		background: rgba(140, 100, 240, 0.15);
		color: #a585f5;
	}

	.done {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}
</style>
