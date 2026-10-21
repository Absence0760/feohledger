<script lang="ts">
	import '../../app.css';
	import type { Snippet } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	// The tab title for /login and the steps under it that set none of their
	// own (/login/mfa). A child page with its own <title> replaces it.
	let { children }: { children: Snippet } = $props();
</script>

<svelte:head>
	<title>{m('auth.login.pageTitle')}</title>
</svelte:head>

{@render children()}
