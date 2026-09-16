"""S3 Object-Lock audit-shipping adapter.

Each `ship(rows)` call writes a single gzip-compressed JSONL file to S3
under `audit/<tenant>/<YYYY>/<MM>/<DD>/<timestamp>-<uuid>.jsonl.gz`. The
bucket is expected to have Object Lock enabled in **COMPLIANCE** mode with
a default retention period configured by infra (`infra/s3.tf`
`aws_s3_bucket_object_lock_configuration.audit_logs`) — the adapter does
NOT configure Object Lock itself, and deliberately does not stamp
`ObjectLockMode` / `ObjectLockRetainUntilDate` onto each PUT either. The
retention policy has one owner, and it is the infrastructure; duplicating
it here would give two places to change it and one of them would be wrong.

What the adapter owes in return is a check that the bucket it was pointed
at is the bucket the DPA describes. `test_connection()` `head_bucket`s,
reads `get_object_lock_configuration`, and requires the default retention
rule to be COMPLIANCE for at least `audit_shipping_s3_min_retention_days`.
`app/main.py`'s lifespan calls it for every configured adapter and REFUSES
TO BOOT on a False.

**Mode is checked because COMPLIANCE and GOVERNANCE are different
promises.** Under GOVERNANCE a principal holding
`s3:BypassGovernanceRetention` can delete a locked object; under COMPLIANCE
no one can, including the root account, until retention expires. The
published DPA (Annex II, *Write-once archival of audit events*) says
compliance mode, so a GOVERNANCE bucket makes that page false — and it
would have passed, since the check previously read only the
`ObjectLockEnabled` flag and this module's own docstring said "Governance".
A hand-created bucket is exactly how that happens.

`ship()` deliberately does not re-check per batch — that would be an extra
S3 round-trip on every tick. Object Lock cannot be turned off after
creation, though the default retention RULE can be edited afterwards, so
the boot check is a start-up assertion rather than a standing guarantee.

One object per batch is intentional: it keeps the ship atomic (either
the PUT succeeded or it didn't), preserves the natural batch boundary
for auditor replay, and avoids S3's 5GB single-PUT limit — even 500
rows of audit JSON compressed well under 1MB in practice.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import uuid
from datetime import UTC, datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.services.audit_shipping.base import AuditLogRow, AuditShippingAdapter
from app.services.audit_shipping.dispatcher import register_audit_shipping_adapter

logger = logging.getLogger(__name__)


#: The only Object Lock mode this sink accepts. Not configurable on purpose:
#: the DPA publishes compliance mode, so a knob that let a deployment run
#: GOVERNANCE would make a published page false for whoever turned it.
REQUIRED_OBJECT_LOCK_MODE = "COMPLIANCE"


def _retention_days(retention: dict) -> int:
    """Default-retention period in days, from whichever unit S3 reports.

    A rule carries `Days` or `Years`, never both. `Years` is converted at
    365 rather than 365.25 so the conversion can only UNDER-state the
    period — rounding up could pass a bucket that is a day short of the
    floor, and a check that rounds in the generous direction is not a check.
    """
    if "Days" in retention:
        return int(retention["Days"])
    if "Years" in retention:
        return int(retention["Years"]) * 365
    return 0


@register_audit_shipping_adapter("s3_objectlock")
class S3ObjectLockAdapter(AuditShippingAdapter):
    """Ships audit rows as gzipped JSONL into an Object-Lock-enabled bucket.

    Config:
        bucket_name:  Override FEOH_AUDIT_SHIPPING_S3_BUCKET.
        region_name:  AWS region (default us-east-1).
        key_prefix:   Optional prefix under which all objects are written
                      (default: "audit").
    """

    provider_name = "s3_objectlock"

    def __init__(self, config: dict):
        super().__init__(config)
        self.bucket = config.get("bucket_name") or settings.audit_shipping_s3_bucket
        if not self.bucket:
            raise ValueError(
                "S3ObjectLockAdapter requires FEOH_AUDIT_SHIPPING_S3_BUCKET "
                "or bucket_name in config."
            )
        region = config.get("region_name") or "us-east-1"
        self.key_prefix = config.get("key_prefix", "audit").strip("/")
        # endpoint_url=None → real S3; set FEOH_AWS_ENDPOINT_URL for LocalStack.
        self._client = boto3.client(
            "s3", region_name=region, endpoint_url=settings.aws_endpoint_url or None
        )

    # -- private helpers -----------------------------------------------------

    def _make_key(self, rows: list[AuditLogRow]) -> str:
        # Partition by the FIRST row's tenant + UTC date. Batches coming out
        # of the shipper are single-tenant + sorted by created_at, so this
        # gives a stable, sort-friendly key layout.
        first = rows[0]
        day = first.created_at.astimezone(UTC)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return (
            f"{self.key_prefix}/{first.tenant_db}/"
            f"{day.year:04d}/{day.month:02d}/{day.day:02d}/"
            f"{stamp}-{uuid.uuid4().hex[:8]}.jsonl.gz"
        )

    @staticmethod
    def _encode(rows: list[AuditLogRow]) -> bytes:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            for row in rows:
                gz.write((json.dumps(row.to_json()) + "\n").encode("utf-8"))
        return buf.getvalue()

    # -- adapter API ---------------------------------------------------------

    async def ship(self, rows: list[AuditLogRow]) -> None:
        if not rows:
            return

        key = self._make_key(rows)
        body = self._encode(rows)

        def _put():
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/x-ndjson",
                ContentEncoding="gzip",
            )

        try:
            await asyncio.to_thread(_put)
            logger.debug(
                "[audit-shipping:s3] wrote %d row(s) to s3://%s/%s",
                len(rows),
                self.bucket,
                key,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error(
                "[audit-shipping:s3] put_object failed (%d row(s)): %s",
                len(rows),
                exc,
            )
            raise

    async def test_connection(self) -> bool:
        """Verify the bucket is the write-once bucket the DPA describes.

        Three things, each a hard failure the adapter cannot fix at
        runtime: Object Lock is enabled (only possible at bucket creation),
        the default retention rule is COMPLIANCE (GOVERNANCE is a weaker
        promise than the one published), and its period is at least the
        configured floor.

        Returning False here refuses the boot — see `app/main.py`'s
        lifespan. That is the intended outcome: shipping audit evidence to
        a bucket someone can empty is worse than not shipping it, because
        the archive exists and cannot be relied on.
        """

        def _check() -> bool:
            self._client.head_bucket(Bucket=self.bucket)
            try:
                resp = self._client.get_object_lock_configuration(Bucket=self.bucket)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code == "ObjectLockConfigurationNotFoundError":
                    logger.error(
                        "[audit-shipping:s3] bucket %s does not have Object "
                        "Lock enabled; refusing to ship.",
                        self.bucket,
                    )
                    return False
                raise

            config = resp.get("ObjectLockConfiguration", {})
            if config.get("ObjectLockEnabled") != "Enabled":
                logger.error(
                    "[audit-shipping:s3] bucket %s reports Object Lock status "
                    "%r rather than 'Enabled'; refusing to ship.",
                    self.bucket,
                    config.get("ObjectLockEnabled"),
                )
                return False

            retention = config.get("Rule", {}).get("DefaultRetention", {})
            if not retention:
                # Object Lock on with no default rule means every PUT lands
                # unlocked unless the caller stamps a retention itself — and
                # this adapter deliberately does not. The bucket looks
                # compliant and protects nothing.
                logger.error(
                    "[audit-shipping:s3] bucket %s has Object Lock enabled but "
                    "no default retention rule, so shipped objects would be "
                    "deletable; refusing to ship.",
                    self.bucket,
                )
                return False

            mode = retention.get("Mode")
            if mode != REQUIRED_OBJECT_LOCK_MODE:
                logger.error(
                    "[audit-shipping:s3] bucket %s is in Object Lock mode %r, "
                    "not %s. Under GOVERNANCE a principal with "
                    "s3:BypassGovernanceRetention can delete audit evidence, "
                    "which is not the guarantee this sink is documented to "
                    "give; refusing to ship.",
                    self.bucket,
                    mode,
                    REQUIRED_OBJECT_LOCK_MODE,
                )
                return False

            days = _retention_days(retention)
            floor = settings.audit_shipping_s3_min_retention_days
            if days < floor:
                logger.error(
                    "[audit-shipping:s3] bucket %s locks objects for %d day(s), "
                    "under the %d-day floor "
                    "(FEOH_AUDIT_SHIPPING_S3_MIN_RETENTION_DAYS); refusing to "
                    "ship.",
                    self.bucket,
                    days,
                    floor,
                )
                return False

            logger.info(
                "[audit-shipping:s3] bucket %s verified: Object Lock %s, %d day(s).",
                self.bucket,
                mode,
                days,
            )
            return True

        try:
            return await asyncio.to_thread(_check)
        except (BotoCoreError, ClientError):
            return False
