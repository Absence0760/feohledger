"""`fit_name`: the one rule for an ERP display name too long for its column."""

from __future__ import annotations

import pytest

from app.services.erp_record_names import SHORTENED_MARK, fit_name


def test_a_name_that_fits_exactly_is_kept_as_is():
    name = "a" * 255
    assert fit_name(name, 255) == (name, False)


def test_one_character_over_is_cut_to_the_column_and_marked():
    stored, cut = fit_name("a" * 256, 255)
    assert cut
    assert len(stored) == 255
    assert stored == "a" * 254 + SHORTENED_MARK


def test_whitespace_at_the_cut_is_stripped_before_the_mark():
    stored, cut = fit_name("abc" + " " * 10 + "def", 8)
    assert (stored, cut) == ("abc" + SHORTENED_MARK, True)


def test_an_empty_name_is_kept():
    assert fit_name("", 255) == ("", False)


def test_the_same_name_always_fits_the_same_way():
    name = "Professional Services – " + "Consulting " * 30
    assert fit_name(name, 255) == fit_name(name, 255)
    stored, _ = fit_name(name, 255)
    assert fit_name(stored, 255) == (stored, False)


@pytest.mark.parametrize("max_length", [0, len(SHORTENED_MARK)])
def test_a_column_with_no_room_for_the_mark_is_refused(max_length):
    with pytest.raises(ValueError):
        fit_name("abcdef", max_length)
