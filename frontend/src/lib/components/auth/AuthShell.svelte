<script lang="ts">
	import type { Snippet } from 'svelte';
	import { asset } from '$app/paths';
	import Atmosphere from '$lib/components/marketing/Atmosphere.svelte';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import { m } from '$lib/i18n/store.svelte';

	/**
	 * The split-screen frame every pre-auth employee page renders inside: the
	 * form on one side, a brand panel on the other.
	 *
	 * **Why one shell.** Signup, sign-in, forgot and reset password are a single
	 * journey a new user walks in one sitting — the verification email lands
	 * them on sign-in, and "Forgot password?" is one click from it. Before this,
	 * each hand-rolled its own centred card with its own copy of ~80 lines of
	 * input and button CSS, which is how a restyle of one leaves the next page
	 * looking a decade older. The form-control styles live here, exposed to the
	 * caller's markup through `:global()` under `.form-wrap`, so a page supplies
	 * markup and behaviour and inherits the look.
	 *
	 * **DOM order is form first, panel second**, whichever side the panel is
	 * drawn on. The form is the page's purpose: a screen-reader or keyboard user
	 * reaches it immediately, and the page's `<h1>` precedes the panel's `<h2>`
	 * so the heading outline is in order (WCAG 1.3.1 / 2.4.6). Nothing in the
	 * panel is focusable, so drawing it on the other side cannot create a focus
	 * order that disagrees with the visual one (2.4.3).
	 *
	 * **Platform-branded by policy, not by oversight.** These pages render
	 * before any tenant branding is known — the mark and panel are the same on
	 * every host for the same reason the favicon is (`docs/white-label.md` §
	 * The fallback mark). The supplier portal's sign-in is deliberately NOT on
	 * this shell: it resolves the tenant's own branding before it renders, and
	 * a FeohLedger panel there would be platform branding a white-label partner
	 * never chose.
	 *
	 * **Motion.** The backdrop is held `still`, and the page's life comes from a
	 * short entrance stagger and from focus transitions. Continuous ambient
	 * motion would owe a pause control under WCAG 2.2.2, and a pause button on a
	 * sign-in form is clutter; a sub-second entrance is outside the criterion.
	 *
	 * Props:
	 * - `panel` — the panel's page-specific content, rendered under the tagline.
	 * - `panelOnMobile` — keep the panel (stacked under the form) below the
	 *   desktop breakpoint. Signup takes it, because its panel says what happens
	 *   after you press the button; sign-in's panel is brand only, and on a phone
	 *   it would push the form's context below the fold for nothing.
	 */
	interface Props {
		children: Snippet;
		panel?: Snippet;
		panelOnMobile?: boolean;
	}

	let { children, panel, panelOnMobile = false }: Props = $props();

	const panelHeadingId = 'auth-panel-heading';
</script>

