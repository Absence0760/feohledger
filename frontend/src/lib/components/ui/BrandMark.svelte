<script lang="ts">
	import { asset } from '$app/paths';

	/**
	 * The FeohLedger mark: feoh (ᚠ) written as a split tally stick on its navy
	 * tile (docs/decisions.md §171). Served from `static/logo-mark.svg`, which
	 * `assets/gen-icons.sh` writes from the generator; never edit it by hand.
	 *
	 * Decorative by default (`alt=""`). Beside the product name the name carries
	 * it; on the auth cards the page heading and `<title>` carry the context, and
	 * an image announced as "FeohLedger" above "Set a new password" adds nothing.
	 * Pass `label` only where the mark is the one thing naming the product in
	 * view, as in the collapsed sidebar rail.
	 *
	 * Platform-only. Chrome a white-label tenant sees picks its mark through
	 * `brand.mark`, which only resolves to this while the product still carries
	 * the platform's name.
	 */
	interface Props {
		size?: number;
		label?: string;
	}

	let { size = 24, label = '' }: Props = $props();
</script>

<img class="brand-mark" src={asset('/logo-mark.svg')} width={size} height={size} alt={label} />

<style>
	.brand-mark {
		display: block;
		flex-shrink: 0;
	}
</style>
