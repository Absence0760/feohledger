"""Tests for the approval-routing additions:
  - department / GL / vendor routing rules on chain levels
  - parallel_mode "all" vs "any"
  - escalation: when a level sits past escalation_hours, the sweeper
    appends `escalation_to_user_ids` onto its `approver_ids` list

The sweeper itself talks to a tenant DB; that integration lives in the
e2e suite. These pin the pure-Python edges.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace


def _instance(state_data=None):
    return SimpleNamespace(id=uuid.uuid4(), state_data=state_data, state="active")


# ---------- routing rules ------------------------------------------------


def test_routing_rule_eq_match_includes_level():
    from app.services.approval_chain import resolve_applicable_levels

    chain = [
        {
            "name": "IT",
            "routing_rules": [{"field": "department", "operator": "eq", "value": "IT"}],
        }
    ]
    out = resolve_applicable_levels(chain, amount=1000, invoice_attrs={"department": "IT"})
    assert len(out) == 1


def test_routing_rule_eq_mismatch_excludes_level():
    from app.services.approval_chain import resolve_applicable_levels

    chain = [
        {
            "name": "IT",
            "routing_rules": [{"field": "department", "operator": "eq", "value": "IT"}],
        }
    ]
    out = resolve_applicable_levels(chain, amount=1000, invoice_attrs={"department": "Finance"})
    assert out == []


def test_routing_rule_in_set_match():
    from app.services.approval_chain import resolve_applicable_levels

    chain = [
        {
            "name": "Ops",
            "routing_rules": [
                {"field": "gl_account", "operator": "in", "value": ["6000", "6100", "6200"]}
            ],
        }
    ]
    out = resolve_applicable_levels(chain, amount=500, invoice_attrs={"gl_account": "6100"})
    assert len(out) == 1


def test_routing_rule_starts_with():
    from app.services.approval_chain import resolve_applicable_levels

    chain = [
        {
            "name": "OpEx",
            "routing_rules": [{"field": "gl_account", "operator": "starts_with", "value": "6"}],
        }
    ]
    out = resolve_applicable_levels(chain, amount=500, invoice_attrs={"gl_account": "6100"})
    assert len(out) == 1


def test_routing_rules_and_compose_with_amount():
    """Routing rules AND with min/max amount; both must hold."""
    from app.services.approval_chain import resolve_applicable_levels

    chain = [
        {
            "name": "IT-large",
            "min_amount": 1000,
            "routing_rules": [{"field": "department", "operator": "eq", "value": "IT"}],
        }
    ]
    # Right dept, amount too low → excluded.
    assert resolve_applicable_levels(chain, amount=500, invoice_attrs={"department": "IT"}) == []
    # Right dept, amount fine → included.
    assert (
        len(resolve_applicable_levels(chain, amount=2000, invoice_attrs={"department": "IT"})) == 1
    )


def test_routing_unknown_field_silently_passes():
    """A stale UI config that references a field the engine doesn't know
    must not hard-fail — the unknown field reads as None and only
    matches `ne`/`not_in` rules. This is the fail-open design."""
    from app.services.approval_chain import resolve_applicable_levels

    chain = [
        {
            "name": "X",
            "routing_rules": [{"field": "made_up_field", "operator": "eq", "value": "x"}],
        }
    ]
    # No invoice has `made_up_field`, so eq fails → level excluded.
    assert resolve_applicable_levels(chain, amount=1, invoice_attrs={}) == []


# ---------- Decimal-exact amount routing (money is never float) ----------


def test_amount_routing_is_decimal_exact_at_fractional_boundary():
    """A boundary invoice must route on exact-Decimal comparison, not on the
    float the amount used to be cast to. Thresholds may arrive as numeric
    strings (JSON config); the engine coerces both sides to Decimal."""
    from decimal import Decimal

    from app.services.approval_chain import resolve_applicable_levels

    chain = [{"name": "tier", "min_amount": "100.10", "max_amount": "200.20"}]

    # Exact lower/upper boundary amounts are inclusive.
    assert len(resolve_applicable_levels(chain, Decimal("100.10"))) == 1
    assert len(resolve_applicable_levels(chain, Decimal("200.20"))) == 1
    # One cent outside either edge is excluded.
    assert resolve_applicable_levels(chain, Decimal("100.09")) == []
    assert resolve_applicable_levels(chain, Decimal("200.21")) == []


def test_amount_routing_accepts_decimal_thresholds_and_float_literals():
    """Mixed threshold types (Decimal level config + float literal) still
    compare exactly — a float min_amount goes through str() so it doesn't drift."""
    from decimal import Decimal

    from app.services.approval_chain import resolve_applicable_levels

    chain = [{"name": "big", "min_amount": 5000.0}]
    assert len(resolve_applicable_levels(chain, Decimal("5000.00"))) == 1
    assert resolve_applicable_levels(chain, Decimal("4999.99")) == []


