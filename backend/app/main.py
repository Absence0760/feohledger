import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import (
    access_reviews,
    adaptive_workflows,
    admin,
    analytics,
    api_keys,
    assistant,
    audit,
    auth,
    auth_saml,
    auth_sso,
    bank_reconciliation,
    billing,
    billing_webhook,
    budgets,
    cards,
    cash_flow,
    catalogs,
    chat_notifications,
    contracts,
    credit_memos,
    dashboard,
    discounts,
    email_actions,
    email_intake,
    enrichment,
    entities,
    erp_webhook,
    exception_agents,
    exceptions,
    expense_cards,
    expense_policies,
    expense_preapprovals,
    expenses,
    gl_accounts,
    goods_receipts,
    health,
    inspections,
    intake,
    invoices,
    notifications,
    organization,
    partner,
    payments,
    peppol_inbound,
    portal,
    portal_auth,
    positive_pay,
    privacy,
    purchase_orders,
    recurring,
    reports,
    requisitions,
    retention,
    scheduled_reports,
    scim,
    signup,
    slack_approvals,
    tax,
    tax_intl,
    teams_approvals,
    vendor_risk,
    vendor_statement_recon,
    vendors,
    webhooks,
    workflow,
    workflow_definitions,
    workflow_experiments,
)
from app.api.v1 import router as public_v1_router
from app.api.v1_openapi import router as public_v1_openapi_router
from app.config import settings

