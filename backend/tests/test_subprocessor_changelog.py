"""The sub-processor change log's rules (issue #427, decisions §321).

The change log is the single source of the published § 10 table and of every
notice sent to customers, so its rules are the 30-day promise in executable
form: a notice entry must state when it takes effect, at least 30 days after it
is published, or say why not.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.subprocessor_changelog import (
    CHANGELOG,
    NOTICE_PERIOD_DAYS,
    ChangelogEntry,
    ChangelogError,
    assert_valid,
    long_date,
    notice_entries,
    validate,
)

PUB = date(2026, 11, 2)


def _notice(**kw) -> ChangelogEntry:
    base = dict(
        id="2026-11-02-newco",
        published_on=PUB,
        change="NewCo is added to § 2.",
        notice=True,
        effective_on=PUB + timedelta(days=NOTICE_PERIOD_DAYS),
    )
    base.update(kw)
    return ChangelogEntry(**base)


def test_the_committed_change_log_is_valid():
    assert validate() == []
    assert_valid()


def test_every_committed_id_is_unique_and_dated():
    ids = [e.id for e in CHANGELOG]
    assert len(ids) == len(set(ids))
    assert all(e.id.startswith(e.published_on.isoformat()) for e in CHANGELOG)


def test_a_full_thirty_day_notice_is_valid():
    assert validate((_notice(),)) == []
    assert _notice().notice_period_days == NOTICE_PERIOD_DAYS


def test_a_short_notice_without_a_reason_is_refused():
    short = _notice(effective_on=PUB + timedelta(days=NOTICE_PERIOD_DAYS - 1))
    problems = validate((short,))
    assert len(problems) == 1
    assert "the DPA promises 30" in problems[0]
    with pytest.raises(ChangelogError):
        assert_valid((short,))


def test_a_short_notice_with_an_urgent_reason_is_allowed():
    urgent = _notice(
        effective_on=PUB + timedelta(days=3),
        urgent_reason="The current provider is withdrawing service on 5 November.",
    )
    assert validate((urgent,)) == []


def test_an_urgent_reason_on_a_full_notice_is_refused():
    assert validate((_notice(urgent_reason="no reason needed"),))


def test_a_notice_entry_must_state_when_it_takes_effect():
    assert validate((_notice(effective_on=None),))


def test_an_effective_date_before_publication_is_refused():
    problems = validate((_notice(effective_on=PUB - timedelta(days=1), urgent_reason="x"),))
    assert any("before published_on" in p for p in problems)


def test_a_non_notice_entry_carries_no_dates():
    plain = ChangelogEntry(id="2026-11-02-x", published_on=PUB, change="Editorial.")
    assert validate((plain,)) == []
    bad = ChangelogEntry(id="2026-11-02-x", published_on=PUB, change="Editorial.", effective_on=PUB)
    assert validate((bad,))


def test_duplicate_ids_out_of_order_rows_and_bad_ids_are_refused():
    a = _notice()
    assert any("duplicate" in p for p in validate((a, a)))
    later = _notice(
        id="2026-11-05-later", published_on=date(2026, 11, 5), effective_on=date(2026, 12, 5)
    )
    assert any("out of order" in p for p in validate((later, a)))
    assert validate((_notice(id="NewCo"),))
    assert validate((_notice(id="2026-11-03-newco"),))  # date prefix != published_on
    assert validate((_notice(change="  "),))


def test_notice_entries_are_only_the_published_notice_rows():
    plain = ChangelogEntry(id="2026-11-01-x", published_on=date(2026, 11, 1), change="x")
    future = _notice(
        id="2026-12-01-future", published_on=date(2026, 12, 1), effective_on=date(2026, 12, 31)
    )
    entries = (plain, _notice(), future)
    assert notice_entries(PUB - timedelta(days=1), entries) == []
    assert [e.id for e in notice_entries(PUB, entries)] == ["2026-11-02-newco"]
    assert len(notice_entries(date(2026, 12, 1), entries)) == 2


def test_a_notice_entry_cannot_predate_the_mechanism():
    # It would be mailed to every organisation retroactively on the first tick.
    old = _notice(
        id="2026-10-01-old", published_on=date(2026, 10, 1), effective_on=date(2026, 11, 1)
    )
    assert any("retroactively" in p for p in validate((old,)))


def test_long_date_is_the_legal_pages_style():
    assert long_date(date(2026, 9, 5)) == "5 September 2026"


def _generator():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "gen_subprocessor_changelog.py"
    spec = importlib.util.spec_from_file_location("gen_subprocessor_changelog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_published_change_log_is_generated_from_this_one():
    gen = _generator()
    assert gen.main(["--check"]) == 0
    rendered = gen.render()
    for entry in CHANGELOG:
        assert entry.id in rendered
        assert long_date(entry.published_on) in rendered


def test_the_generator_refuses_an_invalid_change_log(monkeypatch, capsys):
    gen = _generator()
    short = _notice(effective_on=PUB + timedelta(days=1))
    monkeypatch.setattr(gen, "CHANGELOG", (*CHANGELOG, short))
    assert gen.main(["--check"]) == 1
    assert "invalid" in capsys.readouterr().err
