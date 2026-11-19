<script lang="ts">
	import type { BadgeTone } from './badgeTone';
	import Badge from './Badge.svelte';

	/**
	 * A `<Badge>` whose short pill text needs a sentence of explanation.
	 *
	 * The explanation goes in two places: a visually-hidden span inside the
	 * badge, so it is part of the badge's text for a screen reader (and for
	 * anyone who copies the row), and the `title`, which stays as a tooltip for
	 * the pointer user. A `title` alone reaches neither a keyboard, a touch
	 * screen nor a screen reader, which is how `/recurring`'s "Not generating"
	 * and "GL … needs re-coding" badges hid their reasons until decisions §442
	 * and §455.
	 *
	 * The hidden span opens with a space so the pill text and the explanation
	 * do not run together into one word when read as text. It is written as an
	 * expression because Svelte trims a literal space at the edge of an
	 * element's children — the hand-written round-15 GL badge lost it that way.
	 */
	let {
		tone = 'neutral',
		variant = '',
		label,
		explanation
	}: {
		tone?: BadgeTone;
		/** Extra class for e2e / semantic hooks. Never for colour. */
		variant?: string;
		/** The visible pill text. */
		label: string;
		/** The sentence that says why; visually hidden, and the tooltip. */
		explanation: string;
	} = $props();
</script>

<Badge {tone} {variant} title={explanation}
	>{label}<span class="visually-hidden">{` ${explanation}`}</span></Badge
>
