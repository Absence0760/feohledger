"""Positive Pay / payment-fraud file endpoints (``/api/positive-pay``).

Positive Pay is a treasury fraud control. We hand the bank a file of the
cheques we *issued* (a ``check_issue`` file, generated per payment run) — or, for
ACH debit-block, the accounts authorized to debit us (a standalone
``ach_authorization`` file). When an item is later presented for payment, the
bank matches it against our issued file and flags anything that doesn't line up.
Return processing takes the bank's "what I saw presented" list back, classifies
each item against what we issued, and raises a ``fraud_flag`` Exception on any
altered or never-issued cheque.

The rendered file legitimately contains full account / routing numbers (that's
its purpose) and lives only in MinIO under ``file_key``; the DB row, the audit
trail, and every error / log line carry only PII-free metadata — the originating
account is masked to ``account_last4``. Money is ``Decimal`` end-to-end; every
mutation is RBAC-gated (treasury control — clerks excluded), writes an audit
row, and is entity-scoped (multi-entity). The check-issue file is idempotent on
``(payment_run_id, bank_format)`` via the partial unique index — re-generation
returns the existing row. See ``backend/docs/positive-pay.md``.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_roles,
)
from app.models.exception import Exception as APException
from app.models.organization import Organization
from app.models.payment import PaymentRun
from app.models.positive_pay import (
    FILE_TYPE_ACH_AUTHORIZATION,
    FILE_TYPE_CHECK_ISSUE,
    STATUS_RETURNED_PROCESSED,
    PositivePayFile,
)
from app.models.user import User
from app.schemas.positive_pay import (
    GenerateAchAuthorizationRequest,
    GenerateCheckIssueRequest,
    PositivePayFileResponse,
    PositivePayListResponse,
    PositivePaySummaryResponse,
    ProcessReturnRequest,
    ProcessReturnResponse,
)
from app.services import positive_pay as service
from app.services import storage
from app.services.audit_dispatch import dispatch_audit
from app.services.currency_conversion import resolve_reporting_currency
from app.services.exception_service import create_exception
from app.services.positive_pay import (
    CLASS_AMOUNT_MISMATCH,
    CLASS_NOT_ON_FILE,
    IssuedItem,
    PresentedItem,
    classify_presented_items,
    normalize_check_number,
)
from app.services.positive_pay_adapters import (
    FormatterContext,
    PositivePayFieldOverflow,
    UnknownPositivePayFormatError,
    get_positive_pay_formatter,
    list_available_formats,
)
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.dates import utc_today
from app.utils.search import ilike_contains

router = APIRouter(prefix="/positive-pay", tags=["positive-pay"])

_READ_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)
_WRITE_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _file_to_response(row: PositivePayFile) -> PositivePayFileResponse:
    return PositivePayFileResponse(
        id=str(row.id),
        file_type=row.file_type,
        bank_format=row.bank_format,
        status=row.status,
        payment_run_id=str(row.payment_run_id) if row.payment_run_id else None,
        item_count=row.item_count,
        total_amount=row.total_amount,
        currency=row.currency,
        account_last4=row.account_last4,
        file_key=row.file_key,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        meta=dict(row.meta) if row.meta else None,
    )


def _resolve_company_account(org: Organization) -> tuple[str, str]:
    """Pull ``(company_name, originating_account_number)`` from org settings.

    Company name from ``settings.company.name``; the originating cheque account
    from ``settings.payments.check_account_number`` (falling back to
    ``payments.account_number``). Both default to ``""`` when unset — the
    formatter still renders a valid (account-less) file and ``account_last4``
    becomes ``None``. The account number is a full number used only inside the
    rendered file; it never enters a log or the audit trail.
    """
    settings_dict = org.settings or {}
    company = (settings_dict.get("company") or {}).get("name") or ""
    payments = settings_dict.get("payments") or {}
    account = payments.get("check_account_number") or payments.get("account_number") or ""
    return company, account


def _last4(account_number: str) -> str | None:
    cleaned = (account_number or "").strip()
    return cleaned[-4:] if len(cleaned) >= 4 else (cleaned or None)


def _require_formatter(bank_format: str):
    """Resolve the bank layout or 422 naming the unregistered format.

    ``get_positive_pay_formatter`` refuses a NAMED layout it has no formatter
    for rather than rendering ``csv`` under the requested name — see its module
    docstring. Refusing here keeps the whole record honest: no MinIO object, no
    ``PositivePayFile`` row claiming a layout it isn't, no audit row asserting
    it, and — for check-issue — no ``(run, bank_format)`` idempotency slot
    burned on a format the bank can't read. 422 because the bad value is in the
    request body; the format name is caller input, never a credential.
    """
    try:
        return get_positive_pay_formatter(bank_format)
    except UnknownPositivePayFormatError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{exc.name}' is not a supported bank format "
                f"(one of: {', '.join(list_available_formats())})."
            ),
        ) from None


async def _get_scoped_file(
    db: AsyncSession, file_id: uuid.UUID, entity_id: uuid.UUID | None
) -> PositivePayFile:
    row = (
        await db.execute(
            apply_entity_scope(
                select(PositivePayFile).where(PositivePayFile.id == file_id),
                PositivePayFile,
                entity_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Positive pay file not found")
    return row


# --------------------------------------------------------------------------- #
# Generate — check-issue (per payment run) + ACH authorization (org-wide)
# --------------------------------------------------------------------------- #


def _render_or_422(render):
    """Run a formatter and turn a field overflow into an actionable 422.

    A `PositivePayFieldOverflow` means an identifier or amount cannot occupy
    its column without being changed into a different value. That is a
    configuration mismatch between the tenant's data and the bank layout
    (wrong `bank_format` for these account numbers, say) — recoverable by the
    operator, so it is a 422 naming the column rather than a 500. The message
    carries the field and width only; the offending value is a full account or
    routing number and never leaves the file.
    """
    try:
        return render()
    except PositivePayFieldOverflow as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/payment-runs/{run_id}/check-issue",
    response_model=PositivePayFileResponse,
)
async def generate_check_issue(
    run_id: uuid.UUID,
    body: GenerateCheckIssueRequest,
    response: Response,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    """Generate the check-issue Positive Pay file for a payment run.

    The run must have **executed**: 422 otherwise. Idempotent: if a file already
    exists for ``(run_id, bank_format)`` it is returned with 200 (no second
    file). Otherwise the run's cheque payments are rendered via the requested
    bank formatter, stored in MinIO, and a metadata row is persisted (201).
    """
    bank_format = body.bank_format or "csv"

    run = (
        await db.execute(
            apply_entity_scope(
                select(PaymentRun).where(PaymentRun.id == run_id), PaymentRun, entity_id
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Payment run not found")

    # A run that hasn't executed has issued NOTHING. Its payments are still
    # `pending` with a NULL `reference`, so `build_check_issue_items` yields an
    # EMPTY `issued_map` — and because the (run, format) slot is claimed for
    # good by `uq_positive_pay_run_format`, that empty snapshot can never be
    # regenerated once the run does execute. Return processing then classifies
    # every cheque the bank presents as `not_on_file` and floods the queue with
    # fraud_flag Exceptions against real, legitimately-issued payments. Refuse
    # up front — before the idempotency lookup, so an unexecuted run can neither
    # mint nor hand back such a file. (A file generated against a draft run
    # BEFORE this guard existed is unusable for the same reason: delete it via
    # `DELETE /api/positive-pay/{id}` and regenerate once the run has executed.)
    if run.executed_at is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Payment run has not been executed — no cheques have been issued yet. "
                "Generate the check-issue file after the run executes."
            ),
        )

    # Idempotency: one check-issue file per (run, format) — return the existing.
    existing = (
        await db.execute(
            select(PositivePayFile).where(
                PositivePayFile.payment_run_id == run_id,
                PositivePayFile.bank_format == bank_format,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return _file_to_response(existing)

    # Resolve the layout BEFORE building the items — an unsupported format is a
    # request error, and refusing it here means no cheque data is projected and
    # no object is uploaded for a file that could never have been rendered
    # correctly. (After the idempotency lookup, so a row generated under a
    # since-removed format name is still returned rather than 422'd.)
    formatter = _require_formatter(bank_format)

    company_name, account_number = _resolve_company_account(org)
    items, total, mapping = await service.build_check_issue_items(
        db, run=run, entity_id=entity_id, account_number=account_number
    )

    ctx = FormatterContext(
        company_name=company_name,
        account_number=account_number,
        file_date=utc_today(),
        currency=(org.settings or {}).get("invoice_defaults", {}).get("currency", "USD"),
    )
    content = _render_or_422(lambda: formatter.format_check_issue(items, ctx)).encode("utf-8")
    content_hash = hashlib.sha256(content).hexdigest()
    file_id = uuid.uuid4()
    filename = f"positive-pay-check-issue-{run_id}.{formatter.file_extension}"
    file_key, _ = await storage.upload_positive_pay_file(
        org_id, file_id, content, filename, formatter.content_type
    )

    row = PositivePayFile(
        id=file_id,
        organization_id=org_id,
        entity_id=entity_id,
        payment_run_id=run_id,
        file_type=FILE_TYPE_CHECK_ISSUE,
        bank_format=bank_format,
        item_count=len(items),
        total_amount=total,
        currency=resolve_reporting_currency(org.settings),
        content_hash=content_hash,
        file_key=file_key,
        account_last4=_last4(account_number),
        generated_by=user.id,
        # Persist the POINT-IN-TIME issued check_number → {invoice_id, amount}
        # snapshot — what was actually on the file we sent to the bank — so
        # return processing (`process_return`) classifies against this,
        # never a live re-query of the run's payments. A live re-query would
        # reflect a payment's CURRENT status (e.g. voided after the file was
        # sent), silently dropping a legitimately-issued cheque out of the
        # comparison set and mislabeling its bank presentment `not_on_file`
        # (issue #178). PII-free (check numbers, invoice ids, amounts only).
        meta={
            "issued_map": {
                key: {"invoice_id": str(invoice_id), "amount": str(amount)}
                for key, invoice_id, amount in mapping
            }
        },
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        # Lost an idempotency race — the unique index claimed the slot. Roll
        # back and return the row that won. The file_id we generated was never
        # persisted, so its just-uploaded object is unreferenced — delete it so
        # a loser's account-number-bearing bytes don't orphan in the bucket.
        await db.rollback()
        await storage.delete_file(file_key)
        winner = (
            await db.execute(
                select(PositivePayFile).where(
                    PositivePayFile.payment_run_id == run_id,
                    PositivePayFile.bank_format == bank_format,
                )
            )
        ).scalar_one()
        response.status_code = status.HTTP_200_OK
        return _file_to_response(winner)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="positive_pay.check_issue_generated",
        entity_type="positive_pay_file",
        entity_id=row.id,
        details={
            "file_type": FILE_TYPE_CHECK_ISSUE,
            "bank_format": bank_format,
            "item_count": len(items),
            "total_amount": str(total),
            "run_id": str(run_id),
        },
    )
    await db.commit()
    await db.refresh(row)
    response.status_code = status.HTTP_201_CREATED
    return _file_to_response(row)


@router.post("/ach-authorization", response_model=PositivePayFileResponse)
async def generate_ach_authorization(
    body: GenerateAchAuthorizationRequest,
    response: Response,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    """Generate a standalone ACH debit-authorization file for the org.

    Lists every active vendor with ACH bank details (routing + account), renders
    via the requested formatter, stores the file, and persists a metadata row
    (``file_type=ach_authorization``, no payment run). 201.
    """
    bank_format = body.bank_format or "csv"
    # Same refusal as check-issue: an unsupported layout never reaches a file.
    formatter = _require_formatter(bank_format)

    company_name, account_number = _resolve_company_account(org)

    items = await service.build_ach_authorization_items(db, org_id=org_id, entity_id=entity_id)

    ctx = FormatterContext(
        company_name=company_name,
        account_number=account_number,
        file_date=utc_today(),
        currency=(org.settings or {}).get("invoice_defaults", {}).get("currency", "USD"),
    )
    content = _render_or_422(lambda: formatter.format_ach_authorization(items, ctx)).encode("utf-8")
    content_hash = hashlib.sha256(content).hexdigest()
    file_id = uuid.uuid4()
    filename = f"positive-pay-ach-authorization-{file_id}.{formatter.file_extension}"
    file_key, _ = await storage.upload_positive_pay_file(
        org_id, file_id, content, filename, formatter.content_type
    )

    row = PositivePayFile(
        id=file_id,
        organization_id=org_id,
        entity_id=entity_id,
        payment_run_id=None,
        file_type=FILE_TYPE_ACH_AUTHORIZATION,
        bank_format=bank_format,
        item_count=len(items),
        total_amount=Decimal("0"),
        currency=resolve_reporting_currency(org.settings),
        content_hash=content_hash,
        file_key=file_key,
        account_last4=_last4(account_number),
        generated_by=user.id,
    )
    db.add(row)
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="positive_pay.ach_authorization_generated",
        entity_type="positive_pay_file",
        entity_id=row.id,
        details={
            "file_type": FILE_TYPE_ACH_AUTHORIZATION,
            "bank_format": bank_format,
            "item_count": len(items),
        },
    )
    await db.commit()
    await db.refresh(row)
    response.status_code = status.HTTP_201_CREATED
    return _file_to_response(row)


# --------------------------------------------------------------------------- #
# List + detail + download
# --------------------------------------------------------------------------- #


def _positive_pay_list_filters(
    query,
    *,
    file_type: str | None,
    status_filter: str | None,
    search: str | None = None,
):
    """Apply the positive-pay list ``file_type`` / ``status`` / free-text filters.

    Shared by ``GET /api/positive-pay`` and its ``/summary`` so the KPI rollup
    can never describe a different set than the rows it sits above. Entity scope
    is applied by the caller.

    ``search`` matches the bank format, the file type, and the two ids as text —
    the row's label renders an 8-character prefix of the file id (or of the
    payment run id), so pasting what is on screen finds the row. The page
    filtered in the browser over the rows it had already loaded, so a file on
    page 2 read as "nothing matched" while the footer's "Showing all N" (the
    server's whole-set total) sat under a client-narrowed table.

    The one thing that deliberately does NOT move to the server is the rendered
    label itself: it is built from a LOCALISED string, so matching it in SQL
    would make the result set depend on the caller's browser language. Matching
    `file_type` covers the same intent ("ach" still finds the ACH-authorization
    files) without that.
    """
    if file_type:
        query = query.where(PositivePayFile.file_type == file_type)
    if status_filter:
        query = query.where(PositivePayFile.status == status_filter)
    if search and search.strip():
        term = search.strip()
        query = query.where(
            or_(
                ilike_contains(PositivePayFile.bank_format, term),
                ilike_contains(PositivePayFile.file_type, term),
                ilike_contains(cast(PositivePayFile.id, String), term),
                ilike_contains(cast(PositivePayFile.payment_run_id, String), term),
            )
        )
    return query


@router.get("", response_model=PositivePayListResponse)
async def list_files(
    file_type: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    query = _positive_pay_list_filters(
        apply_entity_scope(select(PositivePayFile), PositivePayFile, entity_id),
        file_type=file_type,
        status_filter=status_filter,
        search=search,
    )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = (
        query.order_by(PositivePayFile.created_at.desc(), PositivePayFile.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list((await db.execute(query)).scalars().all())
    return PositivePayListResponse(
        items=[_file_to_response(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# Literal `/summary` declared BEFORE `/{file_id}` so it isn't captured as a
# {file_id} UUID.
@router.get("/summary", response_model=PositivePaySummaryResponse)
async def positive_pay_summary(
    file_type: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Whole-set status counts + total exported items + total flagged returns.

    Takes the SAME ``file_type`` / ``status`` / ``search`` filters as the list. The page's
    ``itemsExported`` / ``returnsFlagged`` reduced over the LOADED page while
    the "Files" card showed the server's whole-set ``total``. The return-summary
    figures live in a per-file ``meta`` JSONB block; the positive-pay file set
    is one row per payment run, so they are summed in Python rather than with
    fragile nested-JSONB SQL.
    """
    rows = list(
        (
            await db.execute(
                _positive_pay_list_filters(
                    apply_entity_scope(select(PositivePayFile), PositivePayFile, entity_id),
                    file_type=file_type,
                    status_filter=status_filter,
                    search=search,
                )
            )
        )
        .scalars()
        .all()
    )

    by_status: dict[str, int] = {}
    items_exported = 0
    returns_flagged = 0
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        items_exported += int(row.item_count or 0)
        rs = (row.meta or {}).get("return_summary") or {}
        returns_flagged += int(rs.get("amount_mismatches") or 0) + int(rs.get("not_on_file") or 0)

    return PositivePaySummaryResponse(
        total=len(rows),
        by_status=by_status,
        items_exported=items_exported,
        returns_flagged=returns_flagged,
    )


