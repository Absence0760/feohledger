"""Re-extraction options — `skip_vendor_match` + `suppress_auto_approve`.

A supplier-portal resubmission (`POST /api/portal/invoices/{id}/resubmit`) swaps
the source file on an invoice that has ALREADY been triaged once, so the
corrected document has to be re-read — otherwise the reviewer reconciles a new
PDF against the stale fields from the version they rejected
(`docs/followups.md`). Two things must not happen on that pass:

* `vendor_matching.match_and_link_vendor` must NOT run. It is a fuzzy matcher;
  re-run over a re-typed vendor name it can land `Invoice.vendor_id` on a
  DIFFERENT supplier, which drops the invoice out of the `vendor_id ==`-scoped
  portal list (the vendor loses sight of their own resubmission) and re-points
  the payee whose `Vendor.bank_details` the payment run reads.
* The unattended auto-approve gates must NOT fire. A human rejected this
  document; approving its replacement with no human in the loop would let a
  supplier launder a rejected invoice past the reviewer who rejected it.

Both flags travel in BOTH dispatch modes, and both default to `False` — the
unchanged ingest behaviour — so an in-flight job written by an older producer
decodes to exactly what it used to do.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.extraction_dispatch import ExtractionOptions

# ---------------------------------------------------------------------------
# The options carrier itself
# ---------------------------------------------------------------------------


def test_options_default_to_todays_behaviour():
    opts = ExtractionOptions()
    assert opts.skip_vendor_match is False
    assert opts.suppress_auto_approve is False


def test_absent_payload_keys_decode_to_false():
    """Backward compatibility with an SQS message written before the keys
    existed: an absent key is False, never a truthy surprise on the money path."""
    assert ExtractionOptions.from_payload({}) == ExtractionOptions()
    assert ExtractionOptions.from_payload(None) == ExtractionOptions()
    assert (
        ExtractionOptions.from_payload({"invoice_id": "x", "org_id": "y", "actor_id": "z"})
        == ExtractionOptions()
    )


def test_payload_round_trips():
    opts = ExtractionOptions(skip_vendor_match=True, suppress_auto_approve=True)
    assert ExtractionOptions.from_payload(opts.as_payload()) == opts


# ---------------------------------------------------------------------------
# local mode — the in-process queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_dispatch_carries_the_flags_on_the_queue_tuple():
    import app.services.extraction_dispatch as mod

    with (
        patch.object(mod.settings, "extraction_mode", "local"),
        patch.object(mod, "_ensure_workers"),
    ):
        inv, org = uuid.uuid4(), uuid.uuid4()
        await mod.dispatch_extraction(
            inv, org, None, skip_vendor_match=True, suppress_auto_approve=True
        )
        item = mod._job_queue.get_nowait()

    assert item[:3] == (inv, org, None)
    assert item[3] == ExtractionOptions(skip_vendor_match=True, suppress_auto_approve=True)


def test_worker_tolerates_a_job_enqueued_without_the_options_slot():
    """A 3-tuple already sitting in the queue when this module is reloaded (dev
    auto-reload) must still drain. A crash here would strand the invoice in
    `pending` with no `failed` transition and no worker left running."""
    import app.services.extraction_dispatch as mod

    inv, org, actor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seen: list = []

    async def _fake_run_local(invoice_id, org_id, actor_id, options=None):
        seen.append((invoice_id, org_id, actor_id, options))

    original = list(mod._worker_threads)
    mod._worker_threads.clear()
    try:
        with patch.object(mod, "_run_local", _fake_run_local):
            mod._job_queue.put((inv, org, actor))  # legacy shape
            mod._ensure_workers()
            for _ in range(200):
                if seen:
                    break
                import time

                time.sleep(0.01)
    finally:
        mod._worker_threads.clear()
        mod._worker_threads[:] = original

    assert seen and seen[0][:3] == (inv, org, actor)
    assert seen[0][3] == ExtractionOptions()


# ---------------------------------------------------------------------------
# lambda mode — the SQS body
# ---------------------------------------------------------------------------


def test_sqs_body_carries_flat_backward_compatible_keys():
    import app.services.extraction_dispatch as mod

    client = MagicMock()
    inv, org, actor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with (
        patch("boto3.client", return_value=client),
        patch.object(mod.settings, "sqs_extraction_queue_url", "https://sqs.test/q"),
    ):
        mod._send_to_sqs(
            inv, org, actor, ExtractionOptions(skip_vendor_match=True, suppress_auto_approve=True)
        )

    body = json.loads(client.send_message.call_args.kwargs["MessageBody"])
    # The pre-existing keys are untouched — an older consumer keeps working.
    assert body["invoice_id"] == str(inv)
    assert body["org_id"] == str(org)
    assert body["actor_id"] == str(actor)
    # …and the new ones ride flat beside them, never nested.
    assert body["skip_vendor_match"] is True
    assert body["suppress_auto_approve"] is True


def test_sqs_body_omits_nothing_when_the_flags_are_off():
    """Explicit `false` rather than an omitted key: a consumer reading
    `.get(k, False)` behaves identically either way, and an explicit value
    makes the message self-describing in a dead-letter queue."""
    import app.services.extraction_dispatch as mod

    client = MagicMock()
    with (
        patch("boto3.client", return_value=client),
        patch.object(mod.settings, "sqs_extraction_queue_url", "https://sqs.test/q"),
    ):
        mod._send_to_sqs(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    body = json.loads(client.send_message.call_args.kwargs["MessageBody"])
    assert body["skip_vendor_match"] is False
    assert body["suppress_auto_approve"] is False


@pytest.mark.asyncio
async def test_lambda_mode_dispatch_hands_the_options_to_the_sender():
    import app.services.extraction_dispatch as mod

    with (
        patch.object(mod.settings, "extraction_mode", "lambda"),
        patch.object(mod, "_send_to_sqs") as sender,
    ):
        inv, org = uuid.uuid4(), uuid.uuid4()
        await mod.dispatch_extraction(inv, org, None, skip_vendor_match=True)

    sender.assert_called_once_with(
        inv, org, None, ExtractionOptions(skip_vendor_match=True, suppress_auto_approve=False)
    )


# ---------------------------------------------------------------------------
# run_extraction honours them
# ---------------------------------------------------------------------------


def test_run_extraction_accepts_both_flags_defaulted_off():
    """Signature contract — the local worker and the Lambda consumer both call
    `run_extraction` by keyword, so a rename here breaks both at once."""
    import inspect

    from app.services.extraction import run_extraction

    params = inspect.signature(run_extraction).parameters
    assert params["skip_vendor_match"].default is False
    assert params["suppress_auto_approve"].default is False
    assert params["skip_vendor_match"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["suppress_auto_approve"].kind is inspect.Parameter.KEYWORD_ONLY


def test_vendor_matching_is_the_only_thing_the_skip_guards():
    """Source contract: `match_and_link_vendor` must sit behind the flag. If a
    refactor moves the call out from under the guard, the portal resubmit path
    silently regains the ability to re-point an invoice's payee."""
    import inspect

    from app.services.extraction import run_extraction

    src = inspect.getsource(run_extraction)
    guard = src.index("if skip_vendor_match:\n            # Re-extraction")
    call = src.index("await match_and_link_vendor(")
    assert guard < call, "match_and_link_vendor must be inside the else branch of the guard"
    assert 'vendor_action = "preserved"' in src


