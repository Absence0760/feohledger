"""Unit tests for the approval chain service.

Covers segregation-of-duties enforcement, delegate resolution,
amount-based level filtering, chain state initialisation, and
approval advancement. All tests are DB-free — DB sessions are
replaced with AsyncMock so no running Postgres is required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_invoice(*, uploaded_by_id=None, amount=None, segregation_actor_ids=None):
    """Return a minimal Invoice-like object for use in segregation tests.

    Carries ``segregation_actor_ids`` because the real ``Invoice`` does: SoD keys
    on the uploader **plus** that set (the recurring template's author and its
    material editors). A stand-in without it would exercise only half the rule.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        uploaded_by_id=uploaded_by_id,
        amount=amount,
        segregation_actor_ids=segregation_actor_ids,
    )


def _make_instance(*, state_data=None, steps_config_snapshot=None):
    """Return a minimal WorkflowInstance-like object."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        state_data=state_data,
        steps_config_snapshot=steps_config_snapshot,
    )


def _make_user(*, delegate_to_id=None, delegate_until=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        delegate_to_id=delegate_to_id,
        delegate_until=delegate_until,
    )


def _db_returning(scalar_value):
    """Build an AsyncMock session whose execute() returns scalar_value."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_value)
    db.execute = AsyncMock(return_value=result)
    return db


# ---------------------------------------------------------------------------
# check_segregation
# ---------------------------------------------------------------------------


def test_segregation_blocks_uploader_from_approving():
    """Uploader matching actor_id with require_segregation=True raises 403."""
    from app.services.approval_chain import check_segregation

    actor_id = uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=actor_id)
    config = {"require_segregation": True}

    with pytest.raises(HTTPException) as exc_info:
        check_segregation(invoice, actor_id, config)

    assert exc_info.value.status_code == 403
    assert "segregation" in exc_info.value.detail.lower()


def test_segregation_allows_different_user():
    """A different user from the uploader is never blocked, even with require_segregation=True."""
    from app.services.approval_chain import check_segregation

    uploader_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=uploader_id)
    config = {"require_segregation": True}

    # Must not raise
    check_segregation(invoice, actor_id, config)


def test_segregation_disabled_allows_uploader():
    """When require_segregation is False the uploader may approve their own invoice."""
    from app.services.approval_chain import check_segregation

    actor_id = uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=actor_id)
    config = {"require_segregation": False}

    # Must not raise
    check_segregation(invoice, actor_id, config)


def test_segregation_skips_null_uploaded_by():
    """uploaded_by_id=None means "no employee creator" — the check is skipped.

    This branch reads fail-open on a fraud control, and is only sound because
    every path under `app/` that creates an invoice for a signed-in employee
    stamps the column (manual create, upload, CSV import, generate-now, the
    inter-company mirror). The remaining NULLs have no control-plane user to
    record at all: email intake and inbound PEPPOL (system), supplier-portal
    submit / PO flip (a tenant-scoped VendorUser, who holds no employee JWT and
    can never reach an approval endpoint), and the recurring sweep. Failing
    CLOSED here would make all of those permanently unapprovable — an outage,
    not a control. `tests/test_invoice_uploader_stamping.py` is what keeps the
    premise true; see `docs/decisions.md`.
    """
    from app.services.approval_chain import check_segregation

    actor_id = uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=None)
    config = {"require_segregation": True}

    # Must not raise even though require_segregation is True
    check_segregation(invoice, actor_id, config)


def test_segregation_defaults_on_when_key_missing():
    """Empty approval config (no require_segregation key) still blocks the uploader.

    SoD is default-on as of the SOC 2 baseline pass — orgs must explicitly
    opt out by setting the key to False.
    """
    from app.services.approval_chain import check_segregation

    actor_id = uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=actor_id)
    config: dict = {}  # no require_segregation key

    with pytest.raises(HTTPException) as exc_info:
        check_segregation(invoice, actor_id, config)

    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# violates_segregation — the pure predicate shared by check_segregation and the
# amount-floor auto-approve path (which degrades to human review, not a 403).
# ---------------------------------------------------------------------------


def test_violates_segregation_true_for_self_uploader():
    """Uploader == actor with require_segregation on → predicate is True."""
    from app.services.approval_chain import violates_segregation

    actor_id = uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=actor_id)
    assert violates_segregation(invoice, actor_id, {"require_segregation": True}) is True
    # Default-on when the key is absent.
    assert violates_segregation(invoice, actor_id, {}) is True


