"""The backend resolves "today" in UTC, and stays that way.

`date.today()` uses the SERVER's local timezone. On a UTC container — the
deployed shape — that agrees with `datetime.now(UTC).date()`, so a mixture is
latent rather than live. That is exactly what makes it easy to keep
reintroducing, and it stops being latent the moment anything runs off a non-UTC
host, because these modules derive more than a display value from "today":

  * `api/analytics._commitment_rows` bounds its horizon on it, so the `/cfo`
    chart and a copilot answer built minutes apart can be assembled from
    different row sets for part of each day;
  * `services/cash_flow_plan.compute_plan_id` hashes it, so a plan proposed near
    midnight can 409 as stale against its own enact call;
  * `services/scheduled_reports` slices each report's `period_days` window from
    it, so the emailed snapshot and the API export of the same report disagree
    at the boundary.

`app/utils/dates.utc_today()` is the one definition, and **the scan is now the
whole of `app/`** — no allowlist. It got there in two moves: the conversion
waves (the cash-flow stack, then the AP surfaces whose "today" is a comparison
rather than a label — the discount deadline, the past-due and future-date fraud
flags, the recurring period key that IS the generation idempotency guard, the
regulated `Invoice.approval_date`, plus the stamps and filenames that sit
beside a `datetime.now(UTC)` in the same response), and then the last six
modules that resolved UTC correctly but *inlined* `datetime.now(UTC).date()`
instead of importing the helper.

The allowlist was the right shape while the tree was mixed — it is how a module
got converted, one at a time, without the guard going red on the unconverted
rest. It is the wrong shape now that nothing under `app/` reads the local date:
an opt-in list cannot see a NEW module, and a new module is precisely where the
next `date.today()` arrives. A whole-tree scan has no such gap, and it costs
nothing to maintain.

`test_no_module_inlines_the_utc_today_expression` is the second half. A module
spelling `datetime.now(UTC).date()` is *correct*, so the local-today scan can
never flag it — but it is one careless edit (dropping the `UTC` argument) from
being silently wrong, and the edited line still reads as deliberate. One owner
removes the class rather than policing it.

Every scan in this file reports its result as "no offenders found", which is
also what a scan that looks at nothing reports. Three guards make that
distinguishable — the tree walk must visit at least `MIN_APP_MODULES` files,
the AST matcher must still flag a violation PLANTED in real app source (not
just in a one-line snippet), and the `datetime.now(UTC).date()` text needle
must still occur in the one module allowed to spell it. Without them, a moved
`APP_DIR` or a rewritten helper turns this whole file green and inert.

Shape borrowed from `tests/test_payment_methods.py`'s source scan.
"""

from __future__ import annotations

import ast
import os
import pathlib
import time
from datetime import UTC, date, datetime

import pytest

from app.utils.dates import utc_today

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
TESTS_DIR = pathlib.Path(__file__).resolve().parent

#: Modules exempt from the whole-`app/` scan below. Empty, and it should stay
#: that way: an exemption here is a module that reads the SERVER's local
#: calendar date on purpose, which nothing in a multi-tenant backend has a
#: reason to do. (`scripts/seed*.py` still uses the local date deliberately —
#: demo fixtures on a dev laptop have nothing to reconcile against — but
#: `scripts/` is outside `app/` and outside this scan.)
LOCAL_TODAY_EXEMPT: tuple[str, ...] = ()

#: The one module allowed to spell the expression `utc_today()` wraps.
UTC_TODAY_OWNER = "utils/dates.py"

