<script lang="ts">
	import type { SubscriptionStatus } from '$lib/types/billing';
	import Badge from './Badge.svelte';
	import type { BadgeTone } from './badgeTone';

	let { status }: { status: SubscriptionStatus } = $props();

	const LABELS: Record<SubscriptionStatus, string> = {
		trialing: 'Trialing',
		active: 'Active',
		past_due: 'Past Due',
		canceled: 'Canceled'
	};

	// The tone is the semantic call; the recipe behind it lives in `Badge`.
	// `canceled` is `neutral` rather than `muted` on purpose: a cancelled
	// subscription is the absence of a signal, not a weak one.
	const TONES: Record<SubscriptionStatus, BadgeTone> = {
		trialing: 'accent',
		active: 'success',
		past_due: 'danger',
		canceled: 'neutral'
	};
</script>

<!-- `variant={status}` keeps the per-status class as a selector hook (e2e and
     ad-hoc styling read it); its colour now comes from the tone. -->
<Badge tone={TONES[status]} variant={status}>{LABELS[status]}</Badge>
