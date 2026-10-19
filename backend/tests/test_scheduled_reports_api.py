"""Scheduled-report CRUD — `/api/analytics/scheduled-reports`.

The runner in `services/scheduled_reports.py` has always been complete; nothing
under `app/api/` referenced the `ScheduledReport` model, so a row could only be
created by direct SQL, `list_due_schedules` returned `[]` on every tick forever,
and the documented 5-strike auto-disable was a one-way door.

Covered here, end-to-end against the live test tenants:

- create / list / get / patch / delete round-trip;
- validation against the RUNNER's own registries — a `report_type` outside
  `report_export.EXPORTERS` raises on every tick and burns the auto-disable
  without ever sending; an unknown `cadence` silently reschedules as daily;
- the recipient list: shape-checked, de-duped, bounded, at least one;
- RBAC — mutations are admin-only (a schedule is a standing instruction to email
  the tenant's AP spend to an arbitrary address), reads are admin + CFO;
- tenant isolation — tenant B cannot see or fetch tenant A's schedule;
- the audit row per mutation is PII-free: recipient COUNT, never the addresses;
- re-enabling a 5-strike-disabled schedule clears the stale retry marker.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.scheduled_report import ScheduledReport
from app.models.workflow import AuditLog

_BASE = "/api/analytics/scheduled-reports"


def _payload(**overrides) -> dict:
    body = {
        "name": "Weekly AP Register",
        "report_type": "invoice_register",
        "cadence": "weekly",
        "recipients": ["cfo@acme.test"],
        "period_days": 30,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #
async def test_create_list_get_patch_delete_round_trip(realdb):
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(_BASE, json=_payload())
        assert created.status_code == 201, created.text
        row = created.json()
        assert row["name"] == "Weekly AP Register"
        assert row["cadence"] == "weekly"
        assert row["enabled"] is True
        assert row["next_run_at"]  # defaulted to "due on the next tick"
        schedule_id = row["id"]

        listed = await c.get(_BASE)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert schedule_id in {s["id"] for s in body["schedules"]}
        # The client never hardcodes the catalogs.
        assert "invoice_register" in body["report_types"]
        assert set(body["cadences"]) == {"daily", "weekly", "monthly"}

        fetched = await c.get(f"{_BASE}/{schedule_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["id"] == schedule_id

        patched = await c.patch(f"{_BASE}/{schedule_id}", json={"cadence": "daily"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["cadence"] == "daily"
        assert patched.json()["name"] == "Weekly AP Register"  # untouched

        deleted = await c.delete(f"{_BASE}/{schedule_id}")
        assert deleted.status_code == 204, deleted.text

        gone = await c.get(f"{_BASE}/{schedule_id}")
        assert gone.status_code == 404


async def test_the_created_row_is_what_the_runner_would_pick_up(realdb):
    """The whole point of the surface: a row created here is due and visible to
    `list_due_schedules`, which returned `[]` on every tick before."""
    from app.services.scheduled_reports import list_due_schedules

    past = datetime.now(UTC) - timedelta(hours=1)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(_BASE, json=_payload(next_run_at=past.isoformat()))
    assert resp.status_code == 201, resp.text

    mk = realdb.sessionmaker("a")
    async with mk() as db:
        due = await list_due_schedules(db, now=datetime.now(UTC))
    assert str(resp.json()["id"]) in {str(s.id) for s in due}


# --------------------------------------------------------------------------- #
# Validation against the runner's registries
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "override",
    [
        {"report_type": "not_a_report"},
        {"report_type": ""},
        {"cadence": "hourly"},
        {"cadence": "yearly"},
    ],
)
async def test_unknown_report_type_or_cadence_rejected_422(realdb, override):
    """A `report_type` the exporter registry doesn't hold raises `ValueError` on
    every tick and burns the 5-strike auto-disable without ever sending; an
    unknown cadence silently falls back to daily, so a "yearly" row would have
    emailed 365 times a year."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(_BASE, json=_payload(**override))
    assert resp.status_code == 422, resp.text


async def test_patch_cannot_smuggle_an_unknown_cadence(realdb):
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(_BASE, json=_payload())
        schedule_id = created.json()["id"]
        resp = await c.patch(f"{_BASE}/{schedule_id}", json={"cadence": "fortnightly"})
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    "recipients",
    [
        [],
        ["not-an-email"],
        ["ok@x.test", "also bad"],
        ["a@x.test"] * 21,
    ],
)
async def test_recipient_list_is_bounded_and_shape_checked(realdb, recipients):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(_BASE, json=_payload(recipients=recipients))
    assert resp.status_code == 422, resp.text


