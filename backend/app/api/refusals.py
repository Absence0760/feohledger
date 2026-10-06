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
