"""Coverage for the Adaptive AI Workflows first slice.

Two tiers (mirrors the repo):

  * Pure-Python edges — the approval-pattern aggregates, the σ/median-multiple
    anomaly guard, and the auto-approve-threshold derivation. These pin the
    deterministic (no-LLM, no-cloud-key) statistics without a DB.

  * Real-Postgres end-to-end (``realdb``) — drives the four ``/api/adaptive/*``
    routes against a live tenant DB so the audit_log ⋈ invoices join, the
    write-on-GET suggestion upsert, durable dismissal, the stale transition,
    RBAC, and tenant isolation are all real.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.adaptive_workflows import (
    ApproverOutcome,
    ApproverPattern,
    EligibleApprover,
    VendorBaseline,
    _stdev,
    compute_approver_outcomes,
    compute_approver_patterns,
    compute_effectiveness,
    compute_outcome_stats,
    compute_vendor_baseline,
    compute_vendor_patterns,
    derive_suggestions,
    detect_invoice_anomaly,
    is_overturned,
    outcome_adjusted_threshold,
    recommend_approvers,
    recommend_auto_approve_threshold,
)

# ---------------------------------------------------------------------------
# Pure-function unit tests (no DB)
# ---------------------------------------------------------------------------


def _decision(
    *,
    approver_id=None,
    vendor_id=None,
    vendor_name="V",
    amount="0",
    decision="approved",
    unmodified=True,
    ttd=None,
):
    return {
        "approver_id": approver_id,
        "vendor_id": vendor_id,
        "vendor_name": vendor_name,
        "amount": Decimal(amount),
        "decision": decision,
        "unmodified": unmodified,
        "time_to_approve_days": None if ttd is None else Decimal(ttd),
    }


def test_approver_patterns_basic():
    rows = [
        _decision(approver_id="A", decision="approved", ttd="1.0"),
        _decision(approver_id="A", decision="approved", ttd="2.0"),
        _decision(approver_id="A", decision="approved", ttd="3.0"),
        _decision(approver_id="A", decision="approved", ttd="1.0"),
        _decision(approver_id="A", decision="approved", ttd="3.0"),
        _decision(approver_id="A", decision="rejected"),
    ]
    pats = compute_approver_patterns(rows, names={"A": "Dana Lee"})
    assert len(pats) == 1
    p = pats[0]
    assert p.approver_id == "A"
    assert p.approver_name == "Dana Lee"
    assert p.approved_count == 5
    assert p.rejected_count == 1
    assert p.approval_rate_pct == Decimal("83.3")  # 5/6
    assert p.median_time_to_approve_days == Decimal("2.0")
    assert p.avg_time_to_approve_days == Decimal("2.0")
    assert p.sample_size == 6


def test_vendor_consistency():
    rows = [
        _decision(vendor_id="V1", vendor_name="Acme", amount="100", unmodified=True)
        for _ in range(24)
    ]
    pats = compute_vendor_patterns(rows)
    assert pats[0].consistency_pct == Decimal("100.0")

    for r in rows[:3]:
        r["unmodified"] = False
    pats = compute_vendor_patterns(rows)
    assert pats[0].consistency_pct == Decimal("87.5")  # 21/24


def test_vendor_amount_stats():
    rows = [
        _decision(vendor_id="V1", vendor_name="Acme", amount=a) for a in ("3900", "4800", "5200")
    ]
    p = compute_vendor_patterns(rows)[0]
    assert p.avg_approved_amount == Decimal("4633.33")
    assert p.median_approved_amount == Decimal("4800.00")
    assert p.min_approved_amount == Decimal("3900.00")
    assert p.max_approved_amount == Decimal("5200.00")


def test_stdev_decimal():
    vals = [Decimal(x) for x in ("2", "4", "4", "4", "5", "5", "7", "9")]
    assert _stdev(vals).quantize(Decimal("0.01")) == Decimal("2.00")


def test_baseline_insufficient_history():
    rows = [{"amount": Decimal("100"), "approver_id": "A", "time_to_approve_days": None}] * 4
    assert compute_vendor_baseline(rows, min_history=5) is None
    inv = SimpleNamespace(id="i1", vendor_id="V1", vendor_name="Acme", amount=Decimal("999"))
    res = detect_invoice_anomaly(inv, None, amount=Decimal("999"))
    assert res.insufficient_history is True
    assert res.flags == []
    assert res.baseline is None


def _baseline(mean, stdev, *, median=None, approvers=None, med_time="0"):
    return VendorBaseline(
        vendor_id="V1",
        vendor_name="Acme",
        sample_size=24,
        mean_amount=Decimal(mean),
        median_amount=Decimal(median if median is not None else mean),
        stdev_amount=Decimal(stdev),
        min_amount=Decimal("0"),
        max_amount=Decimal("0"),
        typical_approver_ids=approvers or [],
        median_time_to_approve_days=Decimal(med_time),
    )


def test_anomaly_amount_high_flagged_with_baseline():
    baseline = _baseline("4812.50", "410.20", median="4800.00")
    inv = SimpleNamespace(id="i1", vendor_id="V1", vendor_name="Acme", amount=Decimal("19500"))
    # `amount` is explicit and required — the subject expressed in the same
    # currency as the baseline (see detect_invoice_anomaly's contract).
    res = detect_invoice_anomaly(inv, baseline, amount=Decimal("19500"))
    codes = [f.code for f in res.flags]
    assert codes == ["amount_high"]
    # Explainability: baseline returned + expected bound is mean + 2σ.
    assert res.baseline is baseline
    expected = Decimal("4812.50") + Decimal("2.0") * Decimal("410.20")
    assert res.flags[0].expected == str(expected)


def test_anomaly_tight_variance_guard():
    # σ=0, all amounts 5000; invoice 5200: > mean+2σ but NOT > median*3 → no flag.
    baseline = _baseline("5000", "0", median="5000")
    inv = SimpleNamespace(id="i1", vendor_id="V1", vendor_name="Acme", amount=Decimal("5200"))
    res = detect_invoice_anomaly(inv, baseline, amount=Decimal("5200"))
    assert [f.code for f in res.flags] == []


def test_anomaly_unusual_approver():
    baseline = _baseline("5000", "100", median="5000", approvers=["A", "B"])
    inv = SimpleNamespace(id="i1", vendor_id="V1", vendor_name="Acme", amount=Decimal("5000"))
    res = detect_invoice_anomaly(inv, baseline, amount=Decimal("5000"), proposed_approver_id="Z")
    assert "unusual_approver" in [f.code for f in res.flags]
    res2 = detect_invoice_anomaly(inv, baseline, amount=Decimal("5000"), proposed_approver_id="A")
    assert "unusual_approver" not in [f.code for f in res2.flags]


def test_derive_auto_approve_suggestion():
    rows = [
        _decision(vendor_id="V1", vendor_name="Acme", amount=str(4000 + i * 50), unmodified=True)
        for i in range(24)
    ]
    pats = compute_vendor_patterns(rows)
    sugg = derive_suggestions(pats)
    assert len(sugg) == 1
    s = sugg[0]
    assert s.kind == "auto_approve_threshold"
    # max amount is 4000+23*50 = 5150 → round up to 5500.
    assert s.payload["suggested_threshold"] == "5500.00"
    assert s.confidence_pct <= Decimal("99")
    assert s.dedupe_key == "auto_approve_threshold:V1"

    # One rejection → no suggestion.
    rows_rej = rows + [_decision(vendor_id="V1", vendor_name="Acme", decision="rejected")]
    assert derive_suggestions(compute_vendor_patterns(rows_rej)) == []

    # 11 approvals (< min_history 12) → none.
    assert derive_suggestions(compute_vendor_patterns(rows[:11])) == []


def test_derive_no_suggestion_when_any_modification():
    # 20 approvals, 1 real modification → consistency 95.0 (clears the % floor)
    # but the gate is absolute: any modification disqualifies. This pins the
    # control-bypass the rationale ("approved with no corrections") must never
    # falsely claim — see backend/docs/adaptive-workflows.md.
    rows = [
        _decision(vendor_id="V1", vendor_name="Acme", amount="4800", unmodified=True)
        for _ in range(20)
    ]
    rows[0]["unmodified"] = False  # exactly one corrected approval
    pats = compute_vendor_patterns(rows)
    assert pats[0].consistency_pct == Decimal("95.0")  # at the old % boundary
    assert pats[0].unmodified_count == 19
    assert pats[0].approved_count == 20
    # Absolute gate → no suggestion despite clearing the 95% floor.
    assert derive_suggestions(pats) == []

    # Even one modification in a large, otherwise-spotless history disqualifies.
    big = [
        _decision(vendor_id="V2", vendor_name="Beta", amount="4800", unmodified=True)
        for _ in range(100)
    ]
    big[0]["unmodified"] = False  # 97.0% consistency, still disqualified
    assert derive_suggestions(compute_vendor_patterns(big)) == []

    # And the truthful happy path: title reflects the real unmodified numerator.
    clean = [
        _decision(vendor_id="V3", vendor_name="Gamma", amount="4800", unmodified=True)
        for _ in range(20)
    ]
    s = derive_suggestions(compute_vendor_patterns(clean))[0]
    assert "20/20 invoices approved unmodified" in s.title


# ---------------------------------------------------------------------------
# Auto-approve threshold recommendation — pure-function tests
# ---------------------------------------------------------------------------


def _clean_vendor_patterns(specs):
    """Build vendor patterns from (vendor_id, max_amount, n) tuples — each a
    spotless history (0 rejections, all unmodified)."""
    rows = []
    for vid, max_amt, n in specs:
        for i in range(n):
            # Make the LAST invoice the max so max_approved_amount == max_amt.
            amt = str(max_amt) if i == n - 1 else "100"
            rows.append(
                _decision(vendor_id=vid, vendor_name=f"V-{vid}", amount=amt, unmodified=True)
            )
    return compute_vendor_patterns(rows)


def test_threshold_recommend_raises_from_zero():
    # 3 clean vendors, max clean amount 5150 → round up to 5500; current 0 →
    # first raise uses the absolute cap only.
    pats = _clean_vendor_patterns([("A", 5150, 12), ("B", 3000, 12), ("C", 1000, 12)])
    rec = recommend_auto_approve_threshold(pats, current_threshold=Decimal("0"))
    assert rec.should_raise is True
    assert rec.reason_code == "ok"
    assert rec.recommended_threshold == Decimal("5500.00")
    assert rec.qualifying_vendor_count == 3
    assert rec.total_clean_invoices == 36
    assert len(rec.evidence) == 3
    # Affects-new-invoices caveat surfaces in the rationale.
    assert "NEW invoices" in rec.rationale


def test_threshold_refuses_insufficient_vendors():
    # Only 2 qualifying vendors (< min 3) → refuse.
    pats = _clean_vendor_patterns([("A", 9000, 12), ("B", 4000, 12)])
    rec = recommend_auto_approve_threshold(pats, current_threshold=Decimal("0"))
    assert rec.should_raise is False
    assert rec.reason_code == "insufficient_evidence"
    assert rec.recommended_threshold == rec.current_threshold == Decimal("0.00")
    assert rec.qualifying_vendor_count == 2


def test_threshold_never_lowers():
    # Evidence supports only 5500 but the org is already at 8000 → no change.
    pats = _clean_vendor_patterns([("A", 5150, 12), ("B", 3000, 12), ("C", 1000, 12)])
    rec = recommend_auto_approve_threshold(pats, current_threshold=Decimal("8000"))
    assert rec.should_raise is False
    assert rec.reason_code == "no_increase"
    assert rec.recommended_threshold == Decimal("8000.00")  # never below current


def test_threshold_relative_cap_clamps_raise():
    # current 1000, max_raise_multiple 2.0 → relative cap 2000. Evidence supports
    # 9000 but the raise is clamped to the cap, and still raises.
    pats = _clean_vendor_patterns([("A", 9000, 12), ("B", 8000, 12), ("C", 7000, 12)])
    rec = recommend_auto_approve_threshold(
        pats, current_threshold=Decimal("1000"), max_raise_multiple=Decimal("2.0")
    )
    assert rec.should_raise is True
    assert rec.reason_code == "at_cap"
    assert rec.recommended_threshold == Decimal("2000.00")
    assert rec.cap_threshold == Decimal("2000.00")
    assert "capped" in rec.rationale


def test_threshold_absolute_cap_clamps_first_raise():
    # current 0 → absolute cap only. absolute_cap 6000, evidence supports 9000.
    pats = _clean_vendor_patterns([("A", 9000, 12), ("B", 8000, 12), ("C", 7000, 12)])
    rec = recommend_auto_approve_threshold(
        pats, current_threshold=Decimal("0"), absolute_cap=Decimal("6000")
    )
    assert rec.should_raise is True
    assert rec.reason_code == "at_cap"
    assert rec.recommended_threshold == Decimal("6000.00")


def test_threshold_modifications_disqualify_vendor():
    # A vendor with one corrected approval is NOT clean evidence — mirrors the
    # absolute gate in derive_suggestions. A + B are clean; C has one corrected
    # approval, so only 2 of 3 qualify (< min 3 vendors) → refuse.
    rows = []
    for vid, max_amt, n in (("A", 5000, 12), ("B", 4000, 12)):
        for i in range(n):
            rows.append(
                _decision(
                    vendor_id=vid,
                    vendor_name=f"V-{vid}",
                    amount=str(max_amt) if i == n - 1 else "100",
                    unmodified=True,
                )
            )
    for i in range(12):
        rows.append(
            _decision(
                vendor_id="C",
                vendor_name="V-C",
                amount="3000",
                unmodified=(i != 0),  # one corrected approval
            )
        )
    rec = recommend_auto_approve_threshold(
        compute_vendor_patterns(rows), current_threshold=Decimal("0")
    )
    # Only A + B qualify (2 < 3) → refuse.
    assert rec.should_raise is False
    assert rec.reason_code == "insufficient_evidence"
    assert rec.qualifying_vendor_count == 2


# ---------------------------------------------------------------------------
# Smart routing — pure-function tests
# ---------------------------------------------------------------------------


def _pat(approver_id, *, approved, rejected=0, median_ttd="1.0"):
    """Build an ApproverPattern with the fields recommend_approvers reads."""
    total = approved + rejected
    rate = Decimal("0") if total == 0 else (Decimal(approved) / Decimal(total) * 100)
    return ApproverPattern(
        approver_id=approver_id,
        approver_name=None,
        approved_count=approved,
        rejected_count=rejected,
        approval_rate_pct=rate.quantize(Decimal("0.1")),
        median_time_to_approve_days=Decimal(median_ttd),
        avg_time_to_approve_days=Decimal(median_ttd),
        sample_size=total,
    )


def test_routing_prefers_faster_consistent_approver():
    # A is fast + clean; B is slow + has rejections. A should rank first.
    pats = [
        _pat("A", approved=20, rejected=0, median_ttd="1.0"),
        _pat("B", approved=10, rejected=10, median_ttd="10.0"),
    ]
    eligible = [
        EligibleApprover("A", "Ann"),
        EligibleApprover("B", "Bob"),
    ]
    res = recommend_approvers(eligible, pats, vendor_id="V1", vendor_name="Acme")
    assert res.insufficient_history is False
    assert [c.approver_id for c in res.candidates] == ["A", "B"]
    assert res.candidates[0].rank == 1
    assert res.candidates[1].rank == 2
    assert res.candidates[0].score > res.candidates[1].score
    # Advisory only — no assignment field, just a ranked read model.
    assert res.candidates[0].approver_name == "Ann"


def test_routing_vendor_familiarity_breaks_close_race():
    # Two identical approvers; the one familiar with the vendor wins.
    pats = [
        _pat("A", approved=15, rejected=0, median_ttd="2.0"),
        _pat("B", approved=15, rejected=0, median_ttd="2.0"),
    ]
    eligible = [
        EligibleApprover("A", "Ann", vendor_approved_count=0),
        EligibleApprover("B", "Bob", vendor_approved_count=5),
    ]
    res = recommend_approvers(eligible, pats, vendor_id="V1", vendor_name="Acme")
    assert res.candidates[0].approver_id == "B"
    assert res.candidates[0].vendor_approved_count == 5
    assert any("this vendor" in r for r in res.candidates[0].reasons)


def test_routing_new_approver_with_no_history_is_routable_but_last():
    # C has no decision history and no familiarity → appears, scored 0, ranked last,
    # and insufficient_history stays False because A/B do have history.
    pats = [_pat("A", approved=20, rejected=0, median_ttd="1.0")]
    eligible = [
        EligibleApprover("A", "Ann"),
        EligibleApprover("C", "Cal"),  # not in patterns at all
    ]
    res = recommend_approvers(eligible, pats, vendor_id="V1", vendor_name="Acme")
    assert res.insufficient_history is False
    ids = [c.approver_id for c in res.candidates]
    assert ids == ["A", "C"]
    cal = res.candidates[1]
    assert cal.sample_size == 0
    assert cal.score == Decimal("0.0")
    assert "no approval history yet" in cal.reasons


def test_routing_insufficient_history_when_nobody_has_acted():
    # No patterns, no familiarity → insufficient_history True (caller falls back).
    eligible = [EligibleApprover("A", "Ann"), EligibleApprover("B", "Bob")]
    res = recommend_approvers(eligible, [], vendor_id="V1", vendor_name="Acme")
    assert res.insufficient_history is True
    # Still returns the candidates (ranked, all score 0) so the UI can list them.
    assert {c.approver_id for c in res.candidates} == {"A", "B"}
    assert all(c.score == Decimal("0.0") for c in res.candidates)


def test_routing_top_n_caps_the_list():
    pats = [_pat(x, approved=10, rejected=0) for x in ("A", "B", "C", "D")]
    eligible = [EligibleApprover(x, x) for x in ("A", "B", "C", "D")]
    res = recommend_approvers(eligible, pats, top_n=2)
    assert len(res.candidates) == 2


def test_routing_is_advisory_no_mutation_surface():
    # Sanity: the suggestion object exposes no apply/assign affordance — it is a
    # frozen read model of ranked candidates only.
    res = recommend_approvers([EligibleApprover("A", "Ann")], [_pat("A", approved=5)])
    assert not hasattr(res, "assign")
    assert not hasattr(res.candidates[0], "apply")


# ---------------------------------------------------------------------------
# Routing outcome down-weighting (feedback loop folded into the router)
# ---------------------------------------------------------------------------


def _arow(approver_id, invoice_id, *, voided=False, corrected=False, rejected=False):
    return {
        "approver_id": approver_id,
        "invoice_id": invoice_id,
        "voided": voided,
        "corrected": corrected,
        "rejected": rejected,
    }


def _aoutcome(approver_id, *, decided, overturned, insufficient=False):
    rate = Decimal("0") if decided == 0 else (Decimal(overturned) / Decimal(decided) * 100)
    return ApproverOutcome(
        approver_id=approver_id,
        decided_count=decided,
        overturned_count=overturned,
        overturn_rate_pct=rate.quantize(Decimal("0.1")),
        insufficient_data=insufficient,
    )


def test_is_overturned_primitive():
    assert is_overturned(voided=True, corrected=False, rejected=False) is True
    assert is_overturned(voided=False, corrected=True, rejected=False) is True
    assert is_overturned(voided=False, corrected=False, rejected=True) is True
    assert is_overturned(voided=False, corrected=False, rejected=False) is False


def test_compute_approver_outcomes_rate_and_dedupe():
    # A: 5 decided, 1 overturned (the same invoice appearing twice counts once).
    rows = [_arow("A", f"i{i}") for i in range(4)]
    rows.append(_arow("A", "bad", voided=True))
    rows.append(_arow("A", "bad", rejected=True))  # same invoice, second signal
    out = compute_approver_outcomes(rows, min_sample=5)
    assert out["A"].decided_count == 5
    assert out["A"].overturned_count == 1
    assert out["A"].overturn_rate_pct == Decimal("20.0")
    assert out["A"].insufficient_data is False


def test_compute_approver_outcomes_skips_rows_without_approver():
    rows = [_arow(None, "i0", voided=True), _arow("A", "i1")]
    out = compute_approver_outcomes(rows, min_sample=1)
    assert set(out) == {"A"}  # the NULL-approver row produced no bucket
    assert out["A"].overturned_count == 0


def test_compute_approver_outcomes_insufficient_below_min_sample():
    rows = [_arow("A", "i0", voided=True), _arow("A", "i1")]
    out = compute_approver_outcomes(rows, min_sample=5)
    assert out["A"].insufficient_data is True


def test_routing_downweights_frequently_overturned_approver():
    # A and B are identical on the forward score (same speed/rate/sample). B's
    # decisions are overturned 40% of the time → B is penalised below A.
    pats = [
        _pat("A", approved=20, rejected=0, median_ttd="1.0"),
        _pat("B", approved=20, rejected=0, median_ttd="1.0"),
    ]
    eligible = [EligibleApprover("A", "Ann"), EligibleApprover("B", "Bob")]
    outcomes = {
        "A": _aoutcome("A", decided=20, overturned=0),
        "B": _aoutcome("B", decided=20, overturned=8),  # 40% > cap → full penalty
    }
    res = recommend_approvers(eligible, pats, outcomes=outcomes, vendor_id="V1")
    a = next(c for c in res.candidates if c.approver_id == "A")
    b = next(c for c in res.candidates if c.approver_id == "B")
    # Same base score; B carries the penalty and ranks below A.
    assert a.base_score == b.base_score
    assert a.outcome_penalty == Decimal("0.0")
    assert b.outcome_penalty > Decimal("0")
    assert b.score < a.score
    assert res.candidates[0].approver_id == "A"
    # Explained on the candidate + reasons.
    assert b.overturn_rate_pct == Decimal("40.0")
    assert b.overturned_count == 8
    assert any("overturned" in r for r in b.reasons)


def test_routing_penalty_is_banded_and_bounded():
    # The penalty maxes out at _W_OUTCOME_PENALTY (30) at/above the cap rate (25%).
    pats = [_pat("A", approved=20, rejected=0, median_ttd="1.0")]
    # 60% overturn — well above the 25% cap; penalty must clamp to 30, not 72.
    outcomes = {"A": _aoutcome("A", decided=20, overturned=12)}
    res = recommend_approvers([EligibleApprover("A", "Ann")], pats, outcomes=outcomes)
    cand = res.candidates[0]
    assert cand.outcome_penalty == Decimal("30.0")
    # Bounded: never hard-zeros — a strong forward score survives the full penalty.
    assert cand.score > Decimal("0")
    assert cand.score == max(Decimal("0"), cand.base_score - Decimal("30.0"))


def test_routing_thin_outcome_evidence_no_penalty():
    # Below the min-sample the API passes insufficient_data=True → no penalty,
    # even at a high overturn rate (one bad call must not sink an approver).
    pats = [_pat("A", approved=20, rejected=0, median_ttd="1.0")]
    outcomes = {"A": _aoutcome("A", decided=2, overturned=1, insufficient=True)}  # 50% but thin
    res = recommend_approvers([EligibleApprover("A", "Ann")], pats, outcomes=outcomes)
    cand = res.candidates[0]
    assert cand.outcome_penalty == Decimal("0.0")
    assert cand.score == cand.base_score
    # The rate is still surfaced (explainability) even though it didn't penalise.
    assert cand.overturn_rate_pct == Decimal("50.0")


def test_routing_no_outcomes_arg_is_backwards_compatible():
    # Omitting `outcomes` (the round-2 call shape) penalises nobody.
    pats = [_pat("A", approved=20, rejected=0, median_ttd="1.0")]
    res = recommend_approvers([EligibleApprover("A", "Ann")], pats)
    cand = res.candidates[0]
    assert cand.outcome_penalty == Decimal("0.0")
    assert cand.score == cand.base_score
    assert cand.overturned_count == 0


def test_routing_clean_peer_outranks_overturned_despite_familiarity():
    # The down-weight is strong enough that a heavily-overturned, vendor-familiar
    # approver loses to a clean peer — the router stops recommending decisions
    # that don't hold up even when the approver "knows" the vendor.
    pats = [
        _pat("A", approved=20, rejected=0, median_ttd="1.0"),  # clean, unfamiliar
        _pat("B", approved=20, rejected=0, median_ttd="1.0"),  # familiar but overturned
    ]
    eligible = [
        EligibleApprover("A", "Ann", vendor_approved_count=0),
        EligibleApprover("B", "Bob", vendor_approved_count=5),
    ]
    outcomes = {
        "A": _aoutcome("A", decided=20, overturned=0),
        "B": _aoutcome("B", decided=20, overturned=10),  # 50% → full 30-pt penalty
    }
    res = recommend_approvers(eligible, pats, outcomes=outcomes, vendor_id="V1")
    # B's +20 familiarity edge is outweighed by the −30 overturn penalty.
    assert res.candidates[0].approver_id == "A"


# ---------------------------------------------------------------------------
# Real-DB / API tests (realdb fixture)
# ---------------------------------------------------------------------------


async def _seed_approved_vendor(
    mk,
    org_id,
    actor_id,
    *,
    vendor_name="Acme Cleaning",
    amounts=None,
    unmodified=True,
    number_prefix="INV",
):
    """Create N approved invoices for one vendor with matching approval audit
    rows + ready_for_review clock-start rows. Returns the vendor_id."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.vendor import Vendor
    from app.models.workflow import AuditLog

    amounts = amounts or [Decimal("4800")] * 24
    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name=vendor_name, status="active")
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)
        base = datetime.now(UTC) - timedelta(days=30)
        for i, amt in enumerate(amounts):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"{number_prefix}-{i}",
                vendor_name=vendor_name,
                vendor_id=vendor.id,
                amount=amt,
                status=InvoiceStatus.approved,
            )
            s.add(inv)
            await s.commit()
            await s.refresh(inv)
            ready_at = base + timedelta(days=i)
            approved_at = ready_at + timedelta(days=1)
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=None,
                    action="invoice.status_changed",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details={"new_status": "ready_for_review"},
                    created_at=ready_at,
                )
            )
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details=None
                    if unmodified
                    else {"changes": {"amount": {"old": "1", "new": "2"}}},
                    created_at=approved_at,
                )
            )
        await s.commit()
        return vendor.id


