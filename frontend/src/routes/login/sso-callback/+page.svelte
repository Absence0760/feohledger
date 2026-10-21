<script lang="ts">
	import AuthCallback from '#lib/components/auth/AuthCallback.svelte';
	import { api } from '#lib/api.ts';

	// The OIDC redirect target: the IdP hands back `code` + `state`, which the
	// backend verifies and trades for a session.
	interface SSOCallbackResponse {
		access_token: string;
		token_type: string;
		must_change_password: boolean;
		tenant_slug: string;
	}

	function exchange(params: URLSearchParams) {
		return api.post<SSOCallbackResponse>('/api/auth/sso/callback', {
			code: params.get('code'),
			state: params.get('state')
		});
	}
</script>

<AuthCallback required={['code', 'state']} missingKey="auth.callback.missingCodeState" {exchange} />