def test_invoice_routing_attrs_picks_off_invoice():
    from app.services.approval_chain import invoice_routing_attrs

    vendor_id = uuid.uuid4()
    inv = SimpleNamespace(
        gl_account="6100",
        cost_center="CC-1",
        department="Eng",
        vendor_id=vendor_id,
    )
    attrs = invoice_routing_attrs(inv)
    assert attrs["gl_account"] == "6100"
    assert attrs["cost_center"] == "CC-1"
    assert attrs["department"] == "Eng"
    assert attrs["vendor_id"] == str(vendor_id)


# ---------- parallel_mode -------------------------------------------------


def test_parallel_mode_any_satisfies_with_required_count():
    """Default `any`: required=2, two distinct approvers → complete."""
    from app.services.approval_chain import advance_approval_chain, init_chain_state

    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "L",
                "approver_ids": ["a", "b", "c"],
                "required_approvals": 2,
                "parallel_mode": "any",
            }
        ],
    )
    a, b = uuid.uuid4(), uuid.uuid4()
    assert advance_approval_chain(inst, a) is False
    assert advance_approval_chain(inst, b) is True


def test_parallel_mode_all_requires_every_listed_approver():
    """`all` mode: every approver_id must approve, regardless of required_approvals."""
    from app.services.approval_chain import advance_approval_chain, init_chain_state

    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "L",
                "approver_ids": [str(a), str(b), str(c)],
                "required_approvals": 1,  # ignored in `all` mode
                "parallel_mode": "all",
            }
        ],
    )
    assert advance_approval_chain(inst, a) is False
    assert advance_approval_chain(inst, b) is False
    assert advance_approval_chain(inst, c) is True


def test_parallel_mode_all_ignores_duplicate_approvals():
    """Approving twice from the same user does not double-count."""
    from app.services.approval_chain import advance_approval_chain, init_chain_state

    a, b = uuid.uuid4(), uuid.uuid4()
    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "L",
                "approver_ids": [str(a), str(b)],
                "parallel_mode": "all",
            }
        ],
    )
    advance_approval_chain(inst, a)
    advance_approval_chain(inst, a)  # duplicate
    # Still missing b, so the level isn't satisfied.
    assert inst.state_data["approval_levels"]["current_level"] == 0


# ---------- escalation ---------------------------------------------------


def test_init_chain_state_stamps_entered_at_on_first_level():
    from app.services.approval_chain import init_chain_state

    inst = _instance()
    init_chain_state(inst, [{"name": "L", "approver_ids": []}])
    levels = inst.state_data["approval_levels"]["levels"]
    assert levels[0]["entered_at"] is not None


def test_advance_stamps_entered_at_on_next_level():
    """When the chain advances past a level, the new current level gets
    its own entered_at timestamp so the sweeper can age it."""
    from app.services.approval_chain import advance_approval_chain, init_chain_state

    inst = _instance()
    init_chain_state(
        inst,
        [
            {"name": "L1", "approver_ids": [], "required_approvals": 1},
            {"name": "L2", "approver_ids": [], "required_approvals": 1},
        ],
    )
    advance_approval_chain(inst, uuid.uuid4())
    levels = inst.state_data["approval_levels"]["levels"]
    assert levels[1]["entered_at"] is not None


