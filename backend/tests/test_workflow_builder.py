"""No-code workflow-builder engine tests.

Covers the pure builder primitives in ``services/workflow_builder.py``:

  - ``evaluate_condition`` across every operator + match all/any + goto resolution
  - ``build_invoice_context`` keeps money as Decimal (never float)
  - ``resolve_parallel`` join / min_approvals semantics
  - ``execute_custom_step`` dry-run has no side effects + correct status per type
  - ``validate_builder_steps`` catches malformed config (bad operator, missing
    url, dangling goto, etc.)

These are pure-function tests (plus one async executor test) — no DB, mirroring
the style of ``test_workflow_state_machine.py``.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.workflow_builder import (
    BUILDER_STEP_TYPES,
    build_invoice_context,
    evaluate_condition,
    execute_custom_step,
    resolve_parallel,
    validate_builder_steps,
)

# ---------------------------------------------------------------------------
# build_invoice_context — money is Decimal
# ---------------------------------------------------------------------------


def test_build_invoice_context_amount_is_decimal_from_orm():
    """The condition engine compares amounts as Decimal; the context builder
    must never hand it a float (binary float would make 0.1+0.2 rules wrong)."""
    invoice = SimpleNamespace(
        amount=Decimal("1234.56"),
        currency="USD",
        vendor_id="vendor-1",
        gl_account="6000",
        cost_center="CC-1",
    )
    ctx = build_invoice_context(invoice)
    assert isinstance(ctx["amount"], Decimal)
    assert ctx["amount"] == Decimal("1234.56")
    assert ctx["currency"] == "USD"
    assert ctx["vendor_id"] == "vendor-1"
    assert ctx["department"] is None  # not a field on Invoice → None


def test_build_invoice_context_decimal_from_string_not_float():
    """A string-decimal SimInvoice amount must parse to its decimal text, not a
    binary float — `"0.1"` stays exact."""
    ctx = build_invoice_context({"amount": "0.1", "currency": "EUR"})
    assert isinstance(ctx["amount"], Decimal)
    assert ctx["amount"] == Decimal("0.1")
    # never a float
    assert not isinstance(ctx["amount"], float)


def test_build_invoice_context_defaults():
    ctx = build_invoice_context({})
    assert ctx["amount"] == Decimal("0")
    # No recorded currency stays None, never a borrowed USD (decisions §473).
    assert ctx["currency"] is None
    assert ctx["vendor_id"] is None


def test_build_invoice_context_stringifies_uuid_vendor_id():
    import uuid

    vid = uuid.uuid4()
    ctx = build_invoice_context({"vendor_id": vid})
    assert ctx["vendor_id"] == str(vid)


# ---------------------------------------------------------------------------
# evaluate_condition — operators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operator,value,amount,expected",
    [
        ("gt", 1000, "1500", True),
        ("gt", 1000, "1000", False),
        ("gte", 1000, "1000", True),
        ("lt", 1000, "999.99", True),
        ("lte", 1000, "1000", True),
        ("lte", 1000, "1000.01", False),
        ("eq", "1000.00", "1000", True),  # Decimal compare: 1000.00 == 1000
        ("ne", 1000, "1001", True),
        ("ne", 1000, "1000", False),
    ],
)
def test_evaluate_condition_amount_operators(operator, value, amount, expected):
    ctx = build_invoice_context({"amount": amount})
    config = {"rules": [{"field": "amount", "operator": operator, "value": value}]}
    assert evaluate_condition(config, ctx)["matched"] is expected


@pytest.mark.parametrize(
    "operator,value,currency,expected",
    [
        ("eq", "USD", "USD", True),
        ("eq", "USD", "EUR", False),
        ("ne", "USD", "EUR", True),
        ("in", ["USD", "EUR"], "EUR", True),
        ("in", ["USD", "EUR"], "GBP", False),
        ("not_in", ["USD", "EUR"], "GBP", True),
        ("not_in", ["USD", "EUR"], "USD", False),
        ("starts_with", "US", "USD", True),
        ("starts_with", "EU", "USD", False),
    ],
)
def test_evaluate_condition_string_operators(operator, value, currency, expected):
    ctx = build_invoice_context({"currency": currency})
    config = {"rules": [{"field": "currency", "operator": operator, "value": value}]}
    assert evaluate_condition(config, ctx)["matched"] is expected


def test_evaluate_condition_in_with_scalar_value_is_coerced():
    ctx = build_invoice_context({"currency": "USD"})
    config = {"rules": [{"field": "currency", "operator": "in", "value": "USD"}]}
    assert evaluate_condition(config, ctx)["matched"] is True


# ---------------------------------------------------------------------------
# evaluate_condition — match all/any
# ---------------------------------------------------------------------------


def test_evaluate_condition_match_all_requires_every_rule():
    ctx = build_invoice_context({"amount": "1500", "currency": "USD"})
    config = {
        "match": "all",
        "rules": [
            {"field": "amount", "operator": "gt", "value": 1000},
            {"field": "currency", "operator": "eq", "value": "USD"},
        ],
    }
    assert evaluate_condition(config, ctx)["matched"] is True

    config["rules"][1]["value"] = "EUR"
    assert evaluate_condition(config, ctx)["matched"] is False


def test_evaluate_condition_match_any_needs_one_rule():
    ctx = build_invoice_context({"amount": "500", "currency": "USD"})
    config = {
        "match": "any",
        "rules": [
            {"field": "amount", "operator": "gt", "value": 1000},  # fails
            {"field": "currency", "operator": "eq", "value": "USD"},  # passes
        ],
    }
    assert evaluate_condition(config, ctx)["matched"] is True


def test_evaluate_condition_no_rules_all_is_true_any_is_false():
    ctx = build_invoice_context({})
    assert evaluate_condition({"match": "all", "rules": []}, ctx)["matched"] is True
    assert evaluate_condition({"match": "any", "rules": []}, ctx)["matched"] is False


# ---------------------------------------------------------------------------
# evaluate_condition — goto resolution
# ---------------------------------------------------------------------------


def test_evaluate_condition_goto_true_branch():
    ctx = build_invoice_context({"amount": "5000"})
    config = {
        "rules": [{"field": "amount", "operator": "gt", "value": 1000}],
        "on_true_goto": 7,
        "on_false_goto": 3,
    }
    result = evaluate_condition(config, ctx)
    assert result["matched"] is True
    assert result["goto"] == 7
    assert "step 7" in result["explanation"]


def test_evaluate_condition_goto_false_branch():
    ctx = build_invoice_context({"amount": "100"})
    config = {
        "rules": [{"field": "amount", "operator": "gt", "value": 1000}],
        "on_true_goto": 7,
        "on_false_goto": 3,
    }
    result = evaluate_condition(config, ctx)
    assert result["matched"] is False
    assert result["goto"] == 3


def test_evaluate_condition_null_goto_falls_through():
    ctx = build_invoice_context({"amount": "5000"})
    config = {
        "rules": [{"field": "amount", "operator": "gt", "value": 1000}],
        "on_true_goto": None,
    }
    result = evaluate_condition(config, ctx)
    assert result["matched"] is True
    assert result["goto"] is None
    assert "fall through" in result["explanation"]


# ---------------------------------------------------------------------------
# resolve_parallel — join / min_approvals
# ---------------------------------------------------------------------------


def test_resolve_parallel_join_all():
    config = {
        "branches": [
            {"name": "Finance", "approver_ids": ["a"]},
            {"name": "Legal", "approver_ids": ["b"]},
            {"name": "Ops", "approver_ids": ["c"]},
        ],
        "join": "all",
    }
    out = resolve_parallel(config)
    assert out["required"] == 3
    assert out["join"] == "all"
    assert len(out["branches"]) == 3


def test_resolve_parallel_join_any():
    config = {
        "branches": [{"approver_ids": ["a"]}, {"approver_ids": ["b"]}],
        "join": "any",
    }
    out = resolve_parallel(config)
    assert out["required"] == 1


def test_resolve_parallel_min_approvals_overrides_join():
    config = {
        "branches": [{"approver_ids": ["a"]}, {"approver_ids": ["b"]}, {"approver_ids": ["c"]}],
        "join": "all",
        "min_approvals": 2,
    }
    out = resolve_parallel(config)
    assert out["required"] == 2
    assert out["min_approvals"] == 2


def test_resolve_parallel_min_approvals_clamped_to_branch_count():
    config = {
        "branches": [{"approver_ids": ["a"]}, {"approver_ids": ["b"]}],
        "min_approvals": 5,
    }
    out = resolve_parallel(config)
    assert out["required"] == 2  # clamped down to len(branches)


def test_resolve_parallel_stringifies_approver_ids_and_names_branches():
    import uuid

    aid = uuid.uuid4()
    config = {"branches": [{"approver_ids": [aid]}], "join": "all"}
    out = resolve_parallel(config)
    assert out["branches"][0]["approver_ids"] == [str(aid)]
    assert out["branches"][0]["name"] == "Branch 1"


# ---------------------------------------------------------------------------
# execute_custom_step — dry-run has no side effects + status per type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_webhook_dry_run_records_no_network():
    step = {
        "type": "webhook",
        "config": {"url": "https://example.com/hook", "method": "POST", "enabled": True},
    }
    ctx = build_invoice_context({"amount": "100"})
    result = await execute_custom_step(step, ctx, dry_run=True)
    assert result["type"] == "webhook"
    assert result["status"] == "ok"
    assert "not sent" in result["detail"]


@pytest.mark.asyncio
async def test_execute_webhook_default_disabled_is_recorded_not_sent():
    step = {"type": "webhook", "config": {"url": "https://example.com/hook"}}
    ctx = build_invoice_context({})
    result = await execute_custom_step(step, ctx, dry_run=False)
    assert result["status"] == "ok"
    assert "not sent" in result["detail"]


@pytest.mark.asyncio
async def test_execute_webhook_missing_url_is_error():
    step = {"type": "webhook", "config": {"method": "POST"}}
    result = await execute_custom_step(step, build_invoice_context({}), dry_run=True)
    assert result["status"] == "error"
    assert "url" in result["detail"]


@pytest.mark.asyncio
async def test_execute_email_dry_run_does_not_call_adapter():
    """dry_run must NOT touch the email adapter at all."""
    step = {
        "type": "email",
        "config": {
            "to": "custom",
            "to_addresses": ["a@b.com"],
            "subject": "Invoice {amount}",
            "body_template": "hi",
        },
    }
    ctx = build_invoice_context({"amount": "100"})
    with patch("app.services.email_adapters.get_email_adapter") as mk:
        result = await execute_custom_step(step, ctx, dry_run=True)
        mk.assert_not_called()
    assert result["status"] == "ok"
    assert "not sent" in result["detail"]


@pytest.mark.asyncio
async def test_execute_email_custom_no_addresses_is_skipped():
    step = {"type": "email", "config": {"to": "custom", "subject": "x"}}
    result = await execute_custom_step(step, build_invoice_context({}), dry_run=False)
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_execute_email_sends_via_adapter_when_not_dry_run():
    step = {
        "type": "email",
        "config": {"to": "custom", "to_addresses": ["a@b.com"], "subject": "Hi"},
    }
    fake_adapter = AsyncMock()
    with patch("app.services.email_adapters.get_email_adapter", return_value=fake_adapter):
        result = await execute_custom_step(step, build_invoice_context({}), dry_run=False)
    fake_adapter.send.assert_awaited_once()
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_execute_delay_never_sleeps():
    step = {"type": "delay", "config": {"duration_seconds": 86400}}
    result = await execute_custom_step(step, build_invoice_context({}), dry_run=False)
    assert result["type"] == "delay"
    assert result["status"] == "ok"
    assert "not slept" in result["detail"]


@pytest.mark.asyncio
async def test_execute_custom_step_rejects_non_custom_type():
    step = {"type": "condition", "config": {}}
    result = await execute_custom_step(step, build_invoice_context({}), dry_run=True)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# validate_builder_steps — catches malformed config
# ---------------------------------------------------------------------------


def test_validate_builder_steps_accepts_a_valid_workflow():
    steps = [
        {"number": 1, "type": "extraction", "name": "Extract", "config": {}},
        {
            "number": 2,
            "type": "condition",
            "name": "Big?",
            "config": {
                "rules": [{"field": "amount", "operator": "gt", "value": 1000}],
                "match": "all",
                "on_true_goto": 3,
                "on_false_goto": None,
            },
        },
        {
            "number": 3,
            "type": "parallel",
            "name": "Dual approve",
            "config": {
                "branches": [{"name": "A", "approver_ids": ["x"]}],
                "join": "all",
            },
        },
    ]
    assert validate_builder_steps(steps) == []


def test_validate_builder_steps_flags_bad_operator():
    steps = [
        {
            "number": 1,
            "type": "condition",
            "name": "c",
            "config": {"rules": [{"field": "amount", "operator": "BOGUS", "value": 1}]},
        }
    ]
    errors = validate_builder_steps(steps)
    assert any("operator" in e for e in errors)


def test_validate_builder_steps_flags_bad_field():
    steps = [
        {
            "number": 1,
            "type": "condition",
            "name": "c",
            "config": {"rules": [{"field": "nope", "operator": "eq", "value": 1}]},
        }
    ]
    assert any("field" in e for e in validate_builder_steps(steps))


def test_validate_builder_steps_flags_missing_webhook_url():
    steps = [{"number": 1, "type": "webhook", "name": "w", "config": {"method": "POST"}}]
    assert any("url" in e for e in validate_builder_steps(steps))


def test_validate_builder_steps_flags_non_http_webhook_url():
    steps = [
        {
            "number": 1,
            "type": "webhook",
            "name": "w",
            "config": {"url": "ftp://x", "method": "POST"},
        }
    ]
    assert any("http" in e for e in validate_builder_steps(steps))


def test_validate_builder_steps_flags_dangling_goto():
    steps = [
        {
            "number": 1,
            "type": "condition",
            "name": "c",
            "config": {
                "rules": [{"field": "amount", "operator": "gt", "value": 1}],
                "on_true_goto": 99,  # no step 99
            },
        }
    ]
    assert any("99" in e and "does not exist" in e for e in validate_builder_steps(steps))


def test_validate_builder_steps_flags_bad_match():
    steps = [
        {
            "number": 1,
            "type": "condition",
            "name": "c",
            "config": {
                "rules": [{"field": "amount", "operator": "gt", "value": 1}],
                "match": "either",
            },
        }
    ]
    assert any("match" in e for e in validate_builder_steps(steps))


def test_validate_builder_steps_flags_min_approvals_over_branch_count():
    steps = [
        {
            "number": 1,
            "type": "parallel",
            "name": "p",
            "config": {"branches": [{"approver_ids": ["a"]}], "min_approvals": 3},
        }
    ]
    assert any("min_approvals" in e for e in validate_builder_steps(steps))


def test_validate_builder_steps_flags_empty_parallel_branches():
    steps = [{"number": 1, "type": "parallel", "name": "p", "config": {"branches": []}}]
    assert any("branches" in e for e in validate_builder_steps(steps))


def test_validate_builder_steps_flags_email_custom_without_addresses():
    steps = [
        {"number": 1, "type": "email", "name": "e", "config": {"to": "custom", "subject": "x"}}
    ]
    assert any("to_addresses" in e for e in validate_builder_steps(steps))


def test_validate_builder_steps_flags_delay_without_duration_or_field():
    steps = [{"number": 1, "type": "delay", "name": "d", "config": {}}]
    assert any("delay" in e for e in validate_builder_steps(steps))


def test_validate_builder_steps_ignores_canonical_steps():
    steps = [
        {"number": 1, "type": "extraction", "name": "x", "config": {}},
        {"number": 2, "type": "approval", "name": "a", "config": {}},
        {"number": 3, "type": "erp_export", "name": "e", "config": {}},
        {"number": 4, "type": "done", "name": "d", "config": {}},
    ]
    assert validate_builder_steps(steps) == []


def test_validate_builder_steps_flags_non_dict_config():
    steps = [{"number": 1, "type": "webhook", "name": "w", "config": "nope"}]
    assert any("config" in e for e in validate_builder_steps(steps))


# ---------------------------------------------------------------------------
# module surface
# ---------------------------------------------------------------------------


def test_builder_step_types_constant():
    # An immutable tuple, not a list: it is the shared source of truth
    # (services/workflow_step_types.py) that this module and the engine both
    # re-export, so a stray `.append()` anywhere would edit both vocabularies.
    assert BUILDER_STEP_TYPES == ("condition", "parallel", "webhook", "email", "delay")


def test_engine_knows_builder_step_types():
    """The engine must accept a definition containing builder steps rather than
    rejecting it."""
    from app.services.workflow_engine import is_known_step_type

    for t in BUILDER_STEP_TYPES:
        assert is_known_step_type(t) is True
    # canonical + legacy alias still recognised
    assert is_known_step_type("extraction") is True
    assert is_known_step_type("review") is True  # legacy alias → approval
    assert is_known_step_type("not_a_step") is False


# ---------------------------------------------------------------------------
# PII guard — a failed email step must not leak the recipient address
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_step_failure_detail_omits_recipient_address():
    """When the email adapter raises with the recipient embedded in its message
    (SES/SMTP echo the address on a bad-recipient error), the step result detail
    — stored in WorkflowInstance.step_results JSONB and logged to CloudWatch —
    must carry only the exception class name, never the address (PII invariant)."""
    from app.services.workflow_builder import _execute_email

    leaky = "SMTPRecipientsRefused: vendor-secret@example.com rejected"

    class _BoomAdapter:
        async def send(self, *_a, **_k):
            raise RuntimeError(leaky)

    config = {
        "to": "custom",
        "to_addresses": ["vendor-secret@example.com"],
        "subject": "Hi",
        "body_template": "body",
    }

    with patch("app.services.email_adapters.get_email_adapter", return_value=_BoomAdapter()):
        result = await _execute_email(config, {}, dry_run=False)

    assert result["status"] == "error"
    assert "vendor-secret@example.com" not in result["detail"]
    assert "example.com" not in result["detail"]
    assert "RuntimeError" in result["detail"]


# ---------------------------------------------------------------------------
# Canonical-step numbers that gate money. `validate_builder_steps` used to skip
# every canonical step outright ("no builder config to check"), so the ONE save
# path that takes `steps_config` free-form — `POST /api/workflows/import` —
# persisted a non-numeric `max_invoice_amount` / `require_cfo_above` /
# `auto_approve_below`, which then raised `InvalidOperation` out of the approval
# gate: a 500 on every approval under that workflow.
# ---------------------------------------------------------------------------


def _approval_step(config: dict) -> list[dict]:
    return [{"number": 1, "type": "approval", "name": "Approval", "config": config}]


@pytest.mark.parametrize("bad", ["abc", "10,000", "5000 USD", "NaN", "Infinity", True])
def test_validate_rejects_a_non_numeric_approval_threshold(bad):
    from app.services.workflow_builder import validate_builder_steps

    for field in ("auto_approve_below", "require_cfo_above", "max_invoice_amount"):
        errors = validate_builder_steps(_approval_step({field: bad}))
        assert errors, (field, bad)
        assert field in errors[0]


def test_validate_accepts_wellformed_approval_thresholds():
    from app.services.workflow_builder import validate_builder_steps

    assert not validate_builder_steps(
        _approval_step(
            {
                "required": True,
                "auto_approve_below": None,
                "require_cfo_above": 5000,
                "max_invoice_amount": "10000.00",
            }
        )
    )


def test_validate_rejects_a_non_numeric_chain_level_bound():
    """`resolve_applicable_levels` reads an unparseable bound as 'no bound', so
    a typo here silently widens the level to every amount instead of failing."""
    from app.services.workflow_builder import validate_builder_steps

    errors = validate_builder_steps(
        _approval_step({"approval_chain": [{"name": "L1", "min_amount": "lots"}]})
    )
    assert errors and "min_amount" in errors[0]


@pytest.mark.parametrize("bad", ["high", 95, -1, True])
def test_validate_rejects_a_bad_extraction_confidence_bar(bad):
    from app.services.workflow_builder import validate_builder_steps

    errors = validate_builder_steps(
        [
            {
                "number": 1,
                "type": "extraction",
                "name": "Extract",
                "config": {"auto_approve_threshold": bad},
            }
        ]
    )
    assert errors and "auto_approve_threshold" in errors[0]


def test_validate_accepts_a_canonical_step_with_no_config_key_at_all():
    """`config` is OPTIONAL on a canonical step — `{"number": 1, "type":
    "approval"}` is how a minimal experiment variant is written
    (`schemas/workflow_experiments` runs this same validator over `config_a` /
    `config_b`). Absent means "no thresholds to check", not "malformed"; the
    five BUILDER types are the ones that genuinely require a config object."""
    from app.services.workflow_builder import validate_builder_steps

    assert not validate_builder_steps(
        [
            {"number": 1, "type": "approval"},
            {"number": 2, "type": "extraction"},
        ]
    )
    # A PRESENT but non-object config is still wrong.
    assert any(
        "config" in e
        for e in validate_builder_steps([{"number": 1, "type": "approval", "config": "oops"}])
    )


def test_validate_still_ignores_canonical_steps_that_carry_no_gates():
    from app.services.workflow_builder import validate_builder_steps

    assert not validate_builder_steps(
        [
            {"number": 1, "type": "erp_export", "name": "ERP", "config": {"whatever": "x"}},
            {"number": 2, "type": "done", "name": "Done", "config": {}},
        ]
    )


def test_validate_checks_the_legacy_review_alias_too():
    """`review` is the legacy alias for `approval`; a definition still spelling
    it that way carries the same live gates."""
    from app.services.workflow_builder import validate_builder_steps

    errors = validate_builder_steps(
        [
            {
                "number": 1,
                "type": "review",
                "name": "Review",
                "config": {"max_invoice_amount": "abc"},
            }
        ]
    )
    assert errors and "max_invoice_amount" in errors[0]