async def _seed_in_review_invoice(mk, org_id, *, vendor_id, vendor_name, amount, number="INV-IR"):
    from app.models.invoice import Invoice, InvoiceStatus

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name=vendor_name,
            vendor_id=vendor_id,
            amount=Decimal(amount),
            status=InvoiceStatus.ready_for_review,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return inv.id


async def test_approval_patterns_endpoint(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get("/api/adaptive/approval-patterns")
    assert r.status_code == 200
    data = r.json()
    vrow = next(v for v in data["vendors"] if v["vendor_name"] == "Acme Cleaning")
    assert vrow["approved_count"] == 24
    assert vrow["consistency_pct"] == "100.0"
    # Amounts serialise as strings (string-Decimal convention).
    assert isinstance(vrow["avg_approved_amount"], str)


async def test_anomaly_endpoint_flags_out_of_range_with_baseline(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    amounts = [Decimal(str(4800 + (i % 5 - 2) * 200)) for i in range(24)]
    vid = await _seed_approved_vendor(mk, org_id, actor_id, amounts=amounts)
    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Acme Cleaning", amount="19500"
    )

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/adaptive/anomalies?invoice_id={inv_id}")
    assert r.status_code == 200
    data = r.json()
    assert "amount_high" in [f["code"] for f in data["flags"]]
    assert data["baseline"] is not None  # explainability contract
    assert data["baseline"]["sample_size"] == 24


async def test_anomaly_endpoint_insufficient_history(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_approved_vendor(
        mk, org_id, actor_id, amounts=[Decimal("4800")] * 3, vendor_name="Thin Vendor"
    )
    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Thin Vendor", amount="9999"
    )
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/adaptive/anomalies?invoice_id={inv_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["insufficient_history"] is True
    assert data["flags"] == []
    assert data["baseline"] is None


async def test_suggestions_persist_and_dismiss(realdb):
    from sqlalchemy import select

    from app.models.adaptive_suggestion import WorkflowSuggestion

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get("/api/adaptive/suggestions")
        assert r.status_code == 200
        suggestions = r.json()["suggestions"]
        assert len(suggestions) == 1
        sid = suggestions[0]["id"]

        # Persisted row exists.
        async with mk() as s:
            row = (
                await s.execute(
                    select(WorkflowSuggestion).where(WorkflowSuggestion.id == uuid.UUID(sid))
                )
            ).scalar_one()
            assert row.status == "open"

        d = await client.post(f"/api/adaptive/suggestions/{sid}/dismiss")
        assert d.status_code == 200
        assert d.json()["suggestions"][0]["status"] == "dismissed"

        # Re-GET re-runs derivation but the dismissal is durable.
        r2 = await client.get("/api/adaptive/suggestions?status=all")
        statuses = {s_["id"]: s_["status"] for s_ in r2.json()["suggestions"]}
        assert statuses[sid] == "dismissed"


async def test_suggestion_goes_stale(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get("/api/adaptive/suggestions")
        assert len(r.json()["suggestions"]) == 1

        # Add a rejection so the condition (0 rejections) no longer holds.
        from app.models.invoice import Invoice, InvoiceStatus
        from app.models.workflow import AuditLog

        async with mk() as s:
            inv = Invoice(
                organization_id=org_id,
                invoice_number="INV-REJ",
                vendor_name="Acme Cleaning",
                vendor_id=vid,
                amount=Decimal("100"),
                status=InvoiceStatus.rejected,
            )
            s.add(inv)
            await s.commit()
            await s.refresh(inv)
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.rejected",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details={"reason": "no"},
                    created_at=datetime.now(UTC),
                )
            )
            await s.commit()

        r2 = await client.get("/api/adaptive/suggestions?status=all")
        statuses = [s_["status"] for s_ in r2.json()["suggestions"]]
        assert "stale" in statuses


async def test_auth_required(realdb):
    async with realdb.client(key="a", role=None) as client:
        for path, method in (
            ("/api/adaptive/approval-patterns", "get"),
            ("/api/adaptive/anomalies", "get"),
            ("/api/adaptive/suggestions", "get"),
            (f"/api/adaptive/suggestions/{uuid.uuid4()}/dismiss", "post"),
        ):
            resp = await getattr(client, method)(path)
            assert resp.status_code == 401, path


async def test_rbac_dismiss_forbidden_for_clerk(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)

    async with realdb.client(key="a", role="ap_manager") as client:
        sid = (await client.get("/api/adaptive/suggestions")).json()["suggestions"][0]["id"]

    # Clerk excluded from read routes (manager/CFO surface).
    async with realdb.client(key="a", role="ap_clerk") as clerk:
        assert (await clerk.get("/api/adaptive/approval-patterns")).status_code == 403
        assert (await clerk.post(f"/api/adaptive/suggestions/{sid}/dismiss")).status_code == 403

    async with realdb.client(key="a", role="admin") as admin:
        assert (await admin.post(f"/api/adaptive/suggestions/{sid}/dismiss")).status_code == 200


async def test_suggestions_scoped_to_organization(realdb):
    """A WorkflowSuggestion row belonging to a different organization_id in the
    same tenant DB must not be marked stale by org A's recompute, nor returned
    in org A's listing. Pins the org-scope on the stale-sweep + listing queries
    inside suggestions() (DB-per-tenant already blocks cross-tenant leakage; the
    organization_id scope guards a stray row within one physical tenant DB)."""
    from sqlalchemy import select

    from app.models.adaptive_suggestion import WorkflowSuggestion

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)

    # Plant a foreign-org suggestion row directly in tenant A's DB.
    other_org = uuid.uuid4()
    foreign_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            WorkflowSuggestion(
                id=foreign_id,
                organization_id=other_org,
                kind="auto_approve_threshold",
                dedupe_key="auto_approve_threshold:FOREIGN",
                vendor_name="Foreign Vendor",
                title="Foreign org suggestion",
                rationale="should never surface to org A",
                payload={"vendor_id": None},
                confidence_pct=Decimal("90"),
                status="open",
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as client:
        # Recompute (write-on-GET) — must not touch the foreign row.
        listed = (await client.get("/api/adaptive/suggestions?status=all")).json()["suggestions"]
        assert all(s_["id"] != str(foreign_id) for s_ in listed)

    # The foreign row is untouched — still "open", not flipped to "stale".
    async with mk() as s:
        row = (
            await s.execute(select(WorkflowSuggestion).where(WorkflowSuggestion.id == foreign_id))
        ).scalar_one()
        assert row.status == "open"


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_suggestions_scoped_to_entity(realdb):
    """A WorkflowSuggestion row belonging to a DIFFERENT entity within the
    SAME org must not be marked stale by another entity's recompute, nor
    returned in that entity's listing (issue #144). The stale-marking query
    and the read-back both used to filter only on organization_id, so
    selecting entity A's (empty) view flipped entity B's still-valid open
    suggestion to stale (a cross-entity write) and could return entity B's
    rows in entity A's response (a cross-entity read leak)."""
    from sqlalchemy import select

    from app.models.adaptive_suggestion import WorkflowSuggestion

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with realdb.client(key="a", role="admin") as client:
        entity_a = (
            await client.post("/api/entities", json={"name": "Entity A", "slug": "entity-a"})
        ).json()["id"]
        entity_b = (
            await client.post("/api/entities", json={"name": "Entity B", "slug": "entity-b"})
        ).json()["id"]

        # Plant an open suggestion under entity B directly.
        foreign_id = uuid.uuid4()
        async with mk() as s:
            s.add(
                WorkflowSuggestion(
                    id=foreign_id,
                    organization_id=org_id,
                    entity_id=uuid.UUID(entity_b),
                    kind="auto_approve_threshold",
                    dedupe_key="auto_approve_threshold:ENTITY_B",
                    vendor_name="Entity B Vendor",
                    title="Entity B suggestion",
                    rationale="should never surface to entity A, nor be staled by it",
                    payload={"vendor_id": None},
                    confidence_pct=Decimal("90"),
                    status="open",
                )
            )
            await s.commit()

        # Recompute (write-on-GET) scoped to entity A — entity A has zero
        # decision history, so every fresh_key is empty; a correctly-scoped
        # sweep touches nothing under entity A and returns nothing.
        listed = (
            await client.get(
                "/api/adaptive/suggestions?status=all", headers={"X-Entity-ID": entity_a}
            )
        ).json()["suggestions"]
        assert all(s_["id"] != str(foreign_id) for s_ in listed)

    # Entity B's row is untouched — still "open", not flipped to "stale".
    async with mk() as s:
        row = (
            await s.execute(select(WorkflowSuggestion).where(WorkflowSuggestion.id == foreign_id))
        ).scalar_one()
        assert row.status == "open"

    # And it IS visible when actually scoped to entity B.
    async with realdb.client(key="a", role="admin") as client:
        listed_b = (
            await client.get(
                "/api/adaptive/suggestions?status=all", headers={"X-Entity-ID": entity_b}
            )
        ).json()["suggestions"]
        assert any(s_["id"] == str(foreign_id) for s_ in listed_b)


async def test_routing_suggestion_endpoint(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    # ap_manager approves 24 of this vendor's invoices → history + familiarity.
    vid = await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)
    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Acme Cleaning", amount="5000"
    )

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/adaptive/routing-suggestion?invoice_id={inv_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["insufficient_history"] is False
    assert data["invoice_id"] == str(inv_id)
    # The approving manager appears, with vendor familiarity, ranked first.
    top = data["candidates"][0]
    assert top["approver_id"] == str(actor_id)
    assert top["rank"] == 1
    assert top["vendor_approved_count"] == 24
    # Scores serialise as string-Decimal (no wire floats).
    assert isinstance(top["score"], str)
    # ap_clerk is NOT an eligible approver → never a candidate.
    clerk_id = str(realdb.info("a").users["ap_clerk"])
    assert all(c["approver_id"] != clerk_id for c in data["candidates"])


async def _seed_decisions_for_vendor(mk, org_id, actor_id, *, vendor_id, n, prefix):
    """Approve ``n`` invoices of an existing ``vendor_id`` by ``actor_id`` (clean,
    no overturns) — gives the actor identical vendor familiarity + decision
    history as ``_seed_approved_vendor`` but against a vendor that already exists,
    so two approvers can share one vendor_id."""
    from sqlalchemy import select

    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.vendor import Vendor
    from app.models.workflow import AuditLog

    async with mk() as s:
        vname = (await s.execute(select(Vendor.name).where(Vendor.id == vendor_id))).scalar()
        base = datetime.now(UTC) - timedelta(days=40)
        for i in range(n):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"{prefix}-{i}",
                vendor_name=vname,
                vendor_id=vendor_id,
                amount=Decimal("4800"),
                status=InvoiceStatus.approved,
            )
            s.add(inv)
            await s.commit()
            await s.refresh(inv)
            ready_at = base + timedelta(days=i)
            approved_at = ready_at + timedelta(days=1)
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=None,
                    action="invoice.status_changed",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details={"new_status": "ready_for_review"},
                    created_at=ready_at,
                )
            )
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details=None,
                    created_at=approved_at,
                )
            )
        await s.commit()