@router.get("/{file_id}", response_model=PositivePayFileResponse)
async def get_file_detail(
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    row = await _get_scoped_file(db, file_id, entity_id)
    return _file_to_response(row)


@router.get("/{file_id}/download")
async def download_file(
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Stream the rendered Positive Pay file from MinIO.

    Keys are stamped ``<org_id>/positive-pay/<file_id>/<filename>``. The first
    segment must equal the caller's org — same 404 for wrong-org and missing
    file so the response can't enumerate prefixes (mirrors the invoice /
    contract file endpoints). The file itself legitimately carries full account
    numbers; that's why it's behind the read-role gate and never logged.
    """
    row = await _get_scoped_file(db, file_id, entity_id)
    if not row.file_key:
        raise HTTPException(status_code=404, detail="File not found")
    if row.file_key.split("/", 1)[0] != str(org_id):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content, content_type = await storage.get_file(row.file_key)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found") from None
    return Response(content=content, media_type=content_type)


# --------------------------------------------------------------------------- #
# Process return — classify the bank's presented items, raise fraud Exceptions
# --------------------------------------------------------------------------- #


@router.post("/{file_id}/process-return", response_model=ProcessReturnResponse)
async def process_return(
    file_id: uuid.UUID,
    body: ProcessReturnRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    """Process the bank's return against a check-issue file.

    Classifies each presented item (``matched_ok`` / ``amount_mismatch`` —
    altered / ``not_on_file`` — never issued) against the POINT-IN-TIME
    ``meta["issued_map"]`` snapshot persisted on the file at generation —
    what was actually sent to the bank — never a live re-query of the run's
    payments (a cheque issued-then-voided must still classify against what
    the bank was told, not the payment's current status; issue #178).
    Raises a deduped ``fraud_flag`` Exception for every fraud signal:
    ``amount_mismatch`` rows map to their invoice; ``not_on_file`` rows
    (a cheque we never wrote) have no invoice and become a standalone
    ``invoice_id=None`` fraud_flag in the queue. The file flips to
    ``returned_processed`` with a PII-free summary in ``meta``.
    """
    row = await _get_scoped_file(db, file_id, entity_id)
    if row.file_type != FILE_TYPE_CHECK_ISSUE:
        raise HTTPException(
            status_code=422, detail="Return processing is only valid for check-issue files"
        )

    # Classify against the persisted point-in-time snapshot, not a live
    # re-derivation from the run's CURRENT payment statuses.
    issued_map_snapshot: dict = (row.meta or {}).get("issued_map") or {}
    issued_items: list[IssuedItem] = []
    issued_map: dict[str, uuid.UUID] = {}
    for key, entry in issued_map_snapshot.items():
        issued_items.append(IssuedItem(check_number=key, amount=Decimal(str(entry["amount"]))))
        issued_map[key] = uuid.UUID(entry["invoice_id"])

    classification = classify_presented_items(
        [PresentedItem(check_number=p.check_number, amount=p.amount) for p in body.presented_items],
        issued_items,
    )

    exceptions_created = 0
    for result in classification.results:
        if result.classification not in (CLASS_AMOUNT_MISMATCH, CLASS_NOT_ON_FILE):
            continue
        # Map the presented cheque back to its invoice. amount_mismatch matched
        # an issued cheque by number (matched_check_number is set) → invoice-
        # scoped fraud_flag. not_on_file has no issued cheque with its number —
        # a cheque we *never wrote*, the strongest fraud signal — so it has no
        # invoice and becomes a standalone (invoice_id=None) fraud_flag.
        key = result.matched_check_number or normalize_check_number(result.check_number)
        invoice_id = issued_map.get(key)

        reason = (
            "altered amount"
            if result.classification == CLASS_AMOUNT_MISMATCH
            else "not on issued file"
        )
        description = f"Positive Pay return: check {result.check_number} {reason}"

        # Dedupe so re-processing a bank redelivery doesn't pile up duplicates.
        # The key is the DESCRIPTION — which carries this cheque's number and
        # its verdict — on both branches, narrowed by `invoice_id` when we have
        # one.
        #
        # It used to be `(invoice_id, "fraud_flag")` on the invoice-scoped
        # branch, mirroring `invoice_warnings._ensure_exception`. That is the
        # right key for a re-derived per-invoice rule (re-running extraction
        # must not re-raise the round-amount flag), but it is the wrong key for
        # a bank return: the invoice may ALREADY carry an open `fraud_flag`
        # raised by something else entirely — the round-amount rule (severity
        # `info`), a settlement-amount divergence, a cheque from an earlier run
        # on the same invoice — and that unrelated row silently absorbed the
        # bank telling us a cheque had been ALTERED. `exceptions_created` came
        # back 0, the queue showed only the pre-existing (possibly
        # `info`-severity) description, and the fraud signal Positive Pay exists
        # to deliver never arrived.
        # Narrowing the key can only ever raise MORE rows, never suppress one.
        dedupe = select(func.count()).where(
            APException.exception_type == "fraud_flag",
            APException.status.in_(["open", "escalated"]),
            APException.description == description,
        )
        if invoice_id is not None:
            dedupe = dedupe.where(APException.invoice_id == invoice_id)
        else:
            dedupe = dedupe.where(APException.invoice_id.is_(None))
        already = (await db.execute(dedupe)).scalar() or 0
        if already > 0:
            continue

        # Shared chokepoint → also emits the `exception.raised` outbound webhook.
        # No Invoice object is loaded here (a return may even be invoice-less),
        # so we pass the bare invoice_id + entity_id; the emit payload then
        # carries identifiers only (no number/vendor/amount) for this source.
        await create_exception(
            db,
            exception_type="fraud_flag",
            severity="error",
            status="open",
            description=description,
            organization_id=org_id,
            invoice_id=invoice_id,
            entity_id=entity_id,
            # The BANK raised this: the return file says a cheque was altered,
            # stale-dated or never issued. The operator who imports the file is
            # not the actor the flag asks about, and stamping them would bar the
            # one person who runs Positive Pay from ever clearing its returns.
            raised_by_user_id=None,
        )
        exceptions_created += 1

    return_summary = {
        "presented_count": classification.presented_count,
        "matched_ok": classification.matched_ok,
        "amount_mismatches": classification.amount_mismatch,
        "not_on_file": classification.not_on_file,
        "exceptions_created": exceptions_created,
    }
    meta = dict(row.meta) if row.meta else {}
    # Keep the latest summary for quick reads, but append every run to a
    # history list so re-processing a bank redelivery never silently clobbers
    # the prior outcome an auditor may be relying on.
    meta["return_summary"] = return_summary
    history = list(meta.get("return_history") or [])
    history.append(return_summary)
    meta["return_history"] = history
    row.meta = meta
    row.status = STATUS_RETURNED_PROCESSED

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="positive_pay.return_processed",
        entity_type="positive_pay_file",
        entity_id=row.id,
        details=return_summary,
    )
    await db.commit()
    await db.refresh(row)
    return ProcessReturnResponse(
        presented_count=classification.presented_count,
        matched_ok=classification.matched_ok,
        amount_mismatches=classification.amount_mismatch,
        not_on_file=classification.not_on_file,
        exceptions_created=exceptions_created,
        file=_file_to_response(row),
    )


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    row = await _get_scoped_file(db, file_id, entity_id)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="positive_pay.deleted",
        entity_type="positive_pay_file",
        entity_id=row.id,
        details={"file_type": row.file_type, "bank_format": row.bank_format},
    )
    # The rendered object is the only place full account / routing numbers live;
    # remove it so deletion doesn't leave PII-bearing bytes at rest. Best-effort
    # (a storage hiccup must not block the DB delete); captured before the row
    # goes away.
    file_key = row.file_key
    await db.delete(row)
    await db.commit()
    await storage.delete_file(file_key)