# ---------------------------------------------------------------------------
# Behaviour of the two flags inside run_extraction
# ---------------------------------------------------------------------------


def _invoice(*, vendor_id, vendor_name):
    from types import SimpleNamespace

    from app.models.invoice import InvoiceStatus

    return SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
        file_key="invoices/test.pdf",
        status=InvoiceStatus("pending"),
        amount=None,
        vendor_name=vendor_name,
        invoice_number="INV-OLD",
        invoice_date=None,
        due_date=None,
        payment_terms=None,
        payment_method=None,
        po_number=None,
        description=None,
        vendor_address=None,
        vendor_tax_id=None,
        reference_number=None,
        bill_to_address=None,
        remit_to_address=None,
        subtotal=None,
        tax_amount=None,
        tax_rate=None,
        discount_amount=None,
        shipping_amount=None,
        currency="USD",
        gl_account=None,
        cost_center=None,
        vendor_id=vendor_id,
        warnings=None,
        po_match=None,
        approval_date=None,
        approved_by=None,
    )


def _db():
    db = AsyncMock()
    db.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    # The locked re-read before an approval (`_locked_auto_approve_suppression`)
    # finds no row, so these tests decide from the invoice double itself.
    result.one_or_none = MagicMock(return_value=None)
    db.execute.return_value = result
    return db