# The stdlib root logger has no handler until something configures one —
# uvicorn's default log config only wires up its OWN "uvicorn"/"uvicorn.access"
# loggers, never root. Without this, every `logging.getLogger(__name__).info(...)`
# call anywhere under `app/` (the console email adapter's "email sent" dump,
# every background sweep's progress line, etc.) is silently dropped: INFO is
# below the root logger's default WARNING level, so it never reaches even
# Python's `logging.lastResort` fallback handler. This runs once per process
# (each uvicorn worker / reload subprocess re-imports this module fresh) and
# is a no-op for Lambda entry points, which never import `app.main`.
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast if the deploy left security-critical config at its insecure
    # defaults. Local dev sets `FEOH_DEBUG=true` to keep these guards as
    # warnings rather than crashes; every other environment must override.
    if not settings.debug:
        if settings.secret_key in ("", "change-me-in-production"):
            raise RuntimeError(
                "FEOH_SECRET_KEY must be set to a non-default value when FEOH_DEBUG=false"
            )
        if settings.email_intake_domain and not settings.email_intake_signing_secret:
            raise RuntimeError(
                "FEOH_EMAIL_INTAKE_SIGNING_SECRET must be set when "
                "FEOH_EMAIL_INTAKE_DOMAIN is configured"
            )
        if settings.peppol_inbound_enabled and not settings.peppol_inbound_signing_secret:
            raise RuntimeError(
                "FEOH_PEPPOL_INBOUND_SIGNING_SECRET must be set when "
                "FEOH_PEPPOL_INBOUND_ENABLED is true"
            )
        if settings.punchout_provider != "mock" and not settings.punchout_return_signing_secret:
            raise RuntimeError(
                "FEOH_PUNCHOUT_RETURN_SIGNING_SECRET must be set when "
                "FEOH_PUNCHOUT_PROVIDER is not 'mock'"
            )
        if settings.billing_webhook_enabled:
            from app.services.billing_adapters import list_available_providers as _billing_providers

            # Two ways to end up serving the fixture adapter on a PUBLIC route,
            # and the equality check only ever caught the first.
            #
            # `MockBillingAdapter.parse_webhook` verifies no signature — it just
            # json.loads the body — so whichever way it is reached,
            # `POST /api/billing/webhook/{provider}` becomes an unauthenticated
            # subscription-lifecycle mutator.
            #
            # 1. The name is literally "mock".
            # 2. The name is a TYPO. `get_billing_adapter` falls back to `mock`
            #    for any unregistered name, and the route's own
            #    `provider != settings.billing_provider` guard compares the URL
            #    segment to the setting, not to the registry — so
            #    FEOH_BILLING_PROVIDER=stripe (the registered name is
            #    `stripe_billing`) satisfied every check and served the mock.
            #
            # This is §26's boot-time allowlist and the audit-shipping guard
            # below, applied to the one other env-sourced provider name whose
            # fallback reaches a public route. See `docs/decisions.md` §29.
            if settings.billing_provider == "mock":
                raise RuntimeError(
                    "FEOH_BILLING_PROVIDER must not be 'mock' when FEOH_BILLING_WEBHOOK_ENABLED "
                    "is true — the mock adapter's parse_webhook performs no signature "
                    "verification, so serving it publicly would accept unauthenticated events"
                )
            if settings.billing_provider not in _billing_providers():
                raise RuntimeError(
                    f"FEOH_BILLING_PROVIDER names no registered adapter "
                    f"({settings.billing_provider!r}) while FEOH_BILLING_WEBHOOK_ENABLED is "
                    f"true — registered providers: {_billing_providers()}. An unregistered "
                    "name silently resolves to the mock adapter, whose parse_webhook performs "
                    "no signature verification, so the public webhook route would accept "
                    "unauthenticated subscription events"
                )
        if settings.webhooks_allow_private_targets:
            raise RuntimeError(
                "FEOH_WEBHOOKS_ALLOW_PRIVATE_TARGETS must not be true when FEOH_DEBUG=false — "
                "it disables the outbound-webhook SSRF guard, letting a tenant admin point "
                "signed webhook deliveries at loopback/private/metadata addresses"
            )
        if settings.audit_shipping_enabled:
            from app.services.audit_log_shipper import _build_adapters, _parse_providers
            from app.services.audit_shipping.dispatcher import list_available_providers

            configured = _parse_providers(settings.audit_shipping_providers)
            unknown = sorted(set(configured) - set(list_available_providers()))
            if unknown:
                raise RuntimeError(
                    f"FEOH_AUDIT_SHIPPING_PROVIDERS names unregistered adapter(s) {unknown} — "
                    f"registered providers: {list_available_providers()}. A typo'd name "
                    "would otherwise silently fall back to the no-op mock adapter and mark "
                    "rows shipped while nothing reaches the real sink."
                )
            # Probe every configured sink before serving. `test_connection()` is
            # where the real WORM checks live — `s3_objectlock` verifies the
            # bucket actually HAS Object Lock enabled (it can only be turned on
            # at bucket-creation time, so a missing config is unfixable at
            # runtime). Nothing called it, so a bucket without Object Lock
            # accepted every `put_object`, the shipper stamped `shipped_at`, and
            # `retention_sweep` then reported `audit_rows_overdue_unshipped: 0`
            # — SOC 2 evidence reading green with no WORM guarantee behind it.
            # Refusing to boot mirrors the unknown-provider guard above: a sink
            # that cannot hold the evidence is a deploy error, not a warning.
            for adapter in _build_adapters():
                if not await adapter.test_connection():
                    raise RuntimeError(
                        f"Audit-shipping sink '{adapter.provider_name}' failed its startup "
                        "test_connection() — refusing to start. Shipping to an unverified "
                        "sink marks audit rows shipped while the WORM/tamper-evidence "
                        "guarantee they are shipped FOR does not exist. Check "
                        "FEOH_AUDIT_SHIPPING_* config (for s3_objectlock: the bucket must "
                        "exist and have Object Lock enabled — it cannot be enabled after "
                        "bucket creation)."
                    )

    # Background reaper for invoices stuck in `pending` extraction. Started
    # on app boot, cancelled cleanly on shutdown. Toggleable via
    # FEOH_EXTRACTION_REAPER_ENABLED so tests / one-shot CLI runs can disable it.
    import asyncio

    from app.services.approval_escalation import run_escalation_loop
    from app.services.audit_log_shipper import run_shipper_loop
    from app.services.billing.dunning_sweep import run_dunning_loop
    from app.services.cash_flow_alerts import run_shortfall_alerts_loop
    from app.services.contract_renewal import run_renewal_loop
    from app.services.discount_auto_trigger import run_discount_optimization_loop
    from app.services.extraction_reaper import run_reaper_loop
    from app.services.payment_reconciler import run_reconciler_loop
    from app.services.qms_sync import run_qms_sync_loop
    from app.services.recurring_invoices import run_recurring_invoices_loop
    from app.services.retention_sweep import run_retention_loop
    from app.services.scheduled_reports import run_scheduled_reports_loop
    from app.services.sweep_health import supervise_task
    from app.services.vendor_rescreen import run_vendor_rescreen_loop
    from app.services.webhooks.delivery import run_webhook_delivery_loop

    def start_sweep(coro, name: str) -> asyncio.Task:
        """Start a supervised sweep task.

        `supervise_task` attaches the done-callback that records a sweep ending
        — cleanly cancelled at shutdown, or *died*, which is a real defect: a
        loop that ends on its own is gone for the lifetime of the process and
        nothing else would say so. The task's own `name` is the key
        `services/sweep_health` files it under, and the same string is what
        `GET /api/health/sweeps` reports, so the two can't drift
        (`tests/test_sweep_health.py` AST-scans this file to pin it).
        """
        return supervise_task(asyncio.create_task(coro, name=name))

    reaper_task: asyncio.Task | None = None
    shipper_task: asyncio.Task | None = None
    escalation_task: asyncio.Task | None = None
    reconciler_task: asyncio.Task | None = None
    renewal_task: asyncio.Task | None = None
    rescreen_task: asyncio.Task | None = None
    discount_task: asyncio.Task | None = None
    qms_task: asyncio.Task | None = None
    retention_task: asyncio.Task | None = None
    recurring_task: asyncio.Task | None = None
    webhooks_task: asyncio.Task | None = None
    dunning_task: asyncio.Task | None = None
    scheduled_reports_task: asyncio.Task | None = None
    shortfall_alerts_task: asyncio.Task | None = None
    if settings.extraction_reaper_enabled:
        reaper_task = start_sweep(run_reaper_loop(), name="extraction-reaper")
    # Centralized audit-log shipper (SOC 2). Disabled by default so local
    # dev doesn't spin up AWS clients; flip FEOH_AUDIT_SHIPPING_ENABLED on in
    # deployed envs.
    if settings.audit_shipping_enabled:
        shipper_task = start_sweep(run_shipper_loop(), name="audit-log-shipper")
    if settings.approval_escalation_enabled:
        escalation_task = start_sweep(run_escalation_loop(), name="approval-escalation")
    if settings.payment_reconcile_enabled:
        reconciler_task = start_sweep(run_reconciler_loop(), name="payment-reconciler")
    # Contract renewal-alert sweep. Disabled by default; flip
    # FEOH_CONTRACT_RENEWAL_ENABLED on in deployed envs.
    if settings.contract_renewal_enabled:
        renewal_task = start_sweep(run_renewal_loop(), name="contract-renewal")
    # Periodic vendor sanctions re-screening sweep. Disabled by default;
    # flip FEOH_VENDOR_RESCREEN_ENABLED on in deployed envs.
    if settings.vendor_rescreen_enabled:
        rescreen_task = start_sweep(run_vendor_rescreen_loop(), name="vendor-rescreen")
    # Dynamic-discounting auto-capture sweep. Disabled by default; flip
    # FEOH_DISCOUNT_OPTIMIZATION_ENABLED on in deployed envs. Only accepts
    # high-ROI offers — never moves money (see discount_auto_trigger).
    if settings.discount_optimization_enabled:
        discount_task = start_sweep(run_discount_optimization_loop(), name="discount-auto-trigger")
    # QMS inspection sync. Disabled by default; flip FEOH_QMS_SYNC_ENABLED on in
    # deployed envs once a real QMS is configured per-org. Pulls inspection
    # records into the quality_inspections table (4-way-match leg).
    if settings.qms_sync_enabled:
        qms_task = start_sweep(run_qms_sync_loop(), name="qms-sync")
    # Retention-policy enforcement sweep (SOX records management). Disabled by
    # default; flip FEOH_RETENTION_ENABLED on in deployed envs. Soft-archives
    # overdue terminal invoices + verifies audit-log WORM shipment; NEVER
    # deletes audit_log rows (composes with the immutability trigger).
    if settings.retention_enabled:
        retention_task = start_sweep(run_retention_loop(), name="retention-sweep")
    # Recurring / subscription invoice generation sweep. Disabled by default;
    # flip FEOH_RECURRING_INVOICES_ENABLED on in deployed envs. Only creates
    # pre-coded invoices in the approval queue — never moves money (see
    # recurring_invoices).
    if settings.recurring_invoices_enabled:
        recurring_task = start_sweep(run_recurring_invoices_loop(), name="recurring-invoices")
    # Outbound-webhook retry/delivery sweep. Disabled by default; flip
    # FEOH_WEBHOOKS_ENABLED on in deployed envs. The emit path delivers inline on
    # the running loop; this sweep is the durable retry backstop.
    if settings.webhooks_enabled:
        webhooks_task = start_sweep(run_webhook_delivery_loop(), name="webhook-delivery")
    # Billing dunning / past-due automation sweep. Disabled by default; flip
    # FEOH_BILLING_DUNNING_ENABLED on in deployed envs. Only cancels subscriptions
    # overdue past the grace window — never moves money (see dunning_sweep).
    if settings.billing_dunning_enabled:
        dunning_task = start_sweep(run_dunning_loop(), name="billing-dunning")
    # Scheduled-report runner. Disabled by default so local dev / tests never
    # email reports; flip FEOH_SCHEDULED_REPORTS_ENABLED on in deployed envs.
    if settings.scheduled_reports_enabled:
        scheduled_reports_task = start_sweep(run_scheduled_reports_loop(), name="scheduled-reports")
    # Projected cash-shortfall alerts. Disabled by default so local dev / tests
    # never email a CFO; flip FEOH_CASHFLOW_SHORTFALL_ALERTS_ENABLED on in
    # deployed envs. Only reads the cash forecast and notifies — never moves
    # money (see cash_flow_alerts).
    if settings.cashflow_shortfall_alerts_enabled:
        shortfall_alerts_task = start_sweep(
            run_shortfall_alerts_loop(), name="cashflow-shortfall-alerts"
        )

    try:
        yield
    finally:
        for task in (
            reaper_task,
            shipper_task,
            escalation_task,
            reconciler_task,
            renewal_task,
            rescreen_task,
            discount_task,
            qms_task,
            retention_task,
            recurring_task,
            webhooks_task,
            dunning_task,
            scheduled_reports_task,
            shortfall_alerts_task,
        ):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Flush queued auth audit rows before the engines go away.
        # `queue_auth_audit` deliberately does NOT await its write — that is
        # what stops `/api/auth/login` leaking account existence through a
        # tenant-DB round trip only a known address pays for (see
        # `docs/authentication.md` § The failure row is written off the response
        # path). The cost of not waiting is a window in which a stopping process
        # could drop the row, and this is the rung that closes it for a CLEAN
        # shutdown. Bounded — a hung write costs the shutdown a known five
        # seconds and is then abandoned loudly, rather than blocking it until an
        # orchestrator SIGKILLs us and every remaining row is lost, not just the
        # stuck one. Must run BEFORE `dispose_all_engines`: the write needs the
        # tenant engine it is about to commit through.
        from app.services.audit_dispatch import (
            AUTH_AUDIT_DRAIN_TIMEOUT_SECONDS,
            drain_auth_audits,
        )

        await drain_auth_audits(timeout=AUTH_AUDIT_DRAIN_TIMEOUT_SECONDS)

        from app.database import dispose_all_engines

        await dispose_all_engines()


