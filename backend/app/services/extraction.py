"""AI invoice extraction service.

Uses the extraction adapter pattern — supports platform (Claude Vision) and BYOK
(OpenAI, Textract, or customer's own Anthropic key) modes.

Tracks usage for billing when platform mode is used.
"""

import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.invoice import Invoice, InvoiceExtractionResult, InvoiceLineItem, InvoiceStatus
from app.models.usage import ExtractionUsage
from app.services.decimal_convention import (
    AmountConvention,
    apply_convention,
    detect_convention,
)
from app.services.extraction_adapters.base import ExtractionResult
from app.services.invoice_warning_catalog import warning
from app.services.workflow_engine import (
    advance_workflow,
    get_workflow_instance,
    transition_invoice,
)
from app.utils.dates import parse_ambiguous_date, resolve_day_first_preference, utc_today

logger = logging.getLogger(__name__)


def _detect_structured_format(file_bytes: bytes, file_key: str) -> str | None:
    """Return the detected structured e-invoice format value, or None.

    Pure, no network. When a file is a UBL / CII / Factur-X document we route
    it to the deterministic ``einvoice`` adapter instead of the org's
    configured vision adapter. A plain scanned PDF (or anything unstructured)
    returns None and falls through to vision/OCR unchanged.
    """
    from app.services.e_invoice import DetectedFormat, detect_format

    # filename ext (xml) helps the XML sniff when the mime is generic.
    fmt = detect_format(file_bytes, mime_type=None, filename=file_key)
    return None if fmt is DetectedFormat.NONE else fmt.value


# Why platform mode landed on the provider it did. Recorded on the resolved
# config (`platform_provider_reason`) and logged, so `mock` output is never
# mistaken for a real read — see `backend/docs/ai-extraction.md`.
PLATFORM_REASON_CONFIGURED = "configured"
PLATFORM_REASON_PLATFORM_KEY = "platform_key"
PLATFORM_REASON_NO_KEY_LOCAL = "no_platform_key_local"
PLATFORM_REASON_NO_KEY_DEPLOYED = "no_platform_key_deployed"

# The offline stand-in a keyless dev box falls back to. Its `extract_statement`
# reads the document's real text layer; its `extract` returns a FIXTURE, which
# is why rule 4 below refuses this fallback in a deployed environment.
PLATFORM_OFFLINE_PROVIDER = "mock"
PLATFORM_DEFAULT_PROVIDER = "claude_vision"


def resolve_platform_provider(
    *, configured: str | None, platform_key: str | None, is_deployed: bool
) -> tuple[str, str]:
    """Pick the adapter PLATFORM-mode extraction runs on, and say why. Pure.

    Precedence, highest first:

    1. ``configured`` (``FEOH_EXTRACTION_PROVIDER``) — an operator naming the
       provider explicitly wins over everything, key present or not.
    2. a configured platform key → ``claude_vision``. **This is the deployed
       path and it is unchanged**: an env that has a key behaves exactly as
       before this function existed.
    3. no key, NOT a deployed environment → ``mock``. The offline reader is
       what makes the whole extraction path exercisable on a laptop with no
       cloud account (guard rail 7); without this a fresh clone POSTs to
       ``api.anthropic.com`` with an empty key and gets ``provider_error``.
    4. no key, DEPLOYED environment → ``claude_vision`` anyway. Deliberately
       NOT ``mock``: `MockExtractionAdapter.extract` returns a fabricated
       invoice ("Extracted Vendor Inc", 1500.00), so falling back there would
       turn a missing credential into invented invoice data on a real tenant's
       document — strictly worse than the loud provider error a keyless
       ``claude_vision`` call produces. The reason code carries the diagnosis.

    Returns ``(provider, reason)``; the reason is one of the ``PLATFORM_REASON_*``
    codes, PII-free and safe to log or persist.
    """
    explicit = (configured or "").strip()
    if explicit:
        return explicit, PLATFORM_REASON_CONFIGURED
    if (platform_key or "").strip():
        return PLATFORM_DEFAULT_PROVIDER, PLATFORM_REASON_PLATFORM_KEY
    if is_deployed:
        return PLATFORM_DEFAULT_PROVIDER, PLATFORM_REASON_NO_KEY_DEPLOYED
    return PLATFORM_OFFLINE_PROVIDER, PLATFORM_REASON_NO_KEY_LOCAL


def _resolve_extraction_config(org_settings: dict | None, *, announce: bool = True) -> dict:
    """Build extraction adapter config based on org settings and program type.

    ``announce=False`` resolves silently. The failure path re-resolves purely to
    read `provider` / `program_type` for the `ExtractionUsage` row, and a second
    identical warning per failed extraction turns the fallback signal into noise
    — which is how a warning stops being read.
    """
    extraction = (org_settings or {}).get("extraction", {})
    program_type = extraction.get("program_type", "platform")

    if program_type == "byok":
        # A COPY: `run_extraction` writes this invoice's `gl_account_catalog`
        # and RAG `few_shot_prompt` into the config it gets back, and handing
        # out the org's own nested dict wrote them into `org_settings` — so a
        # caller reusing that dict for a second extraction sent the previous
        # invoice's chart (another entity's, possibly) and neighbours as this
        # one's hints whenever this one had none of its own to overwrite them.
        return dict(extraction)

    # Platform mode — use app-level keys
    provider, reason = resolve_platform_provider(
        configured=settings.extraction_provider,
        platform_key=settings.anthropic_api_key,
        is_deployed=settings.is_deployed,
    )
    if announce:
        if reason == PLATFORM_REASON_NO_KEY_LOCAL:
            # Loud on purpose: `mock` output must never be mistaken for a real read.
            logger.warning(
                "[extraction] No platform key configured — platform extraction is running "
                "OFFLINE on the '%s' adapter. Its invoice fields are a fixture, not a read "
                "of the document. Set FEOH_ANTHROPIC_API_KEY (or FEOH_EXTRACTION_PROVIDER) "
                "to use a real provider.",
                provider,
            )
        elif reason == PLATFORM_REASON_NO_KEY_DEPLOYED:
            logger.warning(
                "[extraction] No platform key configured in a DEPLOYED environment — "
                "staying on '%s', which will fail at the provider. Refusing to fall back to "
                "the offline adapter, whose invoice fields are fabricated.",
                provider,
            )
    return {
        "program_type": "platform",
        "provider": provider,
        "platform_provider_reason": reason,
        "api_key": settings.anthropic_api_key,
        "model": settings.extraction_model,
    }


