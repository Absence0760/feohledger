"""The sub-processor change log — the one source both the notice and the page read.

Issue #427: `/legal/sub-processors` § 9 and `/legal/dpa` § 8 promise every
customer at least 30 days' notice before a new sub-processor begins processing
their data, and the only mechanism behind that promise was a hand-typed table
row on the page. Nothing told a customer, and nothing measured the 30 days.

This module is the change log as data. Each :class:`ChangelogEntry` is one dated
row of the register's § 10. Two things are derived from it, so that adding the
row and notifying customers are ONE action rather than two
(`docs/decisions.md` §321):

- ``pnpm gen:subprocessor-changelog`` (``scripts/gen_subprocessor_changelog.py``)
  writes the published table, ``frontend/src/lib/legal/
  subprocessorChangelog.generated.ts``; ``check:`` is its CI drift guard.
- ``services/subprocessor_notices`` emails every customer organisation's
  administrators about each entry with ``notice=True``, once per organisation,
  and records when — the sweep behind ``FEOH_SUBPROCESSOR_NOTICES_ENABLED``.

Rules (``validate`` enforces them; the generator refuses to write past a
violation and ``tests/test_subprocessor_changelog.py`` runs it too):

- **Append-only, and an ``id`` never changes.** The notice ledger
  (``subprocessor_notices``) is keyed by ``id``: renaming one re-sends its
  notice to every customer, and deleting one leaves a sent notice describing a
  row the page no longer shows.
- **``notice=True`` is the 30-day class**: a new sub-processor, a replacement,
  a provider moving from optional (§ 3) to engaged for everyone (§ 2), or a
  change to what an existing one receives — the classes § 9 of the page names.
  Such an entry states ``effective_on``, the date the change takes effect, and
  that date is at least :data:`NOTICE_PERIOD_DAYS` after ``published_on``
  unless ``urgent_reason`` says why it is not (the DPA's security/availability
  carve-out, which still notifies, and says so).
- **``notice=False`` is everything else** — a system the customer controls
  (§ 4), an editorial correction, an infrastructure move inside a provider
  already engaged. It carries neither date nor reason.

``published_on`` is the date the row goes on the page. A row is only notified
once that date has arrived, so a row may be committed ahead of its publication
date; the page shows it as soon as the release is deployed, though, so do not
commit one earlier than you mean to announce it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

#: The advance notice the DPA promises, in days. One number, read by the
#: validator, the notice email and the generated page module.
NOTICE_PERIOD_DAYS = 30

#: Where an objection goes. The same mailbox as the frontend's
#: ``CONTACT.privacy`` (``frontend/src/lib/legal/operator.ts``) — the generated
#: module carries this value and a vitest asserts the two agree, so the email
#: and the page cannot name different addresses.
OBJECTION_CONTACT = "privacy@feohledger.com"

#: The day the notice mechanism shipped. A ``notice=True`` entry published
#: before it would be mailed to every organisation retroactively the first time
#: the sweep runs, for a change that already happened without that notice —
#: ``validate`` refuses one, so the historical rows stay ``notice=False``.
NOTICES_SINCE = date(2026, 10, 26)

_ID = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class ChangelogEntry:
    #: Stable key, ``<published_on>-<slug>``. Never renamed (module docstring).
    id: str
    #: The date the row is published on the register.
    published_on: date
    #: The published wording of the row, exactly as the page shows it.
    change: str
    #: Whether this change needs the advance notice (module docstring).
    notice: bool = False
    #: When the change takes effect. Required, and only allowed, with ``notice``.
    effective_on: date | None = None
    #: Why the notice is shorter than :data:`NOTICE_PERIOD_DAYS`, if it is.
    urgent_reason: str | None = None

    @property
    def notice_period_days(self) -> int | None:
        """Days between publication and effect, for a notice entry."""
        if self.effective_on is None:
            return None
        return (self.effective_on - self.published_on).days


#: The register's change log, oldest first. Append new rows at the END.
#:
#: Every row below predates the notice mechanism, and none needed the 30-day
#: notice: the first is the register itself; RDS (7 October) moved the
#: databases from one AWS service to another, in the same region, with no new
#: party and no new data; and the four commerce rows add systems the customer
#: owns and connects (§ 4), which are not our sub-processors at all.
CHANGELOG: tuple[ChangelogEntry, ...] = (
    ChangelogEntry(
        id="2026-09-15-initial-publication",
        published_on=date(2026, 9, 15),
        change="Initial publication of the sub-processor register.",
    ),
    ChangelogEntry(
        id="2026-10-07-rds",
        published_on=date(2026, 10, 7),
        change=(
            "The databases move from the EC2 instance's own disk to Amazon RDS, in the "
            "same region. RDS is added to § 2; the EC2 row no longer holds the databases."
        ),
    ),
    ChangelogEntry(
        id="2026-10-09-woocommerce",
        published_on=date(2026, 10, 9),
        change=(
            "Your own WooCommerce store is added to § 4 as a system you control: a goods "
            "receipt can push stock quantities to it. It is not a sub-processor."
        ),
    ),
    ChangelogEntry(
        id="2026-10-09-square",
        published_on=date(2026, 10, 9),
        change=(
            "Square (Block) is added to § 4 as a commerce platform you can connect: a goods "
            "receipt adds the received quantity to your own Square account's stock."
        ),
    ),
    ChangelogEntry(
        id="2026-10-10-lightspeed",
        published_on=date(2026, 10, 10),
        change=(
            "Lightspeed Retail (X-Series) is added to § 4 as a commerce platform connection: "
            "your own store, receiving received-stock quantities against its own product "
            "identifiers."
        ),
    ),
    ChangelogEntry(
        id="2026-10-10-cin7",
        published_on=date(2026, 10, 10),
        change=(
            "Cin7 Core (Cin7) is added to § 4 as a commerce platform connection: a goods "
            "receipt becomes a stock adjustment in your own Cin7 account, valued at Cin7's "
            "own average cost."
        ),
    ),
)


class ChangelogError(ValueError):
    """The change log breaks one of the module's rules."""