async def _seed_overturns_for_approver(mk, org_id, actor_id, *, n_decided, n_overturned):
    """Seed ``n_decided`` invoices each approved by ``actor_id``, of which
    ``n_overturned`` are later overturned by a DIFFERENT actor (a void). Returns
    the list of invoice ids. The approvals give the actor decision history; the
    overturn rows feed the routing down-weight."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.workflow import AuditLog

    other_actor = uuid.uuid4()
    base = datetime.now(UTC) - timedelta(days=20)
    ids = []
    async with mk() as s:
        for i in range(n_decided):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"OT-{actor_id.hex[:6]}-{i}",
                vendor_name="Overturn Co",
                amount=Decimal("1000"),
                status=InvoiceStatus.approved,
            )
            s.add(inv)
            await s.commit()
            await s.refresh(inv)
            ids.append(inv.id)
            ready_at = base + timedelta(days=i)
            approved_at = ready_at + timedelta(hours=1)
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=None,
                    action="invoice.status_changed",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details={"new_status": "ready_for_review"},
                    created_at=ready_at,
                )
            )
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details=None,
                    created_at=approved_at,
                )
            )
            if i < n_overturned:
                s.add(
                    AuditLog(
                        organization_id=org_id,
                        actor_id=other_actor,  # a DIFFERENT actor walks it back
                        action="invoice.voided_return_to_approved",
                        entity_type="invoice",
                        entity_id=inv.id,
                        details=None,
                        created_at=approved_at + timedelta(days=1),
                    )
                )
        await s.commit()
    return ids


async def test_routing_endpoint_downweights_overturned_approver(realdb):
    """End-to-end: an approver whose decisions are frequently overturned is
    down-weighted below a clean peer, and the overturn evidence surfaces in the
    response (the feedback loop folded into the router)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    clean_id = realdb.info("a").users["ap_manager"]
    dirty_id = realdb.info("a").users["admin"]

    # Both approvers build identical clean approval history on the SAME vendor
    # (same vendor_id → same familiarity, same speed/rate/experience), so the
    # forward score is identical and ONLY the overturn signal can separate them.
    vid = await _seed_approved_vendor(
        mk, org_id, clean_id, amounts=[Decimal("4800")] * 10, number_prefix="CLEAN"
    )
    await _seed_decisions_for_vendor(mk, org_id, dirty_id, vendor_id=vid, n=10, prefix="DIRTY")
    # …but the "dirty" approver also has 6/10 of a SECOND batch of decisions
    # later voided (60% overturn → full penalty); the clean approver has none.
    await _seed_overturns_for_approver(mk, org_id, dirty_id, n_decided=10, n_overturned=6)
    await _seed_overturns_for_approver(mk, org_id, clean_id, n_decided=10, n_overturned=0)

    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Acme Cleaning", amount="5000"
    )

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/adaptive/routing-suggestion?invoice_id={inv_id}")
    assert r.status_code == 200
    cands = {c["approver_id"]: c for c in r.json()["candidates"]}
    clean = cands[str(clean_id)]
    dirty = cands[str(dirty_id)]

    # The dirty approver carries a penalty; the clean one doesn't. The overturn
    # rate is over the approver's WHOLE decision set in the window (10 vendor
    # approvals + 10 overturn-batch = 20 decided, 6 overturned = 30%), which is
    # above the 25% cap → the full 30-point penalty applies.
    assert dirty["outcome_penalty"] == "30.0"
    assert dirty["overturned_count"] == 6
    assert dirty["outcome_sample_size"] == 20
    assert dirty["overturn_rate_pct"] == "30.0"
    assert clean["outcome_penalty"] == "0.0"
    # Down-weighted strictly below the clean peer's final score.
    assert Decimal(dirty["score"]) < Decimal(clean["score"])
    expected = Decimal(dirty["base_score"]) - Decimal(dirty["outcome_penalty"])
    assert Decimal(dirty["score"]) == expected
    # Explained in the reasons.
    assert any("overturned" in reason for reason in dirty["reasons"])