def decide_auto_approve(
    ext_cfg: dict,
    approval_cfg: dict,
    *,
    overall_confidence: float,
    amount,
    aggregate_amount=None,
) -> bool:
    """Decide whether an extracted invoice may auto-approve, skipping human review.

    Two independent triggers (either fires it):
      1. Confidence — ``auto_approve_enabled`` and ``overall_confidence`` clears
         ``auto_approve_threshold`` (default 0.95). A score outside 0..1 counts
         as 0 — see the guard below.
      2. Amount floor — ``auto_approve_below`` set and the invoice amount is
         strictly below it (the "skip review for small invoices" rule).

    But a triggered auto-approve is REVOKED when the invoice would trip the same
    money-control gates a human approval enforces
    (``services/review._enforce_approval_thresholds``):
      * ``max_invoice_amount`` — a hard reject; an over-max invoice must never
        auto-approve.
      * ``require_cfo_above`` — demands a CFO; the ``system (auto-approve)`` actor
        is not a CFO.
    When either gate would trip, the invoice falls back to human review rather
    than auto-approving past the control. Amount is compared with ``Decimal`` so
    a boundary value isn't misjudged by a float cast. Pure — no IO.

    ``aggregate_amount`` is what those two gates are measured against: this
    invoice's amount PLUS the same vendor's other recent invoices
    (``services/structuring.vendor_recent_spend``), which is exactly what the
    human path compares. Evaluating them on this invoice alone left the
    structuring bypass wide open on the *unattended* path — split one payable
    into several under-threshold invoices and each auto-approves past the
    max-amount cap and the CFO gate with no human ever seeing it, which is
    strictly worse than the human hole the guard was added to close. The caller
    computes it (it holds the session); ``None`` falls back to ``amount``, so a
    call site with no vendor link or with structuring disabled keeps the
    single-invoice comparison.

    ``auto_approve_below`` deliberately stays on the SINGLE invoice amount: it is
    a "this document is too small to be worth a human's time" rule, not a spend
    control. Aggregating it would make the floor stop firing for any frequent
    vendor — turning a convenience knob into a second, silent threshold.

    ``amount`` and ``aggregate_amount`` are ``approval_chain.GateAmount``s from
    ``approval_chain.reporting_gate_amount`` — the figure expressed in the org's
    REPORTING currency, which is what all three thresholds here are denominated
    in. A ¥100,000 invoice must not be read as 100,000 of that currency. When a
    ``GateAmount`` reports ``expressible=False`` the amount could not be put
    into it, and every comparison fails **closed**: the floor does not fire, and
    the CFO / max gates do (via ``approval_chain._money_gate_applies``) — both
    directions send the invoice to a human. A bare number is still accepted and
    means "already in the gate currency", the correct reading for a
    single-currency tenant and what every pre-existing caller meant.
    """
    from app.services.approval_chain import _coerce_gate_amount

    amount_gate = _coerce_gate_amount(amount)
    gate_amount = amount_gate if aggregate_amount is None else _coerce_gate_amount(aggregate_amount)

    # A confidence outside 0..1 is not evidence of anything — it is an adapter
    # reporting on a scale we didn't ask for. Treating it as "very confident" is
    # how a model answering `3` where the contract says `0.5` averages its way
    # past the threshold and auto-approves an invoice nobody looked at. Adapters
    # normalise their own fields (`extraction_adapters.base.coerce_confidence`);
    # this is the gate's own guard, so a future adapter computing its overall
    # score some other way still can't trip it.
    confidence = overall_confidence if 0.0 <= overall_confidence <= 1.0 else 0.0

    # Every threshold below is read through `approval_chain`'s fail-CLOSED
    # parsers. `steps_config` is a JSONB blob and `POST /api/workflows/import`
    # accepts it free-form, so any of these can arrive non-numeric; a bare
    # `Decimal(str(...))` / a `>=` against a string raised out of this pure
    # function and landed the invoice in `failed`. Unusable here always means
    # "don't auto-approve" — the direction that sends the invoice to a human.
    from app.services.approval_chain import (
        cfo_gate_applies,
        finite_money_threshold,
        max_amount_gate_applies,
    )

    auto_approved = False
    confidence_threshold = _confidence_threshold(ext_cfg.get("auto_approve_threshold", 0.95))
    if (
        ext_cfg.get("auto_approve_enabled")
        and confidence_threshold is not None
        and confidence >= confidence_threshold
    ):
        auto_approved = True

    # An unusable floor is NOT a floor — it must not trigger auto-approve, and it
    # must not raise. (`finite_money_threshold` returns None for unset too, so
    # "no floor configured" and "floor misconfigured" converge on the same safe
    # behaviour: this trigger simply doesn't fire.)
    auto_below = finite_money_threshold(approval_cfg.get("auto_approve_below"))
    if auto_below is not None and not amount_gate.expressible:
        # A floor IS configured, but this invoice can't be expressed in the
        # currency the floor is denominated in. Comparing the bare numbers is
        # how a ¥1,000,000 invoice slips under a 5,000 floor. Fail closed: the
        # floor trigger doesn't fire and a human reviews it. (The CFO / max
        # gates below fail closed on the same condition, in the opposite
        # direction — they FIRE — because that is what sends the invoice to a
        # human there.)
        logger.warning(
            "[extraction] auto_approve_below is set but the invoice amount could not be "
            "expressed in %s; the amount floor does not apply (fail-closed, human review)",
            amount_gate.currency or "the org's reporting currency",
        )
    elif auto_below is not None and amount_gate.amount < auto_below:
        auto_approved = True

    if not auto_approved:
        return False

    # `cfo_gate_applies` / `max_amount_gate_applies` are the shared fail-CLOSED
    # threshold parses: a malformed `require_cfo_above` or `max_invoice_amount`
    # counts as gate-tripped, so a settings typo revokes auto-approve into human
    # review rather than raising (which would land the invoice in `failed`) or
    # slipping a gated amount past review.
    needs_cfo = cfo_gate_applies(approval_cfg.get("require_cfo_above"), gate_amount)
    exceeds_max = max_amount_gate_applies(approval_cfg.get("max_invoice_amount"), gate_amount)
    if exceeds_max or needs_cfo:
        return False
    return True


