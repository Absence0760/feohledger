"""The DSAR response's typed ``record_counts`` (docs/decisions.md §360).

``/admin/privacy`` summarises an export from these counts, in the reader's
language, instead of walking the loosely-typed bundle or quoting server prose.
These are the pure rules; ``test_privacy.py`` proves each subject type's real
bundle is covered section by section.
"""

from __future__ import annotations

from app.services.privacy_export import (
    DSAR_COUNTED_SECTIONS,
    _capped,
    bundle_record_counts,
)


def counted_sections_of(data: dict) -> set[str]:
    """Every section of a built bundle that holds a countable collection."""

    def countable(value: object) -> bool:
        if isinstance(value, list):
            return True
        return isinstance(value, dict) and isinstance(value.get("total"), int)

    return {key for key, value in data.items() if countable(value)}


def test_a_list_counts_its_length() -> None:
    assert bundle_record_counts({"roles": ["admin", "cfo"]}) == {"roles": 2}


def test_a_capped_collection_counts_its_true_total_not_the_returned_page() -> None:
    capped = _capped([{"id": "1"}, {"id": "2"}], total=1500)
    assert capped["truncated"] is True
    assert bundle_record_counts({"audit_events": capped}) == {"audit_events": 1500}


def test_the_document_manifest_counts_its_total() -> None:
    manifest = {"total": 3, "deleted_on_erasure": 2, "retained_on_erasure": 1, "documents": []}
    assert bundle_record_counts({"documents": manifest}) == {"documents": 3}


def test_an_empty_section_is_zero_and_an_absent_one_is_omitted() -> None:
    assert bundle_record_counts({"chat_messages": _capped([], 0)}) == {"chat_messages": 0}
    assert bundle_record_counts({"profile": {"email": "x@example.test"}}) == {}


def test_profile_shaped_sections_are_never_counted() -> None:
    # `profile`, `vendor`, `activity` and the vendor bundle's legacy `counts`
    # are records of ONE subject (or tallies), not collections.
    bundle = {
        "profile": {"email": "x@example.test"},
        "vendor": {"name": "Acme"},
        "activity": {"audit_actions_authored": 4},
        "counts": {"invoices": 2},
    }
    assert counted_sections_of(bundle) == set()
    assert bundle_record_counts(bundle) == {}


def test_counted_sections_are_unique() -> None:
    assert len(DSAR_COUNTED_SECTIONS) == len(set(DSAR_COUNTED_SECTIONS))