app = FastAPI(
    title="FeohLedger API",
    version="0.1.0",
    lifespan=lifespan,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach SOC 2 tablestakes security headers to every response.

    - `Strict-Transport-Security` (HSTS) is gated on `FEOH_HSTS_ENABLED` so the
      local HTTP dev server doesn't accidentally pin `localhost` to HTTPS in
      developer browsers. Flip the flag on in deployed envs.
    - The other three headers have no HTTP/HTTPS dependency and are always
      set — auditors look for them alongside HSTS and they cost nothing.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if settings.hsts_enabled:
            parts = [f"max-age={settings.hsts_max_age}"]
            if settings.hsts_include_subdomains:
                parts.append("includeSubDomains")
            if settings.hsts_preload:
                parts.append("preload")
            response.headers["Strict-Transport-Security"] = "; ".join(parts)

        # Always-on tablestakes headers.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # CSP locks every API response to its own origin. The SPA frontend is
        # a separate static origin so it doesn't read these headers — but a
        # successful XSS that opened the API origin in an iframe or via a
        # popup would still be defanged because the API origin can't load
        # third-party script. Tune in deployed envs if the API needs to
        # serve any non-JSON content.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)


def _build_cors_origin_regex() -> str:
    """Compose the allow_origin_regex from settings.

    Local dev always gets ``localhost`` (any subdomain + port). Production
    domains come from ``FEOH_CORS_PRODUCTION_DOMAIN`` (comma-separated for
    multi-domain deploys). Empty domain → only localhost matches, which
    is the right default for local boot.
    """
    import re as _re

    parts = [r"localhost(:\d+)?"]
    raw = (settings.cors_production_domain or "").strip()
    for domain in (d.strip() for d in raw.split(",") if d.strip()):
        parts.append(_re.escape(domain))
    return r"https?://([\w-]+\.)?(" + "|".join(parts) + ")"


# CORS — `cors_origins` is the exact-match allowlist (the dev frontend);
# the regex below covers tenant subdomains under localhost + any configured
# production domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=_build_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(access_reviews.router, prefix="/api")
app.include_router(adaptive_workflows.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(api_keys.router, prefix="/api")
# Public versioned programmatic surface (X-API-Key auth) — becomes /api/v1/...
app.include_router(public_v1_router, prefix="/api")
# Published OpenAPI spec + Swagger UI for the public /api/v1 surface only
# (GET /api/v1/openapi.json, GET /api/v1/docs). Both respect FEOH_PUBLIC_API_ENABLED.
app.include_router(public_v1_openapi_router, prefix="/api")
app.include_router(audit.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(cards.router, prefix="/api")
app.include_router(cash_flow.router, prefix="/api")
app.include_router(contracts.router, prefix="/api")
app.include_router(credit_memos.router, prefix="/api")
app.include_router(discounts.router, prefix="/api")
app.include_router(recurring.router, prefix="/api")
app.include_router(reports.router, prefix="/api")
# Mounted at /api/analytics/scheduled-reports. Registered separately from
# analytics.router (which has no top-level path-param route, so there is no
# shadowing) to keep the CRUD surface out of that 1700-line module.
app.include_router(scheduled_reports.router, prefix="/api")
app.include_router(enrichment.router, prefix="/api")
app.include_router(entities.router, prefix="/api")
app.include_router(erp_webhook.router, prefix="/api")
# Registered BEFORE exceptions.router so the literal /exceptions/agent-* collection
# routes win over exceptions.py's parameterised /{exception_id}/... matcher.
app.include_router(exception_agents.router, prefix="/api")
app.include_router(exceptions.router, prefix="/api")
app.include_router(expenses.router, prefix="/api")
app.include_router(expenses.reports_router, prefix="/api")
app.include_router(expense_cards.router, prefix="/api")
app.include_router(expense_policies.router, prefix="/api")
app.include_router(expense_preapprovals.router, prefix="/api")
app.include_router(gl_accounts.router, prefix="/api")
app.include_router(goods_receipts.router, prefix="/api")
app.include_router(inspections.router, prefix="/api")
app.include_router(invoices.router, prefix="/api")
app.include_router(email_actions.public_router, prefix="/api")
app.include_router(notifications.router, prefix="/api")
app.include_router(purchase_orders.router, prefix="/api")
app.include_router(requisitions.router, prefix="/api")
app.include_router(retention.router, prefix="/api")
app.include_router(catalogs.router, prefix="/api")
app.include_router(budgets.router, prefix="/api")
app.include_router(intake.router, prefix="/api")
app.include_router(vendors.router, prefix="/api")
app.include_router(vendor_risk.router, prefix="/api")
app.include_router(vendor_statement_recon.router, prefix="/api")
app.include_router(bank_reconciliation.router, prefix="/api")
app.include_router(positive_pay.router, prefix="/api")
app.include_router(privacy.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(assistant.router, prefix="/api")
app.include_router(organization.router, prefix="/api")
# Hangs off /api/organization/chat-notifications (its own module — see
# api/chat_notifications.py; same shape as email_intake.admin_router).
app.include_router(chat_notifications.router, prefix="/api")
app.include_router(partner.router, prefix="/api")
app.include_router(payments.router, prefix="/api")
app.include_router(signup.router, prefix="/api")
app.include_router(auth_sso.router, prefix="/api")
app.include_router(auth_saml.router, prefix="/api")
app.include_router(billing.router, prefix="/api")
app.include_router(billing_webhook.public_router, prefix="/api")
app.include_router(webhooks.router, prefix="/api")
app.include_router(scim.router, prefix="/api")
app.include_router(workflow.router, prefix="/api")
app.include_router(workflow_definitions.router, prefix="/api")
app.include_router(workflow_experiments.router, prefix="/api")
app.include_router(portal_auth.router, prefix="/api")
app.include_router(portal.router, prefix="/api")
app.include_router(email_intake.public_router, prefix="/api")
app.include_router(email_intake.admin_router, prefix="/api")
app.include_router(peppol_inbound.public_router, prefix="/api")
app.include_router(slack_approvals.public_router, prefix="/api")
app.include_router(teams_approvals.public_router, prefix="/api")
app.include_router(catalogs.public_router, prefix="/api")
app.include_router(tax.router, prefix="/api")
app.include_router(tax_intl.router, prefix="/api")
# GET /api/health (public liveness probe, unchanged) + GET /api/health/sweeps
# (admin-gated background-sweep report). See app/api/health.py.
app.include_router(health.router, prefix="/api")


@app.get("/api/public-config")
async def public_config():
    """Non-secret config exposed to the frontend (e.g., captcha sitekey,
    tenant URL shape used by the signup form and other non-tenant pages).

    ``signup_enabled`` lets `/signup` render "signup is closed" rather than a
    form every submit of which would 404 (`FEOH_SIGNUP_ENABLED`). Publishing it
    leaks nothing: a closed deployment's signup routes already answer 404."""
    return {
        "hcaptcha_sitekey": settings.hcaptcha_sitekey,
        "tenant_url_template": settings.tenant_url_template,
        "signup_enabled": settings.signup_enabled,
    }
