"""The sanctions providers receive a name and a country. Nothing else.

`/legal/sub-processors` §3.5 makes that a published, specific promise about
three named sub-processors — ComplyAdvantage, Dow Jones and Refinitiv — and a
customer's own Article 30 record is built on it. It was true when it was
written, but true by accident: `SanctionsAdapter.screen_vendor` **accepts**
`vendor_tax_id` and `beneficial_owners`, both callers
(`services/vendor_screening.py`, `services/compliance.py`) populate them
unmasked, and each adapter simply happens not to serialise them. An edit that
started sending a tax ID would leave a published page asserting otherwise, with
nothing failing.

So this pins the outbound body's field set **exactly**, per adapter. Exactly,
not "no tax ID in it": a new field carrying a beneficial owner's date of birth
would pass a substring check against today's fixture and still be a new
personal-data flow to a sub-processor the register does not disclose. Adding a
field here is not forbidden — it is a decision that has to update the register
in the same change, which is what an exact assertion forces someone to notice.

The non-personal provider tuning each body carries (`fuzziness`, `content-set`,
`groupId`, `entityType`, `providerTypes`) is part of the pinned set for the same
reason: the point is that the shape is REVIEWED, not that some fields are exempt.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.sanctions_adapters.complyadvantage import ComplyAdvantageAdapter
from app.services.sanctions_adapters.dowjones import DowJonesAdapter
from app.services.sanctions_adapters.refinitiv import RefinitivAdapter

# What the callers actually pass. Both of these reach `screen_vendor` unmasked
# today, which is exactly why the adapters dropping them needs a guard.
VENDOR_NAME = "Acme Industrial GmbH"
VENDOR_COUNTRY = "de"
VENDOR_TAX_ID = "DE811907980"
BENEFICIAL_OWNERS = [
    {"name": "Ada Kowalski", "country": "PL", "dob": "1974-03-02"},
    {"name": "Bo Lindqvist", "country": "SE", "dob": "1981-11-19"},
]

#: Every value that must not cross the wire, in any field, at any depth.
FORBIDDEN = (
    VENDOR_TAX_ID,
    "Ada Kowalski",
    "Bo Lindqvist",
    "1974-03-02",
    "1981-11-19",
    "PL",
    "SE",
)


def _capture(adapter) -> dict:
    """Run one screening against a mocked transport and return the sent body."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    # Every `_parse` tolerates an empty payload; the response is not what this
    # file is about.
    response.json = MagicMock(return_value={})

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=response)
        asyncio.run(
            adapter.screen_vendor(
                vendor_name=VENDOR_NAME,
                vendor_country=VENDOR_COUNTRY,
                vendor_tax_id=VENDOR_TAX_ID,
                beneficial_owners=BENEFICIAL_OWNERS,
            )
        )
        client.post.assert_awaited_once()
        return client.post.await_args.kwargs["json"]


def _assert_nothing_extra_leaked(body: dict) -> None:
    serialized = json.dumps(body)
    for token in FORBIDDEN:
        # Country codes are matched as whole JSON strings: "DE" is legitimately
        # sent, and a bare substring check for "PL" would hit any word
        # containing it.
        needle = f'"{token}"' if len(token) == 2 else token
        assert needle not in serialized, f"{token!r} reached the provider: {serialized}"


def test_complyadvantage_sends_only_the_search_term_and_country_filter():
    body = _capture(ComplyAdvantageAdapter({"api_key": "k"}))

    assert set(body) == {"search_term", "fuzziness", "filters"}
    assert body["search_term"] == VENDOR_NAME
    assert set(body["filters"]) == {"types", "country_codes"}
    assert body["filters"]["country_codes"] == ["DE"]
    _assert_nothing_extra_leaked(body)


def test_dowjones_sends_only_the_search_term_and_country():
    body = _capture(DowJonesAdapter({"api_key": "k"}))

    assert set(body) == {"data"}
    assert set(body["data"]) == {"type", "attributes"}
    assert set(body["data"]["attributes"]) == {"search-term", "content-set", "country"}
    assert body["data"]["attributes"]["search-term"] == VENDOR_NAME
    assert body["data"]["attributes"]["country"] == "DE"
    _assert_nothing_extra_leaked(body)


def test_refinitiv_sends_only_the_name_and_nationality():
    body = _capture(RefinitivAdapter({"api_key": "k", "group_id": "g"}))

    assert set(body) == {
        "groupId",
        "entityType",
        "providerTypes",
        "caseScreeningState",
        "name",
        "nationality",
    }
    assert body["name"] == VENDOR_NAME
    assert body["nationality"] == "DE"
    _assert_nothing_extra_leaked(body)


@pytest.mark.parametrize(
    "adapter_factory",
    [
        lambda: ComplyAdvantageAdapter({"api_key": "k"}),
        lambda: DowJonesAdapter({"api_key": "k"}),
        lambda: RefinitivAdapter({"api_key": "k", "group_id": "g"}),
    ],
    ids=["complyadvantage", "dowjones", "refinitiv"],
)
def test_a_vendor_with_no_bank_country_sends_no_country_at_all(adapter_factory):
    """The country is optional, and its absence must not become a default.

    It is read from the vendor's BANK DETAILS at both call sites, so a vendor
    with none sends no country — the register says a country code is sent "as a
    filter", not that one is always sent, and substituting a guess would be a
    fabricated data point reaching a third party.
    """
    adapter = adapter_factory()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={})

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=response)
        asyncio.run(
            adapter.screen_vendor(
                vendor_name=VENDOR_NAME,
                vendor_country=None,
                vendor_tax_id=VENDOR_TAX_ID,
                beneficial_owners=BENEFICIAL_OWNERS,
            )
        )
        body = client.post.await_args.kwargs["json"]

    serialized = json.dumps(body)
    assert '"DE"' not in serialized
    # No country-bearing key at all, under any of the three providers' names
    # for it — an empty-string or null country would be a value we invented.
    assert "country" not in serialized
    assert "nationality" not in serialized
    _assert_nothing_extra_leaked(body)
