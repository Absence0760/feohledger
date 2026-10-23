<script lang="ts">
	import Badge from './Badge.svelte';
	import type { InvoiceStatus } from '#lib/types/invoice.ts';
	import { invoiceStatusLabelKey, invoiceStatusTone } from '#lib/types/invoice.ts';
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

	// Colour comes from `INVOICE_STATUS_TONES` (total over the union) through
	// the shared `Badge` primitive — never from a per-status rule here. This
	// component used to carry its own colour rules and only six of the twelve
	// statuses had one, so `paid`, `posted_in_erp` and four others rendered as
	// bare text. `status` stays on as the variant class: it is the semantic
	// hook (`.badge.paid`) selectors read.
	let tone = $derived(invoiceStatusTone(status));
</script>

<Badge {tone} variant={status}>{label}</Badge>