async def test_routing_endpoint_no_overturns_no_penalty(realdb):
    """An approver with a spotless outcome record carries no penalty — the
    feedback down-weight only bites when decisions are actually walked back."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    vid = await _seed_approved_vendor(
        mk, org_id, actor_id, amounts=[Decimal("4800")] * 24, number_prefix="NOOT"
    )
    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Acme Cleaning", amount="5000"
    )
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/adaptive/routing-suggestion?invoice_id={inv_id}")
    assert r.status_code == 200
    cand = next(c for c in r.json()["candidates"] if c["approver_id"] == str(actor_id))
    # No overturn rows for this approver → zero penalty, score == base_score, and
    # the explainability fields read clean.
    assert cand["outcome_penalty"] == "0.0"
    assert cand["overturned_count"] == 0
    assert cand["overturn_rate_pct"] == "0.0"
    assert cand["score"] == cand["base_score"]


async def test_routing_endpoint_self_correction_not_an_overturn(realdb):
    """A correction the approver made on their OWN approval is not an overturn of
    themselves; only a later correction by a DIFFERENT actor counts."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    other_actor = realdb.info("a").users["admin"]

    vid = await _seed_approved_vendor(
        mk, org_id, actor_id, amounts=[Decimal("4800")] * 24, number_prefix="SELFC"
    )
    # 6 decided invoices the approver approved; on 3 the approver's OWN approval
    # carried corrections (NOT an overturn), on 2 a DIFFERENT actor later
    # re-approved with corrections (a real overturn).
    base = datetime.now(UTC) - timedelta(days=15)
    async with mk() as s:
        for i in range(6):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"CORR-{i}",
                vendor_name="Corr Co",
                amount=Decimal("1000"),
                status=InvoiceStatus.approved,
            )
            s.add(inv)
            await s.commit()
            await s.refresh(inv)
            ready_at = base + timedelta(days=i)
            approved_at = ready_at + timedelta(hours=1)
            self_changes = {"changes": {"amount": {"old": "1", "new": "2"}}} if i < 3 else None
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=None,
                    action="invoice.status_changed",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details={"new_status": "ready_for_review"},
                    created_at=ready_at,
                )
            )
            # The approver's own approval (sometimes with self-corrections).
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details=self_changes,
                    created_at=approved_at,
                )
            )
            # On 2 invoices a DIFFERENT actor later re-approves WITH corrections.
            if i >= 4:
                s.add(
                    AuditLog(
                        organization_id=org_id,
                        actor_id=other_actor,
                        action="invoice.approved",
                        entity_type="invoice",
                        entity_id=inv.id,
                        details={"changes": {"gl": {"old": "x", "new": "y"}}},
                        created_at=approved_at + timedelta(days=1),
                    )
                )
        await s.commit()

    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Acme Cleaning", amount="5000"
    )
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/adaptive/routing-suggestion?invoice_id={inv_id}")
    assert r.status_code == 200
    cand = next(c for c in r.json()["candidates"] if c["approver_id"] == str(actor_id))
    # 24 Acme approvals + 6 Corr Co approvals = 30 decided. Only the 2 corrected
    # by ANOTHER actor are overturns; the 3 self-corrections are NOT counted.
    assert cand["outcome_sample_size"] == 30
    assert cand["overturned_count"] == 2