def _extraction_stack(*, vendor_name, confidence=0.99, matcher=None, instance=None):
    """Mock every external call `run_extraction` makes. Mirrors
    `tests/test_extraction_usage_placement.py::_patch_extraction_internals`;
    kept local so a change to the flag's blast radius shows up here."""
    from contextlib import ExitStack
    from types import SimpleNamespace

    from app.services.extraction_adapters.base import ExtractedField, ExtractionResult

    stack = ExitStack()

    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"PDF content")}
    stack.enter_context(patch("boto3.client", return_value=s3))

    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.extract = AsyncMock(
        return_value=ExtractionResult(
            success=True,
            overall_confidence=confidence,
            vendor_name=ExtractedField(value=vendor_name, confidence=0.99),
            invoice_number=ExtractedField(value="INV-NEW", confidence=0.99),
            amount=ExtractedField(value="4321.00", confidence=0.99),
            provider="mock",
            raw_response={"raw": True},
        )
    )
    stack.enter_context(
        patch("app.services.extraction_adapters.get_extraction_adapter", return_value=adapter)
    )

    stack.enter_context(patch("app.services.rag.extract_invoice_text", return_value="invoice text"))
    stack.enter_context(patch("app.services.rag.retrieve_similar", AsyncMock(return_value=[])))
    stack.enter_context(patch("app.services.rag.build_few_shot_prompt", return_value=""))
    stack.enter_context(patch("app.services.rag.neighbors_to_metadata", return_value=[]))

    stack.enter_context(
        patch(
            "app.services.vendor_matching.match_and_link_vendor",
            matcher or AsyncMock(return_value=(SimpleNamespace(id=uuid.uuid4()), "created")),
        )
    )
    stack.enter_context(
        patch("app.services.vendor_priors.apply_priors_to_invoice", AsyncMock(return_value=[]))
    )
    stack.enter_context(
        patch(
            "app.services.duplicate_detection.find_semantic_duplicates", AsyncMock(return_value=[])
        )
    )
    stack.enter_context(
        patch("app.services.duplicate_detection.matches_to_warning", return_value=None)
    )
    stack.enter_context(patch("app.services.invoice_warnings.refresh_warnings", AsyncMock()))
    stack.enter_context(
        patch("app.services.extraction.get_workflow_instance", AsyncMock(return_value=instance))
    )
    stack.enter_context(patch("app.services.extraction.advance_workflow", AsyncMock()))
    return stack


_AUTO_APPROVE_SNAPSHOT = {
    "steps": [
        {
            "number": 1,
            "type": "extraction",
            "enabled": True,
            "config": {"auto_approve_enabled": True, "auto_approve_threshold": 0.5},
        },
        {"number": 2, "type": "approval", "enabled": True, "config": {"required": True}},
    ]
}


@pytest.mark.asyncio
async def test_skip_vendor_match_pins_the_payee_but_re_reads_the_money():
    """The whole point of the resubmit re-extraction: the corrected document's
    NUMBERS land on the invoice, its VENDOR does not."""
    from app.services.extraction import run_extraction

    bound_vendor = uuid.uuid4()
    invoice = _invoice(vendor_id=bound_vendor, vendor_name="Bound Supply Co")
    matcher = AsyncMock()

    with (
        _extraction_stack(vendor_name="Totally Different Ltd", matcher=matcher),
        patch("app.services.extraction.transition_invoice", AsyncMock()),
    ):
        await run_extraction(_db(), invoice, actor_id=None, skip_vendor_match=True)

    matcher.assert_not_awaited()
    assert invoice.vendor_id == bound_vendor
    assert invoice.vendor_name == "Bound Supply Co"
    # …and the fields a reviewer actually needs re-read did move.
    assert invoice.invoice_number == "INV-NEW"
    assert str(invoice.amount) == "4321.00"


