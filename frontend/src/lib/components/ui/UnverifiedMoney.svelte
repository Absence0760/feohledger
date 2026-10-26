<script lang="ts">
	import Money from './Money.svelte';
	import Badge from './Badge.svelte';

	/**
	 * A money figure that may be one nobody vouches for any more.
	 *
	 * A purchase order the ERP could not price at its last sync keeps its old
	 * figures in the database, and the API sends `null` for them (decisions
	 * §323). Rendering that `null` as `<Money>`'s dash would read as "no total
	 * recorded", which is a different fact. While `unverified` is set this draws
	 * a warning badge naming the state instead — the caller supplies the label
	 * in its own catalogue (the AP app and the supplier portal word it
	 * differently) and, where it has one, the reason as `title`. The reason is
	 * also expected in visible text somewhere on the page; `title` alone is not
	 * an accessible explanation.
	 */
	interface Props {
		amount: number | string | null | undefined;
		currency?: string | null;
		unverified?: boolean;
		/** What the badge says while `unverified`. */
		label: string;
		title?: string;
		mono?: boolean;
	}

	let { amount, currency = null, unverified = false, label, title, mono = false }: Props =
		$props();
</script>

{#if unverified}
	<Badge tone="warning" variant="unverified-money" {title}>{label}</Badge>
{:else}
	<Money {amount} {currency} {mono} />
{/if}
