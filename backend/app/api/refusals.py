"""Refusals a client must be able to state in the reader's language.

Shared by the employee auth surface (`api/auth.py`) and the supplier portal's
(`api/portal_auth.py`), so the two cannot drift on the shape.
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