async def test_routing_suggestion_404_outside_tenant(realdb):
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/adaptive/routing-suggestion?invoice_id={uuid.uuid4()}")
    assert r.status_code == 404


async def test_routing_suggestion_rbac_and_auth(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)
    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Acme Cleaning", amount="5000"
    )
    path = f"/api/adaptive/routing-suggestion?invoice_id={inv_id}"

    # Unauthenticated → 401.
    async with realdb.client(key="a", role=None) as anon:
        assert (await anon.get(path)).status_code == 401
    # Clerk excluded from this manager/CFO read surface → 403.
    async with realdb.client(key="a", role="ap_clerk") as clerk:
        assert (await clerk.get(path)).status_code == 403
    # CFO allowed.
    async with realdb.client(key="a", role="cfo") as cfo:
        assert (await cfo.get(path)).status_code == 200


async def _seed_review_instance(mk, org_id, invoice_id):
    """Give an in-review invoice a WorkflowInstance with an open `review` step —
    the state a real `ready_for_review` invoice always has. `assign_reviewer`
    needs the instance to assign the step AND to reach its audit-write tail (it
    returns early, before the audit dispatch, when there is no instance)."""
    from app.models.workflow import WorkflowDefinition, WorkflowInstance, WorkflowStep

    async with mk() as s:
        defn = WorkflowDefinition(
            organization_id=org_id,
            name="Default",
            steps_config={"steps": [{"type": "review"}]},
            is_active=True,
            is_default=True,
        )
        s.add(defn)
        await s.commit()
        await s.refresh(defn)
        inst = WorkflowInstance(
            definition_id=defn.id,
            invoice_id=invoice_id,
            current_step=1,
            state="active",
            steps_config_snapshot={"steps": [{"type": "review"}]},
        )
        s.add(inst)
        await s.commit()
        await s.refresh(inst)
        s.add(
            WorkflowStep(
                instance_id=inst.id,
                step_number=1,
                step_type="review",
            )
        )
        await s.commit()


async def test_routing_apply_assigns_top_and_writes_audit(realdb):
    """The apply path assigns the top-ranked approver via the audited
    assign_reviewer service — the invoice's assigned_to_id is set AND an
    invoice.assigned_for_review audit row lands (the manual-assign path's row)."""
    from sqlalchemy import select

    from app.models.invoice import Invoice
    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    # ap_manager has the full approval history + vendor familiarity → ranks #1.
    vid = await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)
    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Acme Cleaning", amount="5000"
    )
    await _seed_review_instance(mk, org_id, inv_id)

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(
            "/api/adaptive/routing-suggestion/apply", json={"invoice_id": str(inv_id)}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assigned"] is True
    assert body["assigned_to_id"] == str(actor_id)
    assert body["rank"] == 1
    assert isinstance(body["score"], str)

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.assigned_to_id == actor_id
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == inv_id,
                        AuditLog.action == "invoice.assigned_for_review",
                    )
                )
            )
            .scalars()
            .all()
        )
        # Exactly one audit row written by the audited assign path.
        assert len(rows) == 1
        assert rows[0].details["reviewer_id"] == str(actor_id)


async def test_routing_apply_idempotent_when_already_assigned(realdb):
    """Re-applying when the invoice is already assigned to the chosen top
    approver is a no-op: assigned=false and no second audit row."""
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)
    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Acme Cleaning", amount="5000"
    )
    await _seed_review_instance(mk, org_id, inv_id)
    path = "/api/adaptive/routing-suggestion/apply"

    async with realdb.client(key="a", role="ap_manager") as client:
        first = await client.post(path, json={"invoice_id": str(inv_id)})
        assert first.status_code == 200
        assert first.json()["assigned"] is True

        second = await client.post(path, json={"invoice_id": str(inv_id)})
        assert second.status_code == 200
        assert second.json()["assigned"] is False
        assert second.json()["assigned_to_id"] == str(actor_id)

    # Still exactly one assignment audit row after the second call.
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == inv_id,
                        AuditLog.action == "invoice.assigned_for_review",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_routing_apply_422_when_no_eligible_history(realdb):
    """No eligible approver has any history to rank on → 422 (caller falls back
    to manual assignment); nothing is assigned."""
    from sqlalchemy import select

    from app.models.invoice import Invoice
    from app.models.vendor import Vendor

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    # A vendor + in-review invoice with ZERO approval history anywhere → all
    # eligible approvers score on familiarity only (also 0) → insufficient_history.
    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="Fresh Vendor", status="active")
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)
        vid = vendor.id
    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Fresh Vendor", amount="500", number="INV-FRESH"
    )

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(
            "/api/adaptive/routing-suggestion/apply", json={"invoice_id": str(inv_id)}
        )
    assert r.status_code == 422

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.assigned_to_id is None


async def test_routing_apply_409_when_not_ready_for_review(realdb):
    """The apply path requires ready_for_review — same precondition + 409 as the
    manual assign endpoint."""
    from app.models.invoice import Invoice, InvoiceStatus

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number="INV-APPROVED",
            vendor_name="Acme Cleaning",
            vendor_id=vid,
            amount=Decimal("5000"),
            status=InvoiceStatus.approved,  # NOT ready_for_review
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        inv_id = inv.id

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(
            "/api/adaptive/routing-suggestion/apply", json={"invoice_id": str(inv_id)}
        )
    assert r.status_code == 409


async def test_routing_apply_404_outside_tenant(realdb):
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(
            "/api/adaptive/routing-suggestion/apply", json={"invoice_id": str(uuid.uuid4())}
        )
    assert r.status_code == 404