<div class="auth-shell" class:panel-on-mobile={panelOnMobile}>
	<div class="form-col">
		<div class="form-wrap">
			{@render children()}
		</div>
	</div>

	<aside class="panel" aria-labelledby={panelHeadingId}>
		<Atmosphere dense still />
		<div class="panel-inner">
			<div class="panel-brand">
				<BrandMark size={30} />
				<span class="panel-brand-name">FeohLedger</span>
			</div>

			<h2 id={panelHeadingId} class="tagline">{m('auth.shell.tagline')}</h2>

			{#if panel}
				<div class="panel-body">
					{@render panel()}
				</div>
			{/if}

			<!-- Decorative: the tagline beside it already says what the render
			     depicts, so an alt text would announce the same idea twice. -->
			<img
				class="panel-art"
				src={asset('/marketing/tally-split.webp')}
				width="1100"
				height="460"
				alt=""
				decoding="async"
			/>
		</div>
	</aside>
</div>

<style>
	.auth-shell {
		min-height: 100vh;
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1.05fr);
		background: var(--bg);
	}

	/* ----------------------------- form column ---------------------------- */
	.form-col {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 48px 32px;
	}
	.form-wrap {
		width: min(420px, 100%);
	}

	/* ------------------------------- panel -------------------------------- */
	.panel {
		position: relative;
		overflow: hidden;
		margin: 14px 14px 14px 0;
		border-radius: 24px;
		border: 1px solid rgba(226, 228, 234, 0.07);
		background: linear-gradient(160deg, #141a2e 0%, #0d101a 70%);
	}
	.panel-inner {
		position: relative;
		z-index: 1;
		height: 100%;
		display: flex;
		flex-direction: column;
		padding: 44px 48px 36px;
	}
	.panel-brand {
		display: inline-flex;
		align-items: center;
		gap: 10px;
		margin-bottom: auto;
	}
	.panel-brand-name {
		font-weight: 700;
		font-size: 1.02rem;
		letter-spacing: -0.01em;
		color: var(--text);
	}
	.tagline {
		margin: 48px 0 18px;
		max-width: 20ch;
		font-size: clamp(1.8rem, 2.6vw, 2.5rem);
		line-height: 1.12;
		letter-spacing: -0.03em;
		font-weight: 800;
		color: var(--text);
		animation: panel-in 0.7s cubic-bezier(0.22, 1, 0.36, 1) 0.1s backwards;
	}
	.panel-body {
		color: var(--text-muted);
		animation: panel-in 0.7s cubic-bezier(0.22, 1, 0.36, 1) 0.22s backwards;
	}
	.panel-art {
		display: block;
		width: 112%;
		max-width: none;
		height: auto;
		margin: 28px -6% 0 auto;
		filter: drop-shadow(0 24px 40px rgba(231, 185, 94, 0.13))
			drop-shadow(0 8px 16px rgba(0, 0, 0, 0.5));
		animation: art-in 1s cubic-bezier(0.22, 1, 0.36, 1) 0.3s backwards;
	}
	@keyframes panel-in {
		from { opacity: 0; transform: translate3d(0, 12px, 0); }
		to { opacity: 1; transform: translate3d(0, 0, 0); }
	}
	@keyframes art-in {
		from { opacity: 0; transform: translate3d(24px, 18px, 0) rotate(-2deg); }
		to { opacity: 1; transform: translate3d(0, 0, 0) rotate(0); }
	}

	@media (max-width: 960px) {
		.auth-shell {
			grid-template-columns: 1fr;
		}
		.form-col {
			padding: 40px 20px 28px;
		}
		.panel {
			display: none;
		}
		.panel-on-mobile .panel {
			display: block;
			margin: 0 16px 16px;
		}
		.panel-inner {
			padding: 28px 24px;
		}
		.tagline {
			margin-top: 24px;
		}
		.panel-art {
			width: 100%;
			margin: 20px 0 0;
		}
	}

	/* --------------------------------------------------------------------
	   Form controls, shared with every caller's markup.

	   `:global()` under `.form-wrap` so the reach is exactly this shell's
	   own column and cannot leak into the app shell.

	   Every target is wrapped in `:where()`, which contributes ZERO
	   specificity: each rule weighs only `.form-wrap` plus Svelte's scoping
	   class. So a page's own component-scoped rule for one of its controls
	   wins on specificity alone, with no `!important` and no repeated-class
	   hacks — the shell supplies defaults, and a page that needs a different
	   control (signup's composite slug field) just writes the rule.
	   -------------------------------------------------------------------- */

	/* The entrance: each direct child of the page's form rises in on a
	   short stagger. Sub-second and one-shot, and collapsed to its end
	   state by the reduced-motion rule in app.css. */
	.form-wrap :global(form > *),
	.form-wrap :global(.auth-stack > *) {
		animation: field-rise 0.5s cubic-bezier(0.22, 1, 0.36, 1) backwards;
	}
	.form-wrap :global(form > *:nth-child(2)),
	.form-wrap :global(.auth-stack > *:nth-child(2)) { animation-delay: 40ms; }
	.form-wrap :global(form > *:nth-child(3)),
	.form-wrap :global(.auth-stack > *:nth-child(3)) { animation-delay: 80ms; }
	.form-wrap :global(form > *:nth-child(4)),
	.form-wrap :global(.auth-stack > *:nth-child(4)) { animation-delay: 120ms; }
	.form-wrap :global(form > *:nth-child(5)),
	.form-wrap :global(.auth-stack > *:nth-child(5)) { animation-delay: 160ms; }
	.form-wrap :global(form > *:nth-child(6)),
	.form-wrap :global(.auth-stack > *:nth-child(6)) { animation-delay: 200ms; }
	.form-wrap :global(form > *:nth-child(n + 7)),
	.form-wrap :global(.auth-stack > *:nth-child(n + 7)) { animation-delay: 240ms; }
	@keyframes field-rise {
		from { opacity: 0; transform: translate3d(0, 10px, 0); }
		to { opacity: 1; transform: translate3d(0, 0, 0); }
	}

	.form-wrap :global(:where(h1)) {
		margin: 0;
		font-size: clamp(1.6rem, 3vw, 1.95rem);
		font-weight: 800;
		letter-spacing: -0.025em;
		line-height: 1.15;
		color: var(--text);
	}

	.form-wrap :global(:where(label)) {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.form-wrap :global(:where(label > span)) {
		font-size: 0.8rem;
		font-weight: 600;
		letter-spacing: 0.01em;
		color: var(--text-muted);
	}

	.form-wrap :global(:where(input:not([type='checkbox']):not([type='radio']))) {
		width: 100%;
		min-height: 46px;
		padding: 11px 14px;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.95rem;
		transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
	}
	.form-wrap :global(:where(input:not([type='checkbox']):not([type='radio']):hover)) {
		border-color: #3a3e4f;
	}
	/* A focus RING, not only a border colour change (WCAG 2.4.7 Focus
	   Visible): a 1px border shifting from #2a2d3a to the accent is exactly
	   the change that is easy to miss, and the field a user is typing into is
	   the one thing on this page they must never lose track of. */
	.form-wrap :global(:where(input:not([type='checkbox']):not([type='radio']):focus)) {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 4px rgba(99, 140, 255, 0.22);
	}
	.form-wrap :global(:where(input[aria-invalid='true'])) {
		border-color: var(--danger);
	}

	.form-wrap :global(:where(button[type='submit'])) {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		width: 100%;
		min-height: 48px;
		margin-top: 6px;
		padding: 12px 18px;
		border: none;
		border-radius: 10px;
		background: var(--accent-strong);
		color: #fff;
		font-family: inherit;
		font-size: 0.95rem;
		font-weight: 600;
		cursor: pointer;
		box-shadow: 0 14px 30px -14px rgba(99, 140, 255, 0.95);
		transition: transform 0.15s, box-shadow 0.15s, background 0.15s;
	}
	.form-wrap :global(:where(button[type='submit']:hover:not(:disabled))) {
		transform: translateY(-1px);
		box-shadow: 0 18px 34px -14px rgba(99, 140, 255, 1);
	}
	.form-wrap :global(:where(button[type='submit']:active:not(:disabled))) {
		transform: translateY(0);
	}
	.form-wrap :global(:where(button:focus-visible)) {
		outline: 2px solid var(--accent);
		outline-offset: 3px;
	}
	.form-wrap :global(:where(button[type='submit']:disabled)) {
		opacity: 0.55;
		cursor: not-allowed;
		box-shadow: none;
	}

	.form-wrap :global(:where(.error)) {
		display: flex;
		gap: 8px;
		padding: 11px 14px;
		border-radius: 10px;
		border: 1px solid rgba(248, 113, 113, 0.35);
		background: var(--danger-tint);
		color: var(--danger-on-tint);
		font-size: 0.86rem;
		line-height: 1.45;
	}
</style>
