<script lang="ts">
	/**
	 * Renders an operator fact inline, or a visibly-marked gap if it is pending.
	 *
	 * Every place a legal page would state the registered entity, the postal
	 * address, the governing law or a representative goes through this, so a
	 * pending fact can never reach the page as an empty string, the word
	 * "undefined", or — worst — a plausible-looking invented value. Fill the
	 * fact in `lib/legal/operator.ts` and the marker disappears here with no
	 * other edit.
	 *
	 * `label` is what the reader is told is missing, phrased to stand alone
	 * inside a sentence: "governed by the laws of [governing law to be confirmed]".
	 */
	let { value, label } = $props<{ value: string | null; label: string }>();
</script>

{#if value}{value}{:else}<span class="fact-pending">[{label} to be confirmed]</span>{/if}
