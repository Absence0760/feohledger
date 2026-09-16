from decimal import Decimal

from pydantic import model_validator
from pydantic_settings import BaseSettings

# Environment names that are NOT a deployed/production environment. In these,
# safety guards (e.g. the captcha requirement) are relaxed for local dev + CI.
_NON_DEPLOYED_ENVS = frozenset({"development", "dev", "local", "test", "ci"})

# Every provider `app/services/extraction_adapters/` registers. Held as a
# literal because config.py must not import the service layer (that would make
# every adapter module load at settings construction); the registry is the
# source of truth and `tests/test_extraction_provider_resolution.py` cross-checks
# the two so a new adapter can't drift out of this set.
#
# It exists to validate `FEOH_EXTRACTION_PROVIDER` at BOOT: an unrecognised name
# falls back to `mock` inside `get_extraction_adapter`, and `mock`'s `extract`
# returns a fabricated invoice — so a typo'd override would quietly turn a
# deployed extraction pipeline into a fixture generator. Refusing at boot is the
# only place that miss is loud.
_EXTRACTION_PROVIDERS = frozenset(
    {"claude_vision", "openai_vision", "aws_textract", "ollama", "einvoice", "mock"}
)


class Settings(BaseSettings):
    model_config = {"env_prefix": "FEOH_"}

    # Deployment environment discriminator (FEOH_ENVIRONMENT). Defaults to
    # "development" so local dev + CI are unaffected; deployed envs set it to
    # "production"/"staging" to opt into the production safety guards below.
    environment: str = "development"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/feohledger"
    tenant_db_prefix: str = "feoh_"

    # Auth / JWT
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 30
    # Maximum concurrent sessions per user. When a user logs in and already
    # has this many active sessions, the oldest JTI is evicted onto the Redis
    # blocklist. Set to 0 to disable the cap. Default 5 — a reasonable mix of
    # desktop + mobile + tablet without encouraging shared credentials.
    max_concurrent_sessions: int = 5
    # Self-service "forgot password" reset-link lifetime. The token lives in
    # Redis only (see `services/password_reset.py`), never a DB row — a stray
    # un-redeemed link just expires. Thirty minutes is enough to find the
    # email and click through, short enough that a lingering inbox message
    # months later is inert.
    password_reset_ttl_minutes: int = 30

    # S3 / MinIO
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "invoices"

    # AWS service emulator (LocalStack). Empty = talk to real AWS (prod default).
    # When set (e.g. http://localhost:4566), the non-S3-storage AWS clients —
    # SQS dispatch (lambda mode), SES email, CloudWatch Logs + S3 Object Lock
    # audit shipping, Textract — target it instead. See docs/local-aws-localstack.md.
    aws_endpoint_url: str = ""

    # Extraction
    extraction_mode: str = "local"  # "local" = in-process, "lambda" = dispatch to SQS
    sqs_extraction_queue_url: str = ""  # required when extraction_mode = "lambda"
    # Background reaper that transitions invoices stuck in `pending` (extraction
    # timed out, worker crashed, Ollama hung, etc.) to `failed` so the reviewer
    # can retry. Threshold = how long an invoice may sit in `pending` before
    # being declared dead. Interval = how often the reaper sweeps.
    extraction_timeout_seconds: int = 600
    extraction_reaper_interval_seconds: int = 60
    extraction_reaper_enabled: bool = True
    # Approval escalation sweeper: scans every tenant's active workflow
    # instances and appends `escalation_to_user_ids` onto any chain level
    # that has been waiting longer than its `escalation_hours`. Disabled
    # by default in local dev (no clock to escalate against in a one-shot
    # demo); flip on in deployed envs.
    approval_escalation_enabled: bool = False

    # Contract renewal-alert sweep. A long-lived loop that finds contracts
    # nearing their end_date and notifies the owner + AP managers once
    # (deduped via Contract.renewal_alert_sent_at). Disabled by default so
    # local dev / tests don't run a background sweep; flip on in deployed
    # envs. The per-contract `renewal_notice_days` overrides the default
    # lead window below.
    contract_renewal_enabled: bool = False
    contract_renewal_interval_seconds: int = 3600
    contract_renewal_default_notice_days: int = 30
    # How many candidate contracts each of the sweep's two passes (renewal
    # alert, end-of-term expiry) pages through at a time, per tenant. It is a
    # PAGE size, not a per-tick cap: both passes keyset-paginate by id until the
    # tenant is exhausted, so nothing starves. Capping is not available to this
    # sweep -- a contract outside its own lead window stays un-alerted, so it
    # never leaves the candidate set and a LIMIT would re-serve the same
    # lowest-id rows every tick forever. The alert pass used to pre-filter on
    # `end_date <= today + 3650 days` (effectively every active contract with an
    # end date) and load the lot in one unbounded SELECT.
    contract_renewal_batch_size: int = 200

    # QMS (Quality Management System) inspection sync. A background loop that
    # pulls quality-inspection records from an external QMS / LIMS into the
    # local `quality_inspections` table (the 4-way-match leg). Disabled by
    # default so local dev / tests don't run a background sweep; flip on in
    # deployed envs once a real QMS is configured per-org in
    # `Organization.settings.qms`. `qms_provider` is the platform-default
    # adapter (`mock` = deterministic, no network/credential — the local-first
    # default); a per-org `settings.qms.provider` overrides it. No secret here:
    # the `generic` adapter reads its base_url + api_key from per-org settings,
    # never from env.
    qms_sync_enabled: bool = False
    qms_sync_interval_seconds: int = 3600
    qms_provider: str = "mock"

    # External vendor enrichment (firmographics from D&B / Clearbit / ...).
    # Platform-default adapter for the on-demand "enrich this vendor from an
    # external source" endpoint. `mock` (deterministic, no network/credential —
    # the local-first default) lets `pnpm dev` + tests run with no cloud account.
    # A per-org `settings.enrichment.provider` overrides it; the real providers
    # (`dun_bradstreet`, `clearbit`) FAIL CLOSED without a per-org `api_key` (no
    # hardcoded fallback secret). Advisory only — enrichment never writes back
    # onto the Vendor row. See `docs/data-enrichment.md` § External enrichment.
    vendor_enrichment_provider: str = "mock"  # "mock" | "dun_bradstreet" | "clearbit"

    # Sanctions & vendor-risk screening. `vendor_screening_enabled` is the
    # master switch for synchronous screening on vendor create / update
    # (default on — the `mock` adapter is safe and local-first, returning
    # `clear` for everything that isn't an obvious fixture). The re-screen
    # sweep is a background loop (like contract renewal) and is OFF by
    # default so local dev / tests don't run it; deployed envs flip it on.
    # `vendor_rescreen_after_days` is the staleness window — a vendor whose
    # last screen is older than this (or never screened) is re-screened on
    # the next sweep. See `docs/vendor-risk-screening.md`.
    vendor_screening_enabled: bool = True
    vendor_rescreen_enabled: bool = False
    vendor_rescreen_interval_seconds: int = 86400
    vendor_rescreen_after_days: int = 7

    # Dynamic discounting & early-payment optimization (see
    # backend/docs/dynamic-discounting.md). The ROI calculator, the
    # accept/decline offer surface, the optimizer, the dashboard AND offer
    # *expiry* all run unconditionally; only the *auto-capture decision* is
    # gated. Expiry used to ride this switch too — it is written down by the
    # same sweep — so with the switch off (the default) a lapsed offer stayed
    # `offered` forever and `capture_rate_pct` read 100.00 on one capture out of
    # ten. It no longer depends on the sweep: every read surface derives it
    # (`services/discount_offers.effective_status`) and the sweep's write is a
    # materialization. `discount_optimization_enabled` is the master switch for
    # that background loop — OFF by default so local dev / tests don't
    # auto-pay. `discount_auto_capture_roi_threshold` is the annualized return
    # (APR %) an offer must clear for the sweep to capture it automatically;
    # `discount_cost_of_capital_pct` is the platform-default annual cost of
    # capital the ROI calculator compares against (per-org override:
    # `Organization.settings.discounting.cost_of_capital_pct`). Percentages,
    # not currency, so float is fine.
    discount_optimization_enabled: bool = False
    discount_optimization_interval_seconds: int = 3600
    # How many candidate `offered` offers the auto-capture sweep pages through
    # at a time, per tenant. It is a PAGE size, not a per-tick cap: the sweep
    # keyset-paginates by id until the tenant is exhausted, so nothing starves.
    # Capping is not available here -- an offer skipped for a below-threshold
    # ROI stays `offered`, so it never leaves the candidate set and a LIMIT
    # would re-serve the same lowest-id rows every tick forever. The sweep used
    # to select every `offered` offer id in one unbounded SELECT.
    discount_optimization_batch_size: int = 200
    discount_auto_capture_roi_threshold: float = 12.0
    discount_cost_of_capital_pct: float = 8.0
    approval_escalation_interval_seconds: int = 600
    # How many candidate workflow instances the escalation sweep pages through
    # at a time, per tenant. It is a PAGE size, not a per-tick cap: the sweep
    # keyset-paginates by id until the tenant is exhausted, so nothing starves.
    # The sweep used to `SELECT ... WHERE state='active' FOR UPDATE` with no
    # LIMIT, locking every active instance in the tenant at once — which blocks
    # `review.approve_invoice` (it takes the same row lock) for the whole tick,
    # and deadlocks against a second replica sweeping in a different order. Now
    # each row is locked individually, in id order, and released on commit.
    approval_escalation_batch_size: int = 200
    # Background-sweep health (see backend/app/services/sweep_health.py). Every
    # long-lived sweep reports each tick's outcome into an in-process registry
    # served by GET /api/health/sweeps. This is how many CONSECUTIVE failed runs
    # (a tick that raised, or one that completed reporting `failures > 0`) a
    # sweep may accumulate before it is called degraded: the aggregate verdict
    # flips, and the loop emits the alertable PII-free "NOT MAKING PROGRESS"
    # ERROR log on each streak multiple. Set to 0 to disable the escalation —
    # the per-tick failure log stays either way. Not a secret; a count, not a
    # currency, so a plain int.
    sweep_failure_alert_streak: int = 3
    # Recurring / subscription invoice generation sweep. `recurring_invoices_enabled`
    # is the master switch for the background loop — OFF by default so local dev /
    # tests don't auto-create invoices. The sweep finds `active` templates whose
    # `next_run_on` has arrived, generates the next Invoice (pre-coded, status
    # `pending`), advances `next_run_on`, and is idempotent on the
    # (template, period_key) DB unique index. `recurring_invoices_max_per_sweep`
    # caps generations per tick so a backlog (or a misconfigured template) can't
    # firehose the queue. See backend/docs/recurring-invoices.md.
    recurring_invoices_enabled: bool = False
    recurring_invoices_interval_seconds: int = 3600
    recurring_invoices_max_per_sweep: int = 200
    # Scheduled-report runner. `scheduled_reports_enabled` is the master switch
    # for the background loop — OFF by default so a local dev box / tests never
    # email reports. The sweep finds `enabled` schedules whose `next_run_at` has
    # arrived, generates the CSV, emails the recipients, and bumps `next_run_at`
    # by the cadence. See backend/docs/analytics.md § Scheduled reports.
    scheduled_reports_enabled: bool = False
    scheduled_reports_tick_seconds: int = 3600
    # Vendor statement reconciliation: the platform-default materiality
    # threshold (in the run's currency) above which a vendor's leftover
    # unreconciled balance (missing-on-our-side + amount-mismatch, unresolved)
    # flags the vendor as not-close-ready on GET /api/vendor-statements/
    # close-readiness. A per-call `?materiality=` query param overrides it.
    # No background sweep — reconciliation is user-triggered. A currency amount,
    # so Decimal (never float) per the money-is-exact invariant. See
    # backend/docs/vendor-statement-reconciliation.md.
    statement_recon_materiality_default: Decimal = Decimal("1000.00")
    # Payment-status reconciler: backstop polling for processors whose
    # webhooks have gone missing. Disabled by default in local dev (the
    # mock adapter settles synchronously, so there's nothing to
    # reconcile); flip on in deployed envs alongside Modern Treasury.
    payment_reconcile_enabled: bool = False
    payment_reconcile_interval_seconds: int = 300
    payment_reconcile_after_minutes: int = 10
    payment_reconcile_max_age_hours: int = 72
    # Run Tesseract OSD on rendered PDF pages before sending to vision adapters,
    # rotating 90/180/270-off-upright scans back to upright. Safe to leave on —
    # a missing ``pytesseract`` / ``tesseract`` binary degrades to a silent no-op.
    extraction_auto_rotate: bool = True

    # Email-to-invoice intake (see backend/docs/email-intake.md).
    # ``email_intake_domain`` is the hostname used in the recipient address
    # (``invoices+<token>@<domain>``). Leave empty to disable intake.
    email_intake_domain: str = ""
    # HMAC-SHA256 signing secret for the webhook body. When empty, signature
    # verification is skipped (dev only — set this in every deployed env).
    email_intake_signing_secret: str = ""

    # ERP
    erp_mode: str = "local"  # "local" = in-process, "lambda" = dispatch to SQS
    sqs_erp_queue_url: str = ""  # required when erp_mode = "lambda"
    # ERP adapter base-URL overrides. These are OPERATOR-controlled (env /
    # process level), not tenant-admin-supplied config, so they are trusted and
    # bypass the SSRF guard that protects admin-supplied base URLs. They exist
    # so local dev + e2e can point the real adapters at the fake ERP container
    # (see backend/docker-compose.yml `fake-erp` service, host port 12112).
    # Deployed envs leave them unset / default.
    # Merge.dev API base URL. Default = live Merge.dev.
    erp_merge_api_base: str = "https://api.merge.dev/api/accounting/v1"
    # NetSuite REST base URL. Empty = derive the per-account URL from the
    # config's account_id as usual (https://<account>.suitetalk.api.netsuite.com).
    erp_netsuite_api_base: str = ""
    # Dynamics 365 BC API base URL. Empty = use the admin-supplied config
    # base_url (with the SSRF guard applied).
    erp_d365_api_base: str = ""
    # Dynamics 365 BC OAuth2 token URL. Empty = the real
    # login.microsoftonline.com URL built from the config's tenant_id.
    erp_d365_token_url: str = ""

    # Audit
    audit_mode: str = "local"  # "local" = in-process, "lambda" = dispatch to SQS
    sqs_audit_queue_url: str = ""  # required when audit_mode = "lambda"

    # Centralized audit-log shipping (SOC 2). A background task sweeps every
    # tenant DB, pulls unshipped `audit_log` rows in batches, and ships them
    # to the configured WORM-compliant sinks (CloudWatch Logs, S3 with Object
    # Lock). Default disabled — local dev doesn't need it and we don't want
    # to fire AWS calls from a developer laptop. Flip on in deployed envs.
    audit_shipping_enabled: bool = False
    audit_shipping_interval_seconds: int = 60
    audit_shipping_batch_size: int = 500
    # Comma-separated list of adapter names to fan out to. Every adapter in
    # the list must ACK before the rows get marked shipped — if any one
    # fails, none are marked shipped and the next tick retries.
    audit_shipping_providers: str = "mock"
    # CloudWatch Logs group name. Tenant log streams are created under this
    # group as `<tenant_db>/<YYYY-MM-DD>`.
    audit_shipping_cloudwatch_group: str = "/ap/audit"
    # S3 bucket must have Object Lock enabled in COMPLIANCE mode with a
    # default retention rule on the bucket. The shipper verifies all three at
    # startup and refuses to boot otherwise, but does not configure Object
    # Lock itself — that's a bucket-provisioning concern (Terraform). Mode is
    # not configurable: `/legal/dpa` Annex II publishes compliance mode, and
    # GOVERNANCE lets a principal with s3:BypassGovernanceRetention delete
    # audit evidence.
    audit_shipping_s3_bucket: str | None = None
    # The floor the bucket's default retention must meet. 2555 days ≈ seven
    # years, matching `infra/variables.tf` `audit_retention_days` and the
    # figure the DPA names. Lowering it is a deployment's call, but it makes
    # that published figure wrong for that deployment.
    audit_shipping_s3_min_retention_days: int = 2555

    # Periodic access reviews (SOX). The dormancy window for the elevated-access
    # review: a user holding an elevated role (admin / ap_manager / cfo) whose
    # last *mutating* audit action is older than this many days — or who has
    # never acted — is flagged DORMANT in `GET /api/access-reviews`. Compute-on-
    # read (no column, no migration); see backend/docs/access-reviews.md.
    access_review_dormant_days: int = 90

    # AI Extraction (platform-level key — used when customers choose "Platform" extraction)
    anthropic_api_key: str = ""  # your Anthropic API key for Claude Vision
    extraction_model: str = "claude-sonnet-4-20250514"
    # Operator override for the adapter PLATFORM-mode extraction runs on. Empty
    # (the code default) means "derive": a configured platform key selects
    # `claude_vision`, and a keyless NON-deployed environment falls back to the
    # offline `mock` reader so `pnpm dev` never calls out with an empty key.
    # A keyless DEPLOYED environment deliberately does NOT fall back — `mock`'s
    # `extract` returns a fixture, and fabricating invoice fields on a real
    # tenant's document is worse than the loud provider error. Set explicitly
    # (e.g. `mock`, `ollama`) to pin the provider regardless of the key; a BYOK
    # org's own `settings.extraction` is unaffected either way. See
    # `app/services/extraction.py::resolve_platform_provider` and
    # `backend/docs/ai-extraction.md` § Platform provider precedence.
    extraction_provider: str = ""

    # Audit-log summarization (invoice detail modal). Reuses the extraction
    # API key + model, so no new secret. Master switch — when False the
    # `/api/invoices/{id}/summary` endpoint returns the deterministic template
    # summary without any LLM call. `audit_summary_model` defaults to the
    # extraction model when empty.
    #
    # Defaults OFF, and the "no new secret" convenience is exactly why it has
    # to. Sharing the extraction key means this feature has no credential of
    # its own to gate it: defaulted True, an operator who configured Anthropic
    # for invoice EXTRACTION silently also began sending invoice numbers,
    # vendor names, amounts and audit timelines to Anthropic for a second,
    # unrelated purpose — a sub-processor engagement the customer never chose
    # and no setting disclosed. Local dev never noticed because an empty key
    # short-circuits either way, so the usual local-first check could not see
    # it. Every integration defaults to its safe local value (root CLAUDE.md,
    # `## Key environment variables`); the template summary is that value here,
    # and it is a genuine feature, not a degraded one.
    audit_summary_enabled: bool = False
    audit_summary_model: str = ""  # falls back to extraction_model when empty

    # Exception-agent rationale polish (services/exception_agents/llm_rationale).
    # Same shape and the same reason as `audit_summary_enabled` above: it reuses
    # the extraction Anthropic key, so it has no credential of its own to act as
    # its gate, and without this flag an extraction key silently enrolled the
    # deployment in a second purpose — a resolver's deterministic rationale,
    # which carries the PO number and invoice amount in prose, POSTed to
    # Anthropic to be reworded. Off by default; the deterministic template is
    # the off-state and the agent's DECISION never depended on the LLM either
    # way. See docs/decisions.md §176.
    exception_agent_rationale_enabled: bool = False

    # Conversational AP Assistant (see backend/docs/conversational-assistant.md).
    # Local-first: the default `mock` adapter routes a natural-language query to
    # one of the five fixed tools via deterministic keyword/intent heuristics —
    # no network, no key. The `claude` adapter (Anthropic Messages API tool-use)
    # is selected only when an API key is configured; the dispatcher
    # auto-downgrades `claude` → `mock` when `FEOH_ANTHROPIC_API_KEY` is empty, so
    # `pnpm dev` never requires a real credential. Reuses the extraction key — no
    # new secret.
    assistant_provider: str = "mock"  # "mock" (local-first default) | "claude"
    # Empty → falls back to `extraction_model` (claude-opus-4-8 family) at
    # request time; never hardcoded in the adapter.
    assistant_model: str = ""
    # Per-org / per-month token budget. 0 disables the cap (matching the
    # FEOH_MAX_CONCURRENT_SESSIONS=0 convention). Per-org override lives in
    # Organization.settings.assistant.monthly_token_budget.
    assistant_monthly_token_budget: int = 200_000
    # Caps the claude/ollama adapter's tool-use loop so a single turn can't run
    # away on cost. Each hop is one API round-trip.
    assistant_max_tool_hops: int = 4
    # Ollama assistant adapter: a local, tool-capable TEXT model (NOT the vision
    # model used for extraction — that can't do function-calling). Must support
    # Ollama tool-use. Base URL reuses `ollama_base_url`. When `ollama` is the
    # selected provider but Ollama is unreachable or the model isn't pulled, the
    # adapter fails soft to `mock`, so a fresh clone still runs (local-first).
    assistant_ollama_model: str = "qwen2.5:7b"

    # AI Cash-Flow Copilot (see docs/cash-flow-copilot.md). Master switch for the
    # finance-leader copilot tools (forecast / cash position / payment what-if /
    # discount optimizer) + their façade routes. When False the tools return a
    # clean "not available" refusal (never a 500) and the routes 404.
    cashflow_copilot_enabled: bool = True
    # Default forecast horizon (days) when the user doesn't specify one.
    cashflow_copilot_default_horizon_days: int = 90

    # Projected cash-shortfall alert sweep (services/cash_flow_alerts.py).
    # `cashflow_shortfall_alerts_enabled` is the master switch: OFF by default so
    # local dev / tests never email a CFO. The sweep only READS the cash forecast
    # and sends a notification — it never creates a Payment/PaymentRun, accepts a
    # discount, or touches an invoice. A per-org
    # `settings.cashflow.min_balance_threshold` is the opt-in; an org without one
    # is skipped entirely.
    cashflow_shortfall_alerts_enabled: bool = False
    # Daily by default — a cash forecast doesn't move hour to hour, and a
    # standing shortfall is announced once per projected period regardless.
    cashflow_shortfall_alerts_interval_seconds: int = 86400
    # How far ahead the alerting forecast looks. Independent of the copilot's
    # own default horizon so an operator can alert on a shorter, more actionable
    # window than the one an interactive question defaults to.
    cashflow_shortfall_alerts_horizon_days: int = 90

    # Virtual Cards (platform-level keys — used when customers choose "Platform" card program)
    lithic_api_key: str = ""  # your Lithic API key
    lithic_sandbox: bool = True
    nium_client_id: str = ""  # your Nium client ID
    nium_client_secret: str = ""
    nium_customer_hash_id: str = ""
    nium_wallet_hash_id: str = ""
    nium_sandbox: bool = True

    # Redis
    redis_url: str = "redis://localhost:6379"

    # CORS
    # `cors_origins` is a list of exact origins (used for the static-frontend
    # dev server). `cors_production_domain` is a single registrable-domain
    # suffix that — when set — adds `https://*.<domain>` to the allowlist
    # via the subdomain regex. Empty in local dev so we never silently
    # match a third-party host; deploys must set it to the real domain
    # (e.g. ``app.example.com``). Comma-separated list of multiple domains
    # is supported.
    cors_origins: list[str] = ["http://localhost:7777", "http://localhost:5173"]
    cors_production_domain: str = ""

    # `X-Forwarded-For` is only honoured when the request's connecting IP
    # belongs to one of these CIDRs (the ALB / CloudFront edges in deployed
    # envs). Anything outside the allowlist is treated as a direct client
    # and uses ``request.client.host`` for rate-limit / audit purposes, so
    # a hostile direct caller can't rotate IPs by spoofing the header.
    # Default empty → never trust XFF (matches local-dev with no proxy).
    # Comma-separated list of CIDRs.
    trusted_proxy_cidrs: str = ""

    # Self-service signup
    email_provider: str = "console"  # "console" (dev default) | "ses" | "smtp"
    email_from: str = "no-reply@localhost"
    aws_ses_region: str = "us-east-1"

    # SMTP email (used when email_provider="smtp"). Defaults target a local
    # Mailpit sink (`pnpm mail:up`) — no auth, no TLS — so outbound mail lands in
    # a web inbox at http://localhost:8025. See docs/local-email-mailpit.md.
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""  # secret — empty for Mailpit; set via sops in deployed envs
    smtp_use_tls: bool = False
    public_url: str = "http://localhost:7777"  # where the frontend is served
    # Externally-reachable base URL of THIS backend. Unlike OIDC (front-channel
    # code flow that redirects to the SPA), SAML POST-binding makes the IdP post
    # the assertion straight to the backend ACS, so the SP entityID + ACS URL we
    # register with each IdP are built off this. Dev: the backend dev server.
    api_public_url: str = "http://localhost:8000"
    tenant_url_template: str = "http://{slug}.localhost:7777"

    # Master switch for the email + in-app notification system. When false,
    # the `transition_invoice` hook and `assign_reviewer` skip notification
    # dispatch entirely — no in-app rows, no emails. Defaults on; flip off to
    # silence notifications without a code change. Notification *send* is always
    # best-effort regardless of this flag (failures never break a transition).
    notifications_enabled: bool = True

    # Outbound chat-notification adapter (Slack / Teams incoming-webhook posts
    # of approval-lifecycle events). Platform default `mock` — local-first, no
    # network, no Slack/Teams credential, so `pnpm dev` runs unchanged. Per-org
    # override on `Organization.settings.chat_notifications.provider` (with the
    # webhook URL + per-event toggles alongside it). `slack` / `teams` fail
    # closed (no-op + PII-free warning) when no webhook URL is configured.
    # See backend/docs/notifications.md § Chat notifications (Slack/Teams).
    chat_notification_provider: str = "mock"  # "mock" (default) | "slack" | "teams"

    hcaptcha_secret: str = ""  # empty = skip captcha verification
    hcaptcha_sitekey: str = ""  # exposed to frontend via a public endpoint
    signup_rate_limit_per_hour: int = 5
    # Per-email cap on verification-email sends, so an attacker rotating IPs
    # can't email-bomb one victim address (the per-IP limit can't catch that).
    signup_email_rate_limit_per_hour: int = 3
    # Inline slug-availability check fires on (debounced) keystrokes, so it
    # needs a generous per-IP cap — high enough for real typing, low enough to
    # stop wire-speed namespace enumeration / DB amplification.
    slug_check_rate_limit_per_hour: int = 120

    # Master switch for the Redis-backed rate limiter at
    # ``app/services/rate_limit.py``. Defaults to True so deployed envs
    # are protected; CI's e2e suite flips this off because all 4 shards
    # × 4 workers hit `/api/auth/login` from the same loopback IP and
    # would otherwise saturate the 10/min cap during the
    # signInAndWait calls in opt-out specs.
    rate_limit_enabled: bool = True

    # RAG / embeddings
    rag_enabled: bool = True
    rag_top_k: int = 3
    embedding_provider: str = "mock"  # "mock" (dev default) | "openai"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # Local AI (Ollama). Fallback base URL for the `ollama` extraction adapter
    # when an org's extraction config doesn't set its own `base_url`. Defaults to
    # a native Ollama on 11434; point at 11435 to use the Compose container
    # (`pnpm ollama:up`). See backend/docs/local-ai-testing.md.
    ollama_base_url: str = "http://localhost:11434"

    # Stripe API base for the stripe_treasury payment adapter. Empty = live Stripe
    # (api.stripe.com). Set to the local stripe-mock container
    # (http://localhost:12111/v1) for offline payment testing (`pnpm stripe:up`).
    stripe_api_base: str = ""
    # Cosine similarity above which a new invoice is flagged as a likely
    # duplicate of an already-stored one. Tighter than rag_top_k retrieval:
    # RAG wants semantically related invoices; dup detection wants near-
    # identical ones.
    duplicate_similarity_threshold: float = 0.95

    # International tax (VAT / GST / withholding) — see docs/international-tax.md.
    # The consumption-tax *rate* is resolved by a pluggable adapter. Default
    # `mock` reads deterministic rates from the country-rules engine, so a
    # fresh clone needs no cloud account (local-first). Cloud providers
    # (`avalara`, `taxjar`) are skeletons until a real key is wired up. A
    # tenant can override per-org via `Organization.settings.tax.rate_provider`;
    # this is the platform-wide fallback.
    tax_rate_provider: str = "mock"  # "mock" (dev default) | "avalara" | "taxjar"
    tax_rate_api_key: str = ""  # secret — empty for mock; set via sops for cloud providers

    # MFA (TOTP + email backup)
    # Master switch — when false, all MFA enrollment, challenge, and enforcement
    # is bypassed. Default is `false` so local dev "just works" without TOTP
    # apps; flip to `true` in deployed environments.
    mfa_enabled: bool = False
    # Issuer label baked into TOTP provisioning URIs (what the user sees in
    # Google Authenticator / 1Password). Customer name keeps it brand-aligned.
    mfa_issuer: str = "FeohLedger"
    # Email-OTP code lifetime. Six minutes balances "user has time to switch
    # from inbox back to the form" with "stolen email is short-lived."
    mfa_email_otp_ttl_seconds: int = 360
    # Short-lived JWT minted when password+email check out but MFA is still
    # required. The user trades it for a real access token by completing the
    # TOTP / email challenge. Five minutes is enough to find your phone.
    mfa_challenge_ttl_seconds: int = 300
    # Lifetime of a *pending* (not-yet-verified) TOTP enrollment secret. The
    # candidate secret lives in Redis — never on the account row — so starting
    # an enrollment can't disturb the second factor already in force. Fifteen
    # minutes covers "install an authenticator app, scan, type the code";
    # past it the user simply restarts enrollment.
    mfa_enroll_pending_ttl_seconds: int = 900

    # WebAuthn / passkeys (an ADDITIONAL MFA factor — separate code path from
    # TOTP, gated by the same `mfa_enabled` master switch above).
    #
    # The Relying Party ID is the registrable domain the passkey is bound to.
    # WebAuthn requires it be the page's effective domain or a registrable
    # parent of it — and (critically) it must be a bare host, never a scheme or
    # port. For local dev the SPA runs on subdomains of `localhost`
    # (`acme.localhost:7777`), so `localhost` is the correct RP ID: passkeys
    # registered under it work across every tenant subdomain. In deployed envs
    # set this to your apex (e.g. `app.example.com`).
    webauthn_rp_id: str = "localhost"
    # Human-readable Relying Party name shown by the authenticator UI.
    webauthn_rp_name: str = "FeohLedger"
    # Comma-separated list of allowed origins the browser ceremony may come
    # from — verified against `response.origin` on both register + authenticate.
    # Multiple because each tenant is its own subdomain origin in dev. A value
    # must include scheme + host (+ port). Empty falls back to the dev origins.
    webauthn_origins: str = "http://localhost:7777"
    # Challenge lifetime (seconds). The register/authenticate options carry a
    # server-minted random challenge stashed in Redis; the browser must complete
    # the ceremony before it expires. 5 minutes matches the MFA challenge TTL.
    webauthn_challenge_ttl_seconds: int = 300

    # SSO / SCIM
    # Base URL the OIDC provider redirects back to. Must exactly match what's
    # registered with Okta / Entra. {base} is substituted from FEOH_PUBLIC_URL.
    sso_redirect_path: str = "/login/sso-callback"
    # Called after successful SSO to hand the browser our own JWT in a short-
    # lived URL fragment; the frontend reads and stores it. Keep the path
    # static so IdP configs don't need per-tenant changes.
    sso_state_ttl_seconds: int = 600  # OIDC state / nonce expiry
    # Hash (not reversible) of the per-tenant SCIM bearer token is what gets
    # stored. The plaintext token is shown to the admin ONCE on generation.
    scim_url_path: str = "/api/scim/v2"

    # Public programmatic API (/api/v1, X-API-Key auth). On by default — the
    # surface is auth-gated regardless; this is an org-platform kill switch.
    # No secret here: API keys are minted per-org and stored hashed (see
    # app/models/api_key.py). See backend/docs/public-api.md.
    public_api_enabled: bool = True

    # Per-API-key request cap on the /api/v1 surface, enforced by the Redis
    # sliding-window limiter (services/rate_limit.py) keyed on the API key id —
    # NOT per-IP, NOT per-org. A key over its limit gets a 429 (Retry-After);
    # an unauthenticated/garbage key still gets the opaque 401 (the limit is
    # checked AFTER the key authenticates, so it never confirms a key exists).
    # The window is one minute. Composes with the master `rate_limit_enabled`
    # switch (CI flips that off). See backend/docs/public-api.md § Rate limiting.
    public_api_rate_limit_per_minute: int = 120

    # ---- Outbound webhooks (push counterpart of /api/v1) -----------------
    # Master switch for outbound webhooks: subscription emit + the background
    # retry/delivery sweep. OFF by default so a fresh clone / pnpm dev never
    # makes outbound HTTP calls and no background task spins up. Flip
    # FEOH_WEBHOOKS_ENABLED on in deployed envs. No secret here — each
    # subscription's signing secret is generated at create time and stored on
    # the row (it's a symmetric HMAC verification key; see app/models/webhook.py).
    # See backend/docs/public-api.md § Outbound webhooks.
    webhooks_enabled: bool = False
    webhooks_delivery_interval_seconds: int = 60
    # SSRF-guard escape hatch for outbound-webhook target URLs. Default false
    # (SAFE): a target host may not resolve to a loopback / RFC1918 private /
    # link-local (incl. the 169.254.169.254 metadata endpoint) / unique-local /
    # multicast / reserved address — enforced at subscription create/update AND
    # re-checked immediately before every dispatch (DNS-rebinding TOCTOU).
    # `true` skips only the address checks so local-first dev can point a
    # webhook at 127.0.0.1 (e.g. a local sink); never enable in a deployed env —
    # main.lifespan refuses to boot with this on when FEOH_DEBUG=false.
    # See backend/docs/public-api.md § Outbound webhooks (target-URL SSRF guard).
    webhooks_allow_private_targets: bool = False

    # ---- Platform billing & metering -------------------------------------
    # Billing provider adapter. `mock` (in-process, deterministic, no network /
    # credential) is the local-first DEFAULT; `stripe_billing` is a fail-closed
    # skeleton that needs a real key. Per-org override on
    # `Organization.settings.billing.provider`. See backend/docs/billing.md.
    billing_provider: str = "mock"  # "mock" (dev default) | "stripe_billing"
    # Live Stripe Billing credentials — NO hardcoded fallback. Empty by default;
    # the stripe_billing adapter fails closed without them. Real values arrive
    # via sops (the private infra-secrets repo) in deployed envs, never committed.
    billing_stripe_api_key: str = ""
    billing_stripe_webhook_secret: str = ""
    # Reject a Stripe billing webhook whose `Stripe-Signature` `t=` is more than
    # this many seconds from now (replay-window guard; Stripe's own default
    # tolerance is 5 minutes, and the same ±5-min window the Slack / Teams
    # interactivity routes enforce). The Redis dedupe covers a REDELIVERY of the
    # same event within its TTL; this covers an OLD, correctly-signed event
    # being replayed at all. `<= 0` disables the age check — for an operator
    # deliberately replaying an archived event during an incident.
    billing_stripe_webhook_max_age_seconds: int = 300
    # Live Stripe Billing API base URL — overridable so tests / a sandbox can
    # point the adapter at a mock server. The adapter still fails closed without
    # an API key regardless of this value.
    billing_stripe_api_base: str = "https://api.stripe.com"
    # Master switch for the INBOUND billing webhook route
    # (`POST /api/billing/webhook/{provider}`). OFF in local dev (no outbound
    # billing integration), flipped ON in deployed envs alongside the live
    # provider. The route is HMAC-gated regardless; when off it 204s silently.
    billing_webhook_enabled: bool = False
    # Master switch for the dunning / past-due automation sweep. OFF by default
    # (mirrors the other background sweeps); flip on in deployed envs. The sweep
    # only flags subscriptions overdue past the grace window as `canceled` and
    # writes an audit row — it NEVER moves money.
    billing_dunning_enabled: bool = False
    billing_dunning_interval_seconds: int = 3600
    # Grace window (days) a subscription may sit `past_due` before the dunning
    # sweep cancels it. Stripe's own retry schedule normally drives the status
    # via webhooks; this is the backstop when a provider webhook never arrives.
    billing_dunning_grace_days: int = 14

    # SAML 2.0 SSO (Service-Provider side). Additive, separate code path from
    # OIDC; like OIDC it is gated PER-TENANT via Organization.settings.sso
    # (protocol="saml") — there is no global on/off, so a fresh clone + pnpm dev
    # simply has no SAML tenant configured and the routes 400/404.
    # Frontend bridge route the SAML ACS 303-redirects to after minting a
    # one-time handoff code. The bridge POSTs the code to /api/auth/saml/exchange
    # and receives the JWT in the response body — the JWT never transits a URL.
    saml_acs_path: str = "/login/saml-callback"
    # TTL for the one-time post-ACS handoff code (Redis). Short — it's consumed
    # by the bridge page within a single redirect hop.
    saml_handoff_ttl_seconds: int = 120
    # Optional SP signing keypair, used ONLY when a tenant requires SP-signed
    # AuthnRequests. Real secret -> sops (KMS, private infra-secrets) in deployed envs;
    # empty by default (local Keycloak runs with client-signature off, so no SP
    # key is needed to run locally). NEVER given a hardcoded non-empty fallback.
    saml_sp_private_key: str = ""
    saml_sp_cert: str = ""

    # Security headers (SOC 2 — TLS + tablestakes hardening)
    # HSTS is gated off by default so local HTTP dev isn't broken. Deployed
    # environments should set FEOH_HSTS_ENABLED=true.
    hsts_enabled: bool = False
    # Two years is the value required by browsers that consider preload
    # submissions (hstspreload.org). Keep it as a setting so ops can dial it
    # down during a cert migration without a code push.
    hsts_max_age: int = 63072000
    hsts_include_subdomains: bool = True
    hsts_preload: bool = True

    # Multi-currency reporting. The reporting (base) currency an org rolls
    # multi-currency invoices/payments up into for analytics + dashboards. A
    # per-org override lives on `Organization.settings.reporting_currency` (and
    # falls back to the legacy `payments.home_currency`, then
    # `invoice_defaults.currency`); this is the platform-wide last-resort default
    # when none of those are set. ISO 4217. See backend/docs/multi-currency.md.
    reporting_currency_default: str = "USD"

    # US 1099 tax compliance (TIN validation + e-filing). Both default to the
    # offline `mock` adapter so a fresh clone runs with no cloud account
    # (local-first). Per-org overrides live on
    # ``Organization.settings.tax.{tin_validation,filing}`` and win over these
    # process-level defaults; deployed envs that validate/file for real set the
    # provider to `tax1099` there (with the live key in the encrypted settings).
    tin_validation_provider: str = "mock"  # "mock" (offline) | "tax1099"
    tax_filing_provider: str = "mock"  # "mock" (offline) | "tax1099"

    # PEPPOL AS4 outbound (e-invoice transmission via a hosted Access Point).
    # Local-first: the in-process `mock` adapter is the default so `pnpm dev`
    # never needs a real PEPPOL credential. Per-org overrides live on
    # ``Organization.settings.peppol.{provider,gateway_url,api_key,...}`` and
    # win over these process-level defaults; deployed envs that transmit for
    # real set the provider to `as4_gateway` there with the live gateway URL +
    # key in the encrypted settings (sops). The gateway API key has NO
    # hardcoded fallback — empty here, real value via sops in deployed.
    peppol_provider: str = "mock"  # "mock" (in-process default) | "as4_gateway"
    peppol_gateway_url: str = ""  # hosted Access Point base URL (deployed only)
    peppol_gateway_api_key: str = ""  # secret — empty for mock; sops in deployed

    # PEPPOL AS4 INBOUND receive (the receiver-corner C4 webhook the Access
    # Point posts inbound documents to). Master switch — mirrors
    # ``email_intake_domain`` / ``audit_shipping_enabled`` gating: the public
    # ``POST /api/peppol/inbound/{tenant_slug}`` route is a no-op 204 until this
    # is on, so the surface is closed by default. The signing secret is the
    # HMAC-SHA256 key the Access Point signs the inbound POST body with; it has
    # NO hardcoded secret fallback (the boot guard refuses to start a deployed
    # env that enables inbound without it). The committed .env.development sets a
    # NON-secret dev value so the webhook is locally testable under `pnpm dev`.
    peppol_inbound_enabled: bool = False
    peppol_inbound_signing_secret: str = ""
    # Hard cap on the inbound webhook body the route will buffer before parsing.
    # A signed-but-oversized POST is fully read into memory then handed to lxml;
    # PEPPOL UBL documents are tens of KB, so a few-MB ceiling rejects a
    # memory-exhaustion attempt (204, no parse) without truncating real invoices.
    peppol_inbound_max_bytes: int = 4 * 1024 * 1024

    # ERP status-callback webhook (`POST /api/erp/webhook/{erp_type}`, public,
    # HMAC-gated). Hard cap on the body the route buffers before JSON-parsing —
    # a signed-but-oversized POST would otherwise be read fully into memory
    # before the signature check ever runs (memory-exhaustion DoS on a public
    # route). ERP status payloads are small JSON; a few-MB ceiling never
    # truncates a real one.
    erp_webhook_max_bytes: int = 4 * 1024 * 1024

    # Email-intake inbound webhook (`POST /api/email-intake/inbound/{provider}`,
    # public, HMAC-gated). Same memory-exhaustion guard as the ERP webhook above
    # — email provider payloads (incl. base64 attachments) can legitimately run
    # larger, but a few-MB ceiling still rejects an unbounded POST before it's
    # buffered / parsed.
    email_intake_max_bytes: int = 4 * 1024 * 1024

    # Card-provider webhook (`POST /api/cards/webhook/{provider}`, public — HMAC
    # is verified only after the owning tenant is identified from the body).
    # Same memory-exhaustion guard as the ERP webhook above: bound the body
    # before it's buffered at all. Lithic/Nium settlement payloads are a few
    # KB; a few-MB ceiling never truncates a real one.
    card_webhook_max_bytes: int = 4 * 1024 * 1024

    # Payment-processor webhook (`POST /api/payments/webhook/{tenant_slug}/
    # {provider}`, public, HMAC verified inside the adapter's `parse_webhook`).
    # Same memory-exhaustion guard — bound the body before it's buffered, ahead
    # of the mock-provider check and tenant/HMAC resolution. Processor status
    # payloads are small JSON; a few-MB ceiling never truncates a real one.
    payment_webhook_max_bytes: int = 4 * 1024 * 1024

    # Punch-out catalogs (live cXML/OCI round-trips). Local-first: the in-process
    # `mock` adapter is the default so `pnpm dev` runs the whole punch-out flow
    # (setup → start URL → returned cart → convert-to-requisition) without an
    # external supplier or credential. Per-org overrides live on
    # ``Organization.settings.punchout.{provider,shared_secret,buyer_identity,...}``
    # and win over these process-level defaults. The cXML supplier shared secret
    # has NO hardcoded fallback — empty here, real value via sops in deployed.
    punchout_provider: str = "mock"  # "mock" (in-process default) | "cxml"
    punchout_shared_secret: str = ""  # cXML supplier credential — sops in deployed
    # HMAC-SHA256 key the supplier signs the PunchOutOrderMessage cart-return POST
    # with. The public return endpoint verifies it (the cart return is
    # public-by-design — no JWT). NO hardcoded secret fallback; the committed
    # .env.development sets a NON-secret dev value so the return is locally
    # testable. In local debug with an empty secret the BuyerCookie match is the
    # sole gate (mirrors peppol_inbound's debug carve-out).
    punchout_return_signing_secret: str = ""
    # Hard cap on the cart-return webhook body the route buffers before parsing
    # (memory-exhaustion guard on the public route; cXML carts are tens of KB).
    punchout_return_max_bytes: int = 4 * 1024 * 1024

    # Digital signatures on approvals (SOX non-repudiation). The HMAC-SHA256
    # signing key over the canonical approval payload (invoice id + exact amount
    # + actor + decision + timestamp), persisted on the approval audit row's
    # `details.signature` block and re-verifiable via
    # GET /api/audit/invoice/{id}/verify-signatures. Empty by default → signing
    # is skipped (no hardcoded production fallback, mirroring the other HMAC
    # secrets); the committed .env.development sets a NON-secret dev value so the
    # feature is exercisable under `pnpm dev`; deployed envs set the real key via
    # sops. See backend/docs/approval-signatures.md.
    approval_signing_key: str = ""

    # Email approval — approve/reject an assigned invoice straight from the
    # review-assignment email, with no login. The link carries a signed,
    # expiring, single-action token (HMAC-SHA256 over tenant + invoice + actor +
    # action + expiry) that IS the credential; the action endpoint re-runs the
    # normal review approve/reject path as that reviewer (segregation, CFO gate,
    # thresholds, audit row, approval signature all still apply). Empty by
    # default → the feature is OFF: no links are added to emails and every token
    # is rejected (fail-closed, no hardcoded fallback, mirroring the other HMAC
    # secrets). The committed .env.development sets a NON-secret dev value so the
    # flow is exercisable under `pnpm dev` (console / Mailpit email); deployed
    # envs set the real key via sops. See backend/docs/email-approval.md.
    email_action_signing_key: str = ""
    # Validity window of an email-approval link, in hours (default 7 days). A
    # reviewer who acts after this re-authenticates in the app instead. This TTL
    # is shared by the Slack approval-button token (same primitive).
    email_action_ttl_hours: int = 168

    # Slack interactive approval — the signing secret Slack signs every
    # interactivity POST with (`X-Slack-Signature` = v0=HMAC-SHA256 over
    # `v0:{X-Slack-Request-Timestamp}:{raw_body}`). Empty default → the feature
    # is OFF: the inbound `/api/approvals/slack/interactivity` webhook rejects
    # every request (fail-closed, no hardcoded fallback, mirroring the other
    # webhook HMAC secrets). The committed .env.development sets a NON-secret dev
    # value so the flow is exercisable in tests; deployed envs set the real
    # Slack app signing secret via sops. The action token carried in the button
    # value reuses `email_action_signing_key` (the `slack`-channel binding keeps
    # it distinct from the email link). See backend/docs/slack-approval.md.
    slack_signing_secret: str = ""
    # Reject a Slack interactivity POST whose `X-Slack-Request-Timestamp` is more
    # than this many seconds from now (replay-window guard; Slack's own
    # recommendation is 5 minutes).
    slack_request_max_age_seconds: int = 300

    # Microsoft Teams interactive approval — the **security token** Teams signs
    # every Outgoing-Webhook POST with. Teams computes
    # `HMAC-SHA256(base64-decode(security_token), raw_body)` and sends it
    # base64-encoded as `Authorization: HMAC <base64-digest>`. We rebuild that and
    # compare constant-time. Empty default → the feature is OFF: the inbound
    # `/api/approvals/teams/interactivity` webhook rejects every request (fail-
    # closed, no hardcoded fallback, mirroring the Slack secret). The committed
    # .env.development sets a NON-secret base64 dev value so the flow is
    # exercisable in tests; deployed envs set the real Teams Outgoing-Webhook
    # security token via sops. The action token carried in the card button reuses
    # `email_action_signing_key` (the `teams`-channel binding keeps it distinct
    # from the email link and the Slack button). See backend/docs/teams-approval.md.
    teams_security_token: str = ""
    # Reject a Teams interactivity POST whose `X-Teams-Request-Timestamp` is more
    # than this many seconds from now (replay-window guard). Teams does not always
    # send a timestamp header; when absent we fall back to the single-use action
    # token jti + the workflow state machine for replay protection.
    teams_request_max_age_seconds: int = 300

    # Partner / reseller link codes (white-label two-sided-consent attach). The
    # HMAC-SHA256 key the platform signs a partner *link code* with: the
    # prospective CHILD tenant's admin mints a short-lived code (proof of
    # consent), and the PARTNER's admin redeems it to attach the child
    # (`POST /api/partner/children`). The signature is what makes attach safe —
    # a partner can't forge a code or point it at an org that didn't consent.
    # Empty default → the feature is OFF: no link code can be minted and every
    # redeem is rejected (fail-closed, no hardcoded fallback, mirroring the
    # other HMAC secrets). The key's PRESENCE is the single on/off knob. The
    # committed .env.development sets a NON-secret dev value so the flow is
    # exercisable under `pnpm dev` / tests; deployed envs set the real key via
    # sops. See docs/white-label.md § Partner / reseller admin.
    partner_link_signing_key: str = ""
    # Validity window of a partner link code, in minutes (default 30). Short by
    # design: a link code is a one-shot, immediately-redeemed handshake between
    # the child's admin and the partner's admin, not a long-lived invite.
    partner_link_ttl_minutes: int = 30

    # Retention policies (SOX records management). The enforcement sweep is a
    # long-lived loop (like contract renewal / qms sync) that finds records past
    # their configured retention window and archives them via a privileged,
    # audited path. Per-record-class retention periods live on
    # `Organization.settings.retention` (configurable, not hardcoded), read +
    # updated via GET/PUT /api/retention-policy. Disabled by default so local
    # dev / tests don't run a background sweep; flip FEOH_RETENTION_ENABLED on in
    # deployed envs. CRITICAL: the sweep composes with the audit-immutability
    # trigger — `audit_log` rows are NEVER deleted; "retention" for the audit
    # class means verifying WORM-shipment and recording a manifest. See
    # backend/docs/retention.md.
    retention_enabled: bool = False
    retention_interval_seconds: int = 86400
    # Per-tenant, per-tick cap on invoices soft-archived by the retention sweep.
    # The candidate query excludes already-archived rows in SQL and orders
    # oldest-first, so a capped tick makes strict forward progress and the next
    # tick resumes where it stopped. Without the cap the sweep loaded EVERY
    # terminal invoice past the window on every tick — a set that only grows —
    # and packed each id into one audit row; at a few million rows that is an
    # OOM-scale tick plus a JSONB manifest large enough to head-of-line the
    # audit shipper's 500-row batch (CloudWatch PutLogEvents caps at 1 MB).
    # Same purpose + shape as `recurring_invoices_max_per_sweep`.
    retention_batch_size: int = 500
    # Platform-default retention windows (months) when an org sets no per-class
    # override in `Organization.settings.retention`. 84 months = 7 years, the
    # common SOX / IRS records-retention baseline.
    retention_default_months: int = 84

    # Data residency (GDPR/CCPA) — the region THIS stack is deployed in, as
    # declared by the operator. A fact about the deployment, not about any
    # tenant (a tenant's *configured* region lives on
    # `Organization.settings.residency.region`), which is why it is env rather
    # than settings-JSON. One of `SUPPORTED_REGIONS` in
    # `services/data_residency.py` (`us`/`eu`/`uk`/`ca`/`au`).
    #
    # Empty is the honest default and means "unknown / cannot attest": the
    # alignment surfaced on GET /api/organization/data-residency reports
    # `status: "unknown"` with `aligned: null`, never a reassuring `true`
    # nobody verified. It is deliberately NOT validated at boot — the value is
    # advisory (nothing routes or blocks on it), and refusing to start over an
    # advisory field would trade a wrong answer for an outage; an unrecognised
    # value reports `unknown` with a reason instead. See docs/data-residency.md.
    deployed_region: str = ""

    # App
    # Default is `False` so a deploy that forgets to set `FEOH_DEBUG` does not
    # ship FastAPI tracebacks (internal paths, env names) to clients. Local
    # dev sets `FEOH_DEBUG=true` in `.env`.
    debug: bool = False

    @property
    def is_deployed(self) -> bool:
        """True for any deployed (non local-dev / non-CI) environment."""
        return self.environment.strip().lower() not in _NON_DEPLOYED_ENVS

    @model_validator(mode="after")
    def _validate_extraction_provider(self) -> "Settings":
        # Empty is the documented "derive from the key" default; anything else
        # must name a registered adapter. See `_EXTRACTION_PROVIDERS`.
        name = self.extraction_provider.strip()
        if name and name not in _EXTRACTION_PROVIDERS:
            raise ValueError(
                f"FEOH_EXTRACTION_PROVIDER={name!r} is not a registered extraction "
                f"adapter (one of: {', '.join(sorted(_EXTRACTION_PROVIDERS))}); leave it "
                "empty to derive the provider from FEOH_ANTHROPIC_API_KEY."
            )
        return self

    @model_validator(mode="after")
    def _require_captcha_in_deployed_envs(self) -> "Settings":
        # Fail fast at boot rather than silently shipping signup with captcha
        # disabled — a 'fail open' captcha is an abuse hole on a public,
        # tenant-creating endpoint.
        if self.is_deployed and not self.hcaptcha_secret:
            raise ValueError(
                "FEOH_HCAPTCHA_SECRET must be set when FEOH_ENVIRONMENT is a deployed "
                f"environment ({self.environment!r}); refusing to boot with captcha "
                "verification disabled on the public signup endpoint."
            )
        return self

    @model_validator(mode="after")
    def _require_real_secret_key_in_deployed_envs(self) -> "Settings":
        # The JWT signing key is the root of the whole auth system. The default
        # `change-me-in-production` is in the public repo, so a deployed instance
        # that never set FEOH_SECRET_KEY would let anyone forge a token for any
        # user/org with HS256 + the known string. Refuse to boot rather than ship
        # that silently (mirrors the captcha guard above). A too-short key is
        # likewise rejected — HS256 wants at least 256 bits of entropy.
        if self.is_deployed and (
            self.secret_key == "change-me-in-production" or len(self.secret_key) < 32
        ):
            raise ValueError(
                "FEOH_SECRET_KEY must be set to a cryptographically random value of "
                f"at least 32 chars when FEOH_ENVIRONMENT is deployed ({self.environment!r}); "
                "refusing to boot with the default / weak JWT signing key."
            )
        return self

    @model_validator(mode="after")
    def _require_live_card_rails_in_deployed_envs(self) -> "Settings":
        # A deployed env that ships real platform card credentials but leaves the
        # sandbox flag True routes every live call to the provider's sandbox host:
        # cards "issue" fine but vendors can't charge them — revenue lost silently
        # with no error. `*_sandbox` defaults to True, so this is the easy miss.
        # Refuse to boot rather than ship a card program pointed at the void.
        if not self.is_deployed:
            return self
        if self.lithic_api_key and self.lithic_sandbox:
            raise ValueError(
                "FEOH_LITHIC_SANDBOX must be false when FEOH_LITHIC_API_KEY is set in a "
                f"deployed environment ({self.environment!r}); refusing to boot a live "
                "card program pointed at the Lithic sandbox host."
            )
        if (self.nium_client_id or self.nium_client_secret) and self.nium_sandbox:
            raise ValueError(
                "FEOH_NIUM_SANDBOX must be false when Nium credentials are set in a "
                f"deployed environment ({self.environment!r}); refusing to boot a live "
                "card program pointed at the Nium sandbox host."
            )
        return self


settings = Settings()
