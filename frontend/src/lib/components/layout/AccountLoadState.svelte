<script lang="ts">
	/**
	 * What the root layout draws in place of the app shell while the signed-in
	 * account (`GET /api/auth/me`) is not loaded yet: a loading line while the
	 * request is in flight, and — when it failed for any reason other than the
	 * session ending (a 5xx, a timeout, the network) — a panel with Retry and
	 * Log out. The shell itself needs the account (roles drive the nav), so
	 * before this the page was simply blank, with no way forward.
	 *
	 * `status` is `auth.userStatus`; the callbacks are the store's `fetchUser`
	 * and `logout`, passed in so this stays presentational.
	 */
	import { tick } from 'svelte';
	import BrandMark from '#lib/components/ui/BrandMark.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { UserStatus } from '#lib/stores/auth.svelte.ts';

	interface Props {
		status: UserStatus;
		onretry: () => void;
		onlogout: () => void;
	}

	let { status, onretry, onlogout }: Props = $props();

	let heading = $state<HTMLHeadingElement | null>(null);
	let loggingOut = $state(false);
	// Once a load has failed, a Retry keeps the panel on screen (its button
	// reading "Retrying…") instead of swapping it for the loading view: the
	// button the user pressed must not vanish from under their focus.
	let failedOnce = $state(false);
	const showPanel = $derived(status === 'error' || (failedOnce && status === 'loading'));
	const retrying = $derived(failedOnce && status === 'loading');

	// The live region is always in the DOM and only its text changes —
	// a region that arrives already filled is often not announced
	// (ui-patterns § Inline form feedback). Filled from an effect, so it is
	// empty on first render.
	let announcement = $state('');
	$effect(() => {
		announcement =
			status === 'loading'
				? m(retrying ? 'common.retrying' : 'shell.accountLoad.loading')
				: '';
	});

	// The panel replaces whatever had focus (often nothing — this is first
	// paint), so put focus on its heading each time a load fails: a screen
	// reader announces what went wrong, and Tab lands on Try again next.
	$effect(() => {
		if (status !== 'error') return;
		failedOnce = true;
		void tick().then(() => heading?.focus());
	});

	function retry() {
		// aria-disabled, not disabled: a focused button that becomes disabled
		// throws keyboard focus to <body>.
		if (retrying) return;
		onretry();
	}

	function logout() {
		if (loggingOut) return;
		loggingOut = true;
		onlogout();
	}
</script>

<div class="account-load" data-account-load={showPanel ? 'error' : 'loading'}>
	<p class="announcement" class:visually-hidden={showPanel} role="status">
		{#if !showPanel}<BrandMark size={36} />{/if}
		<span class="announcement-text">{announcement}</span>
	</p>
	{#if showPanel}
		<section class="panel" aria-labelledby="account-load-heading">
			<BrandMark size={36} />
			<h1 id="account-load-heading" tabindex="-1" bind:this={heading}>
				{m('shell.accountLoad.errorHeading')}
			</h1>
			<p class="body">{m('shell.accountLoad.errorBody')}</p>
			<div class="actions">
				<button
					type="button"
					class="btn-primary"
					onclick={retry}
					aria-disabled={retrying}
				>
					{retrying ? m('common.retrying') : m('common.tryAgain')}
				</button>
				<button type="button" class="btn-cancel" onclick={logout} aria-disabled={loggingOut}>
					{m('shell.logOut')}
				</button>
			</div>
		</section>
	{/if}
</div>

<style>
	.account-load {
		min-height: 100vh;
		display: grid;
		place-items: center;
		padding: 24px 16px;
		background: var(--bg);
	}
	.panel {
		width: min(440px, 100%);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 32px 28px;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 12px;
	}
	h1 {
		margin: 4px 0 0;
		font-size: 1.25rem;
		font-weight: 700;
		color: var(--text);
	}
	.body {
		margin: 0;
		font-size: 0.93rem;
		line-height: 1.55;
		color: var(--text-muted);
	}
	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		margin-top: 8px;
	}
	/* The loading line: the region itself stays at full opacity (only its
	   text arrives late); the visual line fades in after a beat so a fast /me
	   never flashes it. Rests visible (Motion rule 1); app.css's
	   reduced-motion block zeroes the delay. */
	.announcement:not(.visually-hidden) {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 12px;
		margin: 0;
		color: var(--text-muted);
		font-size: 0.93rem;
		animation: account-load-in 0.2s ease-out 0.4s 1 both;
	}
	.panel button[aria-disabled='true'] {
		cursor: progress;
	}
	@keyframes account-load-in {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}
</style>
