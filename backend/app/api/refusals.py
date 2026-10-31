"""Refusals a client must be able to state in the reader's language.

The one builder for every coded refusal, so no surface can drift on the shape:
the employee and supplier-portal auth routers (`api/auth.py`,
`api/portal_auth.py`), the approval path (`services/approval_chain.py`,
`services/review.py`), the exception queue's segregation refusal
(`services/exception_lifecycle.py`), credit-memo application
(`api/credit_memos.py`), the invoice stale-edit 409 (`api/invoices.py`), self-service signup
(`api/signup.py`), vendor merge (`services/vendor_merge.py`) and the shared
rate limiter's 429 (`services/rate_limit.py`).
A server-side catcher that turns one back into text reads it through
`utils/http.detail_text`, never `str(exc.detail)`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException


def coded_refusal(code: str, message: str, **params: object) -> dict:
    """The `detail` of a refusal a client must be able to localize.

    `{"code", "message", "params"}`: a stable machine-readable `code` the client
    keys a translated sentence on, the typed `params` that sentence needs, and
    the English `message` as the fallback for a code the client predates. The
    status code is unchanged — FastAPI serializes an object `detail` as-is, and
    every web client that flattens `detail` to text (`formatApiDetail`, used by
    both `api.ts` and `portalApi.ts`) already renders an object by its
    `message`. Server-composed English inside a translated page is the defect
    this exists to close (`frontend/CLAUDE.md` § Internationalization).

    A code is only ever as specific as the English it replaces: it must never
    distinguish cases the message deliberately folds together (a wrong password
    from a wrong code, an unknown account from a known one).
    """
    return {"code": code, "message": message, "params": params}


#: An action verdict's refusal for a caller whose roles / permissions the
#: action's endpoint would turn away. The endpoints themselves answer that with
#: ``require_roles`` / ``require_permission``'s plain-string 403 (unchanged —
#: every client reads it); a verdict is read BEFORE any click, so it carries the
#: same sentence under a code the page can localize. Built from the same
#: predicates the dependencies use (``api/deps.holds_any_role`` /
#: ``holds_any_permission``), never a restated role list.
ROLE_NOT_PERMITTED = "role_not_permitted"
ROLE_NOT_PERMITTED_MESSAGE = "Your role does not permit this action."


@dataclass(frozen=True)
class ActionRefusal:
    """One gate's refusal, in the two shapes it is read in.

    The gate function returns it (or ``None``); the endpoint raises
    :meth:`http` and the verdict a page reads before offering the control is
    :meth:`verdict` — so both come from the one function (issue #575).
    :meth:`http` keeps the plain-string ``detail`` the endpoint always answered
    with, for the clients (mobile included) that read it as text — unless
    ``coded`` is set, for an endpoint that already answered with the coded
    ``{code, message, params}`` detail (the NACHA export's refusals), which it
    keeps. ``params`` ride both shapes' coded form.
    """

    code: str
    message: str
    http_status: int
    params: dict | None = None
    coded: bool = False

    def http(self) -> HTTPException:
        detail = self.verdict() if self.coded else self.message
        return HTTPException(status_code=self.http_status, detail=detail)

    def verdict(self) -> dict:
        return coded_refusal(self.code, self.message, **(self.params or {}))


def role_not_permitted() -> dict:
    """The coded verdict for :data:`ROLE_NOT_PERMITTED`."""
    return coded_refusal(ROLE_NOT_PERMITTED, ROLE_NOT_PERMITTED_MESSAGE)


def refusal_of(exc: HTTPException, *, fallback_code: str) -> dict:
    """An endpoint gate's raised refusal, as an action verdict.

    A verdict is computed by RUNNING the gate the endpoint runs and catching
    what it raises, so the page and the endpoint cannot disagree on a rule
    (issue #575 § One rule, one place). A coded detail passes through as-is; a
    gate still answering with a plain string is wrapped under
    ``fallback_code`` with that string as its ``message``.
    """
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return detail
    return coded_refusal(fallback_code, str(detail))


#: A wrong authenticator code on a SIGNED-IN factor change that takes only a
#: code — confirming a new enrollment (employee and portal) and the portal's
#: disable. A 400, not a 401: the session is valid and only the code is wrong,
#: and both web clients read a 401 on an authenticated call as an expired
#: session — they clear the token and bounce to the login page, so a mistyped
#: code used to sign the user out. Exactly as specific as the "Invalid code" it
#: replaces. The login challenge's own "Invalid code" stays a 401: there is no
#: session yet, so a 401 is the truth and nobody is signed out by it.
MFA_CODE_INVALID_DETAIL = coded_refusal("mfa_code_invalid", "Invalid code")


#: An ERP pull (chart, vendor or PO sync) refused because the org's OAuth
#: connection is gone — never consented, revoked at the provider, or past its
#: lifetime (``erp_oauth.ErpNotConnectedError``). A 409: the request was fine
#: and only the connection's state stops it, and an administrator reconnecting
#: resolves it. Before this it was the 502 "ERP request failed:
#: ErpNotConnectedError", which named an exception class to a person who could
#: not act on one (live QuickBooks sandbox, 2026-10-19).
ERP_NOT_CONNECTED = "erp_not_connected"

#: An ERP pull whose adapter could not read the whole collection
#: (``ErpListingIncomplete``): a page failed (``gap = fetch_failed``) or the
#: walk hit the adapter's row bound (``truncated``; each adapter has its own).
#: Still a 502 and nothing synced, but stated, where it used to read "ERP
#: request failed: ErpListingIncomplete" — a class name.
ERP_LISTING_INCOMPLETE = "erp_listing_incomplete"


def erp_pull_failure(exc: Exception) -> HTTPException:
    """The HTTP answer for an ERP sync whose adapter read raised ``exc``.

    A lost connection is the coded 409 above, and a listing that could not be
    read whole is a coded 502. Anything else — a provider
    outage, a token refresh that could not reach the provider
    (``ErpTokenRefreshError``, whose connection is still good), an incomplete
    listing — stays the 502 it always was: the gateway failed and a later
    retry may succeed.
    """
    from app.services.erp_adapters.base import (
        ERP_CALL_BUDGET_EXHAUSTED,
        ErpCallBudgetExhausted,
        ErpListingIncomplete,
    )
    from app.services.erp_oauth import ErpNotConnectedError, ErpTokenRefreshError

    if isinstance(exc, ErpCallBudgetExhausted):
        # Refused before the call by the shared-quota budget (decisions §322):
        # a 429 with the time it frees up, nothing read and nothing synced.
        wait = max(1, int((exc.retry_after - datetime.now(UTC)).total_seconds()))
        return HTTPException(
            status_code=429,
            detail=coded_refusal(
                ERP_CALL_BUDGET_EXHAUSTED,
                f"{exc.provider} API calls are paused ({exc.reason}) until "
                f"{exc.retry_after.isoformat()}, so nothing was synced.",
                provider=exc.provider,
                reason=exc.reason,
                retry_after=exc.retry_after.isoformat(),
            ),
            headers={"Retry-After": str(wait)},
        )

    if isinstance(exc, ErpListingIncomplete):
        return HTTPException(
            status_code=502,
            detail=coded_refusal(
                ERP_LISTING_INCOMPLETE,
                f"{exc.provider} could not be read in full ({exc.gap.value}), so nothing was "
                "synced.",
                provider=exc.provider,
                gap=exc.gap.value,
            ),
        )

    if isinstance(exc, ErpNotConnectedError) and not isinstance(exc, ErpTokenRefreshError):
        return HTTPException(
            status_code=409,
            detail=coded_refusal(
                ERP_NOT_CONNECTED,
                "The ERP isn't connected, or its connection has expired. An administrator "
                "can reconnect it under Organization, then sync again.",
            ),
        )
    return HTTPException(status_code=502, detail=f"ERP request failed: {type(exc).__name__}")


#: An invoice whose due date falls before its invoice date. QuickBooks accepts
#: such a bill over the API and then flags it invalid in its own UI (live
#: sandbox, 2026-10-20: changing only the invoice date to escape a closed
#: period left bill 154 due five days before it was dated). Entry refuses the
#: pair; the ERP push refuses it too (``services/erp``), for invoices that
#: arrive by extraction, CSV or the recurring sweep.
INVOICE_DUE_BEFORE_DATE = "invoice_due_before_date"


def refuse_due_before_invoice_date(invoice_date, due_date) -> None:
    """422 when both dates are set and the invoice is due before it is dated."""
    if invoice_date is not None and due_date is not None and due_date < invoice_date:
        raise HTTPException(
            status_code=422,
            detail=coded_refusal(
                INVOICE_DUE_BEFORE_DATE,
                "The due date is before the invoice date. Correct one of them.",
            ),
        )


#: An employee token presented on another workspace's page (``tenant.get_tenant``:
#: the token's ``org`` claim is not the requested tenant). Still a 403 and still
#: refused; coded so the SPA can tell this from an ordinary permission refusal
#: and treat it like an expired session — clear the token and send the user to
#: this workspace's sign-in page — instead of leaving a signed-in screen where
#: every call fails (production, 2026-10-20; ``docs/decisions.md`` §288).
TOKEN_TENANT_MISMATCH = "token_tenant_mismatch"
TOKEN_TENANT_MISMATCH_DETAIL = coded_refusal(
    TOKEN_TENANT_MISMATCH, "Token does not match the requested tenant"
)


#: An SSO / SAML first sign-in whose IdP asserted an address that already names
#: an account in ANOTHER workspace (``identity_provisioning.EmailInOtherWorkspace``).
#: ``users.email`` is globally unique, so this workspace cannot create a second
#: account for it; before this was coded the insert raised at flush and both
#: callbacks answered a bare 500 (``docs/decisions.md`` §291). A 403 with no
#: params: deliberately no link to the account's own sign-in page, because this
#: workspace's IdP — which its own admin controls — can assert any address, so
#: a link would hand that admin the name of another tenant's workspace for any
#: address they type.
SSO_EMAIL_IN_OTHER_WORKSPACE = "sso_email_in_other_workspace"
SSO_EMAIL_IN_OTHER_WORKSPACE_DETAIL = coded_refusal(
    SSO_EMAIL_IN_OTHER_WORKSPACE,
    "This email address already belongs to an account in another workspace, so it "
    "can't sign in here. Sign in on your own workspace's page, or ask this "
    "workspace's administrator to have your identity provider use a different address.",
)

#: ``POST`` / ``PATCH /api/admin/users`` with an address an account in THIS
#: workspace already holds. A 409.
EMAIL_IN_THIS_WORKSPACE = "email_in_this_workspace"
EMAIL_IN_THIS_WORKSPACE_DETAIL = coded_refusal(
    EMAIL_IN_THIS_WORKSPACE, "A user with this email address already exists in this workspace."
)

#: The same writes with an address an account in ANOTHER workspace holds. A 409
#: that names no workspace and no account — only that the address is taken and
#: what to do instead, since one address is one account in one workspace
#: (``docs/decisions.md`` §291).
EMAIL_IN_OTHER_WORKSPACE = "email_in_other_workspace"
EMAIL_IN_OTHER_WORKSPACE_DETAIL = coded_refusal(
    EMAIL_IN_OTHER_WORKSPACE,
    "This email address already has an account in another workspace. An address "
    "can belong to only one workspace, so use a different address for this person. "
    "If they no longer use that other workspace, its administrator can close it, "
    "which frees the address.",
)

#: The admin path's other-workspace budget is spent (``admin.EMAIL_COLLISION_LIMIT``
#: an hour per workspace). A 429 for every create / email rename, free addresses
#: included, until the window rolls — refusing only taken ones would make the
#: 429 itself the oracle the budget bounds (``docs/decisions.md`` §291).
EMAIL_CHECK_THROTTLED = "email_check_throttled"
EMAIL_CHECK_THROTTLED_DETAIL = coded_refusal(
    EMAIL_CHECK_THROTTLED,
    "Too many addresses entered in this workspace recently already belonged to other "
    "workspaces, so adding users or changing their email is paused for up to an hour. "
    "Try again later.",
)


# --- Closing a workspace (`api/organization_close.py`, decisions §306) --------

#: The typed confirmation did not match the workspace's slug. A 400; nothing
#: was checked or changed.
WORKSPACE_CLOSE_CONFIRMATION_MISMATCH = "workspace_close_confirmation_mismatch"
WORKSPACE_CLOSE_CONFIRMATION_MISMATCH_DETAIL = coded_refusal(
    WORKSPACE_CLOSE_CONFIRMATION_MISMATCH,
    "Type this workspace's address exactly as shown to confirm that it should be closed.",
)

#: The workspace holds something a self-service closure must not destroy. A
#: 409 whose `params.blockers` lists the stable codes
#: (`services/workspace_closure.BLOCKERS`), so the page can say what each one
#: is. The way forward for any of them is support, never a retry.
WORKSPACE_CLOSE_REFUSED = "workspace_close_refused"


def workspace_close_refused_detail(blockers: list[str]) -> dict:
    return coded_refusal(
        WORKSPACE_CLOSE_REFUSED,
        "This workspace can't be closed here because it holds records that must be "
        "kept or other people's accounts. Contact support to close it.",
        blockers=list(blockers),
    )


#: Under the closure's lock, the requester was no longer an active administrator
#: of the workspace (deactivated, demoted or removed since the request began).
#: A 403; nothing was deleted.
WORKSPACE_CLOSE_ACTOR_NOT_ADMIN = "workspace_close_actor_not_admin"
WORKSPACE_CLOSE_ACTOR_NOT_ADMIN_DETAIL = coded_refusal(
    WORKSPACE_CLOSE_ACTOR_NOT_ADMIN,
    "Only an active administrator of this workspace can close it.",
)

#: Another closure of the same workspace is running. A 409.
WORKSPACE_CLOSE_IN_PROGRESS = "workspace_close_in_progress"
WORKSPACE_CLOSE_IN_PROGRESS_DETAIL = coded_refusal(
    WORKSPACE_CLOSE_IN_PROGRESS,
    "This workspace is already being closed.",
)

#: The closure's audit record could not be written or shipped to the
#: write-once archive, so nothing was deleted. A 503; a retry is safe.
WORKSPACE_CLOSE_AUDIT_UNAVAILABLE = "workspace_close_audit_unavailable"
WORKSPACE_CLOSE_AUDIT_UNAVAILABLE_DETAIL = coded_refusal(
    WORKSPACE_CLOSE_AUDIT_UNAVAILABLE,
    "The workspace could not be closed right now because its audit record could not be "
    "saved. Nothing was deleted. Try again in a few minutes.",
)

#: The deletion started and failed part-way. A 503; a retry by the same
#: administrator finishes it (every step of `services/tenant_deletion` is
#: idempotent, and the guards are not re-run against a database already gone).
WORKSPACE_CLOSE_INCOMPLETE = "workspace_close_incomplete"
WORKSPACE_CLOSE_INCOMPLETE_DETAIL = coded_refusal(
    WORKSPACE_CLOSE_INCOMPLETE,
    "Closing the workspace did not finish. Try again; if it fails again, contact support.",
)
