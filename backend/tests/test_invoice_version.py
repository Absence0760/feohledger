"""`api/invoice_version.matches_loaded_version` — the one comparison behind the
PATCH guard, single approve and bulk approve (decisions §263).

The token is our own `InvoiceResponse.updated_at` round-tripped by the client,
so the comparison must be exact on the instant and indifferent to how the
offset was spelled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from pydantic import TypeAdapter

from app.api.invoice_version import matches_loaded_version

_ROW = datetime(2026, 10, 7, 14, 30, 5, 123456, tzinfo=UTC)
_invoice = SimpleNamespace(updated_at=_ROW)
_parse = TypeAdapter(datetime).validate_python


def test_the_echoed_isoformat_matches():
    assert matches_loaded_version(_invoice, _parse(_ROW.isoformat()))


def test_z_and_plus_zero_spell_the_same_instant():
    assert matches_loaded_version(_invoice, _parse("2026-10-07T14:30:05.123456Z"))


def test_another_offset_for_the_same_instant_matches():
    plus_two = _ROW.astimezone(timezone(timedelta(hours=2)))
    assert matches_loaded_version(_invoice, plus_two)


def test_a_naive_token_is_read_as_utc():
    assert matches_loaded_version(_invoice, _ROW.replace(tzinfo=None))


def test_one_microsecond_is_a_different_version():
    # A client that truncated to milliseconds (a JS `Date` round trip) would
    # land here — which is why every client echoes the string verbatim.
    assert not matches_loaded_version(_invoice, _ROW - timedelta(microseconds=1))
    assert not matches_loaded_version(_invoice, _parse("2026-10-07T14:30:05.123Z"))