@pytest.mark.asyncio
async def test_default_pass_still_matches_and_takes_the_documents_vendor():
    """Unchanged ingest behaviour when the flag is off — the fresh-upload path
    must keep resolving a vendor from the document."""
    from app.services.extraction import run_extraction

    invoice = _invoice(vendor_id=None, vendor_name="Placeholder")
    matcher = AsyncMock(return_value=(MagicMock(id=uuid.uuid4()), "created"))

    with (
        _extraction_stack(vendor_name="Totally Different Ltd", matcher=matcher),
        patch("app.services.extraction.transition_invoice", AsyncMock()),
    ):
        await run_extraction(_db(), invoice, actor_id=None)

    matcher.assert_awaited_once()
    assert invoice.vendor_name == "Totally Different Ltd"


@pytest.mark.asyncio
async def test_suppress_auto_approve_lands_the_resubmission_at_review():
    from app.models.invoice import InvoiceStatus
    from app.services.extraction import run_extraction

    invoice = _invoice(vendor_id=uuid.uuid4(), vendor_name="Bound Supply Co")
    instance = MagicMock()
    instance.steps_config_snapshot = _AUTO_APPROVE_SNAPSHOT
    transition = AsyncMock()

    with (
        _extraction_stack(vendor_name="Bound Supply Co", instance=instance),
        patch("app.services.extraction.transition_invoice", transition),
        patch("app.services.extraction.decide_auto_approve", return_value=True),
        patch("app.services.extraction.resolve_gate_aggregate", AsyncMock(return_value=None)),
    ):
        await run_extraction(
            _db(),
            invoice,
            actor_id=None,
            skip_vendor_match=True,
            suppress_auto_approve=True,
        )

    args, kwargs = transition.await_args
    assert args[2] is InvoiceStatus.ready_for_review
    assert kwargs["action_name"] == "invoice.extraction_completed"
    assert kwargs["details"]["auto_approved"] is False
    assert kwargs["details"]["auto_approve_suppressed"] == "requested_by_caller"


@pytest.mark.asyncio
async def test_without_the_flag_auto_approve_still_fires():
    """Counterpart: the suppression must be opt-in, not a silent policy change
    for every tenant that configured unattended approval."""
    from app.models.invoice import InvoiceStatus
    from app.services.extraction import run_extraction

    invoice = _invoice(vendor_id=uuid.uuid4(), vendor_name="Bound Supply Co")
    instance = MagicMock()
    instance.steps_config_snapshot = _AUTO_APPROVE_SNAPSHOT
    transition = AsyncMock()

    with (
        _extraction_stack(vendor_name="Bound Supply Co", instance=instance),
        patch("app.services.extraction.transition_invoice", transition),
        patch("app.services.extraction.decide_auto_approve", return_value=True),
        patch("app.services.extraction.resolve_gate_aggregate", AsyncMock(return_value=None)),
    ):
        await run_extraction(_db(), invoice, actor_id=None)

    args, kwargs = transition.await_args
    assert args[2] is InvoiceStatus.approved
    assert kwargs["action_name"] == "invoice.auto_approved"
    assert kwargs["details"]["auto_approve_suppressed"] is None


# ---------------------------------------------------------------------------
# Who supplied the document — `auto_approve_suppression`
# ---------------------------------------------------------------------------
#
# A clerk's own upload / extract is suppressed at dispatch, but the read that
# approves can be someone else's: a manager re-extracting after a clerk swapped
# the file in. `run_extraction` therefore decides from the row itself, in both
# dispatch modes, whoever dispatched it.

_MANAGER = uuid.uuid4()
_CLERK = uuid.uuid4()


