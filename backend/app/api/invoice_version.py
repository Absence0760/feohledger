"""The invoice version a client saw, and the refusals that bind a write to it.

`InvoiceResponse.updated_at` is the version token: a client captures it with the
invoice it shows and echoes it back as `expected_updated_at`. The check always
runs against the row as read under `get_invoice_for_update`'s lock (or the bulk
path's `FOR UPDATE`), so a concurrent writer cannot land between the comparison
and the write it guards.

Two writes are bound this way:

- `PATCH /invoices/{id}` — an edit the client never saw is not overwritten
  (`INVOICE_STALE_EDIT`).
- Approval — `POST /invoices/{id}/approve` and the `approved` target of
  `POST /invoices/bulk/status`. An edit landing between an approver reading an
  invoice and clicking Approve would otherwise be approved unseen, for managers
  as well as clerks (`INVOICE_STALE_APPROVAL`; `docs/decisions.md` §260).

The token is optional on the wire, matching the PATCH precedent: the threat it
answers is an honest approver signing figures they were not shown, and every
first-party client (the web modal and bulk bar, the mobile app) sends it. A
caller who could omit it already holds `invoice.approve` and could approve the
current version directly; the check adds nothing against them. The out-of-app
doors (email / Slack / Teams) carry no `updated_at` — their tokens bind the
facts the message displayed instead (`services/email_action_token.py`).
"""

from datetime import UTC, datetime

from app.models.invoice import Invoice

#: The optimistic-concurrency refusal on `PATCH /invoices/{id}`. A client
#: BRANCHES on it — the web invoice modal turns it into a reload prompt — so it
#: is keyed on this code, never on the sentence (`api/refusals.coded_refusal`).
INVOICE_STALE_EDIT = "invoice_stale_edit"

#: The same refusal on approval: the invoice changed after the approver loaded
#: it. Its own code, because the remedy differs — an edit is reapplied, an
#: approval is re-reviewed — and both clients state it in the reader's language.
INVOICE_STALE_APPROVAL = "invoice_stale_approval"

STALE_APPROVAL_MESSAGE = (
    "This invoice was changed after you loaded it. Reload it and review the "
    "current version before approving."
)


def matches_loaded_version(invoice: Invoice, expected: datetime) -> bool:
    """Whether `expected` (the client's echoed `updated_at`) is the row's current one.

    `invoice.updated_at` is always tz-aware (Postgres `timestamptz`); a naive
    client timestamp is read as UTC rather than raising on a naive/aware
    comparison — it is a token round-tripped from our own response, not
    user-authored input worth refusing over a missing `Z`.
    """
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=UTC)
    return expected == invoice.updated_at