def _confidence_threshold(raw) -> float | None:
    """The extraction confidence bar as a usable float in ``0..1``, or ``None``.

    ``None`` means the configured value can't gate anything — unset, non-numeric
    (``0.99 >= "high"`` is a `TypeError`, not a decision), NaN, or outside the
    0..1 the score itself is normalised into. Callers treat that as "the
    confidence trigger does not fire", so a misconfigured bar sends the invoice
    to a human instead of raising or auto-approving on a comparison nobody
    meant."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.error(
            "[extraction] auto_approve_threshold is not a number (%r); the confidence "
            "auto-approve trigger is disabled for this workflow (fail-closed)",
            raw,
        )
        return None
    if not (0.0 <= value <= 1.0):
        logger.error(
            "[extraction] auto_approve_threshold %r is outside 0..1; the confidence "
            "auto-approve trigger is disabled for this workflow (fail-closed)",
            raw,
        )
        return None
    return value


async def resolve_gate_aggregate(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_settings: dict | None,
) -> Decimal:
    """Amount the money-control gates are measured against for ``invoice``.

    ``invoice.amount`` plus the same vendor's other recent spend, exactly as
    ``review._enforce_approval_thresholds`` computes it — the structuring guard.
    Falls back to the bare amount when the invoice has no vendor link or the org
    disabled ``structuring_enabled``. Never raises: the aggregate hardens a
    control, so a lookup failure must degrade to the single-invoice comparison
    (still gated) rather than break extraction.
    """
    amount = Decimal(str(invoice.amount or 0))
    vendor_id = getattr(invoice, "vendor_id", None)
    if vendor_id is None:
        return amount

    from app.services.structuring import get_structuring_config, vendor_recent_spend

    cfg = get_structuring_config(org_settings)
    if not cfg["enabled"]:
        return amount
    try:
        recent = await vendor_recent_spend(
            db,
            vendor_id=vendor_id,
            exclude_invoice_id=invoice.id,
            window_days=cfg["window_days"],
            currency=getattr(invoice, "currency", None) or "USD",
            entity_id=getattr(invoice, "entity_id", None),
        )
    except Exception:  # noqa: BLE001 — degrade to the single-invoice gate, never break
        logger.warning(
            "[extraction] structuring aggregate lookup failed for invoice %s; "
            "auto-approve gates fall back to the single-invoice amount",
            invoice.id,
        )
        return amount
    return amount + recent


async def run_extraction(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID | None = None,
    org_settings: dict | None = None,
    skip_vendor_match: bool = False,
    suppress_auto_approve: bool = False,
) -> None:
    """Extract invoice fields from the uploaded file and update the invoice.

    This is called as a background task after file upload.

    The ``ExtractionUsage`` billing meter is written through the TENANT session:
    ``extraction_usage`` is not in ``tenant_provisioning.CONTROL_TABLES``, no
    migration creates it in the control plane, and ``services/billing``'s
    ``rollup_usage`` reads it from the tenant DB — the same place the sibling
    meter ``card_rebates`` lives.

    Both keyword flags default to ``False`` — i.e. to the unchanged ingest
    behaviour — and exist for RE-extraction of an invoice that has already been
    triaged once (today: the supplier-portal resubmit of a rejected invoice,
    ``POST /api/portal/invoices/{id}/resubmit``):

    ``skip_vendor_match``
        Keep the payee the invoice is already bound to instead of re-resolving
        it from the corrected document. ``vendor_matching.match_and_link_vendor``
        is a fuzzy matcher: run again over a re-typed vendor name it can land
        ``Invoice.vendor_id`` on a DIFFERENT supplier, which (a) drops the
        invoice out of the ``vendor_id ==``-scoped supplier-portal list, so the
        vendor loses sight of their own resubmission, and (b) silently re-points
        the payee whose ``Vendor.bank_details`` the payment run reads. Also
        pins ``vendor_name``, because that is the field ``PATCH /api/invoices/{id}``
        re-resolves a stale link from — leaving it rewritten would just move the
        re-link to the reviewer's next save.

    ``suppress_auto_approve``
        Never auto-approve out of this pass. A resubmission is a document a human
        already REJECTED; letting the unattended confidence / small-amount gates
        approve the replacement would let a supplier launder a rejected invoice
        past the reviewer who rejected it. The invoice lands at
        ``ready_for_review`` and a human decides again.
    """
    # Cache IDs before try block — after rollback, invoice attrs may be expired
    invoice_id = invoice.id
    invoice_org_id = invoice.organization_id
    invoice_entity_id = invoice.entity_id
    # The payee this invoice is already bound to, captured before the extracted
    # fields land on the row (see `skip_vendor_match` above).
    preserved_vendor_id = invoice.vendor_id
    preserved_vendor_name = invoice.vendor_name

    try:
        config = _resolve_extraction_config(org_settings)

        # `get_extraction_adapter` imports (and therefore registers) every
        # built-in adapter itself, and RAISES `UnknownExtractionProviderError`
        # on a provider name it has no adapter for rather than substituting the
        # fixture-producing `mock` — see `decisions.md` §29. Here that travels
        # the normal failure path below: the invoice lands in `failed` with an
        # `extraction_failed` exception, which is exactly what a config error
        # should look like to a reviewer.
        from app.services.extraction_adapters import get_extraction_adapter

        adapter = get_extraction_adapter(config)
        logger.info("[extraction] Using adapter: %s", adapter.provider_name)

        # Fetch file bytes from S3/MinIO directly (authenticated). boto3 is
        # blocking; `_get_object` runs it in a worker thread so the dispatcher's
        # loop keeps draining while a large file downloads.
        from app.config import settings as app_settings
        from app.services.storage import _get_object

        file_key = invoice.file_key or ""
        logger.info(
            "[extraction] Fetching file from S3: bucket=%s, key=%s",
            app_settings.s3_bucket,
            file_key,
        )
        file_bytes, _file_content_type = await _get_object(file_key)
        logger.info("[extraction] File fetched: %s bytes", len(file_bytes))

        # Structured e-invoice auto-detect (pure, no network). A UBL / CII /
        # Factur-X file is routed to the deterministic `einvoice` adapter,
        # overriding the org's configured vision adapter. Everything else
        # (incl. plain scanned PDFs) falls through unchanged. This is the one
        # detect site every ingress (upload + email-intake) reaches.
        structured_format = _detect_structured_format(file_bytes, file_key)
        extract_mime_type = "application/pdf"
        if structured_format is not None:
            config = {"program_type": "platform", "provider": "einvoice"}
            # CII/UBL standalone XML vs Factur-X PDF — pass the real mime so
            # the adapter's detect runs identically on the bytes.
            extract_mime_type = (
                "application/pdf" if structured_format == "facturx_pdf" else "application/xml"
            )
            logger.info("[extraction] Structured e-invoice detected: %s", structured_format)

        # RAG: embed the invoice text and fetch similar past extractions to
        # prime the adapter. No-op when rag_enabled=False, text layer empty,
        # or the tenant has no embeddings yet.
        from app.services.rag import (
            build_few_shot_prompt,
            extract_invoice_text,
            neighbors_to_metadata,
            retrieve_similar,
        )

        invoice_text = extract_invoice_text(file_bytes)
        neighbors = await retrieve_similar(db, invoice_text, exclude_invoice_id=invoice_id)
        if neighbors:
            config["few_shot_prompt"] = build_few_shot_prompt(neighbors)
            logger.info("[extraction] RAG: %s neighbors injected as few-shot", len(neighbors))

        # GL catalog: inject org-specific chart of accounts so the AI
        # uses real codes instead of the hardcoded default list.
        from sqlalchemy import or_
        from sqlalchemy import select as sa_select

        from app.models.gl_account import GLAccount

        # Scope the catalog hint to the invoice's effective chart: shared
        # accounts (entity_id NULL, available to every entity) ∪ the invoice's
        # own entity. A single-entity tenant has all accounts shared or under
        # the one entity, so this is a no-op there. See docs/multi-entity.md
        # § Chart of accounts.
        gl_result = await db.execute(
            sa_select(GLAccount)
            .where(
                GLAccount.organization_id == invoice_org_id,
                GLAccount.is_active == True,  # noqa: E712
                or_(
                    GLAccount.entity_id == invoice_entity_id,
                    GLAccount.entity_id.is_(None),
                ),
            )
            .order_by(GLAccount.code)
        )
        gl_accounts = gl_result.scalars().all()
        # Set of valid codes used post-extraction to validate AI suggestions
        # against the invoice's actual chart. Empty when that chart has no
        # active account yet — in that mode there is no membership to
        # check, and see `gl_code_refused` below for what is still refused.
        active_gl_codes: set[str] = {gl.code for gl in gl_accounts}
        tenant_chart_ownership = None

        async def gl_code_refused(code: str) -> bool:
            """Whether an automated GL code may NOT land on this invoice.

            With an active chart, anything outside it (unknown, retired, or
            another entity's). With none, only a code that belongs to ANOTHER
            entity's chart — never right for this invoice whether or not
            its own chart has been synced yet (decisions §194/§199).
            That branch reads the tenant's whole chart ownership once, lazily:
            the vendor-prior overlay lands a code only after the document's
            own codes were judged, so the codes cannot be listed up front.
            """
            nonlocal tenant_chart_ownership
            if active_gl_codes:
                return code not in active_gl_codes
            if tenant_chart_ownership is None:
                from app.services.gl_chart import load_chart_ownership

                tenant_chart_ownership = await load_chart_ownership(db, invoice_org_id, None)
            return tenant_chart_ownership.belongs_elsewhere(code, invoice_entity_id)

        if gl_accounts:
            gl_lines = [
                f"{gl.code} \u2014 {gl.name}" + (f" [{gl.account_type}]" if gl.account_type else "")
                for gl in gl_accounts
            ]
            config["gl_account_catalog"] = "\n".join(gl_lines)
            logger.info("[extraction] GL catalog: %s accounts injected", len(gl_accounts))

        # Build a fresh adapter now that config is fully populated.
        adapter = get_extraction_adapter(config)

        result = await adapter.extract(
            file_bytes=file_bytes,
            file_key=file_key,
            mime_type=extract_mime_type,
        )

        if not result.success:
            # Don't log result.error: adapters build it from the raw provider
            # exception / response body (see extraction_adapters/*), which can
            # echo request/response content (PII). The provider name is enough
            # for triage; the structured failure lands on the exception record.
            logger.warning("[extraction] Adapter %s returned failure", adapter.provider_name)
            raise RuntimeError(result.error or "Extraction failed")

        logger.info(
            "[extraction] Success! Confidence: %s, vendor: %s",
            result.overall_confidence,
            result.vendor_name.value,
        )

        # Which separator this document treats as the decimal point — resolved
        # ONCE across every money token the model returned, so the header and
        # the line items can't be read under different rules (`decisions.md` §27).
        amount_convention = extraction_amount_convention(result)

        # Apply extracted fields to invoice
        _apply_extraction(invoice, result, amount_convention, org_settings=org_settings)

        if skip_vendor_match:
            # Restore the bound payee's identity immediately: `_apply_extraction`
            # writes `vendor_name` straight off the document, and that field is
            # what `PATCH /api/invoices/{id}` re-resolves a stale vendor link
            # from. Pinning it here is what makes preserving `vendor_id` below
            # actually hold — otherwise the re-link just happens on the
            # reviewer's next save. Everything else the document says (numbers,
            # dates, money, line items) is re-read, which is the whole point.
            invoice.vendor_name = preserved_vendor_name
            invoice.vendor_id = preserved_vendor_id

        # Self-correction pass — verify arithmetic, date ordering, line-item
        # math.  Lowers confidence on suspect fields and adds warnings.
        from app.services.extraction_self_correction import run_self_correction

        correction_report = await run_self_correction(result, org_settings)
        if correction_report.corrected:
            # Re-apply cleaned values after confidence adjustments
            existing_warnings = list(invoice.warnings or [])
            for v in correction_report.violations:
                # A violation already IS a catalogue-built warning (see
                # `extraction_self_correction`), carrying `check` for the modal.
                # `fields_affected` is confidence-penalty bookkeeping and has
                # never ridden the warning.
                existing_warnings.append({k: val for k, val in v.items() if k != "fields_affected"})
            invoice.warnings = existing_warnings
            logger.info(
                "[extraction] Self-correction: %s violation(s)",
                len(correction_report.violations),
            )

        # Save line items if extracted. Drop GL codes that aren't in the
        # active chart of accounts so the invoice can still post but the
        # rejected codes don't end up in the ERP push.
        #
        # REPLACE, never append. Extraction is re-runnable on any `new` or
        # `failed` invoice, and `PUT /api/invoices/{id}/line-items` is open until
        # approval — so a clerk who hand-keys lines onto a manually-created (or
        # extraction-disabled) invoice and then hits "Extract" used to end up
        # with BOTH sets on the row. The summed lines then disagree with the
        # header amount nothing recomputed, which raises a `line_total_mismatch`
        # exception, and that type BLOCKS the payment run — so a duplicated line
        # set silently strands the invoice, and any ERP push carries it.
        # Extraction's read of the document is authoritative for the lines it
        # produces, exactly as `_apply_extraction` already is for the header
        # fields, and `PUT .../line-items` (itself delete-then-insert) is how a
        # human re-asserts theirs afterwards.
        #
        # Guarded on a NON-EMPTY result: an extraction that found no lines at all
        # is not evidence there are none, so it must not delete a human's work.
        if result.line_items:
            await db.execute(
                sa_delete(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice.id)
            )
        invalid_gl_codes: list[str] = []
        for li in result.line_items:
            li_gl = li.gl_account.value if li.gl_account.value else None
            if li_gl and await gl_code_refused(li_gl):
                invalid_gl_codes.append(li_gl)
                li_gl = None  # don't persist a code that doesn't exist

            line_item = InvoiceLineItem(
                invoice_id=invoice.id,
                line_number=li.line_number,
                item_code=li.item_code.value if li.item_code.value else None,
                description=li.description.value if li.description.value else None,
                quantity=(
                    _clean_decimal(li.quantity.value, amount_convention)
                    if li.quantity.value
                    else None
                ),
                unit_price=(
                    _clean_decimal(li.unit_price.value, amount_convention)
                    if li.unit_price.value
                    else None
                ),
                tax=_clean_decimal(li.tax.value, amount_convention) if li.tax.value else None,
                total=(
                    _clean_decimal(li.total.value, amount_convention) if li.total.value else None
                ),
                gl_account=li_gl,
            )
            db.add(line_item)

        # Apply AI-suggested GL coding. Validate against the invoice's active
        # chart of accounts when one is configured — the AI is constrained
        # via the prompt but can still hallucinate, especially without
        # sufficient examples in the prompt's context window — and against
        # the other entities' charts when it is not.
        suggested_gl = result.suggested_gl_account.value
        suggested_gl_conf = result.suggested_gl_account.confidence
        if suggested_gl and suggested_gl_conf >= 0.7:
            if await gl_code_refused(suggested_gl):
                invalid_gl_codes.append(suggested_gl)
            else:
                invoice.gl_account = suggested_gl

        if result.suggested_cost_center.value and result.suggested_cost_center.confidence >= 0.7:
            invoice.cost_center = result.suggested_cost_center.value

        # Surface invalid GL codes as a single aggregated warning so the
        # reviewer sees the problem in the modal and can pick the right
        # code from the dropdown.
        if invalid_gl_codes:
            existing_warnings = list(invoice.warnings or [])
            existing_warnings.append(
                {
                    # GL codes are identifiers, not prose, so the join stays an
                    # ASCII `", "` on this side (`decisions.md` §148) and the
                    # whole list travels as one opaque `text` param.
                    **warning(
                        "gl_codes_not_in_chart",
                        "warning",
                        codes=", ".join(sorted(set(invalid_gl_codes))),
                    ),
                    "codes": sorted(set(invalid_gl_codes)),
                }
            )
            invoice.warnings = existing_warnings
            logger.info(
                "[extraction] GL validation: rejected %s unknown code(s)",
                len(set(invalid_gl_codes)),
            )

        # Vendor matching
        if skip_vendor_match:
            # Re-extraction of an already-bound invoice: read the existing link
            # rather than re-deriving one. See this function's docstring.
            from app.models.vendor import Vendor as _Vendor

            vendor = (
                (
                    await db.execute(sa_select(_Vendor).where(_Vendor.id == preserved_vendor_id))
                ).scalar_one_or_none()
                if preserved_vendor_id
                else None
            )
            vendor_action = "preserved"
        else:
            from app.services.vendor_matching import match_and_link_vendor

            vendor, vendor_action = await match_and_link_vendor(
                db,
                invoice,
                invoice.organization_id,
            )

        # Per-vendor correction cache: overlay cached priors (currency, tax
        # rate, payment terms, etc.) onto low-confidence extracted fields
        # for this vendor. No-op if the vendor is new or has no priors yet.
        from app.services.vendor_priors import apply_priors_to_invoice

        applied_priors = await apply_priors_to_invoice(db, invoice, result)
        if applied_priors:
            # Log the field NAMES overlaid, not their values (a prior value can
            # be a sensitive field like tax_id). Mirrors audit_access field-diff
            # discipline (names, never values).
            logger.info("[extraction] Applied vendor priors: %s", sorted(applied_priors))

        # Priors can overwrite the AI-validated gl_account with a cached
        # value that was valid at the time of prior caching but has since
        # been deactivated in the chart — or that was learned on another
        # entity's invoice. Re-check once after the overlay, by the same rule.
        if (
            "gl_account" in applied_priors
            and invoice.gl_account
            and await gl_code_refused(invoice.gl_account)
        ):
            stale_code = invoice.gl_account
            invoice.gl_account = None
            existing_warnings = list(invoice.warnings or [])
            existing_warnings.append(
                {
                    **warning("gl_code_stale_prior", "warning", code=stale_code),
                    "codes": [stale_code],
                }
            )
            invoice.warnings = existing_warnings
            logger.info("[extraction] GL validation: rejected stale prior '%s'", stale_code)

        # Semantic duplicate detection — reuses invoice_embeddings. Catches
        # near-duplicates that the rule-based (vendor_name + invoice_number)
        # check in invoice_warnings.py misses. Flags with a warning on the
        # invoice and an APException for the queue; never blocks the
        # extraction itself — reviewer decides.
        from app.services.duplicate_detection import (
            find_semantic_duplicates,
            matches_to_warning,
        )
        from app.services.exception_service import create_exception

        # `entity_id` does NOT filter the search — a group double-bill across two
        # subsidiaries is a real duplicate and the control exists to catch it. It
        # classifies each match, so a hit under another entity is reported
        # without its `invoice_number` / `vendor_name` / `amount` (which the
        # detail modal would otherwise render to a viewer scoped away from that
        # entity). See services/duplicate_detection.py's module docstring.
        duplicate_matches = await find_semantic_duplicates(
            db, invoice_text, exclude_invoice_id=invoice_id, entity_id=invoice_entity_id
        )
        duplicate_warning = matches_to_warning(duplicate_matches)
        if duplicate_warning:
            existing = list(invoice.warnings or [])
            existing.append(duplicate_warning)
            invoice.warnings = existing
            await create_exception(
                db,
                exception_type="duplicate",
                severity="warning",
                description=duplicate_warning["message"],
                status="open",
                organization_id=invoice_org_id,
                invoice=invoice,  # exception follows its invoice (P2)
                # Semantic duplicate detection runs inside the extraction
                # worker (a pool thread, or a Lambda) — no request, no user.
                raised_by_user_id=None,
            )
            logger.info(
                "[extraction] Duplicate detection: %s near-match(es)", len(duplicate_matches)
            )

        # Refresh warnings + PO match — handles missing fields, duplicates,
        # fraud flags, vendor verification status, and 2/3-way PO matching.
        # Runs here so the reviewer sees a fully-populated invoice (with any
        # exceptions surfaced in the queue) the moment extraction lands.
        from app.services.invoice_warnings import refresh_warnings

        await refresh_warnings(db, invoice, org_settings=org_settings)

        # Save extraction result with priors metadata (what the UI will show
        # for transparency — which cache overrides and RAG neighbors shaped
        # this extraction).
        priors_metadata: dict = {}
        if applied_priors:
            priors_metadata["vendor_cache_applied"] = applied_priors
        if neighbors:
            priors_metadata["rag_neighbors"] = neighbors_to_metadata(neighbors)
        if correction_report.corrected:
            priors_metadata["self_correction"] = {
                "violations": correction_report.violations,
                "penalties": correction_report.confidence_penalties,
            }

        extraction_result = InvoiceExtractionResult(
            invoice_id=invoice.id,
            method=result.provider or config.get("provider", "unknown"),
            confidence=Decimal(str(round(result.overall_confidence, 4))),
            raw_result=result.raw_response,
            priors_metadata=priors_metadata or None,
        )
        db.add(extraction_result)

        # Track usage for billing. This rides the tenant transaction and its
        # single commit below, so the meter and the extraction result land
        # together or not at all. It was previously committed to a control-plane
        # session, where `extraction_usage` does not exist — the resulting
        # UndefinedTableError was caught by this function's own handler, which
        # rolled the extraction back and marked a SUCCESSFUL invoice `failed`.
        program_type = config.get("program_type", "platform")
        db.add(
            ExtractionUsage(
                invoice_id=invoice.id,
                provider=result.provider or config.get("provider", "unknown"),
                program_type=program_type,
                period=datetime.now(UTC).strftime("%Y-%m"),
                success=True,
                organization_id=invoice.organization_id,
            )
        )

        # Decide target status: auto-approve or ready_for_review
        from app.services.workflow_engine import get_step_config

        target_status = InvoiceStatus.ready_for_review
        auto_approved = False

        instance = await get_workflow_instance(db, invoice.id)
        if instance and instance.steps_config_snapshot:
            ext_cfg = get_step_config(instance.steps_config_snapshot, "extraction")
            approval_cfg = get_step_config(instance.steps_config_snapshot, "approval")
            # The max-amount / CFO gates are measured against the same
            # same-vendor rolling aggregate the human approval path uses, so a
            # split payable can't auto-approve past a control that a reviewer
            # would have been stopped by.
            # Every threshold `decide_auto_approve` reads is denominated in the
            # org's reporting currency, so both operands are expressed there
            # first. `refresh_warnings` above just locked the rate onto this
            # row, so this is a read, not an FX call.
            from app.services.approval_chain import reporting_gate_amount

            auto_approved = decide_auto_approve(
                ext_cfg,
                approval_cfg,
                overall_confidence=result.overall_confidence,
                amount=reporting_gate_amount(invoice, org_settings=org_settings),
                aggregate_amount=reporting_gate_amount(
                    invoice,
                    amount=await resolve_gate_aggregate(db, invoice, org_settings=org_settings),
                    org_settings=org_settings,
                ),
            )
            if auto_approved:
                target_status = InvoiceStatus.approved

        if suppress_auto_approve and auto_approved:
            # A human already rejected this document once (see the docstring).
            # Fall back to review rather than approving the replacement
            # unattended.
            auto_approved = False
            target_status = InvoiceStatus.ready_for_review
            logger.info(
                "[extraction] auto-approve suppressed for re-extracted invoice %s",
                invoice_id,
            )

        if auto_approved:
            invoice.approval_date = utc_today()
            invoice.approved_by = "system (auto-approve)"

        await transition_invoice(
            db,
            invoice,
            target_status,
            actor_id=actor_id,
            action_name=(
                "invoice.auto_approved" if auto_approved else "invoice.extraction_completed"
            ),
            details={
                "method": result.provider,
                "confidence": result.overall_confidence,
                "auto_approved": auto_approved,
                "vendor_action": vendor_action,
                "vendor_id": str(vendor.id) if vendor else None,
                "gl_suggested": result.suggested_gl_account.value,
                "program_type": program_type,
            },
        )

        # Advance workflow
        if instance:
            if auto_approved:
                await advance_workflow(db, instance, "erp_push", action="auto_approved")
            else:
                await advance_workflow(db, instance, "review", action="extracted")

        await db.commit()

    except Exception as exc:
        # Log the exception CLASS only, never the raw message: a vision/OCR SDK
        # exception can carry extracted invoice PII (vendor tax id / bank /
        # address). No exc_info either — this codebase installs no log-redaction
        # filter, so a traceback would put the raw message into the handler. The
        # structured failure detail lands on the exception record + audit row.
        logger.error("[extraction] Failed: %s", exc.__class__.__name__)
        await db.rollback()

        # Re-fetch invoice after rollback (the old object is expired)
        from sqlalchemy import select as sa_select

        from app.models.invoice import Invoice as InvoiceModel

        result = await db.execute(sa_select(InvoiceModel).where(InvoiceModel.id == invoice_id))
        invoice = result.scalar_one_or_none()
        if not invoice:
            await db.commit()
            return

        # Track failed usage on the tenant session, riding the same commit as
        # the exception record below (see the success path for why it is not the
        # control plane). Still guarded: `_resolve_extraction_config` can raise
        # on a bad provider name, and losing the meter must not also cost us the
        # exception record that tells a human the extraction failed.
        try:
            # Silent: the success path above already announced any provider
            # fallback for this attempt, and repeating it here would double
            # every such warning per failed extraction.
            config = _resolve_extraction_config(org_settings, announce=False)
            db.add(
                ExtractionUsage(
                    invoice_id=invoice_id,
                    provider=config.get("provider", "unknown"),
                    program_type=config.get("program_type", "platform"),
                    period=datetime.now(UTC).strftime("%Y-%m"),
                    success=False,
                    organization_id=invoice_org_id,
                )
            )
        except Exception:
            pass

        # Create exception record (shared chokepoint → emits `exception.raised`)
        from app.services.exception_service import create_exception

        await create_exception(
            db,
            exception_type="extraction_failed",
            severity="error",
            description=f"Extraction failed: {str(exc)[:500]}",
            status="open",
            organization_id=invoice_org_id,
            invoice=invoice,  # exception follows its invoice (P2)
            # The extraction worker raised this; a provider/parse failure is
            # nobody's act.
            raised_by_user_id=None,
        )

        # Transition pending → failed
        await transition_invoice(
            db,
            invoice,
            InvoiceStatus.failed,
            actor_id=actor_id,
            action_name="invoice.extraction_failed",
            details={"error": str(exc)},
        )

        instance = await get_workflow_instance(db, invoice_id)
        if instance:
            instance.state = "failed"
            instance.state_data = {**(instance.state_data or {}), "error": str(exc)}

        await db.commit()


# Sentinel values to treat as "no extraction." Vision models routinely
# emit these literal strings (or leak parts of the prompt schema) when a
# field isn't present on the document; landing them verbatim in the DB
# pollutes downstream search, vendor matching, and exception flagging.
_NULL_SENTINELS = frozenset(
    s.casefold()
    for s in (
        "",
        "null",
        "none",
        "n/a",
        "na",
        "-",
        "—",
        "string or null",
        "string",
        "tbd",
        "unknown",
        "not provided",
        "not specified",
        "not available",
    )
)

# Currency symbols stripped before Decimal conversion. We keep the raw
# value's currency code on `invoice.currency`; the amount column is the
# numeric magnitude only.
_CURRENCY_SYMBOLS = "$\u20ac\u00a3\u00a5\u20b9\u20bd\u20a9\u0e3f\u20aa\u20a6"

# Whitespace stripped from inside a number. A plain space — and the
# non-breaking / narrow-no-break spaces a PDF renderer emits — is itself a
# thousands separator in French convention (``1 234,56``).
_NUMERIC_WHITESPACE = (" ", "\t", "\u00a0", "\u202f")

# The money fields whose tokens vote on the document's decimal convention.
# Deliberately money only: `tax_rate` is a percentage and `quantity` a count,
# and neither is written under an amount's grouping habits.
_HEADER_MONEY_FIELDS = ("amount", "subtotal", "tax_amount", "discount_amount", "shipping_amount")
_LINE_MONEY_FIELDS = ("unit_price", "tax", "total")


def _amount_core(val: str | None) -> str | None:
    """Reduce a model-produced numeric token to digits, separators and a sign.

    Everything :func:`_clean_decimal` strips — sentinels, accounting
    parentheses, currency symbols, internal whitespace, a percent sign, a
    Unicode minus — happens here, so the string the decimal-convention rules
    see is exactly the one that will be parsed. ``None`` when nothing numeric
    survives.
    """
    if val is None:
        return None
    s = str(val).strip()
    if s.casefold() in _NULL_SENTINELS:
        return None

    # Accounting negatives: "(123.45)" → "-123.45"
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1].strip()

    for sym in _CURRENCY_SYMBOLS:
        s = s.replace(sym, "")
    for ws in _NUMERIC_WHITESPACE:
        s = s.replace(ws, "")
    s = s.replace("%", "")
    # Unicode minus → ASCII minus
    s = s.replace("\u2212", "-")

    if not s or s.casefold() in _NULL_SENTINELS:
        return None
    return s


def extraction_amount_convention(result: ExtractionResult) -> AmountConvention | None:
    """Which decimal convention THIS document's money is written in.

    A vision model transcribes what the page says, and an invoice printed in
    most of Europe says ``1.234,56``. The unit that can answer "is that comma a
    decimal point?" is the document, not the token — so the whole extracted
    money set votes once, exactly as a supplier statement's amount column does
    (`decisions.md` §27). ``None`` means the document proved nothing (or
    contradicted itself); every self-describing token is still read on its own
    terms.
    """
    tokens = [getattr(result, f).value for f in _HEADER_MONEY_FIELDS]
    for li in result.line_items:
        tokens.extend(getattr(li, f).value for f in _LINE_MONEY_FIELDS)
    cores = [core for core in (_amount_core(t) for t in tokens) if core is not None]
    return detect_convention(cores)


def _clean_decimal(val: str | None, convention: AmountConvention | None = None) -> Decimal | None:
    """Clean a string value for Decimal conversion.

    Handles the common failure modes vision models produce:
    - Currency symbols (`$`, `€`, `£`, ...)
    - Thousands separators + spaces (incl. the non-breaking kind)
    - Percent signs (e.g. `8.25%` for tax_rate)
    - Parenthesised negatives (`(123.45)` → `-123.45`, classic accounting)
    - Unicode minus sign (`−` U+2212) used by Word-style invoices
    - Sentinel "no value" strings (`null`, `N/A`, `-`, etc.)

    **Which separator is the decimal point is decided by the rules in
    `services/decimal_convention`, not by stripping every comma.** This used to
    do `s.replace(",", "")` unconditionally, so a model transcribing a European
    invoice's ``850,00`` produced ``85000`` — a hundredfold overstatement the
    downstream arithmetic checks then agreed with, because subtotal and tax were
    scaled identically — and ``1.234,56`` came back unparseable ``None``,
    silently dropping the amount. ``convention`` is the document-level answer
    from :func:`extraction_amount_convention`, consulted only for the genuinely
    ambiguous shape (a single separator with a three-digit tail); omitting it
    keeps the historical US reading there.

    Returns None on anything that can't be parsed — never raises.
    """
    core = _amount_core(val)
    if core is None:
        return None
    try:
        return Decimal(apply_convention(core, convention))
    except Exception:
        return None


def _clean_string(val: str | None) -> str | None:
    """Filter sentinel "no value" strings out of free-form text fields.

    Same `_NULL_SENTINELS` treatment as `_clean_decimal` so we don't end
    up with literal `"null"` or `"N/A"` in `vendor_address` etc. Returns
    the trimmed string or None.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.casefold() in _NULL_SENTINELS:
        return None
    return s


# Map of common ways models phrase a payment method → our canonical value.
# Keys are casefolded; values match the dropdown options in
# `InvoiceModal.svelte` so a select-bind round-trips cleanly.
_PAYMENT_METHOD_ALIASES = {
    "ach": "ach",
    "ach transfer": "ach",
    "ach payment": "ach",
    "automated clearing house": "ach",
    "wire": "wire",
    "wire transfer": "wire",
    "swift": "wire",
    "check": "check",
    "cheque": "check",
    "paper check": "check",
    "credit card": "credit_card",
    "creditcard": "credit_card",
    "card": "credit_card",
    "credit": "credit_card",
    "cc": "credit_card",
    "virtual card": "credit_card",
    "vcard": "credit_card",
    "other": "other",
    "rtp": "wire",  # real-time payment — no dropdown option, fold into wire
    "ach preferred. wire transfer accepted.": "ach",  # exact prompt-leak we've seen
}


def _normalize_payment_method(val: str | None) -> str | None:
    """Map the model's free-form payment-method string to a canonical
    lowercase value the frontend dropdown matches.

    Without this, an extraction of `"ACH"` (uppercase) leaves the
    dropdown blank because its options are lowercase. Returns None
    when nothing maps and the value isn't already a known canonical form.
    """
    if val is None:
        return None
    s = str(val).strip().casefold()
    if not s or s in _NULL_SENTINELS:
        return None
    if s in _PAYMENT_METHOD_ALIASES:
        return _PAYMENT_METHOD_ALIASES[s]
    # Heuristic: substring match for compound phrases like "ACH preferred."
    for alias, canonical in _PAYMENT_METHOD_ALIASES.items():
        if alias in s:
            return canonical
    return None


def _clean_date(val: str | None, *, day_first: bool = False) -> date | None:
    """Parse a model-returned date string into a `date`.

    Tries ISO first (the format we ask for in the prompt), then the genuinely
    ambiguous numeric case (`3/15/2024` vs `15/3/2024`) via the shared
    :func:`app.utils.dates.parse_ambiguous_date` — which orders the two
    interpretations by ``day_first`` (the org's own configured signal, see
    :func:`app.utils.dates.resolve_day_first_preference`) rather than always
    guessing US-first — then the unambiguous common variations vision models
    slip into: `YYYY/MM/DD` and human-readable `Month DD, YYYY`.

    Strict `date.fromisoformat` was silently dropping anything but
    `YYYY-MM-DD`, which meant a model returning "March 15, 2024" lost
    the date entirely.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.casefold() in _NULL_SENTINELS:
        return None

    # ISO is the prompt-requested format — try first.
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass

    # The ambiguous DD/MM vs MM/DD (and DD-MM vs MM-DD) case — one shared
    # helper, org-locale-aware, so this call site can't drift from the
    # others that face the same ambiguity (CSV import, bank/vendor-statement
    # reconciliation).
    ambiguous = parse_ambiguous_date(s, day_first=day_first)
    if ambiguous is not None:
        return ambiguous

    # Common alternates that are NOT ambiguous, in priority order.
    formats = (
        "%Y/%m/%d",  # 2024/3/15
        "%B %d, %Y",  # March 15, 2024
        "%b %d, %Y",  # Mar 15, 2024
        "%d %B %Y",  # 15 March 2024
        "%d %b %Y",  # 15 Mar 2024
        "%d-%b-%Y",  # 15-Mar-2024
        "%Y-%m-%d %H:%M:%S",  # ISO with time — drop the time
    )
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _apply_extraction(
    invoice: Invoice,
    result: ExtractionResult,
    convention: AmountConvention | None = None,
    org_settings: dict | None = None,
) -> None:
    """Apply extracted fields to the invoice record.

    Every field goes through a typed cleaner that:
    - Filters sentinel "null" / "N/A" / prompt-leak strings
    - Normalises common case + format variants
    - Drops un-parsable values silently (rather than corrupting the row)

    None of the cleaners overwrite an existing value with None — the model
    might miss a field that the human (or a previous extraction) already
    populated. We only assign when the cleaner returns a real value.

    ``convention`` is the document's decimal convention. Callers that also read
    the line items (``extract_invoice``) resolve it once and pass it here so the
    header and the lines are read by the same rule; omitting it resolves it from
    this result.

    ``org_settings`` resolves the ambiguous-date day-first preference (see
    ``app.utils.dates.resolve_day_first_preference``) for the two date fields
    below — an org with no configured signal keeps the pre-existing
    month-first reading.
    """
    if convention is None:
        convention = extraction_amount_convention(result)
    day_first = resolve_day_first_preference(org_settings)

    # Free-form text fields — sentinel-filter only.
    for src, dst in (
        (result.invoice_number, "invoice_number"),
        (result.vendor_name, "vendor_name"),
        (result.payment_terms, "payment_terms"),
        (result.po_number, "po_number"),
        (result.description, "description"),
        (result.vendor_address, "vendor_address"),
        (result.vendor_tax_id, "vendor_tax_id"),
        (result.reference_number, "reference_number"),
        (result.bill_to_address, "bill_to_address"),
        (result.remit_to_address, "remit_to_address"),
    ):
        cleaned = _clean_string(src.value)
        if cleaned is not None:
            setattr(invoice, dst, cleaned)

    # Decimals.
    for src, dst in (
        (result.amount, "amount"),
        (result.subtotal, "subtotal"),
        (result.tax_amount, "tax_amount"),
        (result.tax_rate, "tax_rate"),
        (result.discount_amount, "discount_amount"),
        (result.shipping_amount, "shipping_amount"),
    ):
        d = _clean_decimal(src.value, convention)
        if d is not None:
            setattr(invoice, dst, d)

    # Dates.
    for src, dst in (
        (result.invoice_date, "invoice_date"),
        (result.due_date, "due_date"),
    ):
        d = _clean_date(src.value, day_first=day_first)
        if d is not None:
            setattr(invoice, dst, d)

    # Currency — uppercase 3-char code or None. Models sometimes return
    # "US Dollars" or "USD." with punctuation; we normalise via the
    # sentinel filter + uppercase, then validate length.
    cur = _clean_string(result.currency.value)
    if cur is not None:
        cur_upper = cur.upper().rstrip(".").strip()
        if len(cur_upper) == 3 and cur_upper.isalpha():
            invoice.currency = cur_upper

    # Payment method — map free-form to canonical dropdown values.
    pm = _normalize_payment_method(result.payment_method.value)
    if pm is not None:
        invoice.payment_method = pm