@pytest.mark.parametrize(
    ("uploaded_by", "actors", "actor", "requested", "expected"),
    [
        # System channels — no employee supplied the document.
        (None, None, None, False, None),
        (None, [], uuid.uuid4(), False, None),
        # The reader is the uploader and nobody else shaped it: touchless.
        (_MANAGER, None, _MANAGER, False, None),
        (_MANAGER, [], str(_MANAGER), False, None),  # actor arrives as a string
        # The follow-up: a clerk swapped the file on a manager's upload.
        (_MANAGER, [str(_CLERK)], _MANAGER, False, "segregation_actors"),
        # A recurring template's author re-reading their own generated invoice
        # is still the author: the set is unconditional.
        (_MANAGER, [str(_MANAGER)], _MANAGER, False, "segregation_actors"),
        (None, [str(_MANAGER)], _MANAGER, False, "segregation_actors"),
        # A clerk's own upload (swap included — the uploader is never restamped)
        # read by a manager.
        (_CLERK, None, _MANAGER, False, "uploaded_by_another_user"),
        (_CLERK, [], None, False, "uploaded_by_another_user"),
        # The caller's own request wins the recorded code.
        (_CLERK, [str(_CLERK)], _MANAGER, True, "requested_by_caller"),
        (None, None, None, True, "requested_by_caller"),
    ],
)
def test_auto_approve_suppression(uploaded_by, actors, actor, requested, expected):
    from types import SimpleNamespace

    from app.services.extraction import auto_approve_suppression

    invoice = SimpleNamespace(uploaded_by_id=uploaded_by, segregation_actor_ids=actors)
    assert auto_approve_suppression(invoice, actor_id=actor, requested=requested) == expected


@pytest.mark.asyncio
async def test_a_managers_read_of_a_document_a_clerk_swapped_in_lands_at_review():
    """`docs/followups.md` — the manager's extraction, no flag, no second look."""
    from app.models.invoice import InvoiceStatus
    from app.services.extraction import run_extraction

    invoice = _invoice(vendor_id=uuid.uuid4(), vendor_name="Bound Supply Co")
    invoice.uploaded_by_id = _MANAGER
    invoice.segregation_actor_ids = [str(_CLERK)]
    instance = MagicMock()
    instance.steps_config_snapshot = _AUTO_APPROVE_SNAPSHOT
    transition = AsyncMock()

    with (
        _extraction_stack(vendor_name="Bound Supply Co", instance=instance),
        patch("app.services.extraction.transition_invoice", transition),
        patch("app.services.extraction.decide_auto_approve", return_value=True),
        patch("app.services.extraction.resolve_gate_aggregate", AsyncMock(return_value=None)),
    ):
        await run_extraction(_db(), invoice, actor_id=_MANAGER)

    args, kwargs = transition.await_args
    assert args[2] is InvoiceStatus.ready_for_review
    assert kwargs["action_name"] == "invoice.extraction_completed"
    assert kwargs["details"]["auto_approved"] is False
    assert kwargs["details"]["auto_approve_suppressed"] == "segregation_actors"
    assert invoice.approved_by is None and invoice.approval_date is None


@pytest.mark.asyncio
async def test_no_suppression_is_recorded_when_the_gates_did_not_fire():
    """`auto_approve_suppressed` means "a person was required where the policy
    alone would have approved" — not "this invoice has a segregation set"."""
    from app.models.invoice import InvoiceStatus
    from app.services.extraction import run_extraction

    invoice = _invoice(vendor_id=uuid.uuid4(), vendor_name="Bound Supply Co")
    invoice.segregation_actor_ids = [str(_CLERK)]
    instance = MagicMock()
    instance.steps_config_snapshot = _AUTO_APPROVE_SNAPSHOT
    transition = AsyncMock()

    with (
        _extraction_stack(vendor_name="Bound Supply Co", instance=instance),
        patch("app.services.extraction.transition_invoice", transition),
        patch("app.services.extraction.decide_auto_approve", return_value=False),
        patch("app.services.extraction.resolve_gate_aggregate", AsyncMock(return_value=None)),
    ):
        await run_extraction(_db(), invoice, actor_id=_MANAGER)

    args, kwargs = transition.await_args
    assert args[2] is InvoiceStatus.ready_for_review
    assert kwargs["details"]["auto_approve_suppressed"] is None