async def test_routing_apply_rbac_and_auth(realdb):
    """Apply is a write — admin/ap_manager only (matches the manual assign
    endpoint). Clerk AND cfo are denied; anon is 401."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_approved_vendor(mk, org_id, actor_id, amounts=[Decimal("4800")] * 24)
    inv_id = await _seed_in_review_invoice(
        mk, org_id, vendor_id=vid, vendor_name="Acme Cleaning", amount="5000"
    )
    path = "/api/adaptive/routing-suggestion/apply"
    payload = {"invoice_id": str(inv_id)}

    async with realdb.client(key="a", role=None) as anon:
        assert (await anon.post(path, json=payload)).status_code == 401
    async with realdb.client(key="a", role="ap_clerk") as clerk:
        assert (await clerk.post(path, json=payload)).status_code == 403
    # CFO can READ the recommendation but cannot APPLY it (write surface).
    async with realdb.client(key="a", role="cfo") as cfo:
        assert (await cfo.post(path, json=payload)).status_code == 403
    # admin allowed.
    async with realdb.client(key="a", role="admin") as admin:
        assert (await admin.post(path, json=payload)).status_code == 200


async def test_tenant_isolation(realdb):
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    actor_a = realdb.info("a").users["ap_manager"]
    await _seed_approved_vendor(mk_a, org_a, actor_a, amounts=[Decimal("4800")] * 24)

    # Tenant B sees none of A's approvals.
    async with realdb.client(key="b", role="ap_manager") as client_b:
        data = (await client_b.get("/api/adaptive/approval-patterns")).json()
        assert all(v["vendor_name"] != "Acme Cleaning" for v in data["vendors"])
        suggestions = (await client_b.get("/api/adaptive/suggestions")).json()["suggestions"]
        assert suggestions == []


# ---------------------------------------------------------------------------
# Auto-approve threshold — recommend (GET) + apply (POST) real-DB tests
# ---------------------------------------------------------------------------


async def _set_adaptive_min_history(realdb, org_id, n):
    """Lower the org's suggestion_min_history so the threshold tests seed a small
    number of clean invoices (keeps the realdb connection churn down)."""
    from sqlalchemy import select

    from app.models.organization import Organization

    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        settings["adaptive"] = {**settings.get("adaptive", {}), "suggestion_min_history": n}
        org.settings = settings
        await s.commit()


async def _seed_active_workflow(mk, org_id, *, auto_approve_below=None):
    """An active workflow definition with an approval step. Returns its id."""
    from app.models.workflow import WorkflowDefinition

    approval_config: dict = {
        "required": True,
        "approver_strategy": "manual",
        "require_segregation": True,
    }
    if auto_approve_below is not None:
        approval_config["auto_approve_below"] = auto_approve_below
    wf_id = uuid.uuid4()
    async with mk() as s:
        defn = WorkflowDefinition(
            id=wf_id,
            organization_id=org_id,
            name="Default",
            steps_config={
                "steps": [
                    {"number": 1, "type": "extraction", "name": "Extract", "config": {}},
                    {
                        "number": 2,
                        "type": "approval",
                        "name": "Manager Approval",
                        "config": approval_config,
                    },
                ]
            },
            is_active=True,
            is_default=True,
        )
        s.add(defn)
        await s.commit()
        return wf_id


async def _seed_clean_vendors(mk, org_id, actor_id, specs):
    """Seed several spotless-history vendors in a SINGLE session/commit.

    specs = [(vendor_name, amount, n)]. One bulk commit keeps the realdb
    connection churn low (the per-invoice-commit `_seed_approved_vendor` pattern,
    multiplied across vendors, trips the documented teardown flake). Each invoice
    gets the matching `ready_for_review` clock-start + `invoice.approved` audit
    rows the decision-row query reads."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.vendor import Vendor
    from app.models.workflow import AuditLog

    base = datetime.now(UTC) - timedelta(days=60)
    async with mk() as s:
        n_seq = 0
        for name, amount, n in specs:
            vendor = Vendor(organization_id=org_id, name=name, status="active")
            s.add(vendor)
            await s.flush()
            for i in range(n):
                inv = Invoice(
                    organization_id=org_id,
                    invoice_number=f"CLEAN-{n_seq}",
                    vendor_name=name,
                    vendor_id=vendor.id,
                    amount=Decimal(str(amount)),
                    status=InvoiceStatus.approved,
                )
                s.add(inv)
                await s.flush()
                ready_at = base + timedelta(hours=n_seq)
                s.add(
                    AuditLog(
                        organization_id=org_id,
                        actor_id=None,
                        action="invoice.status_changed",
                        entity_type="invoice",
                        entity_id=inv.id,
                        details={"new_status": "ready_for_review"},
                        created_at=ready_at,
                    )
                )
                s.add(
                    AuditLog(
                        organization_id=org_id,
                        actor_id=actor_id,
                        action="invoice.approved",
                        entity_type="invoice",
                        entity_id=inv.id,
                        details=None,  # unmodified → clean history
                        created_at=ready_at + timedelta(hours=1),
                    )
                )
                n_seq += 1
        await s.commit()


async def test_threshold_recommendation_endpoint(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    wf_id = await _seed_active_workflow(mk, org_id)
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )

    async with realdb.client(key="a", role="cfo") as client:  # read role
        r = await client.get("/api/adaptive/threshold-recommendation")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["should_raise"] is True
    assert data["reason_code"] == "ok"
    assert data["recommended_threshold"] == "5000.00"
    assert data["current_threshold"] == "0.00"  # quantized by the recommendation
    assert data["qualifying_vendor_count"] == 3
    assert data["workflow_id"] == str(wf_id)
    assert isinstance(data["evidence"], list) and len(data["evidence"]) == 3


async def test_threshold_apply_writes_through_audited_patch_path(realdb):
    """The apply path raises auto_approve_below on the active definition AND
    lands a WorkflowVersion snapshot + audit rows — exactly the manual PATCH
    path's side effects (the threshold change is versioned + auditable)."""
    from sqlalchemy import select

    from app.models.workflow import AuditLog, WorkflowDefinition, WorkflowVersion

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    wf_id = await _seed_active_workflow(mk, org_id)
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )

    async with realdb.client(key="a", role="admin") as client:
        r = await client.post("/api/adaptive/threshold-recommendation/apply", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True
    assert body["previous_threshold"] == "0"
    assert body["new_threshold"] == "5000.00"
    assert body["version_number"] is not None

    async with mk() as s:
        # The threshold is written onto the approval step.
        defn = (
            await s.execute(select(WorkflowDefinition).where(WorkflowDefinition.id == wf_id))
        ).scalar_one()
        approval = next(st for st in defn.steps_config["steps"] if st["type"] == "approval")
        # An exact decimal STRING, not a float — money never round-trips through
        # a binary float, and it is what the manual PATCH path writes too
        # (`WorkflowStepConfig.auto_approve_below` is a `Decimal`, dumped with
        # `mode="json"`). Matches the response's own `new_threshold`, so the two
        # halves of the same apply can no longer disagree about the type.
        assert approval["config"]["auto_approve_below"] == "5000.00"

        # A WorkflowVersion snapshot of the PRIOR config was written.
        versions = (
            (await s.execute(select(WorkflowVersion).where(WorkflowVersion.definition_id == wf_id)))
            .scalars()
            .all()
        )
        assert len(versions) == 1
        prior_approval = next(
            st for st in versions[0].steps_config["steps"] if st["type"] == "approval"
        )
        assert "auto_approve_below" not in prior_approval["config"]  # the prior (unset) state

        # The audited PATCH path's version_snapshot row + the threshold-specific
        # row both land against the definition.
        actions = {
            row.action
            for row in (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_type == "workflow_definition",
                        AuditLog.entity_id == wf_id,
                    )
                )
            )
            .scalars()
            .all()
        }
        assert "workflow.version_snapshot" in actions
        assert "workflow.auto_approve_threshold_raised" in actions


async def test_threshold_apply_noop_on_insufficient_evidence(realdb):
    """Too few clean vendors → applied=false, no version snapshot, no audit row."""
    from sqlalchemy import select

    from app.models.workflow import AuditLog, WorkflowVersion

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    wf_id = await _seed_active_workflow(mk, org_id)
    # Only 2 clean vendors (< min 3).
    await _seed_clean_vendors(mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3)])

    async with realdb.client(key="a", role="admin") as client:
        r = await client.post("/api/adaptive/threshold-recommendation/apply", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is False
    assert body["reason_code"] == "insufficient_evidence"
    assert body["new_threshold"] == body["previous_threshold"] == "0"

    async with mk() as s:
        versions = (
            (await s.execute(select(WorkflowVersion).where(WorkflowVersion.definition_id == wf_id)))
            .scalars()
            .all()
        )
        assert versions == []  # no snapshot for a non-change
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_type == "workflow_definition",
                        AuditLog.entity_id == wf_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


async def test_threshold_apply_idempotent(realdb):
    """A second apply after the threshold is already at the recommended level is
    a no-op (no_increase) — no extra version snapshot."""
    from sqlalchemy import select

    from app.models.workflow import WorkflowVersion

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    wf_id = await _seed_active_workflow(mk, org_id)
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )
    path = "/api/adaptive/threshold-recommendation/apply"

    async with realdb.client(key="a", role="admin") as client:
        first = await client.post(path, json={})
        assert first.json()["applied"] is True
        second = await client.post(path, json={})
        assert second.status_code == 200
        assert second.json()["applied"] is False
        assert second.json()["reason_code"] == "no_increase"

    async with mk() as s:
        versions = (
            (await s.execute(select(WorkflowVersion).where(WorkflowVersion.definition_id == wf_id)))
            .scalars()
            .all()
        )
        # Exactly one snapshot — the second (no-op) apply added none.
        assert len(versions) == 1


async def test_threshold_apply_stale_guard_409(realdb):
    """expected_recommended_threshold that no longer matches → 409, no write."""
    from sqlalchemy import select

    from app.models.workflow import WorkflowVersion

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    wf_id = await _seed_active_workflow(mk, org_id)
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )

    async with realdb.client(key="a", role="admin") as client:
        r = await client.post(
            "/api/adaptive/threshold-recommendation/apply",
            json={"expected_recommended_threshold": "9999.00"},
        )
    assert r.status_code == 409

    async with mk() as s:
        versions = (
            (await s.execute(select(WorkflowVersion).where(WorkflowVersion.definition_id == wf_id)))
            .scalars()
            .all()
        )
        assert versions == []  # nothing written on the stale-guard reject


