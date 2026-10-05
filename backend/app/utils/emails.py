r"""One definition of "does this look like an email address" for the backend.

Three modules used to carry their own copy of the same regex — ``api/signup.py``
(who gets a tenant), ``api/partner.py`` (who administers a partner-provisioned
child tenant) and ``schemas/scheduled_report.py`` (who receives a recurring CSV
of the tenant's AP spend). All three decide **who receives a tenant's data**, so
three copies of one rule is exactly the shape that drifts: tighten one and the
other two keep admitting what it now refuses, silently.

The check is deliberately **permissive**. It exists to reject obvious garbage —
no ``@``, whitespace, a bare hostname, a trailing dot — not to decide
deliverability. The real validation is a round trip: signup and partner
provisioning both mail the address a link that must be clicked, and a scheduled
report that can't be delivered surfaces as a delivery failure on the schedule.
Pulling in ``email-validator`` to be stricter would buy syntax rigour the RFC
itself makes nearly unusable (quoted local parts, address literals) without
answering the question anyone actually cares about.

The domain side uses non-dot character classes delimited by literal dots rather
than a nested quantifier, so the engine can't backtrack catastrophically on an
adversarial input (the polynomial-ReDoS pattern). Keep that shape if the rule
is ever tightened.

One deliberate tightening came with the hoist: the three copies all ended in
``$``, which in Python matches at end-of-string **or just before a trailing
newline** — so ``"user@example.com\n"`` passed all three shape checks and was
stored as a login, a partner child's admin address, or a report recipient. The
shared pattern ends in ``\Z`` instead, which matches only the true end. A
trailing newline in an address is never legitimate input, and a newline in a
value that reaches an SMTP header is the header-injection primitive, so the
narrower anchor is the safe end of the trade.

``tests/test_email_shape.py`` pins the behaviour in both directions — including
the cases this pattern deliberately ADMITS — so a later tightening is a
conscious edit to a failing test rather than a silent change in who can be
mailed.
"""

from __future__ import annotations

import re

from sqlalchemy import ColumnElement, func

__all__ = [
    "EMAIL_SHAPE_PATTERN",
    "email_matches",
    "exact_email_first",
    "is_header_safe",
    "looks_like_email",
    "normalize_email",
]

#: Permissive shape check. Local part: any run of non-space, non-``@``. Domain:
#: one or more dot-delimited labels of non-space, non-``@``, non-dot characters
#: — so ``a@b`` (no dot) and ``a@b.`` (trailing dot) are both refused. Anchored
#: with ``\Z``, not ``$``: see the module docstring.
EMAIL_SHAPE_PATTERN = re.compile(r"^[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+\Z")


#: Characters that can never legitimately appear in an email address and must
#: never reach a mail header: C0 controls (CR and LF above all) plus DEL. CR/LF
#: are the header-injection primitive — a value carrying one can continue the
#: header it lands in, e.g. with an attacker-chosen ``Bcc:``.
_HEADER_UNSAFE_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def is_header_safe(value: str) -> bool:
    r"""True when ``value`` carries nothing that could break out of a mail header.

    Weaker than :func:`looks_like_email` and deliberately so. It exists for the
    one caller that must NOT impose the full shape rule: an SSO-supplied address
    (`services/identity_provisioning.extract_and_check_email`). A corporate IdP
    can legitimately assert an internal-only address like ``user@intranet`` with
    no dot in the domain, and refusing that would lock a whole tenant out of its
    own workspace over a cosmetic rule. A control character is a different
    matter — no IdP has a legitimate reason to emit one, and the value becomes
    both a login and a mail destination.
    """
    return _HEADER_UNSAFE_PATTERN.search(value) is None


def looks_like_email(value: str) -> bool:
    r"""True when ``value`` has the shape of an email address.

    Shape only — never deliverability, and never a claim the mailbox exists.
    Callers raise their own error (a 422 in the API layer, a ``ValueError`` in
    a pydantic validator) so the message can stay appropriate to the surface;
    the recipient-list validator in particular must not echo the offending
    value, which is third-party PII on an HTTP error body.

    Composed over :func:`is_header_safe` because the shape pattern's character
    classes are built on ``\s``, which excludes only whitespace — and Python's
    ``\s`` is just ``\t\n\r\v\f`` plus ``\x1c-\x1f`` (and ``\x85``). So
    the rest of the C0 range — NUL, ``\x01-\x08``, ``\x0e-\x1a`` — and DEL
    passed the shape rule while the header-safety rule refused them, leaving
    the two rules not strictly ordered.

    That gap reached three surfaces that do not additionally call
    ``is_header_safe``: the signup admin address (`api/signup`), a
    partner-provisioned child tenant's admin login (`api/partner`), and the
    scheduled-report recipient list (`schemas/scheduled_report`, whose
    ``strip()`` removes whitespace only) — each a value that becomes both a
    login and a mail destination.

    CR and LF were already refused, so §50's header-injection fix was never
    affected; this closes the wider class §60 introduced ``is_header_safe``
    for. Composing rather than tightening the pattern keeps §60's deliberate
    asymmetry intact: an SSO-asserted dotless ``user@intranet`` is still
    header-safe, because the weaker rule is unchanged.
    """
    return is_header_safe(value) and bool(EMAIL_SHAPE_PATTERN.match(value))


# ---------------------------------------------------------------------------
# Email as an identity: one canonical form, matched without regard to case
# ---------------------------------------------------------------------------
#
# An address is the login identifier (`users.email`, `vendor_users.email`) and
# the key SSO JIT and SCIM link an IdP identity to. Those paths disagreed on
# what "the same address" meant: SCIM and SSO lower-cased what they wrote and
# looked up, while the admin create/update, tenant provisioning and the portal
# invite stored whatever was typed, and every lookup compared exactly. So an
# admin-created `Jane.Doe@Acme.com` was a different person from the
# `jane.doe@acme.com` her IdP asserts: SSO JIT minted a SECOND account (as
# `ap_clerk`) instead of linking hers, SCIM's 409 guard missed and provisioned a
# duplicate — whose later deprovision left the original, with its roles and its
# password, active — and she could not sign in typing her address in lower case.
#
# Every write stores `normalize_email(...)`; every identity lookup goes through
# `email_matches`, which also finds rows written before writes were normalized.


def normalize_email(value: str) -> str:
    """The stored, canonical form of an address: trimmed and lower-cased.

    Lower-casing the local part is technically stricter than RFC 5321, which
    lets a mail server treat it case-sensitively; no mainstream provider or IdP
    does, and SCIM's `userName` is `caseExact: false`. Two accounts that differ
    only in case are a defect, not a feature.
    """
    return value.strip().lower()


def email_matches(column, address: str) -> ColumnElement[bool]:
    """SQL predicate: ``column`` holds ``address``, ignoring case and padding.

    Compares ``lower(column)`` rather than the column itself so a row stored
    before writes were normalized (mixed case) still matches.
    """
    return func.lower(column) == normalize_email(address)


def exact_email_first(column, address: str) -> ColumnElement[bool]:
    """ORDER BY key for a single-row lookup under `email_matches`.

    Rows stored before normalization can include two that differ only in case.
    A login must not 500 on that (an unbounded ``scalar_one_or_none`` would), and
    must not pick between them arbitrarily: the row whose stored address is
    exactly what was submitted sorts first. Use as
    ``.order_by(exact_email_first(...).desc()).limit(1)``.
    """
    return column == address.strip()