def test_violates_segregation_false_for_other_user():
    """A different actor from the uploader never violates SoD."""
    from app.services.approval_chain import violates_segregation

    invoice = _make_invoice(uploaded_by_id=uuid.uuid4())
    assert violates_segregation(invoice, uuid.uuid4(), {"require_segregation": True}) is False


def test_violates_segregation_false_when_disabled_or_null_uploader():
    """Opt-out and pre-existing (NULL uploader) invoices don't violate SoD."""
    from app.services.approval_chain import violates_segregation

    actor_id = uuid.uuid4()
    self_uploaded = _make_invoice(uploaded_by_id=actor_id)
    assert violates_segregation(self_uploaded, actor_id, {"require_segregation": False}) is False

    legacy = _make_invoice(uploaded_by_id=None)
    assert violates_segregation(legacy, actor_id, {"require_segregation": True}) is False


def test_violates_segregation_true_for_an_implicated_actor():
    """The set is the other half of the rule.

    A recurring template's author lands in `uploaded_by_id`; a later material
    editor of that template lands here. One column can only name one of them, so
    stamping the editor *instead* would merely have moved the exemption to the
    author — the gap this set closes.
    """
    from app.services.approval_chain import violates_segregation

    editor = uuid.uuid4()
    author = uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=author, segregation_actor_ids=[str(editor)])

    assert violates_segregation(invoice, editor, {"require_segregation": True}) is True
    # The author is still refused via the column, and a third party still approves.
    assert violates_segregation(invoice, author, {"require_segregation": True}) is True
    assert violates_segregation(invoice, uuid.uuid4(), {"require_segregation": True}) is False


def test_violates_segregation_matches_an_implicated_actor_stored_as_uuid():
    """Stored JSONB is strings, but an in-memory row built by a service may hold
    UUID objects before the flush. Compare stringified, so the predicate's answer
    cannot depend on whether the row has been round-tripped through Postgres."""
    from app.services.approval_chain import violates_segregation

    editor = uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=None, segregation_actor_ids=[editor])
    assert violates_segregation(invoice, editor, {"require_segregation": True}) is True


def test_violates_segregation_false_for_an_empty_implicated_set():
    """`[]` must read exactly as NULL does — "nobody beyond the uploader"."""
    from app.services.approval_chain import violates_segregation

    invoice = _make_invoice(uploaded_by_id=None, segregation_actor_ids=[])
    assert violates_segregation(invoice, uuid.uuid4(), {"require_segregation": True}) is False


def test_violates_segregation_tolerates_a_subject_without_the_attribute():
    """The subject is not always an ``Invoice``.

    Expense reports, requisitions and expense pre-approvals reuse this rule
    through a ``SimpleNamespace`` shim. They pass the attribute explicitly, but a
    future subject that forgets must not raise ``AttributeError`` on the approval
    path: a 500 there takes the control out altogether, which is strictly worse
    than reading "no additional implicated actors" for a subject that has none.
    """
    from app.services.approval_chain import violates_segregation

    uploader = uuid.uuid4()
    bare = SimpleNamespace(uploaded_by_id=uploader)

    assert violates_segregation(bare, uploader, {"require_segregation": True}) is True
    assert violates_segregation(bare, uuid.uuid4(), {"require_segregation": True}) is False


def test_violates_segregation_opt_out_beats_the_implicated_set_too():
    """`require_segregation: false` is an org-level opt-out of the whole rule, so
    it must short-circuit the set as well as the column — otherwise widening the
    rule would have quietly re-enabled SoD for single-operator accounts."""
    from app.services.approval_chain import violates_segregation

    editor = uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=uuid.uuid4(), segregation_actor_ids=[str(editor)])
    assert violates_segregation(invoice, editor, {"require_segregation": False}) is False


def test_implicated_actors_is_the_uploader_plus_the_set_stringified():
    """The single definition of "who is implicated in this payable". The
    inter-company mirror inherits exactly this set from its source, so it has to
    be the same set the predicate refuses — uploader and set together, in one
    comparable shape whichever form the ids arrive in."""
    from app.services.approval_chain import implicated_actors

    uploader, author, editor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=uploader, segregation_actor_ids=[str(author), editor])
    assert implicated_actors(invoice) == {str(uploader), str(author), str(editor)}

    # Nobody created it and nobody shaped it: the empty set, not {"None"}.
    assert implicated_actors(_make_invoice()) == set()
    # A subject without the attribute (an expense-report shim that forgot it)
    # still answers with its uploader rather than raising.
    assert implicated_actors(SimpleNamespace(uploaded_by_id=uploader)) == {str(uploader)}


