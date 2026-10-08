"""Pydantic schemas for the notification center + per-user preferences."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.api.pagination import PageMeta
from app.models.notification import NOTIFICATION_EVENT_TYPES


class NotificationResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    entity_type: str
    entity_id: uuid.UUID | None = None
    title: str
    body: str | None = None
    read_at: datetime | None = None
    created_at: datetime


class NotificationListResponse(PageMeta):
    """Paginated list of the current user's notifications.

    `unread` is the total unread count for the user (independent of the page
    window), so the sidebar badge can use the same response.
    """

    items: list[NotificationResponse]
    total: int
    unread: int


class ChannelPrefs(BaseModel):
    """Per-event delivery channels."""

    email: bool = True
    in_app: bool = True


class NotificationPrefs(BaseModel):
    """The full preference map: one ChannelPrefs per notifiable event type.

    **Must cover every entry in `NOTIFICATION_EVENT_TYPES`.** A key missing here
    isn't inert: `notification_dispatch.resolve_prefs` defaults an unknown event
    to **on**, so an event the schema doesn't enumerate is one the user cannot
    turn off. That is how `chat_message` came to email the AP team on every
    supplier-portal message with no opt-out, while `contract_renewal_due` and
    `cash_shortfall_projected` were equally unmutable.

    Drift guard: `tests/test_notification_prefs_roster.py`.
    """

    invoice_assigned: ChannelPrefs = Field(default_factory=ChannelPrefs)
    invoice_approved: ChannelPrefs = Field(default_factory=ChannelPrefs)
    invoice_rejected: ChannelPrefs = Field(default_factory=ChannelPrefs)
    invoice_paid: ChannelPrefs = Field(default_factory=ChannelPrefs)
    contract_renewal_due: ChannelPrefs = Field(default_factory=ChannelPrefs)
    chat_message: ChannelPrefs = Field(default_factory=ChannelPrefs)
    cash_shortfall_projected: ChannelPrefs = Field(default_factory=ChannelPrefs)
    ai_invoice_usage: ChannelPrefs = Field(default_factory=ChannelPrefs)


class NotificationPrefsUpdate(BaseModel):
    """Partial update — only the supplied event types are changed.

    Same roster obligation as `NotificationPrefs`: an event type absent here is
    one a user can read but never change.
    """

    invoice_assigned: ChannelPrefs | None = None
    invoice_approved: ChannelPrefs | None = None
    invoice_rejected: ChannelPrefs | None = None
    invoice_paid: ChannelPrefs | None = None
    contract_renewal_due: ChannelPrefs | None = None
    chat_message: ChannelPrefs | None = None
    cash_shortfall_projected: ChannelPrefs | None = None
    ai_invoice_usage: ChannelPrefs | None = None


class DeviceTokenRegister(BaseModel):
    """`POST /api/notifications/device-token` body — registers (or
    re-registers) the caller's current push token for one platform.

    Registration only. There is no server-side push-SENDING path yet (no
    Firebase Admin SDK adapter exists in this codebase) — this just persists
    the token for whenever that's built. At most one token per platform per
    user: a fresh registration replaces whatever was stored, since FCM tokens
    rotate and a stale one should be overwritten, not accumulated.
    """

    token: str = Field(min_length=1, max_length=4096)
    platform: Literal["ios", "android"]


class DeviceTokenResponse(BaseModel):
    platform: Literal["ios", "android"]
    updated_at: datetime


class MarkReadResponse(BaseModel):
    id: uuid.UUID
    read_at: datetime | None = None


class ReadAllResponse(BaseModel):
    updated: int


class UnreadCountResponse(BaseModel):
    unread: int


# Re-export for callers that want the canonical event list.
__all__ = [
    "NotificationResponse",
    "NotificationListResponse",
    "ChannelPrefs",
    "NotificationPrefs",
    "NotificationPrefsUpdate",
    "DeviceTokenRegister",
    "DeviceTokenResponse",
    "MarkReadResponse",
    "ReadAllResponse",
    "UnreadCountResponse",
    "NOTIFICATION_EVENT_TYPES",
]
