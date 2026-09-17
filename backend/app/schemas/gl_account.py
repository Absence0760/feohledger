from pydantic import BaseModel, Field


class GLAccountCreate(BaseModel):
    code: str = Field(..., max_length=50)
    name: str = Field(..., max_length=255)
    account_type: str | None = None
    parent_code: str | None = None


class GLAccountUpdate(BaseModel):
    """Correct or retire an existing GL account.

    Two of the row's fields are deliberately ABSENT rather than optional, and
    the reason is the same in both cases: changing them would silently break
    something already recorded against the account.

    ``code`` — an invoice records its GL as a **string**
    (``Invoice.gl_account`` / ``InvoiceLineItem.gl_account``, both
    ``String(100)``), not as a foreign key. Renaming the code leaves every line
    already coded to the old one pointing at nothing, with no constraint to
    catch it and no way to tell afterwards which account was meant.

    ``entity_id`` — it is not a scoping column here, it is the row's meaning
    (``models/gl_account``): NULL means SHARED across every entity, anything
    else means that entity owns it. Moving a row between charts therefore
    either steals the account from every other entity or hands it to all of
    them, and the effective-chart uniqueness guard would have to re-run against
    both the old scope and the new one. A genuine move between charts is a
    create in the destination chart plus a deactivate of the original, which
    keeps both halves auditable and leaves the old code resolvable.

    Every field is optional and unset fields are left alone
    (``exclude_unset``), so a PATCH touching one field cannot blank the rest.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    account_type: str | None = Field(default=None, max_length=50)
    parent_code: str | None = Field(default=None, max_length=50)
    #: Retirement. There is no DELETE on this router — see the route docstring.
    is_active: bool | None = None