def test_violates_segregation_is_membership_in_implicated_actors():
    """The predicate and the set cannot drift: every implicated actor is
    refused, nobody else is, and a ``None`` actor is never a breach."""
    from app.services.approval_chain import implicated_actors, violates_segregation

    invoice = _make_invoice(uploaded_by_id=uuid.uuid4(), segregation_actor_ids=[str(uuid.uuid4())])
    for actor in implicated_actors(invoice):
        assert violates_segregation(invoice, uuid.UUID(actor), {}) is True
    assert violates_segregation(invoice, uuid.uuid4(), {}) is False
    assert violates_segregation(_make_invoice(), None, {}) is False


def test_check_segregation_raises_403_for_an_implicated_actor():
    """The raising half must refuse the set, not just the column."""
    from app.services.approval_chain import check_segregation

    editor = uuid.uuid4()
    invoice = _make_invoice(uploaded_by_id=uuid.uuid4(), segregation_actor_ids=[str(editor)])

    with pytest.raises(HTTPException) as exc_info:
        check_segregation(invoice, editor, {})
    assert exc_info.value.status_code == 403
    assert "segregation" in exc_info.value.detail.lower()


def test_every_segregation_shim_states_both_attributes():
    """Drift guard on the non-invoice subjects.

    ``check_segregation`` is reused for expense reports, requisitions and expense
    pre-approvals via a ``SimpleNamespace`` attribute shim. The predicate reads
    ``segregation_actor_ids`` with a ``getattr`` default, so a shim that omits it
    still *works* — which is exactly the failure mode
    ``test_invoice_uploader_stamping`` exists to prevent: an absent attribute on a
    fraud control reads as an oversight and behaves as an exemption. Every shim
    must answer, even when the answer is ``None``.
    """
    import ast
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    found = 0
    for path in sorted(app_root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        rel = path.relative_to(app_root.parent).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "check_segregation" or not node.args:
                continue
            subject = node.args[0]
            if not (
                isinstance(subject, ast.Call)
                and getattr(subject.func, "id", None) == "SimpleNamespace"
            ):
                continue  # a real Invoice — the model guarantees the attribute
            found += 1
            kwargs = {kw.arg for kw in subject.keywords}
            if "segregation_actor_ids" not in kwargs:
                offenders.append(f"{rel}:{subject.lineno}")

    assert found >= 3, (
        f"expected the expense-report / requisition / pre-approval shims, found {found} — "
        "the scan has stopped matching them, so it guards nothing"
    )
    assert not offenders, (
        "check_segregation(SimpleNamespace(...)) omits `segregation_actor_ids` at "
        + ", ".join(offenders)
        + " — state it (None is a fine answer when the table records no editors) so "
        "the exemption is argued rather than inherited from a getattr default."
    )


def test_default_steps_config_has_segregation_enabled():
    """New workflow definitions default to require_segregation=True on the approval step."""
    from app.services.workflow_engine import DEFAULT_STEPS_CONFIG

    approval = next(s for s in DEFAULT_STEPS_CONFIG["steps"] if s["type"] == "approval")
    assert approval["config"].get("require_segregation") is True


def test_approval_step_schema_defaults_segregation_on():
    """The Pydantic schema defaults require_segregation to True."""
    from app.schemas.workflow import ApprovalStepConfig

    cfg = ApprovalStepConfig()
    assert cfg.require_segregation is True


def test_approval_level_config_rejects_zero_required_approvals():
    """A level requiring 0 approvals is nonsensical — it would be satisfied

    with no actual approver action. `required_approvals` must be >= 1,
    matching the floor already enforced on the sibling `escalation_hours`
    field."""
    from pydantic import ValidationError

    from app.schemas.workflow import ApprovalLevelConfig

    with pytest.raises(ValidationError):
        ApprovalLevelConfig(required_approvals=0)


def test_approval_level_config_rejects_negative_required_approvals():
    from pydantic import ValidationError

    from app.schemas.workflow import ApprovalLevelConfig

    with pytest.raises(ValidationError):
        ApprovalLevelConfig(required_approvals=-1)


def test_approval_level_config_accepts_positive_required_approvals():
    from app.schemas.workflow import ApprovalLevelConfig

    cfg = ApprovalLevelConfig(required_approvals=2)
    assert cfg.required_approvals == 2


# ---------------------------------------------------------------------------
# resolve_assignee
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegation_active():
    """When delegate_to_id is set and delegate_until is in the future, returns the delegate."""
    from app.services.approval_chain import resolve_assignee

    delegate_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = _make_user(
        delegate_to_id=delegate_id,
        delegate_until=datetime.now(UTC) + timedelta(days=1),
    )
    user.id = user_id

    db = _db_returning(user)
    effective_id, original_id = await resolve_assignee(user_id, db)

    assert effective_id == delegate_id
    assert original_id == user_id


@pytest.mark.asyncio
async def test_delegation_expired():
    """When delegate_until is in the past, returns the original user without delegation."""
    from app.services.approval_chain import resolve_assignee

    delegate_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = _make_user(
        delegate_to_id=delegate_id,
        delegate_until=datetime.now(UTC) - timedelta(seconds=1),
    )
    user.id = user_id

    db = _db_returning(user)
    effective_id, original_id = await resolve_assignee(user_id, db)

    assert effective_id == user_id
    assert original_id is None


@pytest.mark.asyncio
async def test_no_delegation():
    """When delegate_to_id is None the function returns the original user unchanged."""
    from app.services.approval_chain import resolve_assignee

    user_id = uuid.uuid4()
    user = _make_user(delegate_to_id=None, delegate_until=None)
    user.id = user_id

    db = _db_returning(user)
    effective_id, original_id = await resolve_assignee(user_id, db)

    assert effective_id == user_id
    assert original_id is None


@pytest.mark.asyncio
async def test_delegation_user_not_found():
    """When the user_id does not exist in the DB, returns (user_id, None) without raising."""
    from app.services.approval_chain import resolve_assignee

    user_id = uuid.uuid4()
    db = _db_returning(None)  # scalar returns None

    effective_id, original_id = await resolve_assignee(user_id, db)

    assert effective_id == user_id
    assert original_id is None


# ---------------------------------------------------------------------------
# check_level_approver — SoD bypass regression (issue #118)
#
# A named-approver chain/level exists to restrict who may clear it to
# specific people. Before this fix, `advance_approval_chain` recorded any
# actor's approval unconditionally — a broad role (ap_manager/cfo/admin) was
# enough to clear a level meant for a named individual.
# ---------------------------------------------------------------------------


def _ctrl_session_factory(user):
    """A `control_session_factory`-shaped mock whose lookup returns `user`
    (or None) for any `SELECT ... WHERE User.id == ...`."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=user)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.mark.asyncio
async def test_check_level_approver_empty_list_allows_anyone():
    """An empty approver_ids list means unrestricted — legacy behaviour."""
    from app.services.approval_chain import check_level_approver

    # No control_session_factory call should even happen — patch it to blow
    # up if touched, to prove the empty-list path short-circuits.
    with patch(
        "app.database.control_session_factory", side_effect=AssertionError("should not run")
    ):
        await check_level_approver([], uuid.uuid4())  # must not raise


@pytest.mark.asyncio
async def test_check_level_approver_direct_member_allowed():
    """An actor whose id is directly in approver_ids is authorized."""
    from app.services.approval_chain import check_level_approver

    actor = uuid.uuid4()
    with patch(
        "app.database.control_session_factory", side_effect=AssertionError("should not run")
    ):
        await check_level_approver([str(actor)], actor)  # must not raise — no DB lookup needed


@pytest.mark.asyncio
async def test_check_level_approver_non_member_rejected():
    """An actor who is not a named approver, and has no active delegation
    from one, is refused with 403 — the core regression."""
    from app.services.approval_chain import check_level_approver

    named_approver_id = uuid.uuid4()
    actor = uuid.uuid4()  # unrelated user, e.g. a different ap_manager

    with patch("app.database.control_session_factory", _ctrl_session_factory(None)):
        with pytest.raises(HTTPException) as exc:
            await check_level_approver([str(named_approver_id)], actor)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_check_level_approver_active_delegate_allowed():
    """A user the named approver has actively delegated to may approve in
    their place."""
    from app.services.approval_chain import check_level_approver

    named_approver_id = uuid.uuid4()
    delegate = uuid.uuid4()
    delegating_user = _make_user(
        delegate_to_id=delegate,
        delegate_until=datetime.now(UTC) + timedelta(days=1),
    )
    delegating_user.id = named_approver_id

    with patch("app.database.control_session_factory", _ctrl_session_factory(delegating_user)):
        await check_level_approver([str(named_approver_id)], delegate)  # must not raise


@pytest.mark.asyncio
async def test_check_level_approver_expired_delegate_rejected():
    """A delegation that has expired does not authorize the ex-delegate."""
    from app.services.approval_chain import check_level_approver

    named_approver_id = uuid.uuid4()
    ex_delegate = uuid.uuid4()
    delegating_user = _make_user(
        delegate_to_id=ex_delegate,
        delegate_until=datetime.now(UTC) - timedelta(seconds=1),
    )
    delegating_user.id = named_approver_id

    with patch("app.database.control_session_factory", _ctrl_session_factory(delegating_user)):
        with pytest.raises(HTTPException) as exc:
            await check_level_approver([str(named_approver_id)], ex_delegate)

    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# resolve_applicable_levels
# ---------------------------------------------------------------------------


def test_all_levels_apply_unbounded():
    """Levels with no amount bounds always apply, regardless of invoice amount."""
    from app.services.approval_chain import resolve_applicable_levels

    chain = [
        {"name": "L1"},
        {"name": "L2"},
    ]
    result = resolve_applicable_levels(chain, amount=99999)
    assert len(result) == 2
    assert result[0]["name"] == "L1"
    assert result[1]["name"] == "L2"


def test_level_filtered_by_min_amount():
    """A level with min_amount=500 is excluded when amount=100."""
    from app.services.approval_chain import resolve_applicable_levels

    chain = [{"name": "Manager", "min_amount": 500}]
    result = resolve_applicable_levels(chain, amount=100)
    assert result == []


def test_level_filtered_by_max_amount():
    """A level with max_amount=500 is excluded when amount=1000."""
    from app.services.approval_chain import resolve_applicable_levels

    chain = [{"name": "Clerk", "max_amount": 500}]
    result = resolve_applicable_levels(chain, amount=1000)
    assert result == []


def test_level_within_range():
    """A level with min=100 and max=500 is included when amount=300."""
    from app.services.approval_chain import resolve_applicable_levels

    chain = [{"name": "Manager", "min_amount": 100, "max_amount": 500}]
    result = resolve_applicable_levels(chain, amount=300)
    assert len(result) == 1
    assert result[0]["name"] == "Manager"


def test_level_at_exact_boundary_is_included():
    """Boundary values (amount == min or amount == max) are inclusive."""
    from app.services.approval_chain import resolve_applicable_levels

    chain = [{"name": "Exact", "min_amount": 100, "max_amount": 500}]

    assert len(resolve_applicable_levels(chain, amount=100)) == 1
    assert len(resolve_applicable_levels(chain, amount=500)) == 1


def test_empty_chain():
    """An empty chain returns an empty result for any amount."""
    from app.services.approval_chain import resolve_applicable_levels

    assert resolve_applicable_levels([], amount=12345) == []


# ---------------------------------------------------------------------------
# init_chain_state
# ---------------------------------------------------------------------------


def test_init_creates_level_structure():
    """After init, state_data contains a well-formed approval_levels dict."""
    from app.services.approval_chain import init_chain_state

    instance = _make_instance(state_data=None)
    levels = [
        {"name": "Manager", "required_approvals": 1, "approver_ids": ["uid-a"]},
        {"name": "CFO", "required_approvals": 2, "approver_ids": ["uid-b", "uid-c"]},
    ]

    init_chain_state(instance, levels)

    chain = instance.state_data["approval_levels"]
    assert chain["current_level"] == 0
    assert len(chain["levels"]) == 2

    first = chain["levels"][0]
    assert first["level"] == 0
    assert first["name"] == "Manager"
    assert first["required"] == 1
    assert first["approver_ids"] == ["uid-a"]
    assert first["approvals"] == []

    second = chain["levels"][1]
    assert second["level"] == 1
    assert second["name"] == "CFO"
    assert second["required"] == 2


def test_init_preserves_existing_state_data():
    """Initialising chain state does not erase unrelated keys like rejection_count."""
    from app.services.approval_chain import init_chain_state

    instance = _make_instance(state_data={"rejection_count": 3, "other_key": "value"})
    levels = [{"name": "L1", "required_approvals": 1, "approver_ids": []}]

    init_chain_state(instance, levels)

    assert instance.state_data["rejection_count"] == 3
    assert instance.state_data["other_key"] == "value"
    assert "approval_levels" in instance.state_data


# ---------------------------------------------------------------------------
# advance_approval_chain
# ---------------------------------------------------------------------------


def test_first_approval_on_single_level_completes():
    """A single-level chain with required=1 returns True on the first approval."""
    from app.services.approval_chain import advance_approval_chain, init_chain_state

    instance = _make_instance(state_data=None)
    init_chain_state(instance, [{"name": "L1", "required_approvals": 1, "approver_ids": []}])

    complete = advance_approval_chain(instance, uuid.uuid4())

    assert complete is True


def test_first_approval_on_multi_level_advances():
    """First approval on a 2-level chain advances to level 1 but returns False."""
    from app.services.approval_chain import advance_approval_chain, init_chain_state

    instance = _make_instance(state_data=None)
    init_chain_state(
        instance,
        [
            {"name": "L1", "required_approvals": 1, "approver_ids": []},
            {"name": "L2", "required_approvals": 1, "approver_ids": []},
        ],
    )

    complete = advance_approval_chain(instance, uuid.uuid4())

    assert complete is False
    assert instance.state_data["approval_levels"]["current_level"] == 1


def test_second_approval_on_multi_level_completes():
    """After advancing to level 1, the second approval (on level 1) returns True."""
    from app.services.approval_chain import advance_approval_chain, init_chain_state

    instance = _make_instance(state_data=None)
    init_chain_state(
        instance,
        [
            {"name": "L1", "required_approvals": 1, "approver_ids": []},
            {"name": "L2", "required_approvals": 1, "approver_ids": []},
        ],
    )

    # First approval advances to level 1
    advance_approval_chain(instance, uuid.uuid4())
    # Second approval completes the chain
    complete = advance_approval_chain(instance, uuid.uuid4())

    assert complete is True


def test_multiple_approvals_required():
    """A level that requires 2 approvals returns False on the first, True on the second."""
    from app.services.approval_chain import advance_approval_chain, init_chain_state

    instance = _make_instance(state_data=None)
    init_chain_state(instance, [{"name": "Dual", "required_approvals": 2, "approver_ids": []}])

    first_result = advance_approval_chain(instance, uuid.uuid4())
    assert first_result is False

    second_result = advance_approval_chain(instance, uuid.uuid4())
    assert second_result is True


def test_same_actor_cannot_satisfy_two_levels():
    """A multi-level chain requires distinct approvers per level. The actor who
    cleared level 0 must NOT be able to advance level 1 alone — that would
    collapse a 3-eye control to a single person."""
    import pytest
    from fastapi import HTTPException

    from app.services.approval_chain import advance_approval_chain, init_chain_state

    instance = _make_instance(state_data=None)
    init_chain_state(
        instance,
        [
            {"name": "L1", "required_approvals": 1, "approver_ids": []},
            {"name": "L2", "required_approvals": 1, "approver_ids": []},
        ],
    )

    actor = uuid.uuid4()
    # Actor clears level 0 (advances, not complete).
    assert advance_approval_chain(instance, actor) is False
    assert instance.state_data["approval_levels"]["current_level"] == 1

    # The SAME actor trying to clear level 1 is refused with 403, and the chain
    # stays on level 1 (no silent self-advance).
    with pytest.raises(HTTPException) as exc:
        advance_approval_chain(instance, actor)
    assert exc.value.status_code == 403
    assert instance.state_data["approval_levels"]["current_level"] == 1

    # A different approver clears level 1 → chain completes.
    assert advance_approval_chain(instance, uuid.uuid4()) is True


def test_empty_chain_state_returns_true():
    """An instance with no approval_levels in state_data is treated as complete."""
    from app.services.approval_chain import advance_approval_chain

    instance = _make_instance(state_data={})

    complete = advance_approval_chain(instance, uuid.uuid4())

    assert complete is True


def test_advance_records_actor_id_in_approvals():
    """Each call to advance records the actor_id on the current level's approvals list."""
    from app.services.approval_chain import advance_approval_chain, init_chain_state

    instance = _make_instance(state_data=None)
    init_chain_state(instance, [{"name": "L1", "required_approvals": 2, "approver_ids": []}])

    actor = uuid.uuid4()
    advance_approval_chain(instance, actor)

    approvals = instance.state_data["approval_levels"]["levels"][0]["approvals"]
    assert len(approvals) == 1
    assert approvals[0]["user_id"] == str(actor)
    assert "at" in approvals[0]
