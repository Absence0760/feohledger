# User Management

## Overview

Users are managed per-organization. There is no self-registration — an admin invites users into their organization. All user and role data lives in the control-plane database, shared across tenants.

## Roles

Four roles are available. Users can have multiple roles.

| Role | Description |
|---|---|
| **Admin** | Full access to all features and user management |
| **AP Manager** | Review and approve invoices |
| **AP Clerk** | Upload invoices and enter data |
| **CFO** | Approve high-value invoices and view reports |

Roles are enforced in the frontend UI. The `/api/auth/me` endpoint returns the user's roles, and the frontend restricts visibility and actions based on them.

### Role-Based UI Restrictions

The sidebar (driven by `frontend/src/lib/nav.ts`) keeps high-traffic routes as
direct rows and folds the rest into groups (Procurement / Billing / Insights /
Automation / Governance / Settings) that open a sub-tabbed page. A group row
appears when the role can see ≥1 of its children and lands on the first of
them; the per-page section tabs are filtered by the same per-route `roles` gate
(and suppressed when only one is visible).

**Which role sees which nav row is not restated here.** It is pinned exactly
once, as the literal `NAV_GATES` table in `frontend/src/lib/nav.test.ts`, with
the backend reasoning beside each row in `nav.ts`; `tests-e2e/auth/rbac.spec.ts`
checks the rendered sidebar and section bars against it for each system role.
Earlier copies of that matrix — in this file and in the e2e spec — went stale
independently of the code, which is why there is only one now. The gates follow
each route's backend READ gate per item: Procurement is where that does the most
work — a clerk sees Purchase Orders, Goods Receipts, Requisitions, Intake,
Catalogs and **Chart of Accounts** (`GET /api/gl-accounts` is auth-gated but
role-open, because a clerk coding an invoice has to be able to look a GL code
up) but not Budgets, whose every read is `require_roles(ADMIN, AP_MANAGER,
CFO)`. On Chart of Accounts the write controls (New Account, Sync from ERP) are
gated on `auth.isManager`, matching `require_roles(ADMIN, AP_MANAGER)` on the
write endpoints.

| Feature | Admin | AP Manager | AP Clerk | CFO |
|---|---|---|---|---|
| Invoice: edit fields | Yes | Yes | Yes | Yes |
| Invoice: change status dropdown | Yes | Yes | No | Yes |
| Invoice: submit for review (new) | Yes | Yes | Yes | Yes |
| Invoice: approve/reject | Yes | Yes | No | No |
| Invoice: delete | Yes | Yes | No | Yes |
| Bulk: delete, status change | Yes | Yes | No | Yes |
| Bulk: export | Yes | Yes | Yes | Yes |

Backend API endpoints are role-gated via `Depends(require_roles(...))` in `backend/app/api/deps.py`. The frontend matrix above mirrors what the backend allows. A coverage gate in `backend/tests/test_rbac.py` fails CI if a new endpoint ships without an auth dependency. Full permission matrix in `authentication.md` § RBAC.

## Invite Flow

1. An admin navigates to **Admin > Users** and clicks **+ Invite User**
2. They enter the user's **full name**, **email**, and select **roles**
3. The system generates a **12-character temporary password** (alphanumeric)
4. A confirmation dialog shows the credentials (email + temp password)
5. The admin clicks **Copy to Clipboard** and shares the credentials with the user out-of-band (Slack, email, etc.)
6. The user logs in at `<tenant>.localhost:7777/login` with their email and temp password
7. The user can change their password via the **profile popover** in the sidebar

The temporary password is shown **only once** — it is not stored or retrievable after the dialog is closed.

## User Lifecycle

```
Invited (active) --> Deactivated --> Reactivated --> ...
                         |
                         +--> Deleted (permanent)
```

- **Active**: Can log in and use the system
- **Deactivated**: Cannot log in. Account preserved for audit trail. Can be reactivated.
- **Deleted**: Permanently removed. Use for users who were created by mistake. Prefer deactivation for users who have activity in the system.

**Forced logout is not a lifecycle state.** A role change, a password reset and deactivation each drop the target's live sessions as a side effect. When you want *only* that — a stolen laptop, a shared credential you've just rotated — use `POST /api/admin/users/:id/revoke-sessions`; deactivating and reactivating to achieve it locks the user out for the duration and leaves a suspension in the audit trail that never happened. See [`authentication.md`](authentication.md) § Session management.

## Self-Service

Users can update their own profile via the sidebar profile popover:

- **Name**: Change display name
- **Password**: Change password (requires current password for verification)

Users cannot change their own email or roles.

