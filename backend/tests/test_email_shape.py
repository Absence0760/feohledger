"""The email shape check has one owner, and its permissiveness is deliberate.

`app/utils/emails.py` replaced three identical copies of the same regex —
`api/signup.py`, `api/partner.py` and `schemas/scheduled_report.py`. All three
decide **who receives a tenant's data by email** (a new tenant's admin, a
partner-provisioned child's admin, and the recurring CSV of a tenant's AP
spend), so the risk was never that the rule is loose; it is that three copies
of a loose rule drift and only one of them gets tightened.

Two halves, both load-bearing:

* the REJECTS pin what the check is for — no `@`, whitespace, a bare hostname,
  a trailing dot;
* the ADMITS pin what it deliberately lets through. `a@b.c`, a single-label TLD
  and an address with no MX record all pass, because shape is not
  deliverability and the real validation is the round trip (signup and partner
  provisioning both mail a link that must be clicked). Anyone tightening the
  rule has to edit a *failing* test here, which is the point: the tightening
  becomes a conscious call about who can still be mailed rather than a silent
  narrowing.

The third test is the drift guard — it fails if a fourth copy of the pattern
appears anywhere under `app/`, which is how this got to three copies.
"""

from __future__ import annotations

import pathlib

import pytest

from app.utils.emails import EMAIL_SHAPE_PATTERN, looks_like_email

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"


@pytest.mark.parametrize(
    "address",
    [
        "demo@acme.com",
        "a@b.c",  # minimal — one-char local part, one-char labels
        "first.last@sub.domain.example.com",
        "user+scheduled-report@example.co.uk",
        "UPPER@Example.COM",  # case is not normalised by the shape check
        "user_name-123@example-corp.io",
        "ap@例え.jp",  # non-ASCII passes — the round trip is the real check
        "nobody@no-such-domain-anywhere.invalid",  # undeliverable, still valid shape
    ],
)
def test_admits_the_shapes_it_deliberately_allows(address):
    """Deliberately permissive. Tightening any of these is a product decision
    about who can be mailed — make it here, on purpose, not in one of the three
    call sites."""
    assert looks_like_email(address) is True


@pytest.mark.parametrize(
    "address",
    [
        "",
        "plainstring",
        "no-at-sign.example.com",
        "@example.com",  # empty local part
        "user@",  # empty domain
        "user@localhost",  # bare hostname, no dot
        "user@example.",  # trailing dot
        "user@.example.com",  # empty leading label
        "user@example..com",  # empty interior label
        "user name@example.com",  # whitespace in the local part
        "user@exam ple.com",  # whitespace in the domain
        "user@example.com\n",  # trailing newline — `$` alone would admit this
        "\nuser@example.com",
        "two@at@example.com",
        "user@exam@ple.com",
    ],
)
def test_rejects_obvious_garbage(address):
    assert looks_like_email(address) is False


def test_pattern_is_anchored_with_Z_not_dollar():
    """The three hoisted copies all ended in `$`, which matches end-of-string OR
    just before a trailing newline — so `"user@example.com\\n"` passed every one
    of them and was stored as a login / a child tenant's admin / a report
    recipient. `\\Z` matches only the true end.

    Kept as its own test because the parametrised case above reads like one
    more piece of garbage; this one says which anchor is load-bearing and why,
    so nobody "simplifies" it back to `$`.
    """
    assert EMAIL_SHAPE_PATTERN.pattern.endswith(r"\Z")
    assert EMAIL_SHAPE_PATTERN.match("good@example.com\n") is None
    assert EMAIL_SHAPE_PATTERN.match("good@example.com\nevil@example.com") is None


def test_no_fourth_copy_of_the_pattern_under_app():
    """Source scan: the regex literal must appear only in its one owner.

    Three copies is how this started. A grep-able guard is cheaper than
    rediscovering the drift.
    """
    needle = "[^\\s@]+@[^\\s@.]+"
    owner = (APP_DIR / "utils" / "emails.py").resolve()
    offenders = [
        str(path.relative_to(APP_DIR))
        for path in sorted(APP_DIR.rglob("*.py"))
        if path.resolve() != owner and needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"email-shape regex re-declared in {offenders}. Import "
        "`looks_like_email` from app.utils.emails instead — three copies of "
        "this rule is what the shared module exists to prevent."
    )


@pytest.mark.parametrize(
    ("address", "masked"),
    [
        ("admin@acme.test", "a***@acme.test"),
        ("  Jo.Smith@Example.COM ", "J***@Example.COM"),
        ("x@y.z", "x***@y.z"),
        ("no-at-sign", "***"),
        ("@domain.only", "***"),
    ],
)
def test_mask_address_keeps_only_the_first_character_and_the_domain(address, masked):
    """What the operator CLI prints in place of the address a link went to."""
    from app.utils.emails import mask_address

    assert mask_address(address) == masked
