"""First-run setup progress for an organization, derived on read.

The dashboard's setup checklist and the `/organization` "Getting started"
panel tick a step only when the app already holds a fact that says it was
done. There is no tracking table and no "mark as done" write: every flag
below is recomputed from data some other feature owns, so a step cannot be
ticked by visiting a page and cannot drift from the setting it describes
(`docs/decisions.md` §315).

Each flag is deliberately narrow, and named for the fact it reads rather than
for an intention nobody recorded:

- ``company`` — the Company Profile carries an address, phone, website or a
  tax / registration number. The organization's NAME does not count: signup
  sets it, so it says nothing about whether anyone filled the profile in.
- ``users`` — the organization has more than one active user. Signup creates
  exactly one (the admin), so a second one is someone being invited.
- ``security`` — the organization requires a second factor at sign-in, or has
  single sign-on switched on (where the identity provider owns MFA). Whether
  an admin merely LOOKED at the panel is not knowable, so it is not claimed.
- ``erp`` — a real ERP is selected (anything but the local ``mock`` adapter),
  the same predicate the ERP plan gate keys on.
- ``vendors`` — at least one vendor exists.
- ``workflow`` — the approval workflow has been changed since it was created:
  a saved version, an edited definition, or a second definition. Every tenant
  is provisioned with a default workflow, so "one exists" would always be
  true; "someone saved it" is the honest reading of "reviewed".
- ``email_intake`` — an intake address is live (a token is minted and the
  deployment has an intake domain).

Organization-wide on purpose: setup is a property of the tenant, not of the
entity currently selected in the switcher, so nothing here reads
``X-Entity-ID``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.user import User
from app.models.vendor import Vendor
from app.models.workflow import WorkflowDefinition, WorkflowVersion
from app.services.email_intake import intake_address_for
from app.services.erp_adapters.dispatcher import erp_config_is_live

# Company Profile fields that only a person can have filled in. `logo_url`
# belongs to Branding; `name` comes from signup.
_COMPANY_PROFILE_FIELDS = (
    "address",
    "phone",
    "website",
    "tax_id",
    "vat_registration_number",
    "companies_house_number",
)


@dataclass(frozen=True)
class OnboardingProgress:
    company: bool
    users: bool
    security: bool
    erp: bool
    vendors: bool
    workflow: bool
    email_intake: bool

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


def _block(settings: object, key: str) -> dict:
    if not isinstance(settings, dict):
        return {}
    value = settings.get(key)
    return value if isinstance(value, dict) else {}


def company_profile_filled(settings: object) -> bool:
    company = _block(settings, "company")
    return any(
        isinstance(company.get(f), str) and company[f].strip() for f in _COMPANY_PROFILE_FIELDS
    )


def sign_in_secured(settings: object) -> bool:
    return (
        _block(settings, "mfa").get("required") is True
        or _block(settings, "sso").get("enabled") is True
    )


async def compute_onboarding(
    *, db: AsyncSession, control_db: AsyncSession, org: Organization
) -> OnboardingProgress:
    """Read every flag for ``org``. Four small queries, no writes."""
    settings = org.settings or {}

    active_users = (
        await control_db.execute(
            select(func.count(User.id)).where(
                User.organization_id == org.id, User.is_active.is_(True)
            )
        )
    ).scalar_one()

    has_vendor = (await db.execute(select(Vendor.id).limit(1))).first() is not None

    definitions = (await db.execute(select(func.count(WorkflowDefinition.id)))).scalar_one()
    workflow_changed = (
        definitions > 1
        or (
            await db.execute(
                select(
                    or_(
                        exists(select(WorkflowVersion.id)),
                        exists(
                            select(WorkflowDefinition.id).where(
                                WorkflowDefinition.updated_at > WorkflowDefinition.created_at
                            )
                        ),
                    )
                )
            )
        ).scalar_one()
    )

    return OnboardingProgress(
        company=company_profile_filled(settings),
        users=active_users > 1,
        security=sign_in_secured(settings),
        erp=erp_config_is_live(_block(settings, "erp")),
        vendors=has_vendor,
        workflow=bool(workflow_changed),
        email_intake=intake_address_for(org) is not None,
    )