#: Test modules that anchor their fixtures on the UTC date and must keep doing
#: so. A test is not exempt from this: when the module under test resolves
#: "today" in UTC and the test builds its expectations from the LOCAL date, the
#: two disagree for the whole window each day where the local calendar date
#: differs from UTC's — several hours daily anywhere west of UTC, and the
#: entire working day in Asia-Pacific. CI runners are UTC, so the suite reads
#: green there no matter how wrong the test is; the failure only ever surfaces
#: on a contributor's laptop, as an unreproducible date assertion.
#:
#: Found by running the suite under `TZ=Pacific/Kiritimati` (UTC+14, so the
#: local date is a day AHEAD of UTC), which turns the whole class of bug from a
#: clock-watching flake into a deterministic failure:
#:
#:     TZ=Pacific/Kiritimati pytest -q
#:
#: That is the way to check a new date-sensitive test, and the way this list was
#: seeded: 36 test modules read the local date, but only five compared it
#: against an app-computed UTC value. The rest use it self-consistently
#: (fixture and assertion from the same sample), so they are deliberately NOT
#: listed — converting them would be churn, and this allowlist is opt-in by
#: design, exactly like the app-module list above. Widening it is how a module
#: converts; the two cash-flow plan modules joined on exactly that basis, since
#: `compute_plan_id` hashes the UTC date they build their fixtures around.
UTC_TODAY_TEST_MODULES = (
    # `services/invoice_warnings` past-due + future-invoice-date fraud flags.
    "test_exception_flow.py",
    # The cash-flow copilot's saved plans + plan-vs-actual: `compute_plan_id`
    # hashes the UTC date and the variance labels its buckets from it, so a
    # local-date fixture here 409s its own plan (or mislabels a period) for
    # hours of every day off a UTC host.
    "test_cash_flow_saved_plans.py",
    "test_cash_flow_plan_lifecycle.py",
    # `api/tax` / `services/tax_1099` W-9 received-date stamp.
    "test_tax.py",
    # `api/portal` W-9 self-service stamp — the same column, other surface.
    "test_portal_tax_forms.py",
    # `services/analytics` aging-band boundaries.
    "test_analytics_aging_reconciliation.py",
    # `api/dashboard` upcoming-payment overdue inequality.
    "test_dashboard_aggregations.py",
    # `discount_offers.effective_status_sql` is evaluated against
    # `api/discounts`' own `utc_today()`, so the boundary fixtures here — an
    # offer whose `valid_until` IS today and is therefore still open — were
    # already failing locally after 20:00 EDT, hours before anyone would have
    # called it a timezone bug.
    "test_discounts_api.py",
    # The supplier-portal surface of those same offers — same column, same
    # `utc_today()` boundary, other router, exactly as `test_tax.py` and
    # `test_portal_tax_forms.py` pair up above. Listing only the AP side left
    # `test_decline_still_works_on_the_last_day_of_the_window` seeding
    # `valid_until` from the LOCAL date and asserting the decline succeeds, so
    # it failed on any host west of UTC after 20:00 — which is how it was
    # found.
    "test_portal_discount_offers.py",
)


