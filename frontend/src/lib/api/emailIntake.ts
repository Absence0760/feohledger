/**
 * Per-tenant email-to-invoice intake address (admin only).
 *
 * Vendors mail PDFs to `invoices+<token>@<domain>` and the queue fills itself.
 * The `+<token>` part is a **bearer secret**: anyone who knows the address can
 * drop invoices into this tenant's AP queue, and the inbound webhook returns a
 * uniform opaque ack precisely so the token can't be enumerated from outside.
 * Rotation is therefore the containment control for a leak, and it is
 * destructive — the previous address stops resolving the instant the new token
 * is persisted, with no overlap window.
 *
 * Two shapes of "there is no address", and they call for opposite copy:
 *
 *  - `FEOH_EMAIL_INTAKE_DOMAIN` is unset, so the whole channel is off for this
 *    deployment (`intake_address_for` returns null before it ever looks at the
 *    token);
 *  - no token has been provisioned for this org yet — nothing provisions one
 *    automatically, `POST /rotate-token` is the only writer.
 *
 * `domain_configured` separates them on the FIRST read. It used to be
 * inferrable only after a token existed (`enabled: true` + a null `address`
 * proved the platform had no domain), so establishing the unavailable state
 * cost a throwaway token write; the field was added to the route to close that.
 *
 * See `backend/docs/email-intake.md`.
 */
import { api } from '#lib/api.ts';

const BASE = '/api/organization/email-intake';

export interface EmailIntakeStatus {
	/** `invoices+<token>@<domain>`, or null when no address can be rendered. */
	address: string | null;
	/** Whether this org's intake token is provisioned and accepting mail. */
	enabled: boolean;
	/**
	 * Whether the PLATFORM has an intake domain at all — operator config, not
	 * tenant data. False means no org on this deployment can have an address,
	 * however its token is set.
	 */
	domain_configured: boolean;
}

/** `POST /rotate-token` answers with the address only — no `enabled` field. */
export interface EmailIntakeRotated {
	address: string | null;
}

export const getEmailIntake = () => api.get<EmailIntakeStatus>(BASE);

/**
 * Mint a fresh intake token. Also the *provisioning* call for an org that has
 * never had one — the endpoint is the same either way, which is why the UI
 * labels it differently in the two cases: minting a first address invalidates
 * nothing, rotating an existing one invalidates the address every vendor holds.
 */
export const rotateEmailIntakeToken = () => api.post<EmailIntakeRotated>(`${BASE}/rotate-token`, {});