async def test_threshold_apply_rbac_and_auth(realdb):
    """Apply is admin-only (matches workflow-definition edit). CFO/clerk read the
    recommendation but cannot apply it; anon is 401."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    await _seed_active_workflow(mk, org_id)
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )
    path = "/api/adaptive/threshold-recommendation/apply"

    async with realdb.client(key="a", role=None) as anon:
        assert (await anon.post(path, json={})).status_code == 401
    async with realdb.client(key="a", role="ap_clerk") as clerk:
        assert (await clerk.post(path, json={})).status_code == 403
    # ap_manager can edit reviewers but NOT workflow definitions → 403 on apply.
    async with realdb.client(key="a", role="ap_manager") as mgr:
        assert (await mgr.post(path, json={})).status_code == 403
    async with realdb.client(key="a", role="cfo") as cfo:
        # CFO reads the recommendation fine...
        assert (await cfo.get("/api/adaptive/threshold-recommendation")).status_code == 200
        # ...but cannot apply it.
        assert (await cfo.post(path, json={})).status_code == 403
    async with realdb.client(key="a", role="admin") as admin:
        assert (await admin.post(path, json={})).status_code == 200


async def test_threshold_apply_409_no_workflow(realdb):
    """Apply with no workflow definition at all → 409."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )
    async with realdb.client(key="a", role="admin") as client:
        r = await client.post("/api/adaptive/threshold-recommendation/apply", json={})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Feedback loop — outcome adjustment + effectiveness (pure-function tests)
# ---------------------------------------------------------------------------


def _outcome(inv_id, *, voided=False, corrected=False, rejected=False):
    return {
        "invoice_id": inv_id,
        "voided": voided,
        "corrected": corrected,
        "rejected": rejected,
    }


def _raise_rec(*, recommended="5500.00", current="0"):
    """A base ThresholdRecommendation that DOES raise — the input the outcome
    adjuster pulls back."""
    from app.services.adaptive_workflows import ThresholdRecommendation

    return ThresholdRecommendation(
        should_raise=True,
        current_threshold=Decimal(current),
        recommended_threshold=Decimal(recommended),
        cap_threshold=Decimal("25000.00"),
        qualifying_vendor_count=3,
        total_clean_invoices=36,
        evidence=[{"vendor_name": "Clean A"}],
        rationale="history supports the raise",
        reason_code="ok",
    )


def test_outcome_stats_counts_distinct_overturns():
    # 10 auto-approved invoices; 2 voided (one also rejected → counted once), 1
    # corrected. overturned = {v1, v2, c1} = 3.
    rows = [_outcome(f"i{i}") for i in range(7)]
    rows.append(_outcome("v1", voided=True))
    rows.append(_outcome("v2", voided=True, rejected=True))  # one invoice, two signals
    rows.append(_outcome("c1", corrected=True))
    stats = compute_outcome_stats(rows)
    assert stats.auto_approved_count == 10
    assert stats.voided_count == 2
    assert stats.rejected_count == 1
    assert stats.corrected_count == 1
    assert stats.overturned_count == 3  # distinct invoices, v2 counted once
    assert stats.overturn_rate_pct == Decimal("30.0")
    assert stats.insufficient_data is False


def test_outcome_stats_dedupes_repeated_invoice_rows():
    # The same invoice id appearing twice (defensive) is counted once.
    rows = [_outcome("dup", voided=True), _outcome("dup", voided=True)]
    stats = compute_outcome_stats(rows, min_sample=1)
    assert stats.auto_approved_count == 1
    assert stats.overturned_count == 1


def test_outcome_stats_insufficient_data_below_min_sample():
    rows = [_outcome("i0", voided=True), _outcome("i1")]
    stats = compute_outcome_stats(rows, min_sample=5)
    assert stats.insufficient_data is True
    # The rate is still computed, but the caller treats it as unmeasurable.
    assert stats.overturn_rate_pct == Decimal("50.0")


def test_outcome_adjustment_passes_through_when_outcomes_clean():
    # 0% overturn over a healthy sample → the raise stands.
    stats = compute_outcome_stats([_outcome(f"i{i}") for i in range(10)])
    base = _raise_rec()
    adj = outcome_adjusted_threshold(base, stats)
    assert adj is base  # unchanged passthrough
    assert adj.should_raise is True


def test_outcome_adjustment_pulls_back_on_elevated_overturns():
    # 8% overturn (between 5% pullback and 15% freeze) → refuse to raise.
    rows = [_outcome(f"i{i}") for i in range(25)]
    for i in range(2):  # 2/25 = 8%
        rows[i]["voided"] = True
    stats = compute_outcome_stats(rows)
    assert stats.overturn_rate_pct == Decimal("8.0")
    base = _raise_rec(recommended="5500.00", current="2000")
    adj = outcome_adjusted_threshold(base, stats)
    assert adj.should_raise is False
    assert adj.reason_code == "outcome_pullback"
    # Never lowers — holds at current, does not raise.
    assert adj.recommended_threshold == Decimal("2000")
    assert adj.current_threshold == Decimal("2000")
    assert "overturn rate" in adj.rationale


def test_outcome_adjustment_freezes_on_high_overturns():
    # 20% overturn (≥ 15% freeze) → freeze; stronger rationale, still no raise.
    rows = [_outcome(f"i{i}") for i in range(10)]
    for i in range(2):  # 2/10 = 20%
        rows[i]["rejected"] = True
    stats = compute_outcome_stats(rows)
    assert stats.overturn_rate_pct == Decimal("20.0")
    adj = outcome_adjusted_threshold(_raise_rec(), stats)
    assert adj.should_raise is False
    assert adj.reason_code == "outcome_freeze"
    assert "too often" in adj.rationale


def test_outcome_adjustment_no_pullback_on_thin_evidence():
    # Only 2 auto-approvals — even at 50% overturn the loop won't react (noise).
    rows = [_outcome("i0", voided=True), _outcome("i1")]
    stats = compute_outcome_stats(rows, min_sample=5)
    base = _raise_rec()
    adj = outcome_adjusted_threshold(base, stats)
    assert adj is base  # untouched — insufficient_data short-circuits


def test_outcome_adjustment_noop_when_base_already_not_raising():
    from app.services.adaptive_workflows import ThresholdRecommendation

    rows = [_outcome(f"i{i}") for i in range(10)]
    for i in range(3):
        rows[i]["voided"] = True
    stats = compute_outcome_stats(rows)  # 30% overturn
    base = ThresholdRecommendation(
        should_raise=False,
        current_threshold=Decimal("8000"),
        recommended_threshold=Decimal("8000"),
        cap_threshold=Decimal("25000"),
        qualifying_vendor_count=3,
        total_clean_invoices=36,
        evidence=[],
        rationale="no increase",
        reason_code="no_increase",
    )
    adj = outcome_adjusted_threshold(base, stats)
    assert adj is base  # already not raising — nothing to pull back


def test_effectiveness_overturn_metric_measured_and_insufficient():
    # Measured: ≥ min_sample auto-approvals → a real overturn figure.
    stats = compute_outcome_stats([_outcome(f"i{i}", voided=(i < 1)) for i in range(10)])
    metrics = compute_effectiveness(stats, applied_suggestion_count=0, total_suggestion_count=0)
    overturn = next(m for m in metrics if m.name == "auto_approval_overturn_rate")
    assert overturn.insufficient_data is False
    assert overturn.value_pct == Decimal("10.0")

    # Insufficient: too few auto-approvals → no fabricated number.
    thin = compute_outcome_stats([_outcome("i0", voided=True)], min_sample=5)
    metrics2 = compute_effectiveness(thin, applied_suggestion_count=0, total_suggestion_count=0)
    overturn2 = next(m for m in metrics2 if m.name == "auto_approval_overturn_rate")
    assert overturn2.insufficient_data is True
    assert overturn2.value_pct is None
    assert "Not yet measurable" in overturn2.label


def test_effectiveness_acceptance_metric():
    stats = compute_outcome_stats([_outcome(f"i{i}") for i in range(10)])
    # 3 of 10 suggestions applied → 30%.
    metrics = compute_effectiveness(stats, applied_suggestion_count=3, total_suggestion_count=10)
    acc = next(m for m in metrics if m.name == "recommendation_acceptance_rate")
    assert acc.insufficient_data is False
    assert acc.value_pct == Decimal("30.0")

    # No suggestions surfaced → insufficient, never a divide-by-zero / fake 0%.
    metrics2 = compute_effectiveness(stats, applied_suggestion_count=0, total_suggestion_count=0)
    acc2 = next(m for m in metrics2 if m.name == "recommendation_acceptance_rate")
    assert acc2.insufficient_data is True
    assert acc2.value_pct is None


# ---------------------------------------------------------------------------
# Feedback loop — real-DB endpoint tests
# ---------------------------------------------------------------------------


async def _seed_auto_approved(
    mk, org_id, *, count, voided_ids=(), rejected_ids=(), base_amount=4800
):
    """Seed `count` invoices each with an `invoice.auto_approved` audit row.
    Invoices whose index is in `voided_ids` get a later
    `invoice.voided_return_to_approved` row; `rejected_ids` get a later
    `invoice.rejected`. Returns the list of invoice ids (by index)."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.workflow import AuditLog

    base = datetime.now(UTC) - timedelta(days=20)
    ids = []
    async with mk() as s:
        for i in range(count):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"AUTO-{i}",
                vendor_name="Auto Vendor",
                amount=Decimal(str(base_amount)),
                status=InvoiceStatus.approved,
            )
            s.add(inv)
            await s.flush()
            ids.append(inv.id)
            auto_at = base + timedelta(hours=i)
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=None,
                    action="invoice.auto_approved",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details={"auto_approved": True},
                    created_at=auto_at,
                )
            )
            if i in voided_ids:
                s.add(
                    AuditLog(
                        organization_id=org_id,
                        actor_id=None,
                        action="invoice.voided_return_to_approved",
                        entity_type="invoice",
                        entity_id=inv.id,
                        details={"void_reason": "duplicate"},
                        created_at=auto_at + timedelta(days=1),
                    )
                )
            if i in rejected_ids:
                s.add(
                    AuditLog(
                        organization_id=org_id,
                        actor_id=None,
                        action="invoice.rejected",
                        entity_type="invoice",
                        entity_id=inv.id,
                        details={"reason": "bad"},
                        created_at=auto_at + timedelta(days=1),
                    )
                )
        await s.commit()
    return ids


async def test_feedback_endpoint_measures_overturn_and_adjusts(realdb):
    """With auto-approvals that were later voided/rejected, the feedback endpoint
    reports a real overturn rate AND pulls the threshold recommendation back."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    await _seed_active_workflow(mk, org_id)
    # Clean vendors → the base recommendation WOULD raise.
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )
    # 10 auto-approvals, 2 overturned (20% > 15% freeze).
    await _seed_auto_approved(mk, org_id, count=10, voided_ids=(0,), rejected_ids=(1,))

    async with realdb.client(key="a", role="cfo") as client:
        r = await client.get("/api/adaptive/feedback")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["outcomes"]["auto_approved_count"] == 10
    assert data["outcomes"]["overturned_count"] == 2
    assert data["outcomes"]["overturn_rate_pct"] == "20.0"
    # Base recommendation raised; adjusted pulled back (freeze ≥ 15%).
    assert data["base_recommendation"]["should_raise"] is True
    assert data["adjusted_recommendation"]["should_raise"] is False
    assert data["adjusted_recommendation"]["reason_code"] == "outcome_freeze"
    # Effectiveness metric is measured (10 ≥ min sample).
    overturn = next(m for m in data["metrics"] if m["name"] == "auto_approval_overturn_rate")
    assert overturn["insufficient_data"] is False
    assert overturn["value_pct"] == "20.0"


