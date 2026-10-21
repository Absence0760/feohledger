<script lang="ts">
	import AuthCallback from '#lib/components/auth/AuthCallback.svelte';
	import { api } from '#lib/api.ts';

	// The backend ACS 303-redirected here with a one-time handoff code. We
	// exchange it for the JWT in the response BODY — the token never rides in
	// the URL (no fragment, no query), so it can't leak via history / Referer /
	// server logs.
	interface SAMLExchangeResponse {
		access_token: string;
		token_type: string;
		must_change_password: boolean;
		tenant_slug: string;
	}

	function exchange(params: URLSearchParams) {
		return api.post<SAMLExchangeResponse>('/api/auth/saml/exchange', { code: params.get('code') });
	}
</script>

<AuthCallback required={['code']} missingKey="auth.callback.missingCode" {exchange} />
