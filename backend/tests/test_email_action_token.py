"""Unit tests for the email-approval signed-token primitive (pure, no DB).

Covers: build/verify round-trip, wrong key, tampered payload, tampered
signature, action-flip detection, expiry, empty-key fail-closed, invalid
action, malformed input, the email link builder, and the displayed-facts
digest that binds an Approve token to the version its message showed.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services.email_action_token import (
    ACTION_APPROVE,
    ACTION_REJECT,
    _sign,
    approval_facts_changed,
    build_action_token,
    build_email_action_links,
    digest_of_invoice,
    verify_action_token,
)

_KEY = "unit-test-signing-key"
_FACTS = "f" * 32


def _build(action: str = ACTION_APPROVE, key: str = _KEY, **kw) -> str:
    return build_action_token(
        tenant_slug=kw.get("tenant_slug", "acme"),
        invoice_id=kw.get("invoice_id", uuid.uuid4()),
        actor_id=kw.get("actor_id", uuid.uuid4()),
        action=action,
        facts=kw.get("facts", _FACTS),
        signing_key=key,
        ttl_hours=kw.get("ttl_hours", 24),
        now=kw.get("now"),
    )


def test_round_trip_preserves_facts():
    inv, actor = uuid.uuid4(), uuid.uuid4()
    token = _build(ACTION_REJECT, tenant_slug="acme", invoice_id=inv, actor_id=actor)
    decoded = verify_action_token(token, _KEY)
    assert decoded is not None
    assert decoded.tenant_slug == "acme"
    assert decoded.invoice_id == inv
    assert decoded.actor_id == actor
    assert decoded.action == ACTION_REJECT
    assert decoded.jti  # a random per-token id is present


def test_each_token_has_a_unique_jti():
    a = verify_action_token(_build(), _KEY)
    b = verify_action_token(_build(), _KEY)
    assert a.jti != b.jti


def test_wrong_key_rejected():
    assert verify_action_token(_build(), "other-key") is None


def test_tampered_signature_rejected():
    token = _build()
    body, _, sig = token.rpartition(".")
    flipped = sig[:-1] + ("a" if sig[-1] != "a" else "b")
    assert verify_action_token(f"{body}.{flipped}", _KEY) is None


def test_tampered_payload_rejected():
    # Flip a byte in the payload portion — signature no longer matches.
    token = _build()
    body, _, sig = token.rpartition(".")
    mangled = ("A" if body[0] != "A" else "B") + body[1:]
    assert verify_action_token(f"{mangled}.{sig}", _KEY) is None


def test_action_cannot_be_swapped():
    """An attacker can't turn a reject link into an approve link: the action is
    inside the signed payload, so any edit breaks the signature."""
    reject = _build(ACTION_REJECT)
    decoded = verify_action_token(reject, _KEY)
    assert decoded.action == ACTION_REJECT  # stays reject; no way to forge approve


def test_expired_token_rejected():
    token = _build(now=time.time() - 48 * 3600, ttl_hours=24)  # issued 2 days ago, 24h TTL
    assert verify_action_token(token, _KEY) is None


def test_not_yet_expired_token_accepted():
    token = _build(now=time.time() - 1 * 3600, ttl_hours=24)
    assert verify_action_token(token, _KEY) is not None


def test_empty_key_fails_closed_on_build_and_verify():
    assert (
        build_action_token(
            tenant_slug="a",
            invoice_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            action=ACTION_APPROVE,
            facts=_FACTS,
            signing_key="",
            ttl_hours=24,
        )
        is None
    )
    # Even a structurally valid token is rejected when no key is configured.
    assert verify_action_token(_build(), "") is None


def test_invalid_action_not_built_or_verified():
    assert (
        build_action_token(
            tenant_slug="a",
            invoice_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            action="delete",
            facts=_FACTS,
            signing_key=_KEY,
            ttl_hours=24,
        )
        is None
    )


def test_malformed_tokens_rejected():
    for bad in [None, "", "no-dot", "a.b.c.d", ".", "x.", ".y", "garbage.deadbeef"]:
        assert verify_action_token(bad, _KEY) is None


def test_link_builder_returns_none_without_key():
    assert (
        build_email_action_links(
            api_base_url="http://localhost:8000",
            tenant_slug="acme",
            invoice_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            facts=_FACTS,
            signing_key="",
            ttl_hours=24,
        )
        is None
    )


def test_link_builder_emits_both_valid_links():
    inv, actor = uuid.uuid4(), uuid.uuid4()
    links = build_email_action_links(
        api_base_url="http://localhost:8000/",
        tenant_slug="acme",
        invoice_id=inv,
        actor_id=actor,
        facts=_FACTS,
        signing_key=_KEY,
        ttl_hours=24,
    )
    assert links is not None
    text, html = links
    assert "Approve:" in text and "Reject:" in text
    assert text.count("/api/invoices/email-action/") == 2
    assert 'href="http://localhost:8000/api/invoices/email-action/' in html
    # Each link's embedded token must verify and carry the right action + facts.
    for line in text.splitlines():
        token = line.split("email-action/")[1]
        decoded = verify_action_token(token, _KEY)
        assert decoded is not None
        assert decoded.invoice_id == inv and decoded.actor_id == actor
        assert decoded.action in (ACTION_APPROVE, ACTION_REJECT)


# ---------------------------------------------------------------------------
# One message = one decision: the Approve/Reject tokens share a consume key
# ---------------------------------------------------------------------------


def test_every_builder_mints_its_two_tokens_as_one_pair():
    from app.services.email_action_token import (
        CHANNEL_SLACK,
        CHANNEL_TEAMS,
        build_slack_action_tokens,
        build_teams_action_tokens,
    )

    facts = {
        "tenant_slug": "acme",
        "invoice_id": uuid.uuid4(),
        "actor_id": uuid.uuid4(),
        "facts": _FACTS,
        "signing_key": _KEY,
        "ttl_hours": 24,
    }
    text, _html = build_email_action_links(api_base_url="http://x", **facts)
    email = [verify_action_token(ln.split("email-action/")[1], _KEY) for ln in text.splitlines()]
    slack = [
        verify_action_token(t, _KEY, expected_channel=CHANNEL_SLACK)
        for t in build_slack_action_tokens(**facts)
    ]
    teams = [
        verify_action_token(t, _KEY, expected_channel=CHANNEL_TEAMS)
        for t in build_teams_action_tokens(**facts)
    ]
    for approve, reject in (email, slack, teams):
        assert approve.jti != reject.jti  # still two distinct tokens
        assert approve.pair_id and approve.pair_id == reject.pair_id
        assert approve.consume_key == reject.consume_key
    # ...and every message is its own pair.
    assert len({email[0].pair_id, slack[0].pair_id, teams[0].pair_id}) == 3


def test_an_unpaired_token_consumes_on_its_own_jti():
    decoded = verify_action_token(_build(), _KEY)
    assert decoded.pair_id is None
    assert decoded.consume_key == decoded.jti


# ---------------------------------------------------------------------------
# The displayed-facts digest: an Approve token binds the version it showed
# ---------------------------------------------------------------------------


def _invoice(**kw):
    base = {
        "invoice_number": "INV-1",
        "vendor_name": "Acme Supplies",
        "vendor_id": uuid.UUID(int=7),
        "amount": Decimal("1500.00"),
        "currency": "USD",
        "payment_method": "ach",
        "due_date": date(2026, 11, 1),
        "entity_id": uuid.UUID(int=3),
    }
    return SimpleNamespace(**{**base, **kw})


def test_facts_round_trip_through_the_token():
    decoded = verify_action_token(_build(facts="abc123"), _KEY)
    assert decoded.facts == "abc123"


def test_a_token_is_not_minted_without_facts():
    # Nothing to bind the decision to, so no link at all — not an unbound one.
    assert _build(facts="") is None


def test_a_token_without_the_facts_claim_does_not_verify():
    # A token minted before the claim existed, correctly signed: it must fail
    # closed rather than verify as "matches anything".
    payload = {
        "t": "acme",
        "i": str(uuid.uuid4()),
        "a": str(uuid.uuid4()),
        "act": ACTION_APPROVE,
        "ch": "email",
        "exp": int(time.time()) + 3600,
        "jti": "legacy",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    assert verify_action_token(f"{body}.{_sign(body, _KEY)}", _KEY) is None


def test_digest_is_canonical_over_the_amount():
    # The notification and the endpoint both digest the row; `1500` and
    # `1500.00` are one amount and must give one digest.
    assert digest_of_invoice(_invoice(amount=Decimal("1500"))) == digest_of_invoice(_invoice())


def test_digest_tolerates_every_optional_fact_being_absent():
    bare = _invoice(
        invoice_number=None,
        vendor_name=None,
        vendor_id=None,
        amount=None,
        currency=None,
        payment_method=None,
        due_date=None,
        entity_id=None,
    )
    assert len(digest_of_invoice(bare)) == 32


def test_digest_changes_with_every_bound_fact():
    seen = digest_of_invoice(_invoice())
    for change in (
        {"amount": Decimal("1500.01")},
        {"currency": "EUR"},
        {"invoice_number": "INV-2"},
        {"vendor_name": "Acme Supplies Ltd"},
        # Same displayed name, different payee: still a different invoice.
        {"vendor_id": uuid.UUID(int=8)},
        {"amount": None},
        # Not displayed, but each changes the payment the approval releases.
        {"payment_method": "wire"},
        {"due_date": date(2026, 10, 15)},
        {"entity_id": uuid.UUID(int=4)},
    ):
        assert digest_of_invoice(_invoice(**change)) != seen, change


def test_only_an_approve_token_is_held_to_its_facts():
    stale = digest_of_invoice(_invoice(amount=Decimal("1.00")))
    approve = verify_action_token(_build(ACTION_APPROVE, facts=stale), _KEY)
    reject = verify_action_token(_build(ACTION_REJECT, facts=stale), _KEY)
    assert approval_facts_changed(approve, _invoice())
    # Sending an invoice back is safe whatever it now says.
    assert not approval_facts_changed(reject, _invoice())
    current = verify_action_token(_build(ACTION_APPROVE, facts=digest_of_invoice(_invoice())), _KEY)
    assert not approval_facts_changed(current, _invoice())


def test_every_assignment_notification_binds_the_invoice_digest():
    """An `invoice_assigned` message's Approve / Reject actions bind
    `action_facts`; a site that sends the event without it offers no actions
    at all — silently. So every `notify_event(event_type=EVENT_INVOICE_ASSIGNED,
    ...)` under `app/` must pass `action_facts=digest_of_invoice(...)`."""
    import ast
    from pathlib import Path

    app = Path(__file__).resolve().parents[1] / "app"
    sites = []
    for path in app.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "notify_event":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            event = kw.get("event_type")
            if not (isinstance(event, ast.Name) and event.id == "EVENT_INVOICE_ASSIGNED"):
                continue
            facts = kw.get("action_facts")
            ok = isinstance(facts, ast.Call) and getattr(facts.func, "id", None) == (
                "digest_of_invoice"
            )
            sites.append((f"{path.relative_to(app)}:{node.lineno}", ok))
    assert sites, "no invoice_assigned notification found — the scan is broken"
    assert all(ok for _, ok in sites), [s for s, ok in sites if not ok]