async def test_feedback_endpoint_clean_outcomes_keep_the_raise(realdb):
    """Auto-approvals with NO overturns → the base raise survives the loop."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    await _seed_active_workflow(mk, org_id)
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )
    await _seed_auto_approved(mk, org_id, count=8)  # all clean

    async with realdb.client(key="a", role="admin") as client:
        r = await client.get("/api/adaptive/feedback")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["outcomes"]["overturn_rate_pct"] == "0.0"
    assert data["base_recommendation"]["should_raise"] is True
    assert data["adjusted_recommendation"]["should_raise"] is True
    assert data["adjusted_recommendation"]["reason_code"] == "ok"


async def test_feedback_endpoint_insufficient_outcome_data(realdb):
    """Too few auto-approvals → overturn metric is honestly 'not yet measurable'
    and the loop does NOT pull back (it leaves the base recommendation alone)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    await _seed_active_workflow(mk, org_id)
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )
    # Only 2 auto-approvals, both overturned — but below the min sample of 5.
    await _seed_auto_approved(mk, org_id, count=2, voided_ids=(0, 1))

    async with realdb.client(key="a", role="cfo") as client:
        r = await client.get("/api/adaptive/feedback")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["outcomes"]["insufficient_data"] is True
    overturn = next(m for m in data["metrics"] if m["name"] == "auto_approval_overturn_rate")
    assert overturn["insufficient_data"] is True
    assert overturn["value_pct"] is None
    # Despite a high raw overturn, the loop leaves the raise intact (no noise reaction).
    assert data["adjusted_recommendation"]["should_raise"] is True


async def test_feedback_endpoint_writes_access_audit(realdb):
    """The feedback read is sensitive → it writes a PII-free
    `adaptive_feedback.viewed` audit row (SOX access auditing)."""
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_adaptive_min_history(realdb, org_id, 3)
    await _seed_active_workflow(mk, org_id)

    async with realdb.client(key="a", role="cfo") as client:
        assert (await client.get("/api/adaptive/feedback")).status_code == 200

    async with mk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "adaptive_feedback.viewed")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        # PII-free — only scope metadata, no amounts/vendor/PII.
        assert "lookback_days" in rows[0].details
        assert "auto_approved_count" in rows[0].details


async def test_feedback_endpoint_rbac_and_auth(realdb):
    """Feedback is a manager/CFO read surface — clerk 403, anon 401, CFO 200."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _seed_active_workflow(mk, org_id)
    path = "/api/adaptive/feedback"

    async with realdb.client(key="a", role=None) as anon:
        assert (await anon.get(path)).status_code == 401
    async with realdb.client(key="a", role="ap_clerk") as clerk:
        assert (await clerk.get(path)).status_code == 403
    async with realdb.client(key="a", role="cfo") as cfo:
        assert (await cfo.get(path)).status_code == 200
    async with realdb.client(key="a", role="admin") as admin:
        assert (await admin.get(path)).status_code == 200


# ---------------------------------------------------------------------------
# The feedback loop must govern the WRITE, not just the dashboard.
#
# `outcome_adjusted_threshold` was only ever called from `GET /feedback`, so
# `POST /threshold-recommendation/apply` — the single endpoint that actually
# widens auto-approve — recomputed the FORWARD recommendation and widened it
# anyway. An admin could read "holding at $0, 20% of auto-approvals were later
# voided or rejected" and, in the same breath, apply a $5,000 raise with
# `reason_code: "ok"`. Its routing sibling never had this gap (the per-approver
# penalty is folded into the candidate score, so apply inherits it).
# ---------------------------------------------------------------------------


async def _seed_overturned_auto_approvals(realdb, mk, org_id, actor_id):
    """Clean vendor history (base recommendation WOULD raise to $5,000) plus 10
    auto-approvals of which 2 were walked back — a 20% overturn rate, past the
    15% freeze band."""
    await _set_adaptive_min_history(realdb, org_id, 3)
    wf_id = await _seed_active_workflow(mk, org_id)
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )
    await _seed_auto_approved(mk, org_id, count=10, voided_ids=(0,), rejected_ids=(1,))
    return wf_id


async def test_threshold_apply_is_held_back_by_the_outcome_freeze(realdb):
    from sqlalchemy import select

    from app.models.workflow import AuditLog, WorkflowDefinition, WorkflowVersion

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    wf_id = await _seed_overturned_auto_approvals(realdb, mk, org_id, actor_id)

    async with realdb.client(key="a", role="admin") as client:
        r = await client.post("/api/adaptive/threshold-recommendation/apply", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is False
    assert body["reason_code"] == "outcome_freeze"
    assert body["new_threshold"] == body["previous_threshold"] == "0"
    assert body["version_number"] is None
    # The rationale explains the hold with the measured rate, not a bare refusal.
    assert "20.0% overturn rate" in body["rationale"]

    async with mk() as s:
        defn = (
            await s.execute(select(WorkflowDefinition).where(WorkflowDefinition.id == wf_id))
        ).scalar_one()
        approval = next(st for st in defn.steps_config["steps"] if st["type"] == "approval")
        # Auto-approve was NOT widened.
        assert approval.get("config", {}).get("auto_approve_below") in (None, "0")
        # A no-op writes neither a version snapshot nor a threshold audit row.
        assert (
            await s.execute(select(WorkflowVersion).where(WorkflowVersion.definition_id == wf_id))
        ).scalars().all() == []
        raised = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "workflow.auto_approve_threshold_raised"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert raised == []


async def test_threshold_read_and_apply_agree_on_the_adjusted_recommendation(realdb):
    """`GET /threshold-recommendation` returns the same decision the apply POST
    acts on — one resolver, so the read can't say 'raise' while the write holds
    (and `GET /feedback` still shows the un-adjusted base for explainability)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _seed_overturned_auto_approvals(realdb, mk, org_id, actor_id)

    async with realdb.client(key="a", role="admin") as client:
        read = (await client.get("/api/adaptive/threshold-recommendation")).json()
        fb = (await client.get("/api/adaptive/feedback")).json()

    assert read["should_raise"] is False
    assert read["reason_code"] == "outcome_freeze"
    assert read["recommended_threshold"] == read["current_threshold"]
    # /feedback still exposes BOTH, so the held-back raise is explainable.
    assert fb["base_recommendation"]["should_raise"] is True
    assert fb["adjusted_recommendation"]["reason_code"] == read["reason_code"]


async def test_threshold_apply_still_raises_when_the_outcomes_are_clean(realdb):
    """Guard against over-refusing: the loop must only hold back a raise when
    the auto-approved population is genuinely being walked back."""
    from sqlalchemy import select

    from app.models.workflow import WorkflowDefinition

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    await _set_adaptive_min_history(realdb, org_id, 3)
    wf_id = await _seed_active_workflow(mk, org_id)
    await _seed_clean_vendors(
        mk, org_id, actor_id, [("Clean A", 5000, 3), ("Clean B", 3000, 3), ("Clean C", 1000, 3)]
    )
    await _seed_auto_approved(mk, org_id, count=8)  # all clean

    async with realdb.client(key="a", role="admin") as client:
        r = await client.post("/api/adaptive/threshold-recommendation/apply", json={})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    assert r.json()["new_threshold"] == "5000.00"

    async with mk() as s:
        defn = (
            await s.execute(select(WorkflowDefinition).where(WorkflowDefinition.id == wf_id))
        ).scalar_one()
        approval = next(st for st in defn.steps_config["steps"] if st["type"] == "approval")
        assert approval["config"]["auto_approve_below"] == "5000.00"


# ---------------------------------------------------------------------------
# `_approval_auto_below` reads the CURRENT threshold off the frozen JSONB. Its
# hand-rolled `except (TypeError, ValueError)` did not catch what a bad value
# actually raises — `Decimal("abc")` raises `decimal.InvalidOperation`, an
# `ArithmeticError` — so a malformed `auto_approve_below` 500'd all three
# threshold surfaces. `"NaN"` / `"Infinity"` parsed straight THROUGH the guard
# and blew up downstream instead (`Decimal("NaN") > 0` raises; an infinite
# current threshold reaches `_q2`'s quantize).
# ---------------------------------------------------------------------------


def test_approval_auto_below_reads_an_unusable_threshold_as_unset():
    from app.api.adaptive_workflows import _approval_auto_below

    for bad in ("abc", "10,000", "", "5000 USD", {"x": 1}, [1], "NaN", "Infinity", "-Infinity"):
        steps = {"steps": [{"type": "approval", "config": {"auto_approve_below": bad}}]}
        assert _approval_auto_below(steps) == Decimal("0"), bad


def test_approval_auto_below_reads_a_usable_threshold_exactly():
    from app.api.adaptive_workflows import _approval_auto_below

    for raw, expected in (("2500.00", Decimal("2500.00")), (2500, Decimal("2500"))):
        steps = {"steps": [{"type": "approval", "config": {"auto_approve_below": raw}}]}
        assert _approval_auto_below(steps) == expected
    assert _approval_auto_below({"steps": []}) == Decimal("0")
    assert _approval_auto_below({"steps": [{"type": "approval", "config": {}}]}) == Decimal("0")
