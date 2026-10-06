"""Email approval — approve / reject an assigned invoice from an email link.

``GET  /api/invoices/email-action/{token}``          — PUBLIC, renders a confirm page
``POST /api/invoices/email-action/{token}/confirm``  — PUBLIC, performs the action

The single-action token (built in :mod:`app.services.email_action_token`) IS the
credential — there is no JWT and no session, so both routes are public-by-design
and live in ``ALTERNATE_AUTH``. The token is HMAC-signed over the tenant +
invoice + reviewer + action + expiry, so it can't be forged or have its action
flipped.

Two-step on purpose: the **GET only renders an HTML confirmation page** — it
never mutates — so an email link-prefetcher or corporate security scanner that
issues a bare GET can't auto-approve an invoice. The state change happens only
on the **POST** the reviewer submits from that page.

The POST re-runs the *normal* :mod:`app.services.review` approve/reject path as
the reviewer named in the token, so segregation of duties, the approval
thresholds, the CFO gate, the immutable audit row, and the approval digital
signature all apply exactly as if they had logged in. Single-use is layered:
the workflow state machine (the invoice must be in ``ready_for_review``) plus a
Redis consume on the token's PAIR (``ActionToken.consume_key``) — redeeming
either link of a message spends both, which closes the reject→resubmit replay
window.
"""

from __future__ import annotations

import html
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.api.permissions import PERM_INVOICE_APPROVE, effective_permissions
from app.config import settings
from app.database import _make_tenant_url, get_control_db
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.user import User
from app.services import review as review_svc
from app.services.email_action_token import (
    ACTION_APPROVE,
    ActionToken,
    verify_action_token,
)
from app.utils.http import detail_text

logger = logging.getLogger(__name__)

public_router = APIRouter(prefix="/invoices", tags=["email-approval"])

_CONSUMED_PREFIX = "email_action:consumed:"


def may_approve(reviewer: User) -> bool:
    """Is this reviewer allowed to decide an invoice, out of the app?

    Gates on the same granular permission the in-app route does
    (``workflow.py`` → ``require_permission(PERM_INVOICE_APPROVE)``), not on a
    hardcoded role-name set. The out-of-app surfaces must not be a *weaker* door
    than the in-app one — and they must not be a needlessly *narrower* one
    either: the whole point of the permission layer is that an org can mint a
    custom role carrying ``invoice.approve``, and such a reviewer could approve
    in the app while every approval email, Slack button and Teams card sent to
    them dead-ended on "not permitted".

    For the four system roles this is exactly equivalent to the role-name set it
    replaces — ``ROLE_DEFAULT_PERMISSIONS`` grants ``invoice.approve`` to
    admin / ap_manager / cfo and to no one else — so nothing about the built-in
    matrix changes. Shared with the Slack and Teams interactivity endpoints so
    the three surfaces can't drift.
    """
    return PERM_INVOICE_APPROVE in effective_permissions(reviewer.roles or ())


# ---------------------------------------------------------------------------
# Minimal, self-contained HTML rendering (no SPA dependency, no external assets)
# ---------------------------------------------------------------------------


def _page(title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<meta name=robots content=noindex>"  # keep approval links out of search indexes
        f"<title>{html.escape(title)}</title></head>"
        '<body style="font-family:system-ui,sans-serif;max-width:34rem;margin:3rem auto;'
        'padding:0 1rem;color:#1f2937;line-height:1.5">'
        f"{body}</body></html>"
    )
    return HTMLResponse(content=doc, status_code=status_code)


def _invalid_link_page() -> HTMLResponse:
    return _page(
        "Link invalid or expired",
        "<h1>This link is invalid or has expired</h1>"
        "<p>Email approval links expire for security. Please sign in to the app "
        "to review this invoice.</p>",
        status_code=400,
    )