def local_today_call_lines(source: str, *, filename: str = "<test>") -> list[int]:
    """Line numbers of every local-timezone "today" call in ``source``.

    Matches every spelling that was live in this codebase:

    * ``date.today()`` / ``datetime.today()`` — and aliases like ``_dt.today()``,
      which is how the local import in ``scheduled_reports._materialise_rows``
      spells it (``func.value`` is a ``Name``);
    * ``datetime.date.today()`` — the module-style call ``api/positive_pay`` and
      ``services/positive_pay`` used (``func.value`` is an ``Attribute``). The
      original Name-only check missed this form entirely, so either module could
      have been added to the allowlist above while still reading local time.

    …plus a third spelling nothing in ``app/`` uses yet, and the one a purely
    ``.today()``-shaped guard could never see: ``datetime.now().date()``. A bare
    ``now()`` returns a NAIVE local-time datetime, so ``.date()`` on it is the
    server's local date wearing a name that reads as deliberate.
    ``datetime.now(UTC).date()`` is correct and passes — the tz argument is the
    entire difference, so the check is on the empty call, not the attribute.

    A comment or docstring mentioning ``date.today()`` is never flagged — the
    scan is over the AST, not the text, which is what lets a converted module
    keep explaining why it converted.
    """

    def _is_local_today(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return False
        if node.func.attr == "today":
            # `date.today()` / `datetime.today()` / `_dt.today()` /
            # `datetime.date.today()`.
            return isinstance(node.func.value, ast.Name | ast.Attribute)
        if node.func.attr == "date":
            # `datetime.now().date()` — naive, so local. `now(UTC)` is fine, and
            # so is `.date()` on a tz-aware column (`run.executed_at.date()`).
            inner = node.func.value
            return (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "now"
                and not inner.args
                and not inner.keywords
            )
        return False

    tree = ast.parse(source, filename=filename)
    return [node.lineno for node in ast.walk(tree) if _is_local_today(node)]


def test_utc_today_is_the_utc_calendar_date():
    assert utc_today() == datetime.now(UTC).date()
    assert isinstance(utc_today(), date)


@pytest.mark.parametrize("tz", ["Pacific/Kiritimati", "Pacific/Midway", "Asia/Tokyo", "UTC"])
def test_utc_today_ignores_the_process_timezone(tz):
    """The behavioural half of this file: everything else here is a source scan.

    `Pacific/Kiritimati` is UTC+14 and `Pacific/Midway` UTC-11, so between them
    the local calendar date is a day ahead of / behind UTC for most of the
    24-hour cycle — which is the whole failure mode, reproduced deterministically
    instead of waited for. Whatever the host is set to, `utc_today()` answers the
    UTC date; on the two skewed zones it also, for most of the day, disagrees
    with `date.today()`, which is the disagreement the scans exist to prevent
    anyone reintroducing.

    `TZ` + `tzset()` is process-global, so the original value is restored in a
    `finally` — a leaked timezone would silently re-point every date-sensitive
    test that runs after this one.
    """
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = tz
        time.tzset()
        assert utc_today() == datetime.now(UTC).date()
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.parametrize(
    "snippet",
    [
        "date.today()",
        "datetime.today()",
        "_dt.today()",
        "datetime.date.today()",
        "x = foo or datetime.date.today()",
        # Naive `now()` → a local date, under a name that reads deliberate.
        "datetime.now().date()",
        "datetime.datetime.now().date()",
    ],
)
def test_scanner_catches_every_local_today_spelling(snippet):
    """The scanner's own regression test.

    `datetime.date.today()` slipped past the first version of this guard, which
    is how two Positive Pay modules stayed on local time while the guard read
    green. `datetime.now().date()` is the same trap one step further out — it
    isn't spelled `today` at all. Pin every spelling, whether or not `app/`
    currently contains it: the ones it doesn't are exactly the ones a future
    edit reaches for after the obvious spelling starts failing.
    """
    assert local_today_call_lines(snippet) == [1]


@pytest.mark.parametrize(
    "snippet",
    [
        "utc_today()",
        "datetime.now(UTC).date()",
        "datetime.now(tz=UTC).date()",
        "datetime.now(UTC)",
        # `.date()` on a tz-aware COLUMN is the right way to read a stored
        # instant's calendar day — `services/positive_pay` does exactly this
        # for `run.executed_at`, with utc_today() as the fallback.
        "run.executed_at.date()",
        "# date.today() is wrong here, see the module docstring",
        '"""Never call date.today() in this module."""',
    ],
)
def test_scanner_ignores_the_correct_forms_and_prose(snippet):
    assert local_today_call_lines(snippet) == []


def test_no_module_under_app_reads_the_servers_local_today():
    """Fails on any local-timezone "today" read anywhere under `app/`.

    If this fires, import `utc_today` from `app/utils/dates.py` rather than
    reaching for the local-timezone call — the two agree only while the host
    happens to run UTC, and these modules feed the cash-flow horizon, the
    plan_id hash, the scheduled-report window, the early-pay discount cutoff,
    the recurring-template period key and the regulated `approval_date`, all of
    which must agree with each other on a non-UTC host.

    Whole-tree, deliberately: the allowlist this replaced could not see a
    module nobody had added to it, and a brand-new module is exactly where the
    next `date.today()` shows up.
    """
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        relative = str(path.relative_to(APP_DIR))
        if relative in LOCAL_TODAY_EXEMPT:
            continue
        lines = local_today_call_lines(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(f"{relative}:{line}" for line in lines)

    assert not offenders, (
        f"local-timezone 'today' read at {offenders}. Use "
        "app.utils.dates.utc_today() — `date.today()` (and naive "
        "`datetime.now().date()`) resolve in the SERVER's timezone, which "
        "matches UTC only by accident of where the container runs."
    )


#: Floor on how many modules the whole-`app/` scan must actually walk. The
#: tree holds ~520 and only grows; the number exists so a scan that silently
#: stops finding files (a moved `APP_DIR`, a glob typo, a refactor that nests
#: the package one level deeper) fails as a broken guard instead of passing as
#: a clean tree. A source scan that visits nothing reports no offenders.
MIN_APP_MODULES = 400


def test_the_whole_tree_scan_actually_walks_the_tree():
    """First half of the vacuous-pass guard: the scan must SEE the code.

    Every assertion in this file below the behavioural tests is of the form
    "no offenders found", which is exactly what an empty file list produces.
    """
    files = sorted(APP_DIR.rglob("*.py"))
    assert APP_DIR.is_dir(), f"{APP_DIR} is not a directory — the scans below scan nothing"
    assert len(files) >= MIN_APP_MODULES, (
        f"only {len(files)} modules found under {APP_DIR} (expected >= "
        f"{MIN_APP_MODULES}) — the scans in this file are reporting a clean "
        "tree because they are looking at almost none of it"
    )
    assert (APP_DIR / "utils" / "dates.py").is_file()


def test_the_whole_tree_scan_can_still_see_a_violation_in_real_app_source():
    """Second half: the scan must still RECOGNISE one when it is there.

    The synthetic-snippet tests above prove the AST matcher works on one-line
    strings. This proves it works where it runs — over a real, several-hundred
    line module, parsed the same way, with one local-`today` line planted at a
    known position. Nothing is written to disk; the planted source exists only
    in memory.
    """
    target = APP_DIR / "api" / "cash_flow.py"
    source = target.read_text(encoding="utf-8")
    assert local_today_call_lines(source, filename=str(target)) == [], (
        f"{target.name} already reads the local date — the plant below cannot prove anything"
    )

    planted = source + "\n\n_planted_local_date = date.today()\n"
    found = local_today_call_lines(planted, filename=str(target))
    assert found == [len(planted.splitlines())], (
        "the scanner did not flag a planted `date.today()` in real app source "
        f"(reported {found}) — the whole-tree scan's clean result is not evidence"
    )


def test_the_inline_expression_scan_still_matches_its_owner():
    """The `datetime.now(UTC).date()` scan is a TEXT needle, so it goes
    vacuous the moment its own spelling stops occurring anywhere.

    `utils/dates.py` is the one module allowed to spell it — and therefore the
    proof that the needle still matches something. Were the helper rewritten
    as `datetime.now(tz=UTC).date()`, the scan would report a clean tree while
    every module was free to inline the other spelling.
    """
    needle = "now(UTC).date()"
    owner = (APP_DIR / UTC_TODAY_OWNER).read_text(encoding="utf-8")
    assert needle in owner, (
        f"{UTC_TODAY_OWNER} no longer spells `{needle}` — "
        "test_no_module_inlines_the_utc_today_expression is now scanning for a "
        "string that occurs nowhere, so it can never fail. Re-point the needle "
        "at whatever `utc_today()` now inlines."
    )


def test_the_local_today_exemption_list_is_honest():
    """An exemption must name a file that exists AND actually reads the local
    date.

    The list is empty and meant to stay that way. A stale entry — a module
    that has since been converted, or renamed — would sit there silently
    re-authorising the next `date.today()` to land in that file.
    """
    for relative in LOCAL_TODAY_EXEMPT:
        path = APP_DIR / relative
        assert path.is_file(), f"LOCAL_TODAY_EXEMPT names {relative}, which does not exist"
        assert local_today_call_lines(path.read_text(encoding="utf-8"), filename=str(path)), (
            f"LOCAL_TODAY_EXEMPT still names {relative}, which no longer reads the "
            "local date. Remove the entry — an exemption for a converted module "
            "is a standing permit for the next one."
        )


def test_the_converted_test_module_list_is_not_empty():
    """`UTC_TODAY_TEST_MODULES` drives a parametrised guard, and pytest runs a
    parametrisation over an empty sequence zero times — so an emptied list
    would remove the guard without removing a test."""
    assert len(UTC_TODAY_TEST_MODULES) >= 5


def test_no_module_inlines_the_utc_today_expression():
    """`datetime.now(UTC).date()` has exactly one home.

    Inlining it is *correct*, which is what makes it worth removing: the
    local-today scan above cannot flag it, so a later edit that drops the `UTC`
    argument turns a right line into a wrong one with nothing to catch it and
    nothing in the diff that looks like a timezone decision. Six modules sat in
    that state — `api/api_keys`, `api/bank_reconciliation`, the recurring /
    contract-renewal / discount auto-capture sweeps and the mock financing
    adapter — alongside `api/cash_flow` and the copilot tools that predated the
    helper.
    """
    needle = "now(UTC).date()"
    offenders = [
        str(path.relative_to(APP_DIR))
        for path in sorted(APP_DIR.rglob("*.py"))
        if str(path.relative_to(APP_DIR)) != UTC_TODAY_OWNER
        and needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} inline `datetime.now(UTC).date()`. Import "
        "`utc_today` from app.utils.dates instead — one owner means a "
        "timezone change is one edit, and a dropped `UTC` argument cannot "
        "hide inside a line that already looked deliberate."
    )


@pytest.mark.parametrize("relative", UTC_TODAY_TEST_MODULES)
def test_test_module_never_anchors_on_the_servers_local_today(relative):
    """Fails on any local-timezone "today" read in a converted TEST module.

    If this fires, import `utc_today` from `app/utils/dates.py` in the test
    too. The assertion is only as correct as the clock it samples: the code
    under test reads the UTC date, so a fixture anchored on the local date is a
    day off for part of every day on a non-UTC host — and lands precisely on
    the boundaries these tests exist to pin.

    Reproduce the whole class deterministically with
    `TZ=Pacific/Kiritimati pytest -q` rather than waiting for a clock.
    """
    path = TESTS_DIR / relative
    assert path.exists(), f"{relative} moved — update UTC_TODAY_TEST_MODULES"

    offenders = local_today_call_lines(path.read_text(encoding="utf-8"), filename=str(path))

    assert not offenders, (
        f"{relative} anchors on the server's local 'today' at line(s) "
        f"{offenders}. Use app.utils.dates.utc_today() — the module under test "
        "resolves today in UTC, so a local-date fixture disagrees with it for "
        "hours every day off a UTC host, and CI (which runs UTC) will not tell "
        "you."
    )
