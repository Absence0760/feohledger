from pydantic import BaseModel, Field


class SignupStartRequest(BaseModel):
    company_name: str = Field(..., min_length=2, max_length=255)
    slug: str = Field(..., min_length=3, max_length=30)
    admin_name: str = Field(..., min_length=2, max_length=255)
    admin_email: str = Field(..., min_length=3, max_length=320)
    # hCaptcha response token from the widget.
    captcha_token: str | None = None
    # Email-copy language for the verification + welcome emails: the signup
    # form sends the visitor's UI locale. Any unsupported / absent value falls
    # back to English (normalize_locale) — never a refusal.
    locale: str | None = Field(default=None, max_length=16)


class SignupStartResponse(BaseModel):
    status: str = "verification_email_sent"
    # English, for API clients. The signup page states its own confirmation in
    # the visitor's language from the address it submitted; it does not render
    # this.
    message: str


class SignupCompleteRequest(BaseModel):
    token: str = Field(..., min_length=16, max_length=64)


class SignupCompleteResponse(BaseModel):
    status: str = "provisioned"
    slug: str
    tenant_url: str
    admin_email: str
    # The new admin's single-use set-password link (the same one the welcome
    # email carries). Returned to the browser that just proved it can read the
    # admin's mailbox by opening the verification link — the email goes to
    # that same inbox, so this discloses nothing the email does not
    # (decisions §283). None when the token
    # could not be stored; the admin then uses Forgot password.
    set_password_url: str | None = None
    # The workspace's sign-in page, with the address prefilled.
    sign_in_url: str | None = None
    # True when this link had already been used and its workspace exists: the
    # page is being re-opened (a reload, a second tab, a mail scanner that ran
    # it first). Nothing is re-issued, so `set_password_url` is None — the
    # welcome email holds the one link (decisions §292).
    already_verified: bool = False


class SlugCheckResponse(BaseModel):
    slug: str
    available: bool
    # Why it is unavailable: `reason` is the English sentence, `code` the
    # `utils/slug.SLUG_*` rule a client localizes on — `signup_slug_held` when
    # the address is resting after a workspace closure (decisions §306), so the
    # form says "temporarily unavailable", not "taken". Both None when available.
    reason: str | None = None
    code: str | None = None