def _info_page(title: str, message: str) -> HTMLResponse:
    return _page(title, f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>")


def _invoice_ref(invoice: Invoice) -> str:
    ref = f"Invoice {invoice.invoice_number} ({invoice.vendor_name})"
    if invoice.amount is not None:
        ref += f" for {invoice.currency or 'USD'} {invoice.amount:,.2f}"
    return ref


def _confirm_page(token: str, action: str, invoice: Invoice) -> HTMLResponse:
    ref = html.escape(_invoice_ref(invoice))
    verb = "Approve" if action == ACTION_APPROVE else "Reject"
    colour = "#16a34a" if action == ACTION_APPROVE else "#dc2626"
    action_url = f"/api/invoices/email-action/{html.escape(token, quote=True)}/confirm"
    reason_field = (
        ""
        if action == ACTION_APPROVE
        else (
            "<p><label>Reason (optional)<br>"
            '<textarea name=reason rows=3 style="width:100%;box-sizing:border-box" '
            'placeholder="Why is this being rejected?"></textarea></label></p>'
        )
    )
    body = (
        f"<h1>{verb} invoice?</h1>"
        f"<p>You are about to <strong>{verb.lower()}</strong> {ref}.</p>"
        f'<form method=post action="{action_url}">'
        f"{reason_field}"
        f'<button type=submit style="background:{colour};color:#fff;border:0;'
        'padding:10px 20px;border-radius:4px;font-size:1rem;cursor:pointer">'
        f"Confirm {verb.lower()}</button>"
        "</form>"
    )
    return _page(f"{verb} invoice", body)


# ---------------------------------------------------------------------------
# Shared resolution: token -> (org, tenant-session-factory, invoice, reviewer)
# ---------------------------------------------------------------------------


async def _resolve_org(ctrl_db: AsyncSession, slug: str) -> Organization | None:
    result = await ctrl_db.execute(select(Organization).where(Organization.slug == slug))
    return result.scalar_one_or_none()


@asynccontextmanager
async def _tenant_session(org: Organization) -> AsyncIterator[AsyncSession]:
    """Open a short-lived session bound to the tenant DB, disposed on exit.

    A fresh per-call engine (``pool_size=1``) — same shape as
    ``peppol_receive`` / email-intake — not the module-global pool: this route
    has no ``get_tenant_db`` dependency (the token, not a header, carries the
    tenant), and a per-call engine keeps the connection on the request's own
    event loop and is overridable/clean in tests."""
    engine = create_async_engine(_make_tenant_url(org.db_name), pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _load_reviewer(
    ctrl_db: AsyncSession, actor_id: uuid.UUID, org_id: uuid.UUID
) -> User | None:
    """Load the reviewer named in the token, scoped to the token's org, with
    roles eager-loaded. Returns None if missing, inactive, or wrong org."""
    result = await ctrl_db.execute(
        select(User).options(selectinload(User.roles)).where(User.id == actor_id)
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or user.organization_id != org_id:
        return None
    return user


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@public_router.get("/email-action/{token}", response_class=HTMLResponse)
async def email_action_confirm_page(
    token: str,
    ctrl_db: AsyncSession = Depends(get_control_db),
) -> HTMLResponse:
    """Render the confirmation page for an email-approval link. Read-only —
    never mutates (so prefetchers can't auto-approve)."""
    decoded = verify_action_token(token, settings.email_action_signing_key)
    if decoded is None:
        return _invalid_link_page()

    org = await _resolve_org(ctrl_db, decoded.tenant_slug)
    if org is None:
        return _invalid_link_page()

    async with _tenant_session(org) as db:
        invoice = (
            await db.execute(select(Invoice).where(Invoice.id == decoded.invoice_id))
        ).scalar_one_or_none()

    if invoice is None:
        return _invalid_link_page()
    if invoice.status != InvoiceStatus.ready_for_review:
        return _info_page(
            "No longer awaiting review",
            "This invoice is no longer awaiting review — it may already have been "
            "approved, rejected, or reassigned. Sign in to the app to see its status.",
        )
    return _confirm_page(token, decoded.action, invoice)


@public_router.post("/email-action/{token}/confirm", response_class=HTMLResponse)
async def email_action_perform(
    token: str,
    reason: str = Form(default=""),
    ctrl_db: AsyncSession = Depends(get_control_db),
) -> HTMLResponse:
    """Perform the approve/reject encoded in the token, as the named reviewer."""
    decoded = verify_action_token(token, settings.email_action_signing_key)
    if decoded is None:
        return _invalid_link_page()

    org = await _resolve_org(ctrl_db, decoded.tenant_slug)
    if org is None:
        return _invalid_link_page()

    reviewer = await _load_reviewer(ctrl_db, decoded.actor_id, org.id)
    if reviewer is None:
        return _info_page(
            "Account unavailable",
            "Your account could not be verified for this action. Please sign in to "
            "the app to review this invoice.",
        )
    reviewer_roles = {r.name for r in (reviewer.roles or [])}
    if not may_approve(reviewer):
        return _info_page(
            "Not permitted",
            "Your account is not permitted to approve or reject invoices. Please "
            "contact your administrator.",
        )

    # Single-use consume on the token jti — claim it BEFORE mutating, release it
    # if the action turns out not to be applicable / permitted so the reviewer
    # can still act in-app. A genuine success keeps the claim for the token TTL.
    consumed = await _claim_jti(decoded)
    if not consumed:
        return _info_page(
            "Already used",
            "This approval link has already been used. Sign in to the app to see "
            "the invoice's current status.",
        )

    try:
        async with _tenant_session(org) as db:
            result = await _apply_action(
                db, decoded, reviewer, reviewer_roles, reason, org_settings=org.settings
            )
            if result.committed:
                await db.commit()
            else:
                await db.rollback()
                await _release_jti(decoded)
            return result.page
    except HTTPException as exc:
        # Threshold / CFO gate / segregation — the reviewer genuinely can't take
        # this action from email. Release the claim so they can sign in instead.
        await _release_jti(decoded)
        return _info_page("Action not allowed", detail_text(exc.detail) or "")
    except Exception:  # noqa: BLE001 — never surface a stack trace on a public route
        logger.exception("email_action: unexpected failure for action=%s", decoded.action)
        await _release_jti(decoded)
        return _page(
            "Something went wrong",
            "<h1>Something went wrong</h1><p>Please sign in to the app to review this invoice.</p>",
            status_code=500,
        )


class _ActionResult:
    __slots__ = ("committed", "page")

    def __init__(self, committed: bool, page: HTMLResponse):
        self.committed = committed
        self.page = page


async def _apply_action(
    db: AsyncSession,
    decoded: ActionToken,
    reviewer: User,
    reviewer_roles: set[str],
    reason: str,
    *,
    org_settings: dict | None = None,
) -> _ActionResult:
    """Run the approve/reject against a row-locked invoice. Returns whether to
    commit + the page to render. May raise HTTPException (threshold/segregation/
    CFO gate) — the caller releases the jti claim and renders the detail.

    ``org_settings`` must be threaded through: approving from an email is the
    same decision as approving in-app, so it has to read the same org
    ``fraud_rules`` / ``matching`` tolerances / structuring window. Omitting it
    reverted all of them to the platform default for this one door."""
    from app.services.workflow_engine import get_invoice_for_update

    invoice = await get_invoice_for_update(db, decoded.invoice_id)
    if invoice.status != InvoiceStatus.ready_for_review:
        return _ActionResult(
            False,
            _info_page(
                "No longer awaiting review",
                "This invoice is no longer awaiting review — it may already have "
                "been approved, rejected, or reassigned.",
            ),
        )

    if decoded.action == ACTION_APPROVE:
        await review_svc.approve_invoice(
            db,
            invoice,
            actor_id=reviewer.id,
            actor_name=reviewer.full_name,
            actor_roles=reviewer_roles,
            org_settings=org_settings,
        )
        # A multi-level approval chain records THIS level and leaves the invoice
        # in `ready_for_review` for the next approver — `approve_invoice` returns
        # early without transitioning. Reporting "has been approved" there tells
        # the reviewer the payable is cleared when it still needs someone else,
        # so read the resulting status rather than assuming the happy path.
        if invoice.status is InvoiceStatus.approved:
            return _ActionResult(
                True,
                _info_page(
                    "Invoice approved",
                    f"{_invoice_ref(invoice)} has been approved. Thank you.",
                ),
            )
        return _ActionResult(
            True,
            _info_page(
                "Approval recorded",
                f"Your approval of {_invoice_ref(invoice)} has been recorded. It still "
                "needs a further approval before it is cleared for payment.",
            ),
        )

    await review_svc.reject_invoice(
        db,
        invoice,
        actor_id=reviewer.id,
        actor_name=reviewer.full_name,
        reason=reason.strip() or "Rejected via email",
    )
    return _ActionResult(
        True,
        _info_page(
            "Invoice rejected",
            f"{_invoice_ref(invoice)} has been rejected.",
        ),
    )


# ---------------------------------------------------------------------------
# Redis single-use consume
# ---------------------------------------------------------------------------


async def _claim_jti(decoded: ActionToken) -> bool:
    """Atomically claim the token's pair. True = first use (proceed); False =
    already consumed. TTL matches the token validity so the key self-expires.

    Claims ``consume_key`` — the message's Approve/Reject PAIR — not the
    individual ``jti``: one message carries one decision, so the sibling link
    must not survive the invoice cycling back to ``ready_for_review``."""
    from app.redis import get_redis

    r = await get_redis()
    ttl = max(1, settings.email_action_ttl_hours * 3600)
    # SET NX EX — only set if absent. Returns True on first claim.
    claimed = await r.set(f"{_CONSUMED_PREFIX}{decoded.consume_key}", "1", nx=True, ex=ttl)
    return bool(claimed)


async def _release_jti(decoded: ActionToken) -> None:
    """Release a previously-claimed jti (best-effort) so a not-applicable /
    not-permitted attempt doesn't permanently burn the link."""
    try:
        from app.redis import get_redis

        r = await get_redis()
        await r.delete(f"{_CONSUMED_PREFIX}{decoded.consume_key}")
    except Exception:  # noqa: BLE001
        logger.warning("email_action: failed releasing jti claim")