async def test_duplicate_recipients_are_deduped_not_rejected(realdb):
    """A duplicate would double-send the same CSV to the same person every
    period — de-duping is the useful behaviour, a 422 is just friction."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            _BASE, json=_payload(recipients=["cfo@acme.test", "CFO@acme.test", "ap@acme.test"])
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["recipients"] == ["cfo@acme.test", "ap@acme.test"]


async def test_a_rejected_recipient_message_names_no_address(realdb):
    """OUR validator message must not name the offending address.

    FastAPI's own 422 envelope echoes the caller's `input` back — that is the
    caller's own submission returned to the caller, not a disclosure of anyone
    else's data, and it is platform-wide behaviour (there is no
    `RequestValidationError` handler in `app/main.py`). What is in our control
    is the `msg` we raise, which is what gets surfaced in a UI toast and copied
    into a support ticket. It stays generic.
    """
    async with realdb.client(key="a", role="admin") as c:
        # Malformed (double `@`) so it is REJECTED — the point is what our own
        # message says back.
        resp = await c.post(_BASE, json=_payload(recipients=["secret.person@@private.test"]))
    assert resp.status_code == 422
    messages = " ".join(d.get("msg", "") for d in resp.json()["detail"])
    assert "secret.person" not in messages
    assert "valid email addresses" in messages


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["ap_manager", "ap_clerk", "cfo"])
async def test_mutations_are_admin_only(realdb, role):
    """A schedule is a standing instruction to email a CSV of the tenant's AP
    spend to an arbitrary address, with no review of any individual send — a
    data-egress control, above the read gate the rest of /analytics uses."""
    async with realdb.client(key="a", role=role) as c:
        resp = await c.post(_BASE, json=_payload())
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("role", ["admin", "cfo"])
async def test_reads_allow_admin_and_cfo(realdb, role):
    async with realdb.client(key="a", role=role) as c:
        resp = await c.get(_BASE)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("role", ["ap_manager", "ap_clerk"])
async def test_reads_refuse_the_operational_roles(realdb, role):
    async with realdb.client(key="a", role=role) as c:
        resp = await c.get(_BASE)
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #
async def test_a_schedule_is_invisible_to_another_tenant(realdb):
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(_BASE, json=_payload(name="Tenant A Only"))
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]

    async with realdb.client(key="b", role="admin") as c:
        listed = await c.get(_BASE)
        fetched = await c.get(f"{_BASE}/{schedule_id}")
        patched = await c.patch(f"{_BASE}/{schedule_id}", json={"enabled": False})

    assert listed.status_code == 200
    assert "Tenant A Only" not in {s["name"] for s in listed.json()["schedules"]}
    assert fetched.status_code == 404
    assert patched.status_code == 404


# --------------------------------------------------------------------------- #
# Audit rows — PII-free
# --------------------------------------------------------------------------- #
async def test_every_mutation_audits_the_count_never_the_addresses(realdb):
    addresses = ["cfo@private.test", "controller@private.test"]
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(_BASE, json=_payload(recipients=addresses))
        schedule_id = created.json()["id"]
        await c.patch(f"{_BASE}/{schedule_id}", json={"period_days": 7})
        await c.delete(f"{_BASE}/{schedule_id}")

    mk = realdb.sessionmaker("a")
    async with mk() as db:
        rows = list(
            (await db.execute(select(AuditLog).where(AuditLog.entity_id == uuid.UUID(schedule_id))))
            .scalars()
            .all()
        )

    actions = {r.action for r in rows}
    assert actions == {
        "scheduled_report.created",
        "scheduled_report.updated",
        "scheduled_report.deleted",
    }
    for row in rows:
        blob = str(row.details)
        for address in addresses:
            assert address not in blob, f"recipient PII in the append-only trail: {row.action}"
        assert row.details.get("recipient_count") == 2
    updated = next(r for r in rows if r.action == "scheduled_report.updated")
    assert updated.details["fields_changed"] == ["period_days"]


async def test_full_form_save_names_only_the_fields_that_differ(realdb):
    """The edit form resends every field; the trail must name only what changed
    (#575), and a save that changes nothing writes no `updated` row at all."""
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(_BASE, json=_payload())
        schedule_id = created.json()["id"]
        try:
            same = await c.patch(f"{_BASE}/{schedule_id}", json=_payload())
            assert same.status_code == 200, same.text
            renamed = await c.patch(f"{_BASE}/{schedule_id}", json=_payload(name="Renamed"))
            assert renamed.status_code == 200, renamed.text
        finally:
            await c.delete(f"{_BASE}/{schedule_id}")

    async with realdb.sessionmaker("a")() as db:
        rows = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == uuid.UUID(schedule_id),
                        AuditLog.action == "scheduled_report.updated",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [r.details["fields_changed"] for r in rows] == [["name"]]


# --------------------------------------------------------------------------- #
# Re-enabling a 5-strike-disabled schedule
# --------------------------------------------------------------------------- #
async def test_re_enabling_clears_the_stale_retry_marker(realdb):
    """`_mark_failure` reads the `[retry N]` prefix off `last_run_error` to
    count consecutive failures. Re-enabling without clearing it means the very
    next failure lands at retry 6 and disables the row again immediately —
    indistinguishable, to the operator, from the re-enable not working."""
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(_BASE, json=_payload())
        schedule_id = uuid.UUID(created.json()["id"])

    mk = realdb.sessionmaker("a")
    async with mk() as db:
        row = (
            await db.execute(select(ScheduledReport).where(ScheduledReport.id == schedule_id))
        ).scalar_one()
        row.enabled = False
        row.last_run_status = "failure"
        row.last_run_error = "[retry 5] email failed: SMTPException"
        await db.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(f"{_BASE}/{schedule_id}", json={"enabled": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True
    assert resp.json()["last_run_status"] is None
    assert resp.json()["last_run_error"] is None
