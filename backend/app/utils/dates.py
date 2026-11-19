"""One definition of "today" for the backend.

``date.today()`` resolves in the **server's local timezone**. On the deployed
shape (a UTC container) that agrees with UTC, so a mixture is latent rather than
live — which is exactly what makes it easy to keep introducing.

It stops being latent the moment anything runs off a non-UTC host, because the
cash-flow stack derives more than a display value from "today":

* ``ap_commitments.commitment_rows`` bounds its horizon on it, so the `/cfo`
  chart and a copilot answer built minutes apart can be assembled from
  *different row sets* for part of each day;
* ``services/cash_flow_plan.compute_plan_id`` hashes it, so a plan proposed near
  midnight can 409 as stale against its own enact call;
* ``services/scheduled_reports`` slices each report's ``period_days`` window
  from it, so an emailed snapshot and the API export of the same report
  disagree at the boundary.

…and the AP surfaces do too. Every one of these is a *comparison*, not a label:

* the early-pay discount cutoff — ``api/discounts``, ``api/portal`` (the
  supplier's own view of the same offer) and ``services/analytics`` all ask
  "is this tier still capturable", and a tenant could see one answer while its
  supplier saw the other;
* ``services/invoice_warnings`` raises a fraud flag when an invoice is dated in
  the future and a past-due flag when its due date has passed — the boundary
  day is where a legitimate invoice becomes a fraud signal;
* ``api/recurring`` derives the period key from it, and that key IS the
  generation idempotency guard (``uq_invoice_recurring_period``), so a shifted
  "today" is a duplicate payable rather than a cosmetic difference;
* ``Invoice.approval_date`` is written from it on all three approval paths
  (``services/review``, ``services/extraction``, ``api/workflow``) — a
  regulated field on the SOX trail.

The stamps and filenames matter less individually, but each sits beside a
``datetime.now(UTC)`` in the same response — a 1099 form's ``generated_at``, a
Positive Pay file's ``file_date`` next to a run's tz-aware ``executed_at``, an
export filename next to the PDF's own provenance header. A local-time answer
made one document disagree with itself for part of each day.

``app/api/cash_flow.py`` and every copilot tool already resolved it as
``datetime.now(UTC).date()``. This module is that expression with a name, so the
rest of the backend converges on it instead of each site re-deciding. No call
site under ``app/`` reads the local date any more; ``scripts/seed*.py`` still
does, deliberately — demo fixtures on a dev laptop have nothing to reconcile
against.

Guard: ``tests/test_utc_today.py`` AST-scans **the whole of** ``app/``, failing
on ``date.today()``, ``datetime.today()``, ``datetime.date.today()`` and
``datetime.now().date()`` — that last one is naive ``now()``, so a local date
under a name that reads as deliberate, and a scan shaped only around the word
``today`` would never see it. It also fails on an inlined
``datetime.now(UTC).date()`` anywhere but here: that spelling is *correct*, so
the first scan can never flag it, yet it is one dropped argument away from
being wrong on a line that still reads as deliberate.

The scan used to be an opt-in allowlist of converged modules, which was right
while the tree was mixed and wrong once it wasn't — a list cannot see a module
nobody added to it, and a new module is where the next ``date.today()`` lands.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

__all__ = ["parse_ambiguous_date", "resolve_day_first_preference", "utc_today"]


def utc_today() -> date:
    """Today's date in UTC — never the server's local timezone."""
    return datetime.now(UTC).date()


def resolve_day_first_preference(settings: dict | None) -> bool:
    """Whether an ambiguous numeric date should be read day-first (DD/MM).

    ``settings`` is a tenant ``Organization.settings`` JSONB dict. The one
    signal in there that unambiguously identifies the org as UK-registered
    is a non-empty ``company.companies_house_number`` — Companies House
    registers UK entities only. ``company.vat_registration_number`` was
    deliberately NOT used for this: VAT registration is common across the EU
    and beyond, so its presence doesn't tell us the org is day-first (or even
    which day-first country it might be), whereas a Companies House number
    does.

    No signal (an org that never set it, or any non-UK org) resolves to
    ``False`` — the pre-existing month-first behavior, so nothing changes for
    the common case. Never raises: a malformed settings shape just means "no
    signal", not an error.
    """
    if not isinstance(settings, dict):
        return False
    company = settings.get("company")
    if not isinstance(company, dict):
        return False
    return bool((company.get("companies_house_number") or "").strip())


def parse_ambiguous_date(raw: str | None, *, day_first: bool) -> date | None:
    """Parse a slash- or dash-separated three-part numeric date.

    ``03/04/2026`` is genuinely ambiguous — both "3 April" (day_first) and
    "March 4" (month_first) are structurally valid, and nothing in the string
    itself can settle it. The disambiguating signal has to come from OUTSIDE
    the string — the caller's ``day_first`` argument (typically resolved via
    :func:`resolve_day_first_preference`).

    Tries the ``day_first``-preferred order first. When that order is
    structurally invalid for this particular string (e.g. ``day_first=True``
    but the second component is > 12, so it cannot be a month — ``03/25/2026``
    can only be March 25), falls back to the other order, which is exactly
    what makes an UNAMBIGUOUS date parse correctly regardless of preference.
    Returns ``None`` — never guesses — when NEITHER order parses (e.g.
    ``13/13/2026``, or the string isn't `/`- or `-`-separated at all).

    This is deliberately narrow: it only disambiguates DD/MM vs MM/DD. ISO
    (`YYYY-MM-DD`), dotted (`DD.MM.YYYY` — never ambiguous with a US format,
    since a dot-separated date is conventionally day-first regardless of
    locale) and human-readable (`March 15, 2024`) forms are unambiguous and
    stay in each caller's own format list.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    sep = "/" if "/" in raw else ("-" if "-" in raw else None)
    if sep is None:
        return None
    day_first_fmt = f"%d{sep}%m{sep}%Y"
    month_first_fmt = f"%m{sep}%d{sep}%Y"
    order = (day_first_fmt, month_first_fmt) if day_first else (month_first_fmt, day_first_fmt)
    for fmt in order:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None