def test_apply_escalation_no_op_when_not_due():
    from app.services.approval_chain import apply_escalation, init_chain_state

    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "L",
                "approver_ids": ["a"],
                "escalation_hours": 4,
                "escalation_to_user_ids": ["esc-1"],
            }
        ],
    )
    # entered_at is now → not yet due.
    changed = apply_escalation(inst)
    assert changed is False


def test_apply_escalation_appends_targets_when_overdue():
    from app.services.approval_chain import apply_escalation, init_chain_state

    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "L",
                "approver_ids": ["a"],
                "escalation_hours": 4,
                "escalation_to_user_ids": ["esc-1", "esc-2"],
            }
        ],
    )
    # Backdate entry so the sweep sees it as overdue.
    levels = inst.state_data["approval_levels"]["levels"]
    levels[0]["entered_at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    inst.state_data = inst.state_data  # nudge

    changed = apply_escalation(inst)
    assert changed is True
    after = inst.state_data["approval_levels"]["levels"][0]
    assert "esc-1" in after["approver_ids"]
    assert "esc-2" in after["approver_ids"]
    assert len(after["escalations"]) == 1
    assert after["escalations"][0]["after_hours"] == 4


def test_apply_escalation_idempotent():
    """Once a level has absorbed the escalation user set, re-running is a
    no-op — the sweeper can run on a tight interval without spamming the
    state. New escalation targets WOULD trigger a fresh event, but a
    redundant call must not."""
    from app.services.approval_chain import apply_escalation, init_chain_state

    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "L",
                "approver_ids": ["a"],
                "escalation_hours": 4,
                "escalation_to_user_ids": ["esc-1"],
            }
        ],
    )
    levels = inst.state_data["approval_levels"]["levels"]
    levels[0]["entered_at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    inst.state_data = inst.state_data

    assert apply_escalation(inst) is True
    assert apply_escalation(inst) is False  # second call is no-op


def test_apply_escalation_skips_levels_without_config():
    from app.services.approval_chain import apply_escalation, init_chain_state

    inst = _instance()
    init_chain_state(inst, [{"name": "L", "approver_ids": ["a"]}])
    # No escalation_hours / escalation_to_user_ids → never escalates.
    levels = inst.state_data["approval_levels"]["levels"]
    levels[0]["entered_at"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    inst.state_data = inst.state_data
    assert apply_escalation(inst) is False


def test_apply_escalation_appends_in_configured_order():
    """The `any` branch used to spell the union as `list(set | set)`, whose
    iteration order depends on PYTHONHASHSEED — so the same escalation rewrote
    an audit-visible JSONB column differently run to run."""
    from app.services.approval_chain import apply_escalation, init_chain_state

    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "L",
                "approver_ids": ["a", "b"],
                "escalation_hours": 4,
                "escalation_to_user_ids": ["esc-1", "esc-2"],
            }
        ],
    )
    levels = inst.state_data["approval_levels"]["levels"]
    levels[0]["entered_at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    inst.state_data = inst.state_data

    assert apply_escalation(inst) is True
    after = inst.state_data["approval_levels"]["levels"][0]
    assert after["approver_ids"] == ["a", "b", "esc-1", "esc-2"]


# ---------------------------------------------------------------------------
# An UNRESTRICTED level (empty `approver_ids`) must not be escalated at all.
# `check_level_approver` reads an empty allow-list as "any RBAC-cleared actor
# may approve", so writing the escalation targets in NARROWS an open level to
# those users alone — 403-ing everyone who could approve it a moment earlier.
# Same inversion as issue #128, arriving through the empty-list case.
# ---------------------------------------------------------------------------


def _overdue_unrestricted_instance(parallel_mode: str):
    from app.services.approval_chain import init_chain_state

    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "Any manager",
                "approver_ids": [],  # unrestricted
                "parallel_mode": parallel_mode,
                "escalation_hours": 4,
                "escalation_to_user_ids": ["esc-1"],
            }
        ],
    )
    levels = inst.state_data["approval_levels"]["levels"]
    levels[0]["entered_at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    inst.state_data = inst.state_data
    return inst


def test_apply_escalation_leaves_an_unrestricted_any_level_open():
    from app.services.approval_chain import apply_escalation

    inst = _overdue_unrestricted_instance("any")
    assert apply_escalation(inst) is False
    after = inst.state_data["approval_levels"]["levels"][0]
    # Still unrestricted — nobody lost the ability to approve.
    assert after["approver_ids"] == []
    assert after["escalations"] == []


def test_apply_escalation_leaves_an_unrestricted_all_level_open():
    from app.services.approval_chain import apply_escalation

    inst = _overdue_unrestricted_instance("all")
    assert apply_escalation(inst) is False
    assert inst.state_data["approval_levels"]["levels"][0]["approver_ids"] == []


async def test_escalating_an_unrestricted_level_never_locks_out_an_eligible_approver():
    """End-to-end on the gate the escalation feeds: before the sweep any actor
    passes `check_level_approver`; after it, the same actor must still pass."""
    from app.services.approval_chain import apply_escalation, check_level_approver

    inst = _overdue_unrestricted_instance("any")
    actor = uuid.uuid4()

    level = inst.state_data["approval_levels"]["levels"][0]
    await check_level_approver(level.get("approver_ids", []), actor)  # no raise

    apply_escalation(inst)

    level = inst.state_data["approval_levels"]["levels"][0]
    # Would raise HTTPException(403) if the escalation had narrowed the level.
    await check_level_approver(level.get("approver_ids", []), actor)


# ---------------------------------------------------------------------------
# 'all' mode escalation must SUBSTITUTE the stuck approver(s), not append on
# top of the requirement (issue #128) — appending makes an 'all' level need
# {A, B, C} where it used to need {A, B}, the opposite of "unblock".
# ---------------------------------------------------------------------------


def test_apply_escalation_all_mode_substitutes_unapproved_approver():
    """A level needing {A, B} (parallel_mode='all') with NEITHER having
    approved yet must become {C} after escalating to C — not {A, B, C}."""
    from app.services.approval_chain import apply_escalation, init_chain_state

    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "L",
                "approver_ids": ["a", "b"],
                "parallel_mode": "all",
                "escalation_hours": 4,
                "escalation_to_user_ids": ["esc-1"],
            }
        ],
    )
    levels = inst.state_data["approval_levels"]["levels"]
    levels[0]["entered_at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    inst.state_data = inst.state_data

    changed = apply_escalation(inst)
    assert changed is True
    after = inst.state_data["approval_levels"]["levels"][0]
    # Neither original stuck approver survives — the level no longer needs them.
    assert after["approver_ids"] == ["esc-1"]


def test_apply_escalation_all_mode_keeps_already_approved_approver():
    """{A, B} where A already approved: escalating to C must shrink the
    requirement to {A, C} — A's prior approval still counts, only the
    UNAVAILABLE approver (B) is substituted. The level clears once C
    approves, without needing a fresh sign-off from A."""
    from app.services.approval_chain import _level_satisfied, apply_escalation, init_chain_state

    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "L",
                "approver_ids": ["a", "b"],
                "parallel_mode": "all",
                "escalation_hours": 4,
                "escalation_to_user_ids": ["esc-1"],
            }
        ],
    )
    levels = inst.state_data["approval_levels"]["levels"]
    # A approves (direct state mutation — approver_ids/approvals are plain
    # strings in this pure layer, no real UUID actor round-trip needed); B
    # never does.
    levels[0]["approvals"].append({"user_id": "a", "at": datetime.now(UTC).isoformat()})
    levels[0]["entered_at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    inst.state_data = inst.state_data

    changed = apply_escalation(inst)
    assert changed is True
    after = inst.state_data["approval_levels"]["levels"][0]
    assert set(after["approver_ids"]) == {"a", "esc-1"}
    assert "b" not in after["approver_ids"]  # the stuck approver is gone
    assert _level_satisfied(after) is False  # esc-1 hasn't approved yet

    # esc-1 approving now clears the level — A's prior approval still counts.
    after["approvals"].append({"user_id": "esc-1", "at": datetime.now(UTC).isoformat()})
    assert _level_satisfied(after) is True


def test_apply_escalation_all_mode_never_makes_level_harder_to_satisfy():
    """Regression for the exact issue #128 scenario: escalating an 'all'
    level must never leave MORE outstanding (unapproved) approvers than
    before — appending grew {A, B} to {A, B, C} (3 outstanding instead of
    2); substitution must keep or shrink the outstanding count."""
    from app.services.approval_chain import apply_escalation, init_chain_state

    inst = _instance()
    init_chain_state(
        inst,
        [
            {
                "name": "L",
                "approver_ids": ["a", "b"],
                "parallel_mode": "all",
                "escalation_hours": 4,
                "escalation_to_user_ids": ["esc-1"],
            }
        ],
    )
    levels = inst.state_data["approval_levels"]["levels"]
    before_outstanding = set(levels[0]["approver_ids"])
    levels[0]["entered_at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    inst.state_data = inst.state_data

    apply_escalation(inst)
    after = inst.state_data["approval_levels"]["levels"][0]
    after_outstanding = set(after["approver_ids"])

    assert len(after_outstanding) <= len(before_outstanding)
    assert "a" not in after_outstanding
    assert "b" not in after_outstanding


# ---------------------------------------------------------------------------
# Escalation must never hand a level to someone who cannot approve it. Two
# refusals apply at approval time: segregation of duties (the payable's
# implicated actors) and the cross-level guard in `advance_approval_chain` (one
# person per level). A target who trips either is not an approver at all, and in
# 'all' mode substituting them in turned a stuck level into a permanently
# UNCLEARABLE one — the sweep is idempotent, so it never tried again.
# ---------------------------------------------------------------------------


def _overdue_chain(inst, levels: list[dict], *, current: int = 0) -> None:
    from app.services.approval_chain import init_chain_state

    init_chain_state(inst, levels)
    state = inst.state_data["approval_levels"]
    state["current_level"] = current
    state["levels"][current]["entered_at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()


def test_apply_escalation_all_mode_skips_an_earlier_level_approver():
    """'x' cleared level 0, so `advance_approval_chain` refuses them at level 1.
    Substituting them in as level 1's only outstanding approver would make the
    level impossible to satisfy — the escalation must not happen."""
    from app.services.approval_chain import apply_escalation

    inst = _instance()
    _overdue_chain(
        inst,
        [
            {"name": "L0", "approver_ids": ["x"]},
            {
                "name": "L1",
                "approver_ids": ["a", "b"],
                "parallel_mode": "all",
                "escalation_hours": 4,
                "escalation_to_user_ids": ["x"],
            },
        ],
        current=1,
    )
    inst.state_data["approval_levels"]["levels"][0]["approvals"].append(
        {"user_id": "x", "at": datetime.now(UTC).isoformat()}
    )

    assert apply_escalation(inst) is False
    level = inst.state_data["approval_levels"]["levels"][1]
    assert level["approver_ids"] == ["a", "b"]
    assert level["escalations"] == []


def test_apply_escalation_never_adds_an_ineligible_target():
    """An implicated actor (the uploader, say) is filtered out of the targets;
    the eligible ones still land, in both modes."""
    from app.services.approval_chain import apply_escalation

    for mode in ("any", "all"):
        inst = _instance()
        _overdue_chain(
            inst,
            [
                {
                    "name": "L",
                    "approver_ids": ["a"],
                    "parallel_mode": mode,
                    "escalation_hours": 4,
                    "escalation_to_user_ids": ["uploader", "esc-1"],
                }
            ],
        )
        assert apply_escalation(inst, ineligible={"uploader"}) is True
        level = inst.state_data["approval_levels"]["levels"][0]
        assert "uploader" not in level["approver_ids"], mode
        assert "esc-1" in level["approver_ids"], mode
        assert level["escalations"][-1]["added_user_ids"] == ["esc-1"], mode


def test_apply_escalation_is_a_no_op_when_every_target_is_ineligible():
    from app.services.approval_chain import apply_escalation

    inst = _instance()
    _overdue_chain(
        inst,
        [
            {
                "name": "L",
                "approver_ids": ["a", "b"],
                "parallel_mode": "all",
                "escalation_hours": 4,
                "escalation_to_user_ids": ["uploader"],
            }
        ],
    )
    assert apply_escalation(inst, ineligible={"uploader"}) is False
    assert inst.state_data["approval_levels"]["levels"][0]["approver_ids"] == ["a", "b"]


# ---------------------------------------------------------------------------
# A chain nobody has approved is re-routed on the corrected invoice
#
# The escalation sweep now initialises a chain (to escalate level 0) before
# anyone approves, from the invoice as it was then. Routing has always been
# decided at the first approval, on the POST-correction figures; a sweep-built
# chain must not freeze it earlier, or an amount corrected up into a higher band
# clears without that band's level.
# ---------------------------------------------------------------------------

_BANDED_CONFIG = {
    "approver_strategy": "chain",
    "approval_chain": [
        {
            "name": "Manager",
            "approver_ids": ["m"],
            "escalation_hours": 4,
            "escalation_to_user_ids": ["esc"],
        },
        {"name": "CFO", "approver_ids": ["cfo"], "min_amount": "10000"},
    ],
}


def _inv(amount: str):
    from decimal import Decimal

    return SimpleNamespace(amount=Decimal(amount), currency="USD", vendor_id=None)


def _escalated_unstarted_chain(amount: str):
    from app.services.approval_chain import apply_escalation, init_chain_for_invoice

    inst = _instance()
    entered = datetime.now(UTC) - timedelta(hours=5)
    assert init_chain_for_invoice(
        inst, _inv(amount), _BANDED_CONFIG, org_settings=None, entered_at=entered
    )
    assert apply_escalation(inst) is True
    return inst, entered


def test_ensure_chain_routed_reroutes_an_unstarted_chain_into_a_higher_band():
    from app.services.approval_chain import ensure_chain_routed, get_chain_progress

    inst, entered = _escalated_unstarted_chain("500")
    assert [lv["name"] for lv in get_chain_progress(inst)["levels"]] == ["Manager"]

    ensure_chain_routed(inst, _inv("50000"), _BANDED_CONFIG, org_settings=None)

    chain = get_chain_progress(inst)
    assert [lv["name"] for lv in chain["levels"]] == ["Manager", "CFO"]
    assert chain["routing"] == [0, 1]
    manager = chain["levels"][0]
    # The Manager level keeps its escalation and the clock it has been running.
    assert manager["approver_ids"] == ["m", "esc"]
    assert len(manager["escalations"]) == 1
    assert datetime.fromisoformat(manager["entered_at"]) == entered


def test_ensure_chain_routed_leaves_an_unchanged_routing_alone():
    import copy

    from app.services.approval_chain import ensure_chain_routed

    inst, _ = _escalated_unstarted_chain("500")
    before = copy.deepcopy(inst.state_data)
    ensure_chain_routed(inst, _inv("600"), _BANDED_CONFIG, org_settings=None)
    assert inst.state_data == before


def test_ensure_chain_routed_never_reroutes_once_someone_has_approved():
    from app.services.approval_chain import (
        advance_approval_chain,
        ensure_chain_routed,
        get_chain_progress,
        init_chain_for_invoice,
    )

    config = {
        "approver_strategy": "chain",
        "approval_chain": [
            {"name": "L0", "approver_ids": []},
            {"name": "L1", "approver_ids": []},
            {"name": "CFO", "approver_ids": [], "min_amount": "10000"},
        ],
    }
    inst = _instance()
    init_chain_for_invoice(inst, _inv("500"), config, org_settings=None)
    advance_approval_chain(inst, uuid.uuid4())
    ensure_chain_routed(inst, _inv("50000"), config, org_settings=None)
    assert [lv["name"] for lv in get_chain_progress(inst)["levels"]] == ["L0", "L1"]


def test_ensure_chain_routed_ignores_a_chain_without_routing():
    """A chain not built by `init_chain_for_invoice` (by hand, or legacy) is
    never rewritten."""
    import copy

    from app.services.approval_chain import ensure_chain_routed, init_chain_state

    inst = _instance()
    init_chain_state(inst, [{"name": "Hand-built", "approver_ids": ["x"]}])
    before = copy.deepcopy(inst.state_data)
    ensure_chain_routed(inst, _inv("50000"), _BANDED_CONFIG, org_settings=None)
    assert inst.state_data == before


def test_ensure_chain_routed_drops_a_chain_no_level_applies_to_any_more():
    from app.services.approval_chain import (
        ensure_chain_routed,
        get_chain_progress,
        init_chain_for_invoice,
    )

    config = {
        "approver_strategy": "chain",
        "approval_chain": [{"name": "Big", "approver_ids": ["b"], "min_amount": "1000"}],
    }
    inst = _instance()
    assert init_chain_for_invoice(inst, _inv("5000"), config, org_settings=None)
    ensure_chain_routed(inst, _inv("50"), config, org_settings=None)
    assert get_chain_progress(inst) == {}


def test_reroute_that_drops_level_zero_starts_the_new_head_fresh():
    """[Manager, CFO] → [CFO]: CFO becomes the head having waited no time at
    all, so it must not inherit Manager's 5-hour-old clock (the next sweep
    would escalate it at once), and Manager's escalation does not follow."""
    from app.services.approval_chain import (
        apply_escalation,
        ensure_chain_routed,
        get_chain_progress,
        init_chain_for_invoice,
    )

    config = {
        "approver_strategy": "chain",
        "approval_chain": [
            {
                "name": "Manager",
                "approver_ids": ["m"],
                "max_amount": "10000",
                "escalation_hours": 4,
                "escalation_to_user_ids": ["esc"],
            },
            {"name": "CFO", "approver_ids": ["cfo"], "min_amount": "500"},
        ],
    }
    inst = _instance()
    old_clock = datetime.now(UTC) - timedelta(hours=5)
    init_chain_for_invoice(inst, _inv("600"), config, org_settings=None, entered_at=old_clock)
    assert apply_escalation(inst) is True

    before = datetime.now(UTC)
    ensure_chain_routed(inst, _inv("50000"), config, org_settings=None)

    chain = get_chain_progress(inst)
    assert chain["routing"] == [1]
    head = chain["levels"][0]
    assert head["name"] == "CFO"
    assert datetime.fromisoformat(head["entered_at"]) >= before
    assert head["approver_ids"] == ["cfo"] and head["escalations"] == []
    assert apply_escalation(inst) is False  # not overdue


def test_route_chain_indices_track_the_configured_levels():
    """`_route_chain` maps applicable levels back to config indices by object
    identity; this pins that `resolve_applicable_levels` hands back the very
    dicts it was given, including two levels that compare equal."""
    from app.services.approval_chain import _route_chain

    twin = {"name": "Twin", "approver_ids": []}
    config = {"approval_chain": [dict(twin), {"name": "Big", "min_amount": "1000"}, dict(twin)]}
    applicable, routing = _route_chain(_inv("50"), config, org_settings=None)
    assert routing == [0, 2]
    assert all(a is config["approval_chain"][i] for a, i in zip(applicable, routing, strict=True))
