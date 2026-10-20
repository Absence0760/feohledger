"""Refusals a client must be able to state in the reader's language.

The one builder for every coded refusal, so no surface can drift on the shape:
the employee and supplier-portal auth routers (`api/auth.py`,
`api/portal_auth.py`), the approval path (`services/approval_chain.py`,
`services/review.py`), the exception queue's segregation refusal
(`services/exception_lifecycle.py`), credit-memo application
(`api/credit_memos.py`) and the invoice stale-edit 409 (`api/invoices.py`).
A server-side catcher that turns one back into text reads it through
`utils/http.detail_text`, never `str(exc.detail)`.
"""

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
    from app.services.erp_adapters.base import ErpListingIncomplete
    from app.services.erp_oauth import ErpNotConnectedError, ErpTokenRefreshError

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
    "can belong to only one workspace, so use a different address for this person.",
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
