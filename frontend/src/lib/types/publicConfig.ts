// Response shape of `GET /api/public-config` (backend/app/main.py::public_config)
// — the non-secret deployment config the SPA needs before any session exists.
// Hand-maintained; the project has no codegen. Exported (rather than declared
// inline in the signup page) so an e2e stub of the endpoint can `satisfies` it
// and a new field becomes a compile error in the fixture (docs/decisions.md §136).

export interface PublicConfig {
	/** hCaptcha sitekey for the signup form. Empty = no widget is rendered. */
	hcaptcha_sitekey: string;
	/** Platform tenant URL shape, `{slug}` substituted (e.g. `https://{slug}.example.com`). */
	tenant_url_template: string;
	/**
	 * `FEOH_SIGNUP_ENABLED`. False on an invite-only deployment: every
	 * `/api/signup/*` route answers 404, so `/signup` renders "signup is closed"
	 * instead of a form that could only be refused.
	 */
	signup_enabled: boolean;
}
