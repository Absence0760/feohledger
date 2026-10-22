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
	 *
	 * The failure itself is `ui/LoadError` in its titled shape — the one
	 * error-and-retry primitive — with Log out passed as its extra action. This
	 * component owns only what is particular to the shell: the brand frame, the
	 * loading line, and keeping the panel up through a retry.
	 */
	import { tick } from 'svelte';
	import BrandMark from '#lib/components/ui/BrandMark.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { UserStatus } from '#lib/stores/auth.svelte.ts';

	interface Props {
		status: UserStatus;
		onretry: () => void;
		onlogout: () => void;
	}

	let { status, onretry, onlogout }: Props = $props();

	let errorBlock = $state<LoadError | null>(null);
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
		void tick().then(() => errorBlock?.focusHeading());
	});

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
		<div class="panel">
			<BrandMark size={36} />
			<LoadError
				bind:this={errorBlock}
				heading={m('shell.accountLoad.errorHeading')}
				headingLevel={1}
				message={m('shell.accountLoad.errorBody')}
				retryLabel={m('common.tryAgain')}
				{retrying}
				{onretry}
			>
				{#snippet actions()}
					<button type="button" class="btn-cancel" onclick={logout} aria-disabled={loggingOut}>
						{m('shell.logOut')}
					</button>
				{/snippet}
			</LoadError>
		</div>
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
	.btn-cancel[aria-disabled='true'] {
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