def validate(entries: tuple[ChangelogEntry, ...] = CHANGELOG) -> list[str]:
    """Every rule the change log breaks, as sentences. Empty means valid."""
    problems: list[str] = []
    seen: set[str] = set()
    previous: date | None = None
    for entry in entries:
        where = f"entry {entry.id!r}"
        if not _ID.fullmatch(entry.id):
            problems.append(f"{where}: id must be '<YYYY-MM-DD>-<lowercase-slug>'")
        elif entry.id[:10] != entry.published_on.isoformat():
            problems.append(f"{where}: id must start with its published_on date")
        if entry.id in seen:
            problems.append(f"{where}: duplicate id — the notice ledger is keyed by it")
        seen.add(entry.id)
        if previous is not None and entry.published_on < previous:
            problems.append(f"{where}: out of order — append new rows at the end")
        previous = entry.published_on
        if not entry.change.strip():
            problems.append(f"{where}: change text is empty")

        if not entry.notice:
            if entry.effective_on is not None or entry.urgent_reason is not None:
                problems.append(
                    f"{where}: effective_on / urgent_reason belong to a notice entry only"
                )
            continue
        if entry.published_on < NOTICES_SINCE:
            problems.append(
                f"{where}: a notice entry cannot be published before {NOTICES_SINCE}, "
                "when the notice mechanism began — it would be mailed retroactively"
            )
        if entry.effective_on is None:
            problems.append(f"{where}: a notice entry must state effective_on")
            continue
        days = (entry.effective_on - entry.published_on).days
        reason = (entry.urgent_reason or "").strip()
        if days < NOTICE_PERIOD_DAYS and not reason:
            problems.append(
                f"{where}: takes effect {days} day(s) after publication; the DPA promises "
                f"{NOTICE_PERIOD_DAYS}. Move effective_on to "
                f"{entry.published_on + timedelta(days=NOTICE_PERIOD_DAYS)} or later, or "
                "state the urgent_reason the DPA's security/availability carve-out needs"
            )
        if days >= NOTICE_PERIOD_DAYS and reason:
            problems.append(f"{where}: urgent_reason given for a full-length notice")
        if days < 0:
            problems.append(f"{where}: effective_on is before published_on")
    return problems


def assert_valid(entries: tuple[ChangelogEntry, ...] = CHANGELOG) -> None:
    problems = validate(entries)
    if problems:
        raise ChangelogError("; ".join(problems))


def notice_entries(
    today: date, entries: tuple[ChangelogEntry, ...] = CHANGELOG
) -> list[ChangelogEntry]:
    """The notice entries already published by ``today`` — what is owed."""
    return [e for e in entries if e.notice and e.published_on <= today]


_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def long_date(value: date) -> str:
    """``15 September 2026`` — the legal pages' date style, locale-independent
    (``strftime('%B')`` follows the process locale)."""
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"
