# Closed workspaces — removing the backups a self-service closure leaves

**When you need this**: weekly, and whenever someone asks whether a closed
workspace is fully gone. An administrator can close an unused workspace from
**Organization → Close workspace** (`POST /api/organization/close`,
`docs/decisions.md` §301). The app deletes the workspace's documents, its
database and its control-plane rows itself. It cannot delete the nightly
**backup dumps**: they live in the backup bucket, which the API has no access to.

`/legal/dpa` § 13 promises those go within **60 days** of the customer's
instruction, and a self-service closure is that instruction. So every closure
is a task for you, and the app keeps the list.

## The queue

```bash
# on the VM (or anywhere with the control-plane database URL)
docker compose … exec -T api python scripts/workspace_closures.py list
```

prints every completed closure whose backups you have not yet marked removed,
oldest first. The oldest is closest to the 60-day deadline.

## For each slug

1. **Remove the backups** with the operator deletion script, which already
   handles a tenant that no longer exists:

   ```bash
   deploy/remove-tenant.sh <slug>
   ```

   It finds no organisation (the closure removed it), says so, and continues
   with the Caddy and backup legs, which key off the slug alone. It removes the
   tenant's Caddy host block if there is one and every version of
   `pg/*/feoh_<slug>.dump` in the backup bucket.

   If it stops with **"nothing found for slug"**, there was nothing to remove:
   the workspace was closed before its first nightly backup and has no host
   block. Go to step 2.

2. **Mark it done**:

   ```bash
   docker compose … exec -T api python scripts/workspace_closures.py mark-cleared <organization-id> --operator <your-handle>
   ```

   The organisation id `list` printed, not the slug, and `--operator` (required)
   is recorded on the row with the time — a handle or a ticket reference, not an
   email address. Exit code 4 means no
   finished closure of that organisation is still owed (already marked,
   mistyped, or a workspace that still exists — check `list`).

   **Until you do this the slug stays taken**, and for 90 days from the
   closure regardless (`utils/slug.CLOSED_SLUG_COOLDOWN`): a new workspace under
   it would get the same `feoh_<slug>` database name, and old bookmarks, IdP
   redirect URIs and invite emails would point at it. The hold is in
   `provision_tenant`, so `scripts/create_tenant.py` and `deploy/add-tenant.sh`
   refuse a held slug too (exit 2).

## What stays, on purpose

- **The `workspace_closures` row** — organisation id, slug, the requesting
  user's id (which no longer resolves), the step-up proof, what was removed and
  when. It holds no address or name. It is the record that the customer asked
  for the deletion and that it happened, and it is what this queue reads.
- **Audit events already shipped to write-once archival.** Where audit shipping
  is on, the closure ships the workspace's whole trail, including its
  `organization.closing` event, before it deletes anything. Those copies cannot
  be deleted before their retention expires, as the DPA discloses.
- **The shared control-plane dump**, which is not selectively editable and ages
  out on the 90-day backup cycle, as the DPA discloses.

## When a closure did not finish

A closure that failed part-way answers the administrator with "did not finish,
try again", and a retry by them completes it. If they have given up, `list`
shows it under "began and did not finish", with the exception class of the
last failure. The database goes first, so a failed drop leaves the workspace
whole; a failure after the drop leaves a workspace that is already unusable. Finish it with the ordinary operator deletion
(`deploy/remove-tenant.sh <slug>`, which is idempotent), then set the row's
status yourself only after confirming nothing is left
(`scripts/delete_tenant.py --slug <slug> --dry-run` exits 4).

## When a customer asks support to close a workspace the page refused

The page refuses any workspace holding records that must be kept (approved
invoices, payments, other users, a paid subscription and the rest of the list
in `docs/user-management.md`). That is the ordinary DPA deletion: confirm the
request is from the customer, offer the export the DPA promises, then
`deploy/remove-tenant.sh <slug>`. Loop in the CISO before deleting a workspace
with financial records.
