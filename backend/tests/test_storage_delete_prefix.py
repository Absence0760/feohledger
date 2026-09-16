"""`storage.delete_prefix` — the most destructive primitive in the app.

It is the only traversal here: everything else in `storage` addresses one known
key. `tenant_deletion` uses it to sweep a deleted tenant's documents, which is
how `/legal/dpa` § 13's "the object-storage key prefix holding your uploaded
invoices, receipts, contracts and tax forms" is kept.

Its two argument guards are the whole safety story and neither can be exercised
by the callers' tests, which all patch it out: an empty prefix enumerates and
deletes the **entire bucket**, and a prefix without a trailing slash also
matches a sibling whose id merely starts with the same characters. Both are
asserted here, against a mocked boto3 client, along with the paging and count
behaviour a caller's report is built from.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.services import storage


def _client(pages: list[dict]) -> MagicMock:
    """A boto3 stand-in whose paginator yields `pages`."""
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate = MagicMock(return_value=iter(pages))
    client.get_paginator = MagicMock(return_value=paginator)
    client.delete_objects = MagicMock(return_value={})
    return client


@pytest.mark.asyncio
async def test_an_empty_prefix_is_refused():
    """Without this, `delete_prefix("")` lists and deletes the whole bucket —
    every tenant's documents, from a call that looks like a no-op."""
    client = _client([])
    with patch.object(storage, "_get_client", return_value=client):
        for bad in ("", "   "):
            with pytest.raises(ValueError, match="whole bucket"):
                await storage.delete_prefix(bad)
    client.delete_objects.assert_not_called()


@pytest.mark.asyncio
async def test_a_prefix_without_a_trailing_slash_is_refused():
    """`<uuid-a>` is a prefix of nothing else in practice, but the rule cannot
    depend on that: the guard is what makes the prefix mean "this org's folder"
    rather than "every key starting with these characters"."""
    client = _client([])
    with patch.object(storage, "_get_client", return_value=client):
        with pytest.raises(ValueError, match="trailing slash"):
            await storage.delete_prefix("6f1e2c3d")
    client.delete_objects.assert_not_called()


@pytest.mark.asyncio
async def test_it_sweeps_every_page_and_counts_what_it_deleted():
    """The count reaches the operator's written confirmation to the customer,
    so a sweep that stopped after the first page would under-report a deletion
    that was also incomplete."""
    pages = [
        {"Contents": [{"Key": "org/a.pdf"}, {"Key": "org/b.pdf"}]},
        {"Contents": [{"Key": "org/sub/c.pdf"}]},
        {},  # a trailing empty page, which S3 does return
    ]
    client = _client(pages)
    with patch.object(storage, "_get_client", return_value=client):
        removed = await storage.delete_prefix("org/")

    assert removed == 3
    assert client.delete_objects.call_count == 2, "an empty page must not issue a delete"
    sent = [
        obj["Key"]
        for call in client.delete_objects.call_args_list
        for obj in call.kwargs["Delete"]["Objects"]
    ]
    assert sent == ["org/a.pdf", "org/b.pdf", "org/sub/c.pdf"]
    assert all(
        call.kwargs["Bucket"] == storage.settings.s3_bucket
        for call in client.delete_objects.call_args_list
    )


@pytest.mark.asyncio
async def test_a_prefix_with_nothing_under_it_deletes_nothing():
    client = _client([{"Contents": []}])
    with patch.object(storage, "_get_client", return_value=client):
        assert await storage.delete_prefix("org/") == 0
    client.delete_objects.assert_not_called()


@pytest.mark.asyncio
async def test_a_missing_bucket_counts_as_nothing_to_delete():
    """On a fresh machine the bucket is created lazily by the first upload, so a
    tenant that never uploaded a file has no bucket to sweep. That is not an
    error, and must not abort a deletion before it reaches the database."""
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate = MagicMock(
        side_effect=ClientError({"Error": {"Code": "NoSuchBucket"}}, "ListObjectsV2")
    )
    client.get_paginator = MagicMock(return_value=paginator)

    with patch.object(storage, "_get_client", return_value=client):
        assert await storage.delete_prefix("org/") == 0


@pytest.mark.asyncio
async def test_any_other_s3_error_propagates():
    """A denied or throttled sweep must not be mistaken for an empty one — the
    caller would go on to drop the database, and the documents would be left
    with nothing pointing at them."""
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate = MagicMock(
        side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")
    )
    client.get_paginator = MagicMock(return_value=paginator)

    with patch.object(storage, "_get_client", return_value=client):
        with pytest.raises(ClientError):
            await storage.delete_prefix("org/")
