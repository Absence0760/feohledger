<script lang="ts">
	import { page } from '$app/state';
	import { api } from '$lib/api';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import SecretReveal from '$lib/components/ui/SecretReveal.svelte';
	import SettingsRail from '$lib/components/ui/SettingsRail.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import type { MessageKey } from '$lib/i18n/messages';
	import { formatDate } from '$lib/utils/time';
	import {
		getChatNotifications,
		revokeChatWebhook,
		rotateChatWebhook,
		updateChatNotifications
	} from '$lib/api/chatNotifications';
	import { getEmailIntake, rotateEmailIntakeToken } from '$lib/api/emailIntake';
	import {
		CHAT_EVENT_LABELS,
		CHAT_PROVIDER_LABELS,
		type ChatNotificationStatus
	} from '$lib/types/chatNotifications';

	interface CompanyProfile {
		address: string;
		phone: string;
		website: string;
		tax_id: string;
		logo_url: string;
		vat_registration_number?: string;
		companies_house_number?: string;
	}

	interface InvoiceDefaults {
		currency: string;
		payment_terms: string;
		number_prefix: string;
		default_gl_account: string;
		default_cost_center: string;
	}

	interface ErpConfig {
		type: string;
		integration_method: string;
		api_key: string;
		account_token: string;
		// Direct adapter fields
		base_url: string;
		tenant_id: string;
		client_id: string;
		client_secret: string;
		environment: string;
		company_id: string;
		account_id: string;
		consumer_key: string;
		consumer_secret: string;
		token_id: string;
		token_secret: string;
	}

	interface FraudRules {
		round_amount_enabled: boolean;
		future_date_enabled: boolean;
		bank_change_enabled: boolean;
		stat_anomaly_enabled: boolean;
		rush_payment_enabled: boolean;
		new_vendor_large_enabled: boolean;
		personal_email_enabled: boolean;
		llm_anomaly_enabled: boolean;
		round_amount_min: string;
		rush_payment_max_days: number;
		new_vendor_max_age_days: number;
		new_vendor_large_amount: string;
		stat_anomaly_sigma: number;
		stat_anomaly_min_history: number;
		personal_email_domains: string[];
	}

	interface OrgSettings {
		company: CompanyProfile;
		invoice_defaults: InvoiceDefaults;
		erp?: ErpConfig;
		fraud_rules?: Partial<FraudRules>;
	}

	const ERP_TYPES = [
		{ value: 'dynamics_365_bc', label: 'Microsoft Dynamics 365 Business Central' },
		{ value: 'sap_s4hana', label: 'SAP S/4HANA' },
		{ value: 'netsuite', label: 'Oracle NetSuite' },
		{ value: 'epicor', label: 'Epicor Kinetic' },
		{ value: 'acumatica', label: 'Acumatica Cloud ERP' },
		{ value: 'sage_x3', label: 'Sage X3' },
		{ value: 'infor', label: 'Infor CloudSuite Industrial' },
		{ value: 'qad', label: 'QAD Adaptive' },
		{ value: 'cetec', label: 'Cetec ERP' },
		{ value: 'delmiaworks', label: 'DELMIAWorks' },
	];

	interface OrgResponse {
		id: string;
		name: string;
		slug: string;
		plan: string;
		settings: OrgSettings;
		created_at: string;
		// The server's own resolved reporting currency (all four rungs of
		// `currency_conversion.resolve_reporting_currency`, admin or not). This
		// page doesn't render it directly — it reloads the `orgCurrency` store
		// after a save instead (see `orgCurrency.reset()` below) — but the field
		// is always present, so the type stays honest with the wire shape.
		resolved_reporting_currency: string;
	}

	let org = $state<OrgResponse | null>(null);
	// Editable fields
	let name = $state('');
	let address = $state('');
	let phone = $state('');
	let website = $state('');
	let taxId = $state('');
	let vatNumber = $state('');
	let companiesHouseNumber = $state('');
	let currency = $state('USD');
	let paymentTerms = $state('Net 30');
	let numberPrefix = $state('INV-');
	let defaultGl = $state('');
	let defaultCostCenter = $state('');
	// ERP
	let erpType = $state('dynamics_365_bc');
	let erpMethod = $state('merge_dev');
	let erpApiKey = $state('');
	let erpAccountToken = $state('');
	let erpBaseUrl = $state('');
	let erpTenantId = $state('');
	let erpClientId = $state('');
	let erpClientSecret = $state('');
	let erpEnvironment = $state('production');
	let erpCompanyId = $state('');
	let erpAccountId = $state('');
	let erpConsumerKey = $state('');
	let erpConsumerSecret = $state('');
	let erpTokenId = $state('');
	let erpTokenSecret = $state('');
	let testingConnection = $state(false);
	// Extraction
	let extractionProgramType = $state('platform');
	let extractionProvider = $state('claude_vision');
	let extractionApiKey = $state('');
	let extractionAwsKeyId = $state('');
	let extractionAwsSecret = $state('');
	let extractionAwsRegion = $state('us-east-1');
	let savingExtraction = $state(false);
	let testingExtraction = $state(false);
	let extractionTestResult = $state<{ success: boolean; message: string } | null>(null);

	async function testExtraction() {
		testingExtraction = true;
		extractionTestResult = null;
		try {
			extractionTestResult = await api.post<{ success: boolean; message: string }>('/api/organization/test-extraction', {
				provider: extractionProgramType === 'platform' ? 'claude_vision' : extractionProvider,
				api_key: extractionApiKey,
				aws_access_key_id: extractionAwsKeyId,
				aws_secret_access_key: extractionAwsSecret,
				aws_region: extractionAwsRegion,
				base_url: extractionOllamaUrl,
				model: extractionOllamaModel,
			});
		} catch (err) {
			extractionTestResult = { success: false, message: err instanceof Error ? err.message : m('org.toast.testFailed') };
		} finally {
			testingExtraction = false;
		}
	}

	const EXTRACTION_PROVIDERS = [
		{ value: 'claude_vision', label: 'Claude Vision (Anthropic)' },
		{ value: 'openai_vision', label: 'GPT-4V (OpenAI)' },
		{ value: 'aws_textract', label: 'AWS Textract' },
		{ value: 'ollama', label: 'Ollama (Local)' },
	];
	let extractionOllamaUrl = $state('http://localhost:11434');
	let extractionOllamaModel = $state('llama3.2-vision:11b');
	// Cards
	let cardsEnabled = $state(false);
	let cardsProgramType = $state('platform');
	let cardsProvider = $state('');
	let cardsRegion = $state('US');
	let cardsApiKey = $state('');
	let cardsClientId = $state('');
	let cardsClientSecret = $state('');
	let cardsCustomerHashId = $state('');
	let cardsWalletHashId = $state('');
	let cardsExpiryDays = $state(30);
	let cardsSandbox = $state(true);
	let savingCards = $state(false);
	const CARD_REGIONS = [
		{ value: 'US', label: 'United States', default_provider: 'lithic' },
		{ value: 'UK', label: 'United Kingdom', default_provider: 'lithic' },
		{ value: 'DE', label: 'Germany (EU)', default_provider: 'lithic' },
		{ value: 'FR', label: 'France (EU)', default_provider: 'lithic' },
		{ value: 'NL', label: 'Netherlands (EU)', default_provider: 'lithic' },
		{ value: 'ZA', label: 'South Africa', default_provider: 'nium' },
		{ value: 'AU', label: 'Australia', default_provider: 'nium' },
		{ value: 'SG', label: 'Singapore', default_provider: 'nium' },
		{ value: 'HK', label: 'Hong Kong', default_provider: 'nium' },
		{ value: 'IN', label: 'India', default_provider: 'nium' },
		{ value: 'CA', label: 'Canada', default_provider: 'nium' },
		{ value: 'AE', label: 'UAE', default_provider: 'nium' },
		{ value: 'JP', label: 'Japan', default_provider: 'nium' },
	];

	let autoProvider = $derived(
		CARD_REGIONS.find(r => r.value === cardsRegion)?.default_provider ?? 'nium'
	);
	let effectiveProvider = $derived(cardsProvider || autoProvider);
	let connectionResult = $state<{ success: boolean; message: string } | null>(null);

	async function testConnection() {
		testingConnection = true;
		connectionResult = null;
		try {
			connectionResult = await api.post<{ success: boolean; message: string }>('/api/organization/test-erp', {
				type: erpType,
				integration_method: erpMethod,
				api_key: erpApiKey,
				account_token: erpAccountToken,
				base_url: erpBaseUrl,
				tenant_id: erpTenantId,
				client_id: erpClientId,
				client_secret: erpClientSecret,
				environment: erpEnvironment,
				company_id: erpCompanyId,
				account_id: erpAccountId,
				consumer_key: erpConsumerKey,
				consumer_secret: erpConsumerSecret,
				token_id: erpTokenId,
				token_secret: erpTokenSecret,
			});
		} catch (err) {
			connectionResult = { success: false, message: err instanceof Error ? err.message : m('org.toast.testFailed') };
		} finally {
			testingConnection = false;
		}
	}

	// ── Read-only mode for non-admins ───────────────────────────────────
	// Every mutating endpoint behind this page is `require_roles(ROLE_ADMIN)`
	// — the server is and stays the authority. What was missing was any
	// client-side acknowledgement of that: a clerk navigating straight here got
	// a fully editable form across ~10 panels whose every Save 403s, which
	// reads as a broken product rather than a permission boundary.
	//
	// `auth.user` starts null while `GET /api/auth/me` is in flight, and
	// `isAdmin` is false until it lands — so gate on the user having RESOLVED,
	// otherwise the banner flashes on every admin's page load and the form is
	// briefly disabled under them.
	const userLoaded = $derived(auth.user !== null);
	const readOnly = $derived(userLoaded && !auth.isAdmin);

	// ── Section navigation ────────────────────────────────────────────────
	// Fifteen panels' worth of unrelated concerns — company identity, DNS, chat
	// webhooks, an AI model choice, ACH rails, fraud thresholds, the billing
	// plan — share this route because they share `PATCH /api/organization`.
	// Stacked on one scroll that made the page a book with no contents page: ten
	// of the fifteen were reachable only by scrolling and reading. So one panel
	// shows at a time, picked from `ui/SettingsRail.svelte` and addressed by
	// `?section=`, the same URL-backed shape `/admin?tab=` already uses (and
	// which `lib/nav.ts` already deep-links into).
	//
	// Field state stays here, at page level, deliberately: only the MARKUP is
	// conditional, so an unsaved edit in one panel survives a trip to another
	// and is still there — with its Save button — on the way back.
	//
	// A slug is part of the URL contract: it lands in bookmarks, in the
	// Getting-started links below, and in the docs. Renaming one breaks those.
	type SectionGroup = {
		/** Omitted for the lead-in panel, which stands outside the grouping. */
		labelKey?: MessageKey;
		items: { slug: string; labelKey: MessageKey }[];
	};

	const SECTION_GROUPS: SectionGroup[] = [
		{ items: [{ slug: 'getting-started', labelKey: 'org.gettingStarted.title' }] },
		{
			labelKey: 'org.rail.group.company',
			items: [
				{ slug: 'company', labelKey: 'org.section.company' },
				{ slug: 'defaults', labelKey: 'org.section.defaults' },
				{ slug: 'branding', labelKey: 'org.section.branding' },
				{ slug: 'custom-domains', labelKey: 'org.section.customDomains' }
			]
		},
		{
			labelKey: 'org.rail.group.integrations',
			items: [
				{ slug: 'erp', labelKey: 'org.section.erp' },
				{ slug: 'extraction', labelKey: 'org.section.extraction' },
				{ slug: 'email-intake', labelKey: 'org.section.emailIntake' },
				{ slug: 'chat', labelKey: 'org.section.chat' },
				{ slug: 'data-sync', labelKey: 'org.section.dataSync' }
			]
		},
		{
			labelKey: 'org.rail.group.money',
			items: [
				// The rail's own short label: the panel heading is "Payments (ACH /
				// Wire / RTP)", which does not sit in a 200px column.
				{ slug: 'payments', labelKey: 'org.rail.payments' },
				{ slug: 'cards', labelKey: 'org.section.cards' }
			]
		},
		{
			labelKey: 'org.rail.group.compliance',
			items: [
				{ slug: 'security', labelKey: 'org.section.security' },
				{ slug: 'fraud', labelKey: 'org.section.fraud' },
				{ slug: 'residency', labelKey: 'org.section.dataResidency' }
			]
		},
		{
			labelKey: 'org.rail.group.account',
			items: [{ slug: 'plan', labelKey: 'org.section.plan' }]
		}
	];

	const SECTION_SLUGS = new Set(SECTION_GROUPS.flatMap((g) => g.items.map((i) => i.slug)));
	const DEFAULT_SECTION = 'getting-started';

	// The five anchors the page was navigated by before it had panels. They live
	// in the Getting-started card, and outside this repo in whatever bookmarks
	// and links people already hold, so they have to keep working.
	const LEGACY_HASH_SECTIONS: Record<string, string> = {
		'org-company': 'company',
		'org-defaults': 'defaults',
		'org-branding': 'branding',
		'org-email-intake': 'email-intake',
		'org-payments': 'payments'
	};

	// An unknown slug falls back rather than rendering an empty page — the same
	// treatment `/admin` gives a stale `?tab=`.
	//
	// A legacy anchor is resolved HERE, by derivation, rather than by rewriting
	// the URL to `?section=` in an effect. Rewriting was the first version and is
	// the wrong shape for this: `replaceState` throws if it runs before the
	// SvelteKit router has initialised, and an effect on first hydration is
	// exactly that window — so the tidier URL came at the cost of a failure mode
	// on the one path whose whole job is to still work for a link someone saved
	// a year ago. Deriving it cannot fail, needs no import, and the stale `#hash`
	// clears itself on the first rail click (`SettingsRail`'s hrefs are
	// query-only). Precedence is query-before-hash so an explicit `?section=`
	// always wins over an anchor that happens to be along for the ride.
	const section = $derived.by(() => {
		const slug = page.url.searchParams.get('section');
		if (slug && SECTION_SLUGS.has(slug)) return slug;
		return LEGACY_HASH_SECTIONS[page.url.hash.replace(/^#/, '')] ?? DEFAULT_SECTION;
	});

	const railGroups = $derived(
		SECTION_GROUPS.map((g) => ({
			label: g.labelKey ? m(g.labelKey) : undefined,
			items: g.items.map((i) => ({ slug: i.slug, label: m(i.labelKey) }))
		}))
	);

	// The page's EAGER read. `GET /api/organization` alone fills the company,
	// defaults, ERP, cards, security, fraud-override, payments and extraction
	// forms plus the plan card, and the whole template is gated on it, so it has
	// no panel to be lazy about.
	//
	// Every other read belongs to exactly one panel and now waits for it — seven
	// requests on arrival became two, and an admin who came to change the ACH
	// cut-off no longer pays for a DNS lookup, a residency read and a chat-webhook
	// probe on the way. Each is `once()`-guarded so returning to a panel does not
	// refetch, and the three admin-only ones keep their own role gate: a doomed
	// request is not a way to find out what your role is.
	$effect(() => {
		loadOrg();
		// `payments.cfo_approval_above` is a bare number denominated in the org's
		// reporting currency (`payment_controls.cfo_approval_decision`), so the
		// field names that currency instead of hardcoding a dollar sign.
		orgCurrency.ensureLoaded();
	});

	/**
	 * Panel-scoped loads, fired the first time their panel is shown.
	 *
	 * `requestedPanels` is keyed by panel rather than by a per-loader boolean so the
	 * set is visible in one place; a loader that also needs `auth` keeps its own
	 * effect below, because folding those in would make every panel's read
	 * re-run the moment `/me` resolves.
	 */
	const requestedPanels = new Set<string>();

	function once(slug: string, load: () => void) {
		if (section !== slug || requestedPanels.has(slug)) return;
		requestedPanels.add(slug);
		load();
	}

	$effect(() => {
		// `/api/public-config` (public) and
		// `/api/organization/branding/custom-domains` + `/data-residency` (both
		// `get_current_user`) answer a clerk with real tenant data, so these three
		// need no role gate — only their panel.
		once('branding', loadPlatformTenantUrl);
		once('custom-domains', loadCustomDomains);
		once('residency', loadResidency);
	});

	async function loadOrg() {
		try {
			const data = await api.get<OrgResponse>('/api/organization');
			org = data;
			name = data.name;
			address = data.settings.company.address;
			phone = data.settings.company.phone;
			website = data.settings.company.website;
			taxId = data.settings.company.tax_id;
			vatNumber = data.settings.company.vat_registration_number ?? '';
			companiesHouseNumber = data.settings.company.companies_house_number ?? '';
			currency = data.settings.invoice_defaults.currency;
			paymentTerms = data.settings.invoice_defaults.payment_terms;
			numberPrefix = data.settings.invoice_defaults.number_prefix;
			defaultGl = data.settings.invoice_defaults.default_gl_account;
			defaultCostCenter = data.settings.invoice_defaults.default_cost_center;
			// ERP
			const erp = (data.settings as unknown as Record<string, unknown>).erp as ErpConfig | undefined;
			if (erp) {
				erpType = erp.type || 'dynamics_365_bc';
				erpMethod = erp.integration_method || 'merge_dev';
				erpApiKey = erp.api_key || '';
				erpAccountToken = erp.account_token || '';
				erpBaseUrl = erp.base_url || '';
				erpTenantId = erp.tenant_id || '';
				erpClientId = erp.client_id || '';
				erpClientSecret = erp.client_secret || '';
				erpEnvironment = erp.environment || 'production';
				erpCompanyId = erp.company_id || '';
				erpAccountId = erp.account_id || '';
				erpConsumerKey = erp.consumer_key || '';
				erpConsumerSecret = erp.consumer_secret || '';
				erpTokenId = erp.token_id || '';
				erpTokenSecret = erp.token_secret || '';
			}
			// Cards
			const cards = (data.settings as unknown as Record<string, unknown>).cards as Record<string, unknown> | undefined;
			if (cards) {
				cardsEnabled = (cards.enabled as boolean) ?? false;
				cardsProgramType = (cards.program_type as string) || 'platform';
				cardsProvider = (cards.provider as string) || '';
				cardsRegion = (cards.region as string) || 'US';
				cardsApiKey = (cards.api_key as string) || '';
				cardsClientId = (cards.client_id as string) || '';
				cardsClientSecret = (cards.client_secret as string) || '';
				cardsCustomerHashId = (cards.customer_hash_id as string) || '';
				cardsWalletHashId = (cards.wallet_hash_id as string) || '';
				cardsExpiryDays = (cards.default_expiry_days as number) || 30;
				cardsSandbox = (cards.sandbox as boolean) ?? true;
			}
			// Security (MFA enforcement)
			const mfaCfg = (data.settings as unknown as Record<string, unknown>).mfa as
				| Record<string, unknown>
				| undefined;
			mfaRequired = (mfaCfg?.required as boolean) ?? false;

			// Fraud rules — this read carries only the org's OVERRIDES. The
			// canonical defaults they layer onto come from an admin-only endpoint
			// fetched in its own role-gated effect, so the two halves can land in
			// either order and whichever arrives second composes the form.
			fraudOverrides =
				((data.settings as unknown as Record<string, unknown>).fraud_rules as
					| Partial<FraudRules>
					| undefined) ?? {};
			composeFraud();
			// Payments
			const pmt = (data.settings as unknown as Record<string, unknown>).payments as
				| Record<string, unknown>
				| undefined;
			if (pmt) {
				paymentsProvider = (pmt.provider as string) || 'mock';
				paymentsProgramType = (pmt.program_type as string) || 'byok';
				paymentsApiKey = (pmt.api_key as string) || '';
				paymentsOrgId = (pmt.org_id as string) || '';
				paymentsOriginatingAccount = (pmt.originating_account_id as string) || '';
				paymentsWebhookSecret = (pmt.webhook_secret as string) || '';
				paymentsSandbox = (pmt.sandbox as boolean) ?? true;
				paymentsCfoThreshold = (pmt.cfo_approval_above as number | null) ?? null;
			}
			// Extraction
			const extraction = (data.settings as unknown as Record<string, unknown>).extraction as Record<string, unknown> | undefined;
			if (extraction) {
				extractionProgramType = (extraction.program_type as string) || 'platform';
				extractionProvider = (extraction.provider as string) || 'claude_vision';
				extractionApiKey = (extraction.api_key as string) || '';
				extractionAwsKeyId = (extraction.aws_access_key_id as string) || '';
				extractionAwsSecret = (extraction.aws_secret_access_key as string) || '';
				extractionAwsRegion = (extraction.aws_region as string) || 'us-east-1';
				extractionOllamaUrl = (extraction.base_url as string) || 'http://localhost:11434';
				extractionOllamaModel = (extraction.model as string) || 'llama3.2-vision:11b';
			}
			// Branding (white-label)
			const brandCfg = (data.settings as unknown as Record<string, unknown>).brand as
				| Record<string, unknown>
				| undefined;
			if (brandCfg) {
				brandProductName = (brandCfg.product_name as string) || '';
				brandLogoUrl = (brandCfg.logo_url as string) || '';
				brandAccentColor = (brandCfg.accent_color as string) || '';
				brandAccentStrongColor = (brandCfg.accent_strong_color as string) || '';
				brandSupportUrl = (brandCfg.support_url as string) || '';
				brandLegalUrl = (brandCfg.legal_url as string) || '';
				brandTenantUrlTemplate = (brandCfg.tenant_url_template as string) || '';
				brandSsoCallbackBaseUrl = (brandCfg.sso_callback_base_url as string) || '';
			}
		} catch {
			toast(m('org.toast.loadFailed'), 'error');
		}
	}

	let savingProfile = $state(false);
	let savingDefaults = $state(false);
	let savingErp = $state(false);

	// Security
	let mfaRequired = $state(false);
	let savingSecurity = $state(false);
	// Advisory, computed server-side on every read (never persisted) — whether
	// "require MFA" is actually enforced right now, or a silent no-op because
	// the platform master switch (FEOH_MFA_ENABLED) is off. Derived off `org`
	// (not a plain load-time assignment) so it stays correct after
	// `saveSecurity()` replaces `org` with the PATCH response too. See
	// backend/app/api/organization.py's `_org_response`.
	let mfaEnforcementActive = $derived(
		((org?.settings as unknown as Record<string, unknown> | undefined)?.mfa as
			| Record<string, unknown>
			| undefined)?.enforcement_active === true
	);

	// Fraud detection — defaults loaded once from the backend; the form
	// reflects (defaults ⊕ org overrides) so a stale UI can't drift from
	// what the warning engine actually evaluates.
	let fraudDefaults = $state<FraudRules | null>(null);
	let fraud = $state<FraudRules | null>(null);
	let personalEmailDomainsText = $state('');
	let savingFraud = $state(false);
	// The org's stored overrides, held on their own because the two halves of
	// this form arrive from two endpoints with two different gates: the overrides
	// ride the role-open `GET /api/organization`, the defaults come from an
	// admin-only route. `null` is "the org read has not landed", which is NOT the
	// same fact as `{}` ("this org overrides nothing").
	let fraudOverrides = $state<Partial<FraudRules> | null>(null);

	/** Compose the form from (defaults ⊕ overrides), once both have landed. */
	function composeFraud() {
		if (!fraudDefaults || fraudOverrides === null) return;
		fraud = { ...fraudDefaults, ...fraudOverrides };
		personalEmailDomainsText = fraud.personal_email_domains.join('\n');
	}

	$effect(() => {
		// `GET /api/organization/fraud-rules/defaults` is admin-only, so this is
		// gated on the role like the chat and email-intake reads rather than fired
		// and swallowed. For a non-admin the panel renders the admin-only hint, so
		// the request was not merely refused — it was pointless.
		//
		// `fraudDefaults` is its own already-loaded guard, so this one needs no
		// `once()` — the panel can be revisited without refetching.
		if (section !== 'fraud' || !userLoaded || !auth.isAdmin || fraudDefaults) return;
		loadFraudDefaults();
	});

	async function loadFraudDefaults() {
		try {
			fraudDefaults = await api.get<FraudRules>('/api/organization/fraud-rules/defaults');
			composeFraud();
		} catch {
			// Non-fatal, and deliberately not substituted: a hardcoded client-side
			// default would render switches claiming a rule set nobody configured,
			// beside a Reset button with nothing true to reset to. The panel stays
			// absent instead.
		}
	}

	// Payments
	let paymentsProvider = $state('mock');
	let paymentsProgramType = $state('byok'); // mock = no key; modern_treasury = byok keys
	let paymentsApiKey = $state('');
	let paymentsOrgId = $state('');
	let paymentsOriginatingAccount = $state('');
	let paymentsWebhookSecret = $state('');
	let paymentsSandbox = $state(true);
	let paymentsCfoThreshold = $state<number | null>(null);
	let savingPayments = $state(false);
	let testingPayments = $state(false);
	let paymentsTestResult = $state<{ success: boolean; message: string } | null>(null);

	async function testPayments() {
		testingPayments = true;
		paymentsTestResult = null;
		try {
			paymentsTestResult = await api.post<{ success: boolean; message: string }>(
				'/api/organization/test-payments',
				{
					provider: paymentsProvider,
					api_key: paymentsApiKey,
					org_id: paymentsOrgId,
					originating_account_id: paymentsOriginatingAccount,
					sandbox: paymentsSandbox,
				}
			);
		} catch (err) {
			paymentsTestResult = {
				success: false,
				message: err instanceof Error ? err.message : m('org.toast.testFailed'),
			};
		} finally {
			testingPayments = false;
		}
	}

	async function patchSettings(section: string, partial: Record<string, unknown>) {
		const data = await api.patch<OrgResponse>('/api/organization', {
			...(partial.company ? { name: name.trim() } : {}),
			settings: partial,
		});
		org = data;
		toast(m('org.toast.sectionSaved', { section }), 'success');
	}

	async function saveProfile() {
		savingProfile = true;
		try {
			await patchSettings(m('org.section.companySaved'), {
				company: {
					address, phone, website,
					tax_id: taxId,
					vat_registration_number: vatNumber,
					companies_house_number: companiesHouseNumber,
					logo_url: org?.settings.company.logo_url ?? '',
				},
			});
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingProfile = false;
		}
	}

	async function saveDefaults() {
		savingDefaults = true;
		try {
			await patchSettings(m('org.section.defaultsSaved'), {
				invoice_defaults: {
					currency,
					payment_terms: paymentTerms,
					number_prefix: numberPrefix,
					default_gl_account: defaultGl,
					default_cost_center: defaultCostCenter,
				},
			});
			// `invoice_defaults.currency` is a rung of the reporting-currency
			// chain, and the store is session-cached: re-resolve it, or every
			// label on this page (the CFO threshold's included) keeps naming the
			// answer from before the save until a reload.
			orgCurrency.reset();
			void orgCurrency.ensureLoaded();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingDefaults = false;
		}
	}

	async function saveErp() {
		savingErp = true;
		try {
			await patchSettings(m('org.section.erpSaved'), {
				erp: {
					type: erpType,
					integration_method: erpMethod,
					api_key: erpApiKey,
					account_token: erpAccountToken,
					base_url: erpBaseUrl,
					tenant_id: erpTenantId,
					client_id: erpClientId,
					client_secret: erpClientSecret,
					environment: erpEnvironment,
					company_id: erpCompanyId,
					account_id: erpAccountId,
					consumer_key: erpConsumerKey,
					consumer_secret: erpConsumerSecret,
					token_id: erpTokenId,
					token_secret: erpTokenSecret,
				},
			});
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingErp = false;
		}
	}

	async function saveExtraction() {
		savingExtraction = true;
		try {
			await patchSettings(m('org.section.extractionSaved'), {
				extraction: {
					program_type: extractionProgramType,
					provider: extractionProvider,
					api_key: extractionApiKey,
					aws_access_key_id: extractionAwsKeyId,
					aws_secret_access_key: extractionAwsSecret,
					aws_region: extractionAwsRegion,
					base_url: extractionOllamaUrl,
					model: extractionOllamaModel,
				},
			});
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingExtraction = false;
		}
	}

	async function saveCards() {
		savingCards = true;
		try {
			await patchSettings(m('org.section.cardsSaved'), {
				cards: {
					enabled: cardsEnabled,
					program_type: cardsProgramType,
					provider: cardsProvider || autoProvider,
					region: cardsRegion,
					api_key: cardsApiKey,
					client_id: cardsClientId,
					client_secret: cardsClientSecret,
					customer_hash_id: cardsCustomerHashId,
					wallet_hash_id: cardsWalletHashId,
					default_expiry_days: cardsExpiryDays,
					sandbox: cardsSandbox,
				},
			});
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingCards = false;
		}
	}

	async function saveSecurity() {
		savingSecurity = true;
		try {
			await patchSettings(m('org.section.securitySaved'), {
				mfa: { required: mfaRequired },
			});
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingSecurity = false;
		}
	}

	async function saveFraud() {
		if (!fraud) return;
		savingFraud = true;
		try {
			const domains = personalEmailDomainsText
				.split(/[\n,]+/)
				.map((d) => d.trim().toLowerCase())
				.filter((d) => d.length > 0);
			const payload: FraudRules = { ...fraud, personal_email_domains: domains };
			await patchSettings(m('org.section.fraudSaved'), { fraud_rules: payload });
			fraud = payload;
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingFraud = false;
		}
	}

	function resetFraudToDefaults() {
		if (!fraudDefaults) return;
		fraud = { ...fraudDefaults };
		personalEmailDomainsText = fraudDefaults.personal_email_domains.join('\n');
	}

	async function savePayments() {
		savingPayments = true;
		try {
			await patchSettings(m('org.section.paymentsSaved'), {
				payments: {
					provider: paymentsProvider,
					program_type: paymentsProgramType,
					api_key: paymentsApiKey,
					org_id: paymentsOrgId,
					originating_account_id: paymentsOriginatingAccount,
					webhook_secret: paymentsWebhookSecret,
					sandbox: paymentsSandbox,
					cfo_approval_above: paymentsCfoThreshold,
				},
			});
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingPayments = false;
		}
	}

	const planLabel = (plan: string): string =>
		({
			free: m('org.plan.free'),
			pro: m('org.plan.pro'),
			enterprise: m('org.plan.enterprise'),
		})[plan] ?? plan;

	// ── White-label branding ────────────────────────────────────────────
	import { brand } from '$lib/stores/brand.svelte';
	import { accentStrongContrast } from '$lib/stores/brandTheme';
	import { formatRatio, WCAG_AA_NORMAL } from '$lib/a11y/contrast';
	import FieldWarning from '$lib/components/ui/FieldWarning.svelte';

	let brandProductName = $state('');
	let brandLogoUrl = $state('');
	let brandAccentColor = $state('');
	let brandAccentStrongColor = $state('');
	let brandSupportUrl = $state('');
	let brandLegalUrl = $state('');
	// Per-tenant base URL stamped into outbound emails (approval links, portal
	// invites, signup confirmations). Empty ⇒ the platform default below.
	// `{slug}` in the value is optional: substituted when present, used
	// verbatim when not. See docs/white-label.md § Tenant URL.
	let brandTenantUrlTemplate = $state('');
	// Deliberately a SEPARATE field from the one above: this value is registered
	// at the customer's IdP, so changing it is an operator-sequenced migration
	// (add the new URI at the IdP first, verify a real login, then remove the
	// old one) rather than a preference. Folding the two would mean fixing
	// invite links silently breaks SSO.
	let brandSsoCallbackBaseUrl = $state('');
	let savingBranding = $state(false);

	// The PLATFORM default (`FEOH_TENANT_URL_TEMPLATE`), read from the public
	// config route so the panel can show an admin what "leave this blank"
	// actually resolves to rather than describing it in the abstract. The org
	// override is what we save; this is only ever displayed.
	let platformTenantUrlTemplate = $state('');

	/** Resolve a `{slug}` template against this tenant. */
	function resolveTenantUrl(template: string): string {
		const t = (template || '').trim();
		if (!t) return '';
		return t.replace(/\{slug\}/g, org?.slug ?? '');
	}

	const defaultTenantUrl = $derived(resolveTenantUrl(platformTenantUrlTemplate));
	const effectiveTenantUrl = $derived(
		resolveTenantUrl(brandTenantUrlTemplate) || defaultTenantUrl
	);

	async function loadPlatformTenantUrl() {
		try {
			const cfg = await api.get<{ tenant_url_template?: string }>('/api/public-config');
			platformTenantUrlTemplate = cfg.tenant_url_template ?? '';
		} catch {
			// Non-fatal: the field still saves, we just can't show the default.
			platformTenantUrlTemplate = '';
		}
	}

	const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
	// Default accent tokens (mirror src/app.css :root) so the color pickers show
	// the live default when the org hasn't set one — without writing it back.
	const DEFAULT_ACCENT = '#638cff';
	const DEFAULT_ACCENT_STRONG = '#3f5fd6';

	// The strong accent is written straight into the `--accent-strong` custom
	// property, whose one contract is that white text sits on it. Warn while
	// the field is still being edited; the backend accepts any valid hex and
	// the brand is the tenant's call, so this advises rather than blocks.
	const accentStrongRatio = $derived(accentStrongContrast(brandAccentStrongColor));
	const accentStrongFails = $derived(
		accentStrongRatio !== null && accentStrongRatio < WCAG_AA_NORMAL
	);

	async function saveBranding() {
		// Client-side validation mirroring the backend BrandConfig guards, so a
		// typo surfaces inline instead of as a 422.
		if (brandAccentColor.trim() && !HEX_RE.test(brandAccentColor.trim())) {
			toast(m('org.branding.toast.accentInvalid'), 'error');
			return;
		}
		if (brandAccentStrongColor.trim() && !HEX_RE.test(brandAccentStrongColor.trim())) {
			toast(m('org.branding.toast.accentStrongInvalid'), 'error');
			return;
		}
		for (const [label, val] of [
			[m('org.branding.label.logoUrl'), brandLogoUrl],
			[m('org.branding.label.supportUrl'), brandSupportUrl],
			[m('org.branding.label.legalUrl'), brandLegalUrl],
			[m('org.branding.label.tenantUrl'), brandTenantUrlTemplate],
			[m('org.branding.label.ssoCallback'), brandSsoCallbackBaseUrl],
		] as const) {
			if (val.trim() && !/^https?:\/\//i.test(val.trim())) {
				toast(m('org.branding.toast.urlInvalid', { label }), 'error');
				return;
			}
		}
		savingBranding = true;
		try {
			await api.put('/api/organization/branding', {
				product_name: brandProductName.trim(),
				logo_url: brandLogoUrl.trim(),
				accent_color: brandAccentColor.trim(),
				accent_strong_color: brandAccentStrongColor.trim(),
				support_url: brandSupportUrl.trim(),
				legal_url: brandLegalUrl.trim(),
				tenant_url_template: brandTenantUrlTemplate.trim(),
				// Sent explicitly on every save. The backend carries an OMITTED
				// value forward precisely so a client that doesn't know about this
				// field can't wipe an IdP-registered callback; since this panel
				// does know about it, sending the current value keeps "clear the
				// box and save" working as the documented rollback.
				sso_callback_base_url: brandSsoCallbackBaseUrl.trim(),
			});
			// Refresh the live brand store so the sidebar logo/name + theme update
			// without a reload.
			brand.reset();
			await brand.ensureLoadedAndApply();
			toast(m('org.branding.toast.saved'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.toast.saveFailed'), 'error');
		} finally {
			savingBranding = false;
		}
	}

	// ── Custom domains (white-label vanity hostnames) ───────────────────
	// Manages settings.brand.custom_domains — the list the backend resolver
	// matches an inbound Host against (with the JWT org-claim cross-check still
	// gating access). See docs/white-label.md § Custom domains.
	let customDomains = $state<string[]>([]);
	let newDomain = $state('');
	let loadingDomains = $state(true);
	let domainsError = $state('');
	let savingDomains = $state(false);
	// The host being removed is armed for a confirm-on-second-click.
	let confirmRemoveDomain = $state<string | null>(null);
	// A REFUSED add/remove, rendered as a persistent inline alert rather than a
	// toast that fades. The backend's refusals are specific and actionable — a
	// host already claimed by another tenant (409), or a host under the
	// platform's own domain — and that message is the whole value of the
	// response, so it stays on screen next to the field that caused it.
	let domainSaveError = $state('');

	// Mirror of the backend normalize_custom_domain: bare, lowercase hostname,
	// no scheme / path / port / spaces. Returns null when there's nothing usable.
	function normalizeDomain(raw: string): string | null {
		let h = (raw || '').trim().toLowerCase();
		if (!h) return null;
		if (h.startsWith('[')) return null; // IPv6 literal — never a custom domain
		// Strip a scheme if the user pasted a full URL.
		h = h.replace(/^https?:\/\//, '');
		// Drop a path/query and a :port suffix.
		h = h.split('/')[0].split('?')[0].split(':')[0];
		if (!h || h.includes(' ')) return null;
		// UX guard, intentionally STRICTER than the backend: require a dotted
		// hostname (label.label…), since a real vanity domain always has a TLD.
		// The backend `normalize_custom_domain` is the authority and accepts a
		// bare single-label host too; this only spares the operator an obvious
		// typo client-side (the safe direction — backend still validates).
		if (!/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/.test(h)) {
			return null;
		}
		return h;
	}

	async function loadCustomDomains() {
		loadingDomains = true;
		domainsError = '';
		try {
			const data = await api.get<{ custom_domains: string[] }>(
				'/api/organization/branding/custom-domains'
			);
			customDomains = data.custom_domains ?? [];
		} catch (err) {
			domainsError = err instanceof Error ? err.message : m('org.customDomains.toast.loadFailed');
		} finally {
			loadingDomains = false;
		}
	}

	// PUT replaces the whole list (the backend endpoint is a full replace),
	// re-reads the normalized result, and refreshes local state.
	async function saveCustomDomains(next: string[]) {
		savingDomains = true;
		domainSaveError = '';
		try {
			const data = await api.put<{ custom_domains: string[] }>(
				'/api/organization/branding/custom-domains',
				{ custom_domains: next }
			);
			customDomains = data.custom_domains ?? [];
			return true;
		} catch (err) {
			// `ApiError.message` is already `formatApiDetail`'d, so a 409
			// ("already registered to another organization") or the
			// platform-domain refusal reads as itself instead of a generic
			// "Failed to save".
			domainSaveError =
				err instanceof Error && err.message
					? err.message
					: m('org.customDomains.toast.saveFailed');
			return false;
		} finally {
			savingDomains = false;
		}
	}

	async function addCustomDomain() {
		const host = normalizeDomain(newDomain);
		if (!host) {
			domainSaveError = m('org.customDomains.toast.invalid');
			return;
		}
		if (customDomains.includes(host)) {
			domainSaveError = m('org.customDomains.toast.duplicate');
			return;
		}
		const ok = await saveCustomDomains([...customDomains, host]);
		if (ok) {
			newDomain = '';
			toast(m('org.customDomains.toast.added'), 'success');
		}
	}

	// ── Data residency (GDPR/CCPA region pin) ───────────────────────────
	// Manages settings.residency.region plus the backend's advisory
	// configured-vs-deployed `alignment` verdict — which is the whole point of
	// showing this here: the pin is a commitment, and an admin should be able to
	// see whether the platform is physically honouring it yet. Nothing on this
	// panel blocks; the region never moves data by itself.
	// See docs/data-residency.md.
	interface ResidencyAlignment {
		status: string; // "aligned" | "misaligned" | "unknown"
		aligned: boolean | null; // null ⇔ status "unknown" — never read as yes
		deployed_region: string | null;
		reason: string | null;
	}
	interface ResidencyResponse {
		region: string;
		default_region: string;
		supported_regions: string[];
		placement: Record<string, string>;
		alignment: ResidencyAlignment;
	}

	// Region tokens come from the server; their display names are ours.
	const REGION_LABEL_KEYS: Record<string, MessageKey> = {
		us: 'org.residency.region.us',
		eu: 'org.residency.region.eu',
		uk: 'org.residency.region.uk',
		ca: 'org.residency.region.ca',
		au: 'org.residency.region.au'
	};

	let residencyRegion = $state(''); // the select's bound value
	let residencySavedRegion = $state(''); // last persisted effective region
	let residencyDefault = $state('');
	let residencyRegions = $state<string[]>([]);
	let residencyPlacement = $state<Record<string, string>>({});
	let residencyAlignment = $state<ResidencyAlignment | null>(null);
	let loadingResidency = $state(true);
	let residencyError = $state('');
	let savingResidency = $state(false);

	// An unmapped token renders as itself rather than vanishing — the server
	// owns the supported set, so a region added there stays selectable here.
	function regionLabel(token: string | null): string {
		if (!token) return '';
		const key = REGION_LABEL_KEYS[token];
		return key ? m(key) : token.toUpperCase();
	}

	function applyResidency(data: ResidencyResponse) {
		residencyRegion = data.region;
		residencySavedRegion = data.region;
		residencyDefault = data.default_region;
		residencyRegions = data.supported_regions ?? [];
		residencyPlacement = data.placement ?? {};
		residencyAlignment = data.alignment ?? null;
	}

	async function loadResidency() {
		loadingResidency = true;
		residencyError = '';
		try {
			applyResidency(await api.get<ResidencyResponse>('/api/organization/data-residency'));
		} catch (err) {
			residencyError = err instanceof Error ? err.message : m('org.residency.toast.loadFailed');
		} finally {
			loadingResidency = false;
		}
	}

	// The PUT answers with the same payload as the GET, alignment included, so
	// the verdict for the region just pinned lands without a second round trip.
	async function saveResidency() {
		savingResidency = true;
		try {
			applyResidency(
				await api.put<ResidencyResponse>('/api/organization/data-residency', {
					region: residencyRegion
				})
			);
			toast(m('org.residency.toast.saved'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.residency.toast.saveFailed'), 'error');
			// Snap the control back to what is actually persisted, so the panel
			// never shows a region the tenant is not pinned to.
			residencyRegion = residencySavedRegion;
		} finally {
			savingResidency = false;
		}
	}

	const alignmentMessage = $derived.by(() => {
		const a = residencyAlignment;
		if (!a) return '';
		if (a.status === 'aligned') {
			return m('org.residency.alignment.aligned', { region: regionLabel(a.deployed_region) });
		}
		if (a.status === 'misaligned') {
			return m('org.residency.alignment.misaligned', {
				configured: regionLabel(residencySavedRegion),
				deployed: regionLabel(a.deployed_region)
			});
		}
		return a.reason === 'deployed_region_unrecognised'
			? m('org.residency.alignment.unknownUnrecognised')
			: m('org.residency.alignment.unknownUnset');
	});

	async function removeCustomDomain(host: string) {
		// Two-click arm/confirm so a stray click can't drop a live domain.
		if (confirmRemoveDomain !== host) {
			confirmRemoveDomain = host;
			return;
		}
		confirmRemoveDomain = null;
		const ok = await saveCustomDomains(customDomains.filter((d) => d !== host));
		if (ok) toast(m('org.customDomains.toast.removed'), 'success');
	}

	// ── Chat notifications (Slack / Teams) ──────────────────────────────
	// The incoming-webhook URL is the credential for both real providers, and
	// it is WRITE-ONLY end to end: no endpoint returns it, so there is no
	// `chatWebhookUrl` mirror of the persisted value here — only the status the
	// server reports (configured yes/no + the bare host it posts to) and the
	// draft the admin is currently typing. Don't add one.
	// See backend/docs/notifications.md § Rotating the webhook URL.
	let chat = $state<ChatNotificationStatus | null>(null);
	let chatEnabled = $state(false);
	let chatProvider = $state('mock');
	let chatEvents = $state<Record<string, boolean>>({});
	let loadingChat = $state(true);
	let chatError = $state('');
	let savingChat = $state(false);
	let newChatWebhook = $state('');
	let savingChatWebhook = $state(false);
	let confirmRemoveChatWebhook = $state(false);

	function applyChat(data: ChatNotificationStatus) {
		chat = data;
		chatEnabled = data.enabled;
		chatProvider = data.provider ?? 'mock';
		// A missing per-event key means "on" (the backend's opt-out default), so
		// materialize the full map here rather than letting an unchecked box
		// mean "unset".
		chatEvents = Object.fromEntries(
			(data.supported_events ?? []).map((e) => [e, data.events?.[e] ?? true])
		);
	}

	function chatProviderLabel(token: string): string {
		return CHAT_PROVIDER_LABELS[token] ?? token;
	}

	function chatEventLabel(token: string): string {
		return CHAT_EVENT_LABELS[token] ?? token;
	}

	// Chat on, a real provider selected, no webhook stored → the adapter fails
	// closed and silently posts nothing. Surface that rather than let the panel
	// read as configured.
	const chatWebhookMissing = $derived(
		!!chat && chat.enabled && chat.provider !== 'mock' && !chat.webhook_configured
	);

	$effect(() => {
		// Its own effect, the same shape as the email-intake one below and for the
		// same reason: `GET /api/organization/chat-notifications` is
		// `require_roles(ROLE_ADMIN)`, so asking as a clerk has exactly one
		// possible answer — a 403, which this panel then rendered as a
		// `role="alert"` reading "Your role does not permit this action.". That is
		// the anti-pattern the comment above says this design avoids, reached by
		// the one read that was not gated on the role it requires.
		//
		// The role branch still has to run for a non-admin who opens the panel:
		// `loadingChat` starts true, and without clearing it the panel would sit
		// on its loading hint instead of reaching the admin-only one below it.
		if (section !== 'chat' || !userLoaded) return;
		if (!auth.isAdmin) {
			loadingChat = false;
			return;
		}
		once('chat', loadChat);
	});

	async function loadChat() {
		loadingChat = true;
		chatError = '';
		try {
			applyChat(await getChatNotifications());
		} catch (err) {
			chatError = err instanceof Error ? err.message : m('org.chat.toast.loadFailed');
		} finally {
			loadingChat = false;
		}
	}

	async function saveChat() {
		savingChat = true;
		try {
			// The response is authoritative — in particular it re-reports
			// `webhook_configured`, which this save deliberately does not touch.
			applyChat(
				await updateChatNotifications({
					enabled: chatEnabled,
					provider: chatProvider,
					events: chatEvents
				})
			);
			toast(m('org.chat.toast.saved'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.chat.toast.saveFailed'), 'error');
		} finally {
			savingChat = false;
		}
	}

	async function saveChatWebhook() {
		const url = newChatWebhook.trim();
		if (!url) {
			toast(m('org.chat.webhook.toast.empty'), 'error');
			return;
		}
		savingChatWebhook = true;
		try {
			applyChat(await rotateChatWebhook(url));
			// Drop the credential from component state the moment it is stored —
			// it is never re-fetchable, so keeping it around buys nothing.
			newChatWebhook = '';
			toast(m('org.chat.webhook.toast.saved'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('org.chat.webhook.toast.saveFailed'), 'error');
		} finally {
			savingChatWebhook = false;
		}
	}

	async function removeChatWebhook() {
		// Two-click arm/confirm — revoking silently stops every approval post.
		if (!confirmRemoveChatWebhook) {
			confirmRemoveChatWebhook = true;
			return;
		}
		confirmRemoveChatWebhook = false;
		savingChatWebhook = true;
		try {
			applyChat(await revokeChatWebhook());
			toast(m('org.chat.webhook.toast.removed'), 'success');
		} catch (err) {
			toast(
				err instanceof Error ? err.message : m('org.chat.webhook.toast.removeFailed'),
				'error'
			);
		} finally {
			savingChatWebhook = false;
		}
	}

	// ── Email intake (per-tenant invoices+<token>@ address) ─────────────
	// The address is what vendors mail invoices to, and its `+<token>` part is
	// a BEARER SECRET — anyone holding it can drop a payable into this tenant's
	// queue, and the inbound webhook answers every outcome with the same opaque
	// ack precisely so the token can't be ground out from outside. That makes
	// rotation the only containment for a leak, which is why it ships here
	// rather than living as an endpoint with no caller.
	//
	// `GET` is admin-only, so a non-admin is never asked for it: the panel says
	// so instead of rendering a load failure the reader can't act on.
	let intakeAddress = $state<string | null>(null);
	let intakeEnabled = $state(false);
	// Operator config, not tenant data: false means NO org on this deployment
	// can have an intake address, whatever its token says.
	let intakeDomainConfigured = $state(true);
	let loadingIntake = $state(true);
	let intakeError = $state('');
	let rotatingIntake = $state(false);
	// Armed two-click on Rotate — the same arm/confirm the domain and chat
	// panels use, because the confirm IS the whole safety here: rotating is
	// irreversible and silently breaks every vendor still mailing the old
	// address.
	let confirmRotateIntake = $state(false);
	// The freshly-minted address, shown once through the shared SecretReveal so
	// the admin gets a copy affordance at the one moment it matters. Unlike an
	// API key this value IS re-readable from this page — the reveal's copy says
	// what actually happened (the old address is dead) rather than claiming the
	// value is unrecoverable.
	let rotatedIntakeAddress = $state<string | null>(null);
	let intakeCopied = $state(false);

	$effect(() => {
		// Deliberately its own effect rather than a line in the page's main
		// loader: this one depends on the auth store, and folding it in would
		// make every other panel re-fetch the moment `/me` resolves.
		// The non-admin branch clears `loadingIntake` for the same reason the
		// chat one does — otherwise the panel never leaves its loading hint.
		if (section !== 'email-intake' || !userLoaded) return;
		if (!auth.isAdmin) {
			loadingIntake = false;
			return;
		}
		once('email-intake', loadEmailIntake);
	});

	async function loadEmailIntake() {
		loadingIntake = true;
		intakeError = '';
		try {
			const data = await getEmailIntake();
			intakeAddress = data.address;
			intakeEnabled = data.enabled;
			// `?? true` is the documented degradation: an older backend that does
			// not send the field must leave the panel assuming the deployment is
			// available, not silently reporting a working one as switched off.
			intakeDomainConfigured = data.domain_configured ?? true;
		} catch (err) {
			intakeError = err instanceof Error ? err.message : m('org.emailIntake.toast.loadFailed');
		} finally {
			loadingIntake = false;
		}
	}

	/**
	 * Whether this deployment can serve an intake address at all.
	 *
	 * `GET` returns `address: null` for two different reasons — the platform has
	 * no intake domain (`FEOH_EMAIL_INTAKE_DOMAIN` unset, the committed default,
	 * so the whole channel is off) or this org simply has no token yet — and
	 * they call for opposite copy. `domain_configured` answers it on the first
	 * read; before that field existed the panel could only find out by minting a
	 * throwaway token, which is a write to answer a question.
	 *
	 * `enabled && address === null` is kept as a second, weaker signal: it stays
	 * true against an older backend that doesn't send the field (the client
	 * defaults it to `true`, i.e. assume-available), so the panel degrades to
	 * the previous behaviour rather than mis-reporting a working deployment.
	 */
	const intakeUnavailable = $derived(
		!intakeDomainConfigured || (intakeEnabled && intakeAddress === null)
	);
	const intakeUnprovisioned = $derived(
		intakeDomainConfigured && !intakeEnabled && intakeAddress === null
	);

	async function copyIntakeAddress() {
		if (!intakeAddress) return;
		try {
			await navigator.clipboard.writeText(intakeAddress);
			intakeCopied = true;
			toast(m('org.emailIntake.toast.copied'), 'success');
		} catch {
			// Clipboard access can be denied (permissions, insecure context).
			// The address stays selectable in the DOM, so say so rather than
			// failing silently.
			toast(m('org.emailIntake.toast.copyFailed'), 'error');
		}
	}

	/** First-time provisioning. Invalidates nothing — there is nothing to invalidate. */
	async function generateIntakeAddress() {
		await mintIntakeToken(m('org.emailIntake.toast.created'), false);
	}

	/** Rotation. Armed two-click: the first click only arms + explains. */
	async function rotateIntakeAddress() {
		if (!confirmRotateIntake) {
			confirmRotateIntake = true;
			return;
		}
		confirmRotateIntake = false;
		await mintIntakeToken(m('org.emailIntake.toast.rotated'), true);
	}

	async function mintIntakeToken(successMessage: string, reveal: boolean) {
		rotatingIntake = true;
		intakeError = '';
		try {
			const data = await rotateEmailIntakeToken();
			intakeAddress = data.address;
			// The token is now provisioned and enabled whatever the address came
			// back as. Recording that is what turns a null address from "unknown"
			// into the proven "this deployment has no intake domain" state.
			intakeEnabled = true;
			intakeCopied = false;
			if (data.address === null) {
				// The write succeeded; there is still no address to hand out.
				// Fall through to the unavailable panel rather than toasting a
				// success that produced nothing usable.
				return;
			}
			if (reveal) rotatedIntakeAddress = data.address;
			toast(successMessage, 'success');
		} catch (err) {
			toast(
				err instanceof Error ? err.message : m('org.emailIntake.toast.rotateFailed'),
				'error'
			);
		} finally {
			rotatingIntake = false;
		}
	}
</script>

<svelte:window
	onclick={(e) => {
		// Un-arm a pending domain-remove confirm when clicking elsewhere.
		if (confirmRemoveDomain && !(e.target as HTMLElement)?.closest?.('.domain-remove')) {
			confirmRemoveDomain = null;
		}
		// Same for the chat-webhook revoke.
		if (
			confirmRemoveChatWebhook &&
			!(e.target as HTMLElement)?.closest?.('.chat-webhook-remove')
		) {
			confirmRemoveChatWebhook = false;
		}
		// Same for the email-intake rotate — an armed rotate left behind is a
		// loaded gun, and this one breaks every vendor's mail path.
		if (confirmRotateIntake && !(e.target as HTMLElement)?.closest?.('.intake-rotate')) {
			confirmRotateIntake = false;
		}
	}}
/>

<PageHeader title={m('org.title')}>
	{#if org}
		{#if readOnly}
			<p class="readonly-banner" data-testid="org-readonly-banner">
				{m('org.readOnly.banner')}
			</p>
		{/if}
		<div class="settings-layout">
			<SettingsRail groups={railGroups} active={section} label={m('org.rail.label')} />

			<!-- A single disabled <fieldset> is the whole read-only mode: the
			     attribute natively disables every descendant control, so a panel
			     added later is covered without anyone remembering to gate it.
			     Doing this per-input across ~10 panels is exactly how a gap gets
			     left behind. The server still enforces admin on every write. -->
			<fieldset class="sections" disabled={readOnly}>
				{#if section === 'getting-started'}
					<section class="getting-started card">
						<h2>{m('org.gettingStarted.title')}</h2>
						<p class="card-hint">{m('org.gettingStarted.intro')}</p>
						<!-- These were `#org-*` anchors into one long scroll. They now name
						     the panel that owns each step directly; the old anchors still
						     resolve, via `LEGACY_HASH_SECTIONS`, for the copies of them
						     that exist outside this repo. -->
						<nav class="gs-links" aria-label={m('org.gettingStarted.title')}>
							<a href="?section=company">{m('org.gettingStarted.company')}</a>
							<a href="?section=defaults">{m('org.gettingStarted.defaults')}</a>
							<a href="/admin">{m('org.gettingStarted.users')}</a>
							<a href="?section=payments">{m('org.gettingStarted.approvals')}</a>
							<a href="?section=branding">{m('org.gettingStarted.branding')}</a>
						</nav>
					</section>
				{/if}

				{#if section === 'company'}
					<section class="card" id="org-company">
						<h2>{m('org.section.company')}</h2>
						<div class="form-grid">
							<label>
								<span>{m('org.company.name')}</span>
								<input type="text" bind:value={name} />
							</label>
							<label>
								<span>{m('org.company.taxId')}</span>
								<input type="text" bind:value={taxId} placeholder={m('org.company.taxIdPlaceholder')} />
							</label>
							<label>
								<span>{m('org.company.vatNumber')}</span>
								<input type="text" bind:value={vatNumber} />
							</label>
							<label>
								<span>{m('org.company.companiesHouseNumber')}</span>
								<input type="text" bind:value={companiesHouseNumber} />
							</label>
							<label class="full-width">
								<span>{m('org.company.address')}</span>
								<textarea bind:value={address} rows="2" placeholder={m('org.company.addressPlaceholder')}></textarea>
							</label>
							<label>
								<span>{m('org.company.phone')}</span>
								<input type="tel" bind:value={phone} />
							</label>
							<label>
								<span>{m('org.company.website')}</span>
								<input type="url" bind:value={website} placeholder="https://" />
							</label>
						</div>
						<div class="section-footer">
							<button class="btn-save-section" disabled={savingProfile} onclick={saveProfile}>
								{savingProfile ? m('org.common.saving') : m('org.company.save')}
							</button>
						</div>
					</section>
				{/if}

				{#if section === 'defaults'}
					<section class="card" id="org-defaults">
						<h2>{m('org.section.defaults')}</h2>
						<p class="card-hint">{m('org.defaults.hint')}</p>
						<div class="form-grid">
							<label>
								<span>{m('org.defaults.currency')}</span>
								<select bind:value={currency}>
									<option value="USD">USD — US Dollar</option>
									<option value="EUR">EUR — Euro</option>
									<option value="GBP">GBP — British Pound</option>
									<option value="CAD">CAD — Canadian Dollar</option>
									<option value="AUD">AUD — Australian Dollar</option>
									<option value="JPY">JPY — Japanese Yen</option>
								</select>
							</label>
							<label>
								<span>{m('org.defaults.paymentTerms')}</span>
								<select bind:value={paymentTerms}>
									<option value="Due on Receipt">Due on Receipt</option>
									<option value="Net 10">Net 10</option>
									<option value="Net 15">Net 15</option>
									<option value="Net 30">Net 30</option>
									<option value="Net 45">Net 45</option>
									<option value="Net 60">Net 60</option>
									<option value="Net 90">Net 90</option>
									<option value="2/10 Net 30">2/10 Net 30</option>
								</select>
							</label>
							<label>
								<span>{m('org.defaults.numberPrefix')}</span>
								<input type="text" bind:value={numberPrefix} placeholder="INV-" />
							</label>
							<label>
								<span>{m('org.defaults.defaultGl')}</span>
								<input type="text" bind:value={defaultGl} placeholder={m('org.defaults.defaultGlPlaceholder')} />
							</label>
							<label>
								<span>{m('org.defaults.defaultCostCenter')}</span>
								<input type="text" bind:value={defaultCostCenter} placeholder={m('org.defaults.defaultCostCenterPlaceholder')} />
							</label>
						</div>
						<div class="section-footer">
							<button class="btn-save-section" disabled={savingDefaults} onclick={saveDefaults}>
								{savingDefaults ? m('org.common.saving') : m('org.defaults.save')}
							</button>
						</div>
					</section>
				{/if}

				{#if section === 'branding'}
					<section class="card" id="org-branding">
						<h2>{m('org.section.branding')}</h2>
						<p class="card-hint">
							{m('org.branding.hint')}
						</p>
						<div class="form-grid">
							<label>
								<span>{m('org.branding.productName')}</span>
								<input
									type="text"
									bind:value={brandProductName}
									placeholder={m('org.branding.productNamePlaceholder')}
									maxlength="120"
								/>
							</label>
							<label>
								<span>{m('org.branding.logoUrl')}</span>
								<input
									type="url"
									bind:value={brandLogoUrl}
									placeholder={m('org.branding.logoUrlPlaceholder')}
								/>
							</label>
							<label>
								<span>{m('org.branding.accentColor')}</span>
								<span class="color-field">
									<input
										type="color"
										aria-label={m('org.branding.accentColorPicker')}
										value={brandAccentColor.trim() || DEFAULT_ACCENT}
										oninput={(e) => (brandAccentColor = e.currentTarget.value)}
									/>
									<input
										type="text"
										bind:value={brandAccentColor}
										placeholder={DEFAULT_ACCENT}
									/>
								</span>
							</label>
							<label>
								<span>{m('org.branding.accentStrong')}</span>
								<span class="color-field">
									<input
										type="color"
										aria-label={m('org.branding.accentStrongPicker')}
										value={brandAccentStrongColor.trim() || DEFAULT_ACCENT_STRONG}
										oninput={(e) => (brandAccentStrongColor = e.currentTarget.value)}
									/>
									<input
										type="text"
										bind:value={brandAccentStrongColor}
										placeholder={DEFAULT_ACCENT_STRONG}
									/>
								</span>
								<FieldWarning
									show={accentStrongFails}
									testId="accent-strong-contrast-warning"
									message={m('common.contrastWarning', {
										ratio: accentStrongRatio === null ? '' : formatRatio(accentStrongRatio)
									})}
								/>
							</label>
							<label>
								<span>{m('org.branding.supportUrl')}</span>
								<input
									type="url"
									bind:value={brandSupportUrl}
									placeholder={m('org.branding.supportUrlPlaceholder')}
								/>
							</label>
							<label>
								<span>{m('org.branding.legalUrl')}</span>
								<input
									type="url"
									bind:value={brandLegalUrl}
									placeholder={m('org.branding.legalUrlPlaceholder')}
								/>
							</label>
							<div class="full-width">
								<label>
									<span>{m('org.branding.tenantUrl')}</span>
									<input
										type="text"
										bind:value={brandTenantUrlTemplate}
										placeholder={defaultTenantUrl || m('org.branding.tenantUrlPlaceholder')}
										autocomplete="off"
										spellcheck="false"
										aria-describedby="brand-tenant-url-hint"
									/>
								</label>
								<p class="field-hint" id="brand-tenant-url-hint">
									{m('org.branding.tenantUrlHint')}
									<span class="tenant-url-effective" data-testid="tenant-url-effective">
										{#if brandTenantUrlTemplate.trim()}
											{m('org.branding.tenantUrlEffective', { url: effectiveTenantUrl })}
										{:else if defaultTenantUrl}
											{m('org.branding.tenantUrlDefault', { url: defaultTenantUrl })}
										{:else}
											{m('org.branding.tenantUrlDefaultUnknown')}
										{/if}
									</span>
								</p>
							</div>
							<div class="full-width">
								<label>
									<span>{m('org.branding.ssoCallback')}</span>
									<input
										type="text"
										bind:value={brandSsoCallbackBaseUrl}
										placeholder={m('org.branding.ssoCallbackPlaceholder')}
										autocomplete="off"
										spellcheck="false"
										aria-describedby="brand-sso-callback-hint"
									/>
								</label>
								<p class="field-hint sso-callback-hint" id="brand-sso-callback-hint">
									<strong>{m('org.branding.ssoCallbackWarning')}</strong>
									{m('org.branding.ssoCallbackHint')}
								</p>
							</div>
						</div>
						<p class="card-hint">
							{m('org.branding.strongHint')}
						</p>
						<div class="section-footer">
							<button class="btn-save-section" disabled={savingBranding} onclick={saveBranding}>
								{savingBranding ? m('org.common.saving') : m('org.branding.save')}
							</button>
						</div>
					</section>
				{/if}

				{#if section === 'custom-domains'}
					<section class="card">
						<h2>{m('org.section.customDomains')}</h2>
						<p class="card-hint">
							{m('org.customDomains.hint', { example: 'ap.acmecorp.com', slug: org?.slug ?? 'tenant' })}
						</p>
						<p class="card-hint">
							{m('org.customDomains.setupHint', {
								runbook: 'docs/founder-runbooks/custom-domain-provisioning.md'
							})}
						</p>

						{#if loadingDomains}
							<p class="card-hint">{m('org.customDomains.loading')}</p>
						{:else if domainsError}
							<p class="domain-error" role="alert">{domainsError}</p>
						{:else}
							{#if domainSaveError}
								<p class="domain-error" role="alert" data-testid="custom-domain-error">
									{domainSaveError}
								</p>
							{/if}
							{#if customDomains.length === 0}
								<p class="card-hint domain-empty">{m('org.customDomains.empty')}</p>
							{:else}
								<ul class="domain-list">
									{#each customDomains as host (host)}
										<li class="domain-row">
											<span class="domain-name mono">{host}</span>
											<span class="domain-remove">
												<button
													type="button"
													class="btn-remove-domain"
													class:armed={confirmRemoveDomain === host}
													disabled={savingDomains}
													aria-label={m('org.customDomains.removeAria', { host })}
													onclick={() => removeCustomDomain(host)}
												>
													{confirmRemoveDomain === host ? m('org.customDomains.confirmRemove') : m('org.customDomains.remove')}
												</button>
											</span>
										</li>
									{/each}
								</ul>
							{/if}

							<form
								class="domain-add"
								onsubmit={(e) => {
									e.preventDefault();
									addCustomDomain();
								}}
							>
								<input
									type="text"
									bind:value={newDomain}
									placeholder={m('org.customDomains.newPlaceholder')}
									aria-label={m('org.customDomains.newAria')}
									autocomplete="off"
									spellcheck="false"
								/>
								<button type="submit" class="btn-save-section" disabled={savingDomains}>
									{savingDomains ? m('org.customDomains.adding') : m('org.customDomains.add')}
								</button>
							</form>
						{/if}
					</section>
				{/if}

				{#if section === 'chat'}
					<section class="card">
						<h2>{m('org.section.chat')}</h2>
						<p class="card-hint">{m('org.chat.hint')}</p>

						{#if !userLoaded || loadingChat}
							<p class="card-hint">{m('org.chat.loading')}</p>
						{:else if readOnly}
							<!-- GET is admin-only, so nothing is fetched for a non-admin and
							     there is nothing to show. Say that, exactly as the Email
							     Intake panel does, instead of rendering a 403 the reader
							     cannot act on. -->
							<p class="card-hint" data-testid="chat-admin-only">
								{m('org.chat.adminOnly')}
							</p>
						{:else if chatError}
							<p class="chat-error" role="alert">{chatError}</p>
						{:else if chat}
							<div class="form-grid">
								<label class="switch-row">
									<input type="checkbox" bind:checked={chatEnabled} />
									<span>{m('org.chat.enabled')}</span>
								</label>
								<label>
									{m('org.chat.provider')}
									<select bind:value={chatProvider}>
										{#each chat.supported_providers as p (p)}
											<option value={p}>{chatProviderLabel(p)}</option>
										{/each}
									</select>
								</label>
							</div>

							<fieldset class="chat-events">
								<legend>{m('org.chat.events')}</legend>
								<p class="card-hint">{m('org.chat.eventsHint')}</p>
								{#each chat.supported_events as ev (ev)}
									<label class="switch-row">
										<input
											type="checkbox"
											checked={chatEvents[ev] ?? true}
											onchange={(e) =>
												(chatEvents = {
													...chatEvents,
													[ev]: (e.currentTarget as HTMLInputElement).checked
												})}
										/>
										<span>{chatEventLabel(ev)}</span>
									</label>
								{/each}
							</fieldset>

							<div class="section-footer">
								<button class="btn-save-section" disabled={savingChat} onclick={saveChat}>
									{savingChat ? m('org.common.saving') : m('org.chat.save')}
								</button>
							</div>

							<h3 class="chat-subhead">{m('org.chat.webhook.title')}</h3>
							<p class="card-hint">{m('org.chat.webhook.hint')}</p>

							{#if chatWebhookMissing}
								<p class="chat-warning" role="alert">
									{m('org.chat.webhook.missingWarning', {
										provider: chatProviderLabel(chat.provider ?? '')
									})}
								</p>
							{/if}

							<div class="chat-webhook-status">
								{#if chat.webhook_configured}
									<span class="chat-webhook-set">
										{chat.webhook_host
											? m('org.chat.webhook.configured', { host: chat.webhook_host })
											: m('org.chat.webhook.configuredUnknownHost')}
									</span>
									<span class="chat-webhook-remove">
										<button
											type="button"
											class="btn-remove-domain"
											class:armed={confirmRemoveChatWebhook}
											disabled={savingChatWebhook}
											aria-label={m('org.chat.webhook.removeAria')}
											onclick={removeChatWebhook}
										>
											{confirmRemoveChatWebhook
												? m('org.chat.webhook.confirmRemove')
												: m('org.chat.webhook.remove')}
										</button>
									</span>
								{:else}
									<span class="card-hint">{m('org.chat.webhook.notConfigured')}</span>
								{/if}
							</div>

							<form
								class="domain-add"
								onsubmit={(e) => {
									e.preventDefault();
									saveChatWebhook();
								}}
							>
								<input
									type="text"
									bind:value={newChatWebhook}
									placeholder={m('org.chat.webhook.placeholder')}
									aria-label={m('org.chat.webhook.inputAria')}
									autocomplete="off"
									spellcheck="false"
								/>
								<button type="submit" class="btn-save-section" disabled={savingChatWebhook}>
									{savingChatWebhook
										? m('org.chat.webhook.saving')
										: chat.webhook_configured
											? m('org.chat.webhook.replace')
											: m('org.chat.webhook.set')}
								</button>
							</form>
							<p class="card-hint">{m('org.chat.webhook.rotateHint')}</p>
						{/if}
					</section>
				{/if}

				{#if section === 'email-intake'}
					<section class="card" id="org-email-intake">
						<h2>{m('org.section.emailIntake')}</h2>
						<p class="card-hint">{m('org.emailIntake.hint')}</p>

						{#if !userLoaded || loadingIntake}
							<p class="card-hint">{m('org.emailIntake.loading')}</p>
						{:else if readOnly}
							<!-- GET is admin-only. Say that, rather than firing a request
							     that 403s and rendering a load failure nobody can act on. -->
							<p class="card-hint" data-testid="email-intake-admin-only">
								{m('org.emailIntake.adminOnly')}
							</p>
						{:else if intakeError}
							<p class="domain-error" role="alert" data-testid="email-intake-error">
								{intakeError}
							</p>
						{:else if intakeUnavailable}
							<!-- Proven: a token exists and the server still renders no
							     address, which only happens when the platform has no
							     intake domain. No rotate control here — it would mint
							     another token that still addresses nothing. -->
							<p class="intake-unavailable" data-testid="email-intake-unavailable">
								{m('org.emailIntake.unavailable')}
							</p>
						{:else if intakeUnprovisioned}
							<p class="card-hint" data-testid="email-intake-unprovisioned">
								{m('org.emailIntake.notProvisioned')}
							</p>
							<button
								type="button"
								class="btn-save-section"
								disabled={rotatingIntake}
								onclick={generateIntakeAddress}
							>
								{rotatingIntake
									? m('org.emailIntake.generating')
									: m('org.emailIntake.generate')}
							</button>
						{:else}
							<p class="intake-address-label">{m('org.emailIntake.addressLabel')}</p>
							<div class="intake-address-row">
								<code class="intake-address mono" data-testid="email-intake-address"
									>{intakeAddress}</code
								>
								<button
									type="button"
									class="btn-save-section"
									aria-label={m('org.emailIntake.copyAria')}
									onclick={copyIntakeAddress}
								>
									{intakeCopied ? m('org.emailIntake.copied') : m('org.emailIntake.copy')}
								</button>
							</div>
							<p class="card-hint">{m('org.emailIntake.secretHint')}</p>

							{#if confirmRotateIntake}
								<p class="intake-rotate-warning" role="alert" data-testid="email-intake-rotate-warning">
									{m('org.emailIntake.rotateWarning')}
								</p>
							{/if}
							<span class="intake-rotate">
								<button
									type="button"
									class="btn-remove-domain"
									class:armed={confirmRotateIntake}
									disabled={rotatingIntake}
									aria-label={m('org.emailIntake.rotateAria')}
									onclick={rotateIntakeAddress}
								>
									{rotatingIntake
										? m('org.emailIntake.rotating')
										: confirmRotateIntake
											? m('org.emailIntake.rotateConfirm')
											: m('org.emailIntake.rotate')}
								</button>
							</span>
						{/if}
					</section>
				{/if}

				{#if section === 'residency'}
					<section class="card">
						<h2>{m('org.section.dataResidency')}</h2>
						<p class="card-hint">{m('org.residency.hint')}</p>

						{#if loadingResidency}
							<p class="card-hint">{m('org.residency.loading')}</p>
						{:else if residencyError}
							<p class="residency-error" role="alert">{residencyError}</p>
						{:else}
							<div class="form-grid">
								<label>
									<span>{m('org.residency.regionLabel')}</span>
									<select bind:value={residencyRegion}>
										{#each residencyRegions as token (token)}
											<option value={token}>
												{token === residencyDefault
													? m('org.residency.regionDefault', { region: regionLabel(token) })
													: regionLabel(token)}
											</option>
										{/each}
									</select>
								</label>
							</div>

							{#if residencyPlacement.db_cluster}
								<p class="card-hint residency-placement">
									{m('org.residency.placement', {
										cluster: residencyPlacement.db_cluster,
										bucket: residencyPlacement.s3_bucket ?? ''
									})}
								</p>
							{/if}

							{#if residencyAlignment}
								<div
									class="residency-alignment"
									class:ok={residencyAlignment.status === 'aligned'}
									class:warn={residencyAlignment.status === 'misaligned'}
								>
									<strong>{m('org.residency.alignment.title')}</strong>
									<p>{alignmentMessage}</p>
									<p class="residency-advisory">{m('org.residency.alignment.advisory')}</p>
								</div>
							{/if}

							<div class="section-footer">
								<button
									class="btn-save-section"
									disabled={savingResidency || residencyRegion === residencySavedRegion}
									onclick={saveResidency}
								>
									{savingResidency ? m('org.common.saving') : m('org.residency.save')}
								</button>
							</div>
						{/if}
					</section>
				{/if}

				{#if section === 'extraction'}
					<!-- The six panels from here to Fraud Detection have NO non-admin data
					     to show. `services/org_settings_view.py::NON_ADMIN_SETTINGS` is an
					     allow-list and admits none of `extraction`, `cards`, `mfa`,
					     `fraud_rules`, the ERP credentials or the payments credentials —
					     deliberately: most of them ARE third-party credentials (an ERP client
					     secret, a processor credential set, a card API key) and the rest have
					     no non-admin consumer. So for a non-admin those blocks arrive ABSENT
					     and every field falls back to its initializer, which is a platform
					     default wearing the tenant's clothes: Extraction reads "Claude Vision
					     / Platform" whatever the tenant bought, Payments reads "Mock",
					     Security reads MFA-not-required, and Fraud Detection disappeared
					     entirely. A disabled <fieldset> around a wrong value is still a wrong
					     value.

					     Each therefore replaces its BODY with the same admin-only hint the
					     Email Intake panel already uses, keeping its heading and its
					     description. Hiding the sections outright was rejected: the heading
					     is true (the setting exists, and knowing who to ask is the useful
					     part), Getting Started links to the Payments panel, and the page would
					     otherwise carry two vocabularies for one fact — a hint here, silence
					     there. Widening the projection to fill the fields was rejected
					     outright: that is the credential leak `org_settings_view` exists to
					     close.

					     ONE exception inside these six, recorded so nobody reads the hint as
					     a stronger claim than it is: the allow-list does admit
					     `erp.integration_method`, so the ERP panel's routing-mode select
					     alone could honestly be shown. It is not, because the panel is the
					     unit and the field beside it (ERP system) plus every credential below
					     are absent — one live select among fourteen missing ones would be a
					     THIRD treatment for the same fact, and this page has just finished
					     getting down to one. That key's declared non-admin consumer is the
					     workflow builder's ERP hint, which still reads it.
					     decisions §153. -->
					<section class="card">
						<h2>{m('org.section.extraction')}</h2>
						<p class="card-hint">{m('org.extraction.hint')}</p>
						{#if readOnly}
							<p class="card-hint" data-testid="extraction-admin-only">
								{m('org.readOnly.sectionAdminOnly')}
							</p>
						{:else}
							<div class="form-grid">
								<label>
									<span>{m('org.extraction.program')}</span>
									<select bind:value={extractionProgramType}>
										<option value="platform">{m('org.extraction.programPlatform')}</option>
										<option value="byok">{m('org.extraction.programByok')}</option>
									</select>
								</label>
								{#if extractionProgramType === 'byok'}
									<label>
										<span>{m('org.extraction.provider')}</span>
										<select bind:value={extractionProvider}>
											{#each EXTRACTION_PROVIDERS as p}
												<option value={p.value}>{p.label}</option>
											{/each}
										</select>
									</label>
								{:else}
									<label>
										<span>{m('org.extraction.provider')}</span>
										<input type="text" value="Claude Vision (Anthropic)" disabled />
									</label>
								{/if}
							</div>

							{#if extractionProgramType === 'platform'}
								<p class="card-hint" style="margin-top: 10px;">{m('org.extraction.platformHint')}</p>
							{:else if extractionProvider === 'claude_vision' || extractionProvider === 'openai_vision'}
								<div class="form-grid" style="margin-top: 14px;">
									<label>
										<span>{extractionProvider === 'claude_vision' ? m('org.extraction.anthropicKey') : m('org.extraction.openaiKey')}</span>
										<input type="password" bind:value={extractionApiKey} placeholder="sk-..." />
									</label>
								</div>
							{:else if extractionProvider === 'aws_textract'}
								<div class="form-grid" style="margin-top: 14px;">
									<label>
										<span>{m('org.extraction.awsKeyId')}</span>
										<input type="text" bind:value={extractionAwsKeyId} />
									</label>
									<label>
										<span>{m('org.extraction.awsSecret')}</span>
										<input type="password" bind:value={extractionAwsSecret} />
									</label>
									<label>
										<span>{m('org.extraction.awsRegion')}</span>
										<input type="text" bind:value={extractionAwsRegion} placeholder="us-east-1" />
									</label>
								</div>
							{:else if extractionProvider === 'ollama'}
								<div class="form-grid" style="margin-top: 14px;">
									<label>
										<span>{m('org.extraction.ollamaUrl')}</span>
										<input type="url" bind:value={extractionOllamaUrl} placeholder="http://localhost:11434" />
									</label>
									<label>
										<span>{m('org.extraction.model')}</span>
										<select bind:value={extractionOllamaModel}>
											<option value="llama3.2-vision:11b">Llama 3.2 Vision 11B</option>
											<option value="llama3.2-vision:90b">Llama 3.2 Vision 90B</option>
											<option value="llava:13b">LLaVA 13B</option>
											<option value="llava:34b">LLaVA 34B</option>
										</select>
									</label>
								</div>
								<p class="card-hint" style="margin-top: 8px;">{m('org.extraction.ollamaHint')} <code>brew install ollama && ollama pull {extractionOllamaModel}</code></p>
							{/if}

							<div class="erp-test-row">
								<button class="btn-save-section" disabled={savingExtraction} onclick={saveExtraction}>
									{savingExtraction ? m('org.common.saving') : m('org.extraction.save')}
								</button>
								<button class="btn-test" disabled={testingExtraction} onclick={testExtraction}>
									{testingExtraction ? m('org.common.testing') : m('org.common.testConnection')}
								</button>
								{#if extractionTestResult}
									<span class="test-result" class:success={extractionTestResult.success} class:failure={!extractionTestResult.success}>
										{extractionTestResult.message}
									</span>
								{/if}
							</div>
						{/if}
					</section>
				{/if}

				{#if section === 'erp'}
					<section class="card">
						<h2>{m('org.section.erp')}</h2>
						<p class="card-hint">{m('org.erp.hint')}</p>
						{#if readOnly}
							<p class="card-hint" data-testid="erp-admin-only">
								{m('org.readOnly.sectionAdminOnly')}
							</p>
						{:else}
							<div class="form-grid">
								<label>
									<span>{m('org.erp.system')}</span>
									<select bind:value={erpType}>
										{#each ERP_TYPES as erp}
											<option value={erp.value}>{erp.label}</option>
										{/each}
									</select>
								</label>
								<label>
									<span>{m('org.erp.method')}</span>
									<select bind:value={erpMethod}>
										<option value="merge_dev">{m('org.erp.methodMergeDev')}</option>
										<option value="direct">{m('org.erp.methodDirect')}</option>
									</select>
								</label>
							</div>

							{#if erpMethod === 'merge_dev'}
								<div class="form-grid" style="margin-top: 14px;">
									<label>
										<span>{m('org.erp.mergeApiKey')}</span>
										<input type="password" bind:value={erpApiKey} placeholder="test_..." />
									</label>
									<label>
										<span>{m('org.erp.accountToken')}</span>
										<input type="password" bind:value={erpAccountToken} placeholder={m('org.erp.accountTokenPlaceholder')} />
									</label>
								</div>
								<p class="card-hint" style="margin-top: 8px;">{m('org.erp.mergeHintPre')} <a href="https://app.merge.dev" target="_blank" rel="noopener">{m('org.erp.mergeDashboard')}</a>{m('org.erp.mergeHintPost')}</p>
							{:else if erpType === 'dynamics_365_bc'}
								<div class="form-grid" style="margin-top: 14px;">
									<label>
										<span>{m('org.erp.baseUrl')}</span>
										<input type="url" bind:value={erpBaseUrl} placeholder="https://api.businesscentral.dynamics.com/v2.0" />
									</label>
									<label>
										<span>{m('org.erp.environment')}</span>
										<input type="text" bind:value={erpEnvironment} placeholder="production" />
									</label>
									<label>
										<span>{m('org.erp.tenantId')}</span>
										<input type="text" bind:value={erpTenantId} />
									</label>
									<label>
										<span>{m('org.erp.clientId')}</span>
										<input type="text" bind:value={erpClientId} />
									</label>
									<label>
										<span>{m('org.erp.clientSecret')}</span>
										<input type="password" bind:value={erpClientSecret} />
									</label>
									<label>
										<span>{m('org.erp.companyId')}</span>
										<input type="text" bind:value={erpCompanyId} />
									</label>
								</div>
							{:else if erpType === 'netsuite'}
								<div class="form-grid" style="margin-top: 14px;">
									<label>
										<span>{m('org.erp.accountId')}</span>
										<input type="text" bind:value={erpAccountId} placeholder="1234567" />
									</label>
									<label>
										<span>{m('org.erp.consumerKey')}</span>
										<input type="text" bind:value={erpConsumerKey} />
									</label>
									<label>
										<span>{m('org.erp.consumerSecret')}</span>
										<input type="password" bind:value={erpConsumerSecret} />
									</label>
									<label>
										<span>{m('org.erp.tokenId')}</span>
										<input type="text" bind:value={erpTokenId} />
									</label>
									<label>
										<span>{m('org.erp.tokenSecret')}</span>
										<input type="password" bind:value={erpTokenSecret} />
									</label>
								</div>
							{:else}
								<div class="form-grid" style="margin-top: 14px;">
									<label>
										<span>{m('org.erp.apiBaseUrl')}</span>
										<input type="url" bind:value={erpBaseUrl} />
									</label>
									<label>
										<span>{m('org.erp.apiKeyClientId')}</span>
										<input type="password" bind:value={erpClientId} />
									</label>
									<label>
										<span>{m('org.erp.apiSecretClientSecret')}</span>
										<input type="password" bind:value={erpClientSecret} />
									</label>
								</div>
								<p class="card-hint" style="margin-top: 8px;">{m('org.erp.directSoonHint', { erp: ERP_TYPES.find(e => e.value === erpType)?.label ?? erpType })}</p>
							{/if}

							<div class="erp-test-row">
								<button class="btn-save-section" disabled={savingErp} onclick={saveErp}>
									{savingErp ? m('org.common.saving') : m('org.erp.save')}
								</button>
								<button class="btn-test" disabled={testingConnection} onclick={testConnection}>
									{testingConnection ? m('org.common.testing') : m('org.common.testConnection')}
								</button>
								{#if connectionResult}
									<span class="test-result" class:success={connectionResult.success} class:failure={!connectionResult.success}>
										{connectionResult.message}
									</span>
								{/if}
							</div>
						{/if}
					</section>
				{/if}

				{#if section === 'payments'}
					<section class="card" id="org-payments">
						<h2>{m('org.section.payments')}</h2>
						<p class="card-hint">
							{m('org.payments.hint')}
						</p>
						{#if readOnly}
							<p class="card-hint" data-testid="payments-admin-only">
								{m('org.readOnly.sectionAdminOnly')}
							</p>
						{:else}
							<div class="form-grid">
								<label>
									<span>{m('org.payments.provider')}</span>
									<select bind:value={paymentsProvider}>
										<option value="mock">{m('org.payments.providerMock')}</option>
										<option value="modern_treasury">Modern Treasury</option>
									</select>
								</label>
							</div>

							{#if paymentsProvider === 'modern_treasury'}
								<div class="form-grid">
									<label>
										<span>{m('org.payments.orgId')}</span>
										<input type="text" bind:value={paymentsOrgId} placeholder="org_..." />
									</label>
									<label>
										<span>{m('org.payments.apiKey')}</span>
										<input type="password" bind:value={paymentsApiKey} placeholder="••••••••" autocomplete="off" />
									</label>
									<label>
										<span>{m('org.payments.originatingAccount')}</span>
										<input type="text" bind:value={paymentsOriginatingAccount} placeholder={m('org.payments.originatingAccountPlaceholder')} />
									</label>
									<label>
										<span>{m('org.payments.webhookSecret')}</span>
										<input type="password" bind:value={paymentsWebhookSecret} placeholder={m('org.payments.webhookSecretPlaceholder')} autocomplete="off" />
									</label>
									<label class="switch-row">
										<input type="checkbox" bind:checked={paymentsSandbox} />
										<span>{m('org.payments.sandbox')}</span>
									</label>
								</div>
								<p class="card-hint">
									{m('org.payments.webhookHint')}
									<code>{org.created_at ? `${window.location.origin.replace(window.location.host, org.slug + '.' + window.location.host)}/api/payments/webhook/${org.slug}/modern_treasury` : '...'}</code>
								</p>
							{/if}

							<div class="form-grid">
								<label>
									<span>{m('org.payments.cfoThreshold', { currency: orgCurrency.label })}</span>
									<input
										type="number"
										min="0"
										step="100"
										placeholder={m('org.payments.cfoThresholdPlaceholder')}
										value={paymentsCfoThreshold ?? ''}
										oninput={(e) => {
											const v = (e.currentTarget as HTMLInputElement).value;
											paymentsCfoThreshold = v ? parseFloat(v) : null;
										}}
									/>
								</label>
							</div>
							<p class="card-hint">
								{m('org.payments.cfoHint')}
							</p>

							<div class="section-footer">
								<button class="btn-save-section" disabled={savingPayments} onclick={savePayments}>
									{savingPayments ? m('org.common.saving') : m('org.payments.save')}
								</button>
								<button class="btn-test" disabled={testingPayments || paymentsProvider === 'mock'} onclick={testPayments}>
									{testingPayments ? m('org.common.testing') : m('org.common.testConnection')}
								</button>
								{#if paymentsTestResult}
									<span class="test-result" class:success={paymentsTestResult.success} class:failure={!paymentsTestResult.success}>
										{paymentsTestResult.message}
									</span>
								{/if}
							</div>
						{/if}
					</section>
				{/if}

				{#if section === 'cards'}
					<section class="card">
						<h2>{m('org.section.cards')}</h2>
						<p class="card-hint">{m('org.cards.hint')}</p>
						{#if readOnly}
							<p class="card-hint" data-testid="cards-admin-only">
								{m('org.readOnly.sectionAdminOnly')}
							</p>
						{:else}
							<div class="form-grid">
								<label>
									<span>{m('org.cards.enabled')}</span>
									<select bind:value={cardsEnabled}>
										<option value={false}>{m('org.cards.disabled')}</option>
										<option value={true}>{m('org.cards.enabledOn')}</option>
									</select>
								</label>
								<label>
									<span>{m('org.cards.program')}</span>
									<select bind:value={cardsProgramType}>
										<option value="platform">{m('org.cards.programPlatform')}</option>
										<option value="byok">{m('org.cards.programByok')}</option>
									</select>
								</label>
								<label>
									<span>{m('org.cards.region')}</span>
									<select bind:value={cardsRegion}>
										{#each CARD_REGIONS as r}
											<option value={r.value}>{r.label}</option>
										{/each}
									</select>
								</label>
								<label>
									<span>{m('org.cards.expiryDays')}</span>
									<input type="number" min="1" max="90" bind:value={cardsExpiryDays} />
								</label>
							</div>

							{#if cardsEnabled && cardsProgramType === 'platform'}
								<p class="card-hint" style="margin-top: 10px;">{m('org.cards.platformHint', { provider: autoProvider === 'lithic' ? 'Lithic' : 'Nium' })}</p>
							{/if}

							{#if cardsEnabled && cardsProgramType === 'byok'}
								<div class="form-grid" style="margin-top: 14px;">
									<label>
										<span>{m('org.cards.provider')}</span>
										<select bind:value={cardsProvider}>
											<option value="">{m('org.cards.providerAuto', { provider: autoProvider === 'lithic' ? 'Lithic' : 'Nium' })}</option>
											<option value="lithic">{m('org.cards.providerLithic')}</option>
											<option value="nium">{m('org.cards.providerNium')}</option>
										</select>
									</label>
								</div>

								{#if effectiveProvider === 'lithic'}
									<div class="form-grid" style="margin-top: 14px;">
										<label>
											<span>{m('org.cards.lithicApiKey')}</span>
											<input type="password" bind:value={cardsApiKey} placeholder="api-key-..." />
										</label>
										<label>
											<span>{m('org.cards.sandboxMode')}</span>
											<select bind:value={cardsSandbox}>
												<option value={true}>{m('org.cards.sandboxTesting')}</option>
												<option value={false}>{m('org.cards.production')}</option>
											</select>
										</label>
									</div>
								{:else if effectiveProvider === 'nium'}
									<div class="form-grid" style="margin-top: 14px;">
										<label>
											<span>{m('org.cards.clientId')}</span>
											<input type="text" bind:value={cardsClientId} />
										</label>
										<label>
											<span>{m('org.cards.clientSecret')}</span>
											<input type="password" bind:value={cardsClientSecret} />
										</label>
										<label>
											<span>{m('org.cards.customerHashId')}</span>
											<input type="text" bind:value={cardsCustomerHashId} />
										</label>
										<label>
											<span>{m('org.cards.walletHashId')}</span>
											<input type="text" bind:value={cardsWalletHashId} />
										</label>
										<label>
											<span>{m('org.cards.sandboxMode')}</span>
											<select bind:value={cardsSandbox}>
												<option value={true}>{m('org.cards.sandboxTesting')}</option>
												<option value={false}>{m('org.cards.production')}</option>
											</select>
										</label>
									</div>
								{/if}
							{/if}

							<div class="section-footer">
								<button class="btn-save-section" disabled={savingCards} onclick={saveCards}>
									{savingCards ? m('org.common.saving') : m('org.cards.save')}
								</button>
							</div>
						{/if}
					</section>
				{/if}

				{#if section === 'security'}
					<section class="card">
						<h2>{m('org.section.security')}</h2>
						<p class="card-hint">
							{m('org.security.hint')}
						</p>
						{#if readOnly}
							<p class="card-hint" data-testid="security-admin-only">
								{m('org.readOnly.sectionAdminOnly')}
							</p>
						{:else}
							<label class="switch-row">
								<input type="checkbox" bind:checked={mfaRequired} />
								<span>{m('org.security.requireMfa')}</span>
							</label>

							{#if mfaRequired && !mfaEnforcementActive}
								<p class="mfa-enforcement-warning" role="alert" data-testid="mfa-enforcement-inactive">
									{m('org.security.mfaEnforcementInactive')}
								</p>
							{/if}

							<div class="section-footer">
								<button class="btn-save-section" disabled={savingSecurity} onclick={saveSecurity}>
									{savingSecurity ? m('org.common.saving') : m('org.security.save')}
								</button>
							</div>
						{/if}
					</section>
				{/if}

				{#if section === 'fraud'}
					{#if readOnly || fraud}
						<section class="card">
							<h2>{m('org.section.fraud')}</h2>
							<p class="card-hint">
								{m('org.fraud.hint')}
							</p>
							{#if readOnly}
								<p class="card-hint" data-testid="fraud-admin-only">
									{m('org.readOnly.sectionAdminOnly')}
								</p>
							{:else if fraud}
								<div class="fraud-grid">
									<label class="switch-row">
										<input type="checkbox" bind:checked={fraud.round_amount_enabled} />
										<span>
											<strong>{m('org.fraud.roundAmount')}</strong>
											<span class="rule-hint">
												{m('org.fraud.roundAmountHint', { min: fraud.round_amount_min })}
											</span>
										</span>
									</label>
									<div class="threshold-row">
										<label>
											<span>{m('org.fraud.minAmount')}</span>
											<input
												type="number"
												min="0"
												step="100"
												bind:value={fraud.round_amount_min}
												disabled={!fraud.round_amount_enabled}
											/>
										</label>
									</div>

									<label class="switch-row">
										<input type="checkbox" bind:checked={fraud.future_date_enabled} />
										<span>
											<strong>{m('org.fraud.futureDate')}</strong>
											<span class="rule-hint">
												{m('org.fraud.futureDateHint')}
											</span>
										</span>
									</label>

									<label class="switch-row">
										<input type="checkbox" bind:checked={fraud.rush_payment_enabled} />
										<span>
											<strong>{m('org.fraud.rushPayment')}</strong>
											<span class="rule-hint">
												{m('org.fraud.rushPaymentHint')}
											</span>
										</span>
									</label>
									<div class="threshold-row">
										<label>
											<span>{m('org.fraud.maxDays')}</span>
											<input
												type="number"
												min="0"
												max="30"
												bind:value={fraud.rush_payment_max_days}
												disabled={!fraud.rush_payment_enabled}
											/>
										</label>
									</div>

									<label class="switch-row">
										<input type="checkbox" bind:checked={fraud.new_vendor_large_enabled} />
										<span>
											<strong>{m('org.fraud.newVendorLarge')}</strong>
											<span class="rule-hint">
												{m('org.fraud.newVendorLargeHint', { amount: fraud.new_vendor_large_amount, days: fraud.new_vendor_max_age_days })}
											</span>
										</span>
									</label>
									<div class="threshold-row">
										<label>
											<span>{m('org.fraud.vendorAge')}</span>
											<input
												type="number"
												min="1"
												bind:value={fraud.new_vendor_max_age_days}
												disabled={!fraud.new_vendor_large_enabled}
											/>
										</label>
										<label>
											<span>{m('org.fraud.largeThreshold')}</span>
											<input
												type="number"
												min="0"
												step="500"
												bind:value={fraud.new_vendor_large_amount}
												disabled={!fraud.new_vendor_large_enabled}
											/>
										</label>
									</div>

									<label class="switch-row">
										<input type="checkbox" bind:checked={fraud.bank_change_enabled} />
										<span>
											<strong>{m('org.fraud.bankChange')}</strong>
											<span class="rule-hint">
												{m('org.fraud.bankChangeHint')}
											</span>
										</span>
									</label>

									<label class="switch-row">
										<input type="checkbox" bind:checked={fraud.personal_email_enabled} />
										<span>
											<strong>{m('org.fraud.personalEmail')}</strong>
											<span class="rule-hint">
												{m('org.fraud.personalEmailHint')}
											</span>
										</span>
									</label>
									<div class="threshold-row">
										<label class="full">
											<span>{m('org.fraud.personalEmailDomains')}</span>
											<textarea
												rows="4"
												bind:value={personalEmailDomainsText}
												disabled={!fraud.personal_email_enabled}
											></textarea>
										</label>
									</div>

									<label class="switch-row">
										<input type="checkbox" bind:checked={fraud.stat_anomaly_enabled} />
										<span>
											<strong>{m('org.fraud.statAnomaly')}</strong>
											<span class="rule-hint">
												{m('org.fraud.statAnomalyHint')}
											</span>
										</span>
									</label>
									<div class="threshold-row">
										<label>
											<span>{m('org.fraud.sigma')}</span>
											<input
												type="number"
												min="0.5"
												step="0.1"
												bind:value={fraud.stat_anomaly_sigma}
												disabled={!fraud.stat_anomaly_enabled}
											/>
										</label>
										<label>
											<span>{m('org.fraud.minPriorInvoices')}</span>
											<input
												type="number"
												min="2"
												bind:value={fraud.stat_anomaly_min_history}
												disabled={!fraud.stat_anomaly_enabled}
											/>
										</label>
									</div>

									<label class="switch-row">
										<input type="checkbox" bind:checked={fraud.llm_anomaly_enabled} />
										<span>
											<strong>{m('org.fraud.llmAnomaly')}</strong>
											<span class="rule-hint">
												{m('org.fraud.llmAnomalyHint')}
											</span>
										</span>
									</label>
								</div>

								<div class="section-footer">
									<button
										type="button"
										class="btn-link"
										onclick={resetFraudToDefaults}
										disabled={savingFraud}
									>
										{m('org.fraud.resetDefaults')}
									</button>
									<button
										class="btn-save-section"
										disabled={savingFraud}
										onclick={saveFraud}
									>
										{savingFraud ? m('org.common.saving') : m('org.fraud.save')}
									</button>
								</div>
							{/if}
						</section>
					{/if}
				{/if}

				{#if section === 'data-sync'}
					<!-- Data Sync is a SIGNPOST, not a second set of sync buttons.
					     Each of the three ERP-synced data sets now has its own page
					     carrying its own Sync-from-ERP action gated on `auth.isManager`
					     — `/gl-accounts` (new), `/purchase-orders`, `/vendors` — which
					     matches every one of those endpoints' real gate
					     (admin | ap_manager). The two buttons that used to live here
					     were admin-only by virtue of this route's nav gate, said nothing
					     about which entity's chart they would write into, and reported
					     into a bare <span> with no list to refresh; the vendors row had
					     already been a link for exactly that reason. Three links is one
					     vocabulary for one fact (decisions §153's objection to a panel
					     growing a second shape, and §161 for the call).
					     It keeps its place because this is where the ERP connection is
					     configured, so "now where do I pull it?" is asked here. -->
					<section class="card">
						<h2>{m('org.section.dataSync')}</h2>
						<p class="card-hint">{m('org.dataSync.hint')}</p>

						<div class="sync-grid">
							<div class="sync-item">
								<div class="sync-info">
									<span class="sync-name">{m('org.dataSync.coa')}</span>
									<span class="sync-desc">{m('org.dataSync.coaDesc')}</span>
								</div>
								<a href="/gl-accounts" class="btn-outline">{m('org.dataSync.manageCoa')}</a>
							</div>

							<div class="sync-item">
								<div class="sync-info">
									<span class="sync-name">{m('org.dataSync.pos')}</span>
									<span class="sync-desc">{m('org.dataSync.posDesc')}</span>
								</div>
								<a href="/purchase-orders" class="btn-outline">{m('org.dataSync.managePos')}</a>
							</div>

							<div class="sync-item">
								<div class="sync-info">
									<span class="sync-name">{m('org.dataSync.vendors')}</span>
									<span class="sync-desc">{m('org.dataSync.vendorsDesc')}</span>
								</div>
								<a href="/vendors" class="btn-outline">{m('org.dataSync.manageVendors')}</a>
							</div>
						</div>
					</section>
				{/if}

				{#if section === 'plan'}
					<section class="card plan-card">
						<h2>{m('org.section.plan')}</h2>
						<div class="plan-info">
							<Badge tone="accent" variant="plan-badge">{planLabel(org.plan)}</Badge>
							<span class="plan-slug">{m('org.plan.tenant')} <code>{org.slug}</code></span>
							<span class="plan-date">{m('org.plan.created', { date: formatDate(org.created_at, '—', { month: 'long', day: 'numeric', year: 'numeric' }) })}</span>
						</div>
					</section>
				{/if}
			</fieldset>
		</div>
	{:else}
		<div class="loading">{m('org.loading')}</div>
	{/if}
</PageHeader>

<!-- The freshly-rotated address, through the same one-time reveal an API-key
     mint and a webhook secret rotation use. The value is re-readable from the
     panel above, so the copy says what is actually irreversible — the OLD
     address is dead — instead of the "shown once" warning those two carry. -->
<SecretReveal
	open={rotatedIntakeAddress !== null}
	ariaLabel={m('org.emailIntake.rotated.aria')}
	heading={m('org.emailIntake.rotated.heading')}
	warningStrong={m('org.emailIntake.rotated.warningStrong')}
	warning={m('org.emailIntake.rotated.warning')}
	secret={rotatedIntakeAddress ?? ''}
	testId="email-intake-address-rotated"
	copyLabel={m('org.emailIntake.copy')}
	copiedLabel={m('org.emailIntake.copied')}
	copiedToast={m('org.emailIntake.toast.copied')}
	copyFailedToast={m('org.emailIntake.toast.copyFailed')}
	doneLabel={m('org.emailIntake.rotated.done')}
	onclose={() => (rotatedIntakeAddress = null)}
/>

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */
	/* A <fieldset> for the read-only mode's native `disabled` cascade — the UA
	   border/padding/margin have to be reset so it lays out exactly as the
	   <div> it replaced. `min-width: 0` stops the fieldset's default
	   min-content sizing forcing the page wider than its container. */
	/* Rail beside panel. One column on a phone, where the rail collapses to its
	   own disclosure (`ui/SettingsRail.svelte`) and sits above the panel.
	   `minmax(0, 1fr)` rather than `1fr` for the panel track: a grid item's
	   default `min-width: auto` is its CONTENT width, so the widest form in any
	   panel — the ERP credential grid — would push the track past the viewport
	   and scroll the document sideways (WCAG 1.4.10), which is the same trap
	   `.sections`' own `min-width: 0` is there for. */
	.settings-layout {
		display: grid;
		/* Explicit, and `minmax(0, …)` in the ONE-column case too, not just in
		   the two-column rule below. An implicit grid column is auto-sized and a
		   grid item's default `min-width: auto` is its CONTENT width, so the
		   widest panel pushed the document 43px past a 320px viewport — a real
		   WCAG 1.4.10 failure, caught by `tests-e2e/a11y/reflow.spec.ts`.
		   `/organization` only escaped it because its `.sections` fieldset
		   already carried `min-width: 0` for an unrelated reason, which is
		   exactly the kind of accident not to rely on. */
		grid-template-columns: minmax(0, 1fr);
		gap: 20px;
	}

	@media (min-width: 60rem) {
		.settings-layout {
			grid-template-columns: 200px minmax(0, 1fr);
			gap: 28px;
			/* Not `stretch`: the rail is sticky, and a stretched grid item is as
			   tall as the row, which leaves it nothing to stick within. */
			align-items: start;
		}
	}

	.sections {
		display: flex;
		flex-direction: column;
		gap: 16px;
		border: 0;
		padding: 0;
		margin: 0;
		min-width: 0;
	}

	/* Non-admins get the page read-only rather than a form whose every Save
	   403s. Informational, not an error — the muted tint, not danger. */
	.readonly-banner {
		margin: 0 0 16px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.85rem;
		background: var(--muted-tint);
		color: var(--muted-on-tint);
	}

	.intake-address-label {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin: 0 0 6px;
	}

	.intake-address-row {
		display: flex;
		align-items: stretch;
		gap: 8px;
		margin-bottom: 12px;
	}

	.intake-address {
		flex: 1;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 12px;
		font-size: 0.9rem;
		word-break: break-all;
		/* One click selects the whole address — it is copied by hand whenever
		   the clipboard API is unavailable. */
		user-select: all;
	}

	/* The armed rotate's explanation. Tinted-badge recipe: the -tint
	   background with its matching -on-tint text, never the base token. */
	.intake-rotate-warning {
		margin: 0 0 10px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	/* "This deployment has no intake domain" — a standing fact about the
	   install, not something the reader did wrong. */
	.intake-unavailable {
		margin: 0;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--muted-tint);
		color: var(--muted-on-tint);
	}

	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 20px 24px;
	}

	.card h2 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0 0 4px;
	}

	.card-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0 0 14px;
	}

	/* First-time-admin wayfinding — links jump to the sections a new tenant
	   configures first. Purely a shortcut; nothing is hidden. */
	.getting-started {
		border-color: var(--accent);
	}

	.gs-links {
		display: flex;
		flex-wrap: wrap;
		gap: 8px 18px;
		margin-top: 4px;
	}

	.gs-links a {
		font-size: 0.85rem;
		color: var(--accent);
		text-decoration: none;
	}

	.gs-links a:hover {
		text-decoration: underline;
	}

	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
		margin-top: 14px;
	}

	.full-width {
		grid-column: 1 / -1;
	}

	/* Per-field explanation sitting OUTSIDE the <label> (it is referenced by
	   aria-describedby instead), so it describes the control without being
	   swallowed into its accessible name. */
	.field-hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 6px 0 0;
		line-height: 1.5;
	}

	.tenant-url-effective {
		display: block;
		color: var(--text);
	}

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	/* The field label only — not the .color-field wrapper span (reset below). */
	label > span:first-child {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	/* Branding color picker: native swatch + hex text input side by side. */
	.color-field {
		display: flex;
		align-items: center;
		gap: 8px;
		text-transform: none;
		letter-spacing: normal;
	}

	.color-field input[type='color'] {
		width: 40px;
		height: 36px;
		padding: 2px;
		flex-shrink: 0;
		cursor: pointer;
	}

	.color-field input[type='text'] {
		flex: 1;
	}

	/* Custom domains */
	.domain-list {
		list-style: none;
		margin: 4px 0 16px;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.domain-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 8px 12px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
	}

	.domain-name {
		font-size: 0.9rem;
		word-break: break-all;
	}

	.residency-placement {
		font-family: var(--font-mono);
		margin: 12px 0 0;
	}

	/* Advisory verdict box. Tinted-badge recipe: each tone takes its own
	   -tint background with the matching -on-tint text (never the base token,
	   which lands under 4.5:1 once composited over the tint). Unknown — the
	   deliberately non-committal state — is the muted default. */
	.residency-alignment {
		margin-top: 14px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--muted-tint);
		color: var(--muted-on-tint);
	}

	.residency-alignment.ok {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	.residency-alignment.warn {
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	.residency-alignment strong {
		display: block;
		margin-bottom: 4px;
	}

	.residency-alignment p {
		margin: 0;
	}

	/* Inherits the box's calibrated colour — a muted token here would be
	   judged against the bare surface, not the tint it actually sits on. */
	.residency-advisory {
		margin-top: 6px;
		font-size: 0.78rem;
	}

	.btn-remove-domain {
		flex-shrink: 0;
		padding: 4px 12px;
		font-size: 0.82rem;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: transparent;
		color: var(--text);
		cursor: pointer;
	}

	.btn-remove-domain:hover:not(:disabled),
	.btn-remove-domain.armed {
		/* --danger-strong, not --danger: this armed state is a fill carrying
		   white text, and the old #e5484d fallback was 3.91:1. */
		border-color: var(--danger-strong);
		color: #fff;
		background: var(--danger-strong);
	}

	.btn-remove-domain:disabled {
		opacity: 0.6;
		cursor: default;
	}

	.domain-add {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.domain-add input {
		flex: 1;
	}

	/* Panel-level load failure (custom domains, data residency, chat) — a
	   persistent region rather than a toast, because it explains why the panel
	   is empty. */
	.domain-error,
	.residency-error,
	.chat-error {
		color: var(--danger);
		font-size: 0.88rem;
		margin: 4px 0 12px;
	}

	/* "Enabled for a real provider but no webhook stored" — the adapter fails
	   closed and posts nothing, so this is a live misconfiguration, not an
	   error the user just caused. Tinted-badge recipe: the -tint background
	   with its matching -on-tint text, never the base token. */
	.chat-warning {
		margin: 4px 0 12px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.82rem;
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	.chat-subhead {
		margin: 20px 0 4px;
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--text);
	}

	.chat-events {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 12px 14px;
		margin: 14px 0 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.chat-events legend {
		padding: 0 6px;
		font-size: 0.82rem;
		font-weight: 600;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.chat-events .card-hint {
		margin: 0 0 4px;
	}

	.chat-webhook-status {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin: 8px 0 12px;
		flex-wrap: wrap;
	}

	.chat-webhook-set {
		font-size: 0.88rem;
		color: var(--text);
	}

	.domain-empty {
		margin-bottom: 12px;
	}

	/* The TEXT-entry recipe, carved away from the three controls it also
	   reached but was never written for. Svelte scopes this to
	   `.card.svelte-x input:where(.svelte-x)`, which outranks the global
	   control base in `app.css`.

	   checkbox/radio: `background:` is a SHORTHAND — it reset
	   `background-image`, the drawn tick, so the twelve `.switch-row` toggles
	   rendered identically checked and unchecked.

	   color: adding those two `:not()`s took this selector from 0-0-1 to
	   0-2-1, which TIES `.color-field input[type='color']` (also 0-2-1) — and
	   on a tie the later rule wins, so `width: 100%` started beating the
	   swatch's own `width: 40px`. The branding swatch grew to the full 478px
	   column and squeezed its sibling hex field to 22px, under the WCAG 2.2 AA
	   SC 2.5.8 24px floor (caught by `tests-e2e/a11y/axe.spec.ts`). A native
	   colour swatch is not a text entry: it wants neither this padding nor
	   this width, so it is carved out rather than out-specified. */
	input:not([type='checkbox']):not([type='radio']):not([type='color']),
	select,
	textarea {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 0.88rem;
		color: var(--text);
		font-family: inherit;
		width: 100%;
		box-sizing: border-box;
	}

	textarea {
		resize: vertical;
	}

	/* Same carve-out: `outline: none` here would strip the checkbox/radio
	   focus ring `app.css` draws for them (WCAG 2.4.7). */
	input:not([type='checkbox']):not([type='radio']):focus,
	select:focus,
	textarea:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.plan-card h2 {
		margin-bottom: 12px;
	}

	.plan-info {
		display: flex;
		align-items: center;
		gap: 16px;
	}

	.plan-slug {
		font-size: 0.82rem;
		color: var(--text-muted);
	}

	.plan-slug code {
		background: var(--bg);
		padding: 2px 6px;
		border-radius: 3px;
		font-size: 0.8rem;
	}

	.plan-date {
		font-size: 0.82rem;
		color: var(--text-muted);
	}

	.section-footer {
		display: flex;
		/* Same reason as `.erp-test-row` below: this row carries a Save button, an
		   optional Test button and an optional result message, all of them
		   `white-space: nowrap`. Without a wrap the row cannot shrink below its
		   own max-content width and pushes the whole document sideways at 320px
		   — WCAG 1.4.10. The `gap` replaces the spacing the buttons used to get
		   for free from sitting on one line. */
		flex-wrap: wrap;
		justify-content: flex-start;
		gap: 12px;
		margin-top: 16px;
		padding-top: 14px;
		border-top: 1px solid var(--border);
	}

	.btn-save-section {
		padding: 8px 18px;
		border-radius: 6px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-save-section:hover:not(:disabled) {
		opacity: 0.85;
	}

	.btn-save-section:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.erp-test-row {
		display: flex;
		align-items: center;
		/* Wraps because `.btn-test` is `white-space: nowrap` (142px) and sits
		   beside a result message: without this the row could not shrink and
		   pushed the whole page into a horizontal scrollbar at 320px — WCAG
		   1.4.10, the same failure the section tab bar had. Caught when the
		   reflow guard started visiting a grouped route. */
		flex-wrap: wrap;
		gap: 12px;
		margin-top: 16px;
		padding-top: 14px;
		border-top: 1px solid var(--border);
	}

	.btn-test {
		padding: 8px 18px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-test:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.btn-test:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.test-result {
		font-size: 0.85rem;
		font-weight: 500;
	}

	.test-result.success {
		color: #1fa86a;
	}

	.test-result.failure {
		color: var(--danger);
	}

	.sync-grid {
		display: flex;
		flex-direction: column;
		gap: 12px;
		margin-top: 14px;
	}

	.sync-item {
		display: flex;
		align-items: center;
		/* The description and its action cannot share a line at 320px, so the
		   action drops below the description instead of pushing the document
		   sideways (WCAG 1.4.10). */
		flex-wrap: wrap;
		gap: 12px;
		padding: 10px 12px;
		background: var(--bg);
		border-radius: 6px;
	}

	.sync-info {
		flex: 1;
		/* `flex: 1` leaves `min-width: auto`, so this column refuses to shrink
		   below its longest word plus the action beside it. Zero lets it give
		   way and the text wrap. */
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.sync-name {
		font-size: 0.88rem;
		font-weight: 500;
		color: var(--text);
	}

	.sync-desc {
		font-size: 0.78rem;
		color: var(--text-muted);
	}

	.btn-outline {
		padding: 8px 18px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		/* Deliberately NOT `white-space: nowrap` (unlike `.btn-test`, whose label
		   is two short words). These labels name their destination — "Manage on
		   Chart of Accounts page" is 265px — so a nowrap label is wider than a
		   320px viewport's whole content column and no amount of wrapping the
		   ROW can rescue it: the item would still overflow on its own line.
		   Letting the label wrap is what makes the page reflow (WCAG 1.4.10).
		   At every width where the label fits, shrink-to-fit keeps it on one
		   line, so nothing changes above ~460px. */
		text-decoration: none;
		display: inline-block;
		text-align: center;
	}

	.btn-outline:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.btn-outline:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.loading {
		text-align: center;
		padding: 40px;
		color: var(--text-muted);
	}

	label.switch-row {
		flex-direction: row;
		align-items: center;
		gap: 10px;
		font-size: 0.9rem;
		color: var(--text);
		cursor: pointer;
	}

	label.switch-row span {
		font-size: 0.9rem;
		font-weight: 400;
		color: var(--text);
		text-transform: none;
		letter-spacing: normal;
	}

	label.switch-row input[type='checkbox'] {
		width: 16px;
		height: 16px;
		accent-color: var(--accent);
		cursor: pointer;
		flex-shrink: 0;
	}

	.mfa-enforcement-warning {
		color: var(--warning-on-tint);
		font-size: 0.82rem;
		font-weight: 600;
		margin: 8px 0 0;
	}

	/* --- Fraud detection panel --- */

	.fraud-grid {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}

	.fraud-grid label.switch-row {
		align-items: flex-start;
	}

	.fraud-grid .rule-hint {
		display: block;
		margin-top: 2px;
		font-size: 0.8rem;
		color: var(--text-muted);
		font-weight: 400;
	}

	.threshold-row {
		display: grid;
		grid-template-columns: repeat(2, minmax(180px, 1fr));
		gap: 12px;
		margin-left: 26px; /* align under the switch label */
		margin-bottom: 4px;
	}

	.threshold-row label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.threshold-row label.full {
		grid-column: 1 / -1;
	}

	.threshold-row label span {
		font-size: 0.72rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	/* Text-entry recipe. No checkbox sits under this selector today, but it
	   outranks the global control base in `app.css`, so the carve-out keeps a
	   later one from losing its tick (`background:` resets the drawn mark). */
	.threshold-row input:not([type='checkbox']):not([type='radio']),
	.threshold-row textarea {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 0.88rem;
		color: var(--text);
		font-family: inherit;
	}

	.threshold-row textarea {
		resize: vertical;
		font-family: 'SF Mono', 'Cascadia Code', monospace;
		font-size: 0.82rem;
	}

	.threshold-row input:not([type='checkbox']):not([type='radio']):focus,
	.threshold-row textarea:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.threshold-row input:disabled,
	.threshold-row textarea:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-link {
		background: none;
		border: none;
		color: var(--text-muted);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
		padding: 0;
		margin-right: auto;
	}

	.btn-link:hover:not(:disabled) {
		color: var(--accent);
	}

	@media (max-width: 600px) {
		.form-grid {
			grid-template-columns: 1fr;
		}
		.threshold-row {
			grid-template-columns: 1fr;
		}
	}
</style>
