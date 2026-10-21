"""The wire shape of an action verdict (issue #575 § One rule, one place).

A list row that carries the caller's verdict on one of its actions carries
``None`` when the endpoint would accept the caller, else a :class:`CodedRefusal`
— the ``{code, message, params}`` detail ``api/refusals.coded_refusal`` builds,
which the endpoint itself would answer with. See
``backend/docs/api-surface.md`` § Action verdicts on list rows.
"""

from typing import Any

from pydantic import BaseModel


class CodedRefusal(BaseModel):
    """Why the endpoint would refuse the caller: a stable ``code`` the client
    localizes on, the server's English ``message`` as the fallback, and the
    ``params`` the localized sentence interpolates."""

    code: str
    message: str
    params: dict[str, Any] = {}