## API Endpoints

### Admin Endpoints (require authentication)

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/users` | List all users in the organization (`user.manage`) |
| GET | `/api/admin/roles` | List all available roles — the picker `role_names` is chosen from (`user.manage`) |
| GET | `/api/admin/permissions` | The granular-permission catalog, key + label (`user.manage`) |
| POST | `/api/admin/users` | Create a user (returns temp password) (`user.manage`) |
| PATCH | `/api/admin/users/:id` | Update user name, email, roles, active status, or reset password (`user.manage`) |
| POST | `/api/admin/users/:id/revoke-sessions` | Force-log-out a user without changing their account (`user.manage`, org-scoped, idempotent, audited) |
| DELETE | `/api/admin/users/:id` | Permanently delete a user (cannot delete yourself) (`user.manage`) |

`user.manage` defaults to `admin` only — identical to the four endpoints'
prior `require_roles(ROLE_ADMIN)` gate — but an org can grant it to a custom
role via the `/admin/roles` editor to split user administration out of full
`admin`. `POST`/`PATCH`/`DELETE /api/admin/roles` (defining what a role can
grant) stay admin-only; see [`authentication.md`](authentication.md) §
Granular permissions / segregation of duties.

### Self-Service Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/auth/me` | Get current user info |
| PATCH | `/api/auth/me` | Update own name or password |
| GET | `/api/auth/sessions` | List your own live sessions (device / IP / method, current one marked) |
| DELETE | `/api/auth/sessions/:jti` | End one of your own sessions |
| POST | `/api/auth/sessions/revoke-others` | Sign out everywhere except the current session |

### Request/Response Examples

**POST /api/admin/users** (Invite)

Request:
```json
{
  "email": "jane@acme.com",
  "full_name": "Jane Smith",
  "role_names": ["ap_manager", "ap_clerk"]
}
```

Response (201):
```json
{
  "id": "uuid",
  "email": "jane@acme.com",
  "full_name": "Jane Smith",
  "is_active": true,
  "roles": [
    { "id": "uuid", "name": "ap_manager", "description": "Review and approve invoices" },
    { "id": "uuid", "name": "ap_clerk", "description": "Upload invoices and enter data" }
  ],
  "created_at": "2026-04-05T...",
  "temporary_password": "aB3kLm9xPq2R"
}
```

**PATCH /api/auth/me** (Self-service password change)

Request:
```json
{
  "current_password": "old-password",
  "password": "new-password"
}
```

## Data Model

Users and roles are stored in the control-plane database (not per-tenant).

```
organizations
  |-- users (organization_id FK)
        |-- user_roles (junction) --> roles
```

### User Fields

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| email | String | Unique across all orgs |
| full_name | String | Display name |
| hashed_password | String | Bcrypt hash (nullable for SSO-only users) |
| is_active | Boolean | Account status |
| must_change_password | Boolean | Forces a password change on next login (set when admins reset, or on signup) |
| sso_provider | String | OIDC provider label (`okta`, `entra`, `oidc`) — set on first SSO login |
| sso_provider_id | String | Provider's `sub` claim — durable identifier for the user across email changes |
| mfa_secret | String | Base32-encoded TOTP secret. Populated during enrollment, "active" once `mfa_enabled` flips true. |
| mfa_enabled | Boolean | True after the user verifies a TOTP code during enrollment |
| mfa_enrolled_at | Timestamp | When `mfa_enabled` flipped true (audit + reporting) |
| organization_id | UUID | FK to organizations |
| created_at | Timestamp | Account creation time |

## Authentication

- **Method**: JWT Bearer tokens
- **Login**: `POST /api/auth/login` with email + password
- **Token lifetime**: 30 minutes (configurable via `FEOH_ACCESS_TOKEN_EXPIRE_MINUTES`)
- **Token revocation**: Redis-backed blocklist (tokens are blocklisted on logout)
- **Password hashing**: Bcrypt

## Future Considerations

- **Email invitations**: Send invite emails with a magic link or temp password via SMTP (admins currently hand temp passwords to invitees out-of-band)
- **Password reset**: Self-service password reset via email (currently requires admin to reset)
- **Approval thresholds**: Role-based invoice approval limits (e.g., CFO required above $10,000)
- **Segregation of duties**: enforce approver ≠ creator on invoice approve (tracked in roadmap under RBAC)
- **SCIM `/Groups`**: map IdP groups to our `Role` rows (`/Users` is shipped; group sync is not)
- **WebAuthn / passkeys**: TOTP MFA shipped — passkeys are a separate code path tracked in roadmap
