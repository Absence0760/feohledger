"""Per-locale catalogue for outbound transactional email copy.

This is the **server-side** i18n surface — the language we *email a person in*,
keyed to a DB-synced ``locale`` preference on ``User`` / ``VendorUser`` (see
``docs/notifications.md`` § Localized email). It is deliberately SEPARATE from
the frontend's per-device UI locale picker (``frontend/src/lib/i18n/``): the
DB ``locale`` is an account-level "what language to email this person" setting,
and is NEVER read back to drive in-app UI.

Design contract (enforced by ``tests/test_email_catalogue.py``):

- **English (``en``) is the always-present fallback.** Every key exists in the
  English dict; a non-English catalogue may translate a subset, and any missing
  key resolves to the English string — never a crash, never an empty string.
- **An unknown / unsupported locale falls back to English.** ``normalize_locale``
  maps anything not in ``SUPPORTED_EMAIL_LOCALES`` (and ``None``) to ``"en"``.
- **Only copy is localized.** Deep links, brand chrome (handled by the adapter's
  ``apply_brand`` path), money amounts, invoice numbers, and vendor names are
  locale-INDEPENDENT — they're interpolated as ``{placeholder}`` tokens, so a
  translation can reorder them but can't drop or mdistort them.
- **Placeholder-faithful.** Every locale's value for a key carries exactly the
  same ``{placeholder}`` token set as the English value (parity test asserts it).

The catalogue is intentionally PII-free in its *templates*: the strings only
carry neutral structural copy. Callers interpolate PII-free context (invoice
number, vendor name, amount + currency, deep links) — never bank details, tax
IDs, or full addresses (see ``notification_templates`` for the same rule).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The same six-locale starter set the web + mobile catalogues ship (see
# frontend/src/lib/i18n/). Adding a locale here = add it to this tuple + add its
# (partial-or-full) dict to ``_CATALOGUE``; missing keys fall back to English.
SUPPORTED_EMAIL_LOCALES: tuple[str, ...] = ("en", "de", "fr", "es", "pt-BR", "ja")
DEFAULT_LOCALE = "en"

# Exact-match map plus a base-language fallback (e.g. "pt" → "pt-BR", "de-AT" →
# "de") so a slightly-off locale string still lands on the nearest catalogue
# rather than English. Kept tiny + pure — no Babel/CLDR dependency.
_EXACT = {loc.lower(): loc for loc in SUPPORTED_EMAIL_LOCALES}
_BASE = {
    "en": "en",
    "de": "de",
    "fr": "fr",
    "es": "es",
    "pt": "pt-BR",
    "ja": "ja",
}


def normalize_locale(locale: str | None) -> str:
    """Resolve an arbitrary locale string to a supported catalogue locale.

    ``None`` / empty / unknown → ``"en"``. Case-insensitive; accepts ``pt-br``,
    ``PT_BR`` etc. and a base-language fallback (``de-AT`` → ``de``). Never
    raises — a bad value can't break email rendering.
    """
    if not locale:
        return DEFAULT_LOCALE
    raw = str(locale).strip().replace("_", "-")
    if not raw:
        return DEFAULT_LOCALE
    if raw.lower() in _EXACT:
        return _EXACT[raw.lower()]
    base = raw.split("-", 1)[0].lower()
    return _BASE.get(base, DEFAULT_LOCALE)


def is_supported_locale(locale: str | None) -> bool:
    """True only when ``locale`` exactly names a supported catalogue locale.

    Used by the set-locale endpoints to REJECT an unknown value (so the stored
    preference is always a known locale), distinct from ``normalize_locale``
    which silently coerces for rendering.
    """
    return bool(locale) and str(locale) in SUPPORTED_EMAIL_LOCALES


# Message keys. Grouped by the email surface that consumes them. Every key MUST
# exist in the English dict below; the parity test enforces the full set across
# every locale (with English fallback for untranslated keys).
#
# Convention: ``<surface>.<event>.<subject|body>``. Bodies are multi-line
# plaintext; HTML wrappers live at the call site (the brand chrome is applied by
# the adapter). ``{placeholder}`` tokens are filled by the caller and are
# locale-independent.

# --- English source of truth -------------------------------------------------
_EN: dict[str, str] = {
    # Invoice lifecycle notifications (employee + vendor). ``{ref}`` is the
    # PII-free "Invoice <number> (<vendor>)" reference built by the caller;
    # ``{money}`` is the pre-formatted " for <CUR> <amount>" fragment (or "").
    "notif.invoice_assigned.title": "{ref} assigned to you for review",
    "notif.invoice_assigned.body": "{ref}{money} has been assigned to you for review.",
    "notif.invoice_approved.title": "{ref} was approved",
    "notif.invoice_approved.body": "{ref}{money} has been approved.",
    "notif.invoice_rejected.title": "{ref} was rejected",
    "notif.invoice_rejected.body": "{ref}{money} was rejected.",
    "notif.invoice_rejected.reason": " Reason: {reason}",
    "notif.invoice_paid.title": "{ref} was paid",
    "notif.invoice_paid.body": "{ref}{money} has been marked paid.",
    "notif.chat_message.title": "New message on {ref}",
    "notif.chat_message.body": "A new message was posted on {ref}.",
    "notif.chat_message.note": " ({note})",
    # Supplier-chat direct portal-link email (AP posted; supplier replies).
    "chat.portal_link.subject": "{org}: new message on invoice {ref}",
    "chat.portal_link.greeting": "Hi {name},",
    "chat.portal_link.body": "{org} posted a new message on invoice {ref}.",
    "chat.portal_link.cta": "View the conversation and reply:",
    # Signup verification email (pre-account; locale optional from the request).
    "signup.verify.subject": "Verify your FeohLedger workspace",
    "signup.verify.greeting": "Hi {name},",
    "signup.verify.body": (
        "Someone (hopefully you) requested to create the '{slug}' workspace on "
        "FeohLedger. Click the link below to confirm and finish setting "
        "up your tenant:"
    ),
    "signup.verify.expiry": (
        "This link expires in 24 hours. If you didn't request this, you can "
        "safely ignore this email."
    ),
    # Welcome email (sent right after tenant provisioning).
    "signup.welcome.subject": "Your FeohLedger workspace '{slug}' is ready",
    "signup.welcome.greeting": "Hi {name},",
    "signup.welcome.body": "Your workspace is live.",
    "signup.welcome.url_label": "URL",
    "signup.welcome.email_label": "Email",
    "signup.welcome.password_label": "Password",
    "signup.welcome.change_note": (
        "You'll be asked to change your password when you first sign in."
    ),
    "signup.welcome.signoff": "Welcome aboard!",
}

# --- German ------------------------------------------------------------------
_DE: dict[str, str] = {
    "notif.invoice_assigned.title": "{ref} Ihnen zur Prüfung zugewiesen",
    "notif.invoice_assigned.body": "{ref}{money} wurde Ihnen zur Prüfung zugewiesen.",
    "notif.invoice_approved.title": "{ref} wurde genehmigt",
    "notif.invoice_approved.body": "{ref}{money} wurde genehmigt.",
    "notif.invoice_rejected.title": "{ref} wurde abgelehnt",
    "notif.invoice_rejected.body": "{ref}{money} wurde abgelehnt.",
    "notif.invoice_rejected.reason": " Grund: {reason}",
    "notif.invoice_paid.title": "{ref} wurde bezahlt",
    "notif.invoice_paid.body": "{ref}{money} wurde als bezahlt markiert.",
    "notif.chat_message.title": "Neue Nachricht zu {ref}",
    "notif.chat_message.body": "Es wurde eine neue Nachricht zu {ref} gepostet.",
    "notif.chat_message.note": " ({note})",
    "chat.portal_link.subject": "{org}: neue Nachricht zu Rechnung {ref}",
    "chat.portal_link.greeting": "Hallo {name},",
    "chat.portal_link.body": "{org} hat eine neue Nachricht zu Rechnung {ref} gepostet.",
    "chat.portal_link.cta": "Konversation ansehen und antworten:",
    "signup.verify.subject": "Bestätigen Sie Ihren FeohLedger-Arbeitsbereich",
    "signup.verify.greeting": "Hallo {name},",
    "signup.verify.body": (
        "Jemand (hoffentlich Sie) hat die Erstellung des Arbeitsbereichs '{slug}' "
        "bei FeohLedger angefordert. Klicken Sie auf den Link unten, um zu "
        "bestätigen und die Einrichtung Ihres Mandanten abzuschließen:"
    ),
    "signup.verify.expiry": (
        "Dieser Link läuft in 24 Stunden ab. Falls Sie dies nicht angefordert "
        "haben, können Sie diese E-Mail ignorieren."
    ),
    "signup.welcome.subject": "Ihr FeohLedger-Arbeitsbereich '{slug}' ist bereit",
    "signup.welcome.greeting": "Hallo {name},",
    "signup.welcome.body": "Ihr Arbeitsbereich ist aktiv.",
    "signup.welcome.url_label": "URL",
    "signup.welcome.email_label": "E-Mail",
    "signup.welcome.password_label": "Passwort",
    "signup.welcome.change_note": (
        "Sie werden bei der ersten Anmeldung aufgefordert, Ihr Passwort zu ändern."
    ),
    "signup.welcome.signoff": "Willkommen an Bord!",
}

# --- French ------------------------------------------------------------------
_FR: dict[str, str] = {
    "notif.invoice_assigned.title": "{ref} vous a été attribuée pour examen",
    "notif.invoice_assigned.body": "{ref}{money} vous a été attribuée pour examen.",
    "notif.invoice_approved.title": "{ref} a été approuvée",
    "notif.invoice_approved.body": "{ref}{money} a été approuvée.",
    "notif.invoice_rejected.title": "{ref} a été rejetée",
    "notif.invoice_rejected.body": "{ref}{money} a été rejetée.",
    "notif.invoice_rejected.reason": " Motif : {reason}",
    "notif.invoice_paid.title": "{ref} a été payée",
    "notif.invoice_paid.body": "{ref}{money} a été marquée comme payée.",
    "notif.chat_message.title": "Nouveau message sur {ref}",
    "notif.chat_message.body": "Un nouveau message a été publié sur {ref}.",
    "notif.chat_message.note": " ({note})",
    "chat.portal_link.subject": "{org} : nouveau message sur la facture {ref}",
    "chat.portal_link.greeting": "Bonjour {name},",
    "chat.portal_link.body": "{org} a publié un nouveau message sur la facture {ref}.",
    "chat.portal_link.cta": "Voir la conversation et répondre :",
    "signup.verify.subject": "Confirmez votre espace de travail FeohLedger",
    "signup.verify.greeting": "Bonjour {name},",
    "signup.verify.body": (
        "Quelqu'un (vous, on l'espère) a demandé la création de l'espace de "
        "travail « {slug} » sur FeohLedger. Cliquez sur le lien ci-dessous "
        "pour confirmer et terminer la configuration de votre client :"
    ),
    "signup.verify.expiry": (
        "Ce lien expire dans 24 heures. Si vous n'êtes pas à l'origine de cette "
        "demande, vous pouvez ignorer cet e-mail en toute sécurité."
    ),
    "signup.welcome.subject": "Votre espace de travail FeohLedger « {slug} » est prêt",
    "signup.welcome.greeting": "Bonjour {name},",
    "signup.welcome.body": "Votre espace de travail est actif.",
    "signup.welcome.url_label": "URL",
    "signup.welcome.email_label": "E-mail",
    "signup.welcome.password_label": "Mot de passe",
    "signup.welcome.change_note": (
        "Il vous sera demandé de changer votre mot de passe lors de votre première connexion."
    ),
    "signup.welcome.signoff": "Bienvenue à bord !",
}

# --- Spanish -----------------------------------------------------------------
_ES: dict[str, str] = {
    "notif.invoice_assigned.title": "{ref} se le ha asignado para revisión",
    "notif.invoice_assigned.body": "{ref}{money} se le ha asignado para revisión.",
    "notif.invoice_approved.title": "{ref} fue aprobada",
    "notif.invoice_approved.body": "{ref}{money} ha sido aprobada.",
    "notif.invoice_rejected.title": "{ref} fue rechazada",
    "notif.invoice_rejected.body": "{ref}{money} fue rechazada.",
    "notif.invoice_rejected.reason": " Motivo: {reason}",
    "notif.invoice_paid.title": "{ref} fue pagada",
    "notif.invoice_paid.body": "{ref}{money} se ha marcado como pagada.",
    "notif.chat_message.title": "Nuevo mensaje en {ref}",
    "notif.chat_message.body": "Se publicó un nuevo mensaje en {ref}.",
    "notif.chat_message.note": " ({note})",
    "chat.portal_link.subject": "{org}: nuevo mensaje en la factura {ref}",
    "chat.portal_link.greeting": "Hola {name}:",
    "chat.portal_link.body": "{org} publicó un nuevo mensaje en la factura {ref}.",
    "chat.portal_link.cta": "Ver la conversación y responder:",
    "signup.verify.subject": "Verifique su espacio de trabajo de FeohLedger",
    "signup.verify.greeting": "Hola {name}:",
    "signup.verify.body": (
        "Alguien (con suerte, usted) solicitó crear el espacio de trabajo "
        "'{slug}' en FeohLedger. Haga clic en el enlace de abajo para "
        "confirmar y terminar de configurar su inquilino:"
    ),
    "signup.verify.expiry": (
        "Este enlace caduca en 24 horas. Si no solicitó esto, puede ignorar este "
        "correo electrónico con total seguridad."
    ),
    "signup.welcome.subject": "Su espacio de trabajo de FeohLedger '{slug}' está listo",
    "signup.welcome.greeting": "Hola {name}:",
    "signup.welcome.body": "Su espacio de trabajo está activo.",
    "signup.welcome.url_label": "URL",
    "signup.welcome.email_label": "Correo electrónico",
    "signup.welcome.password_label": "Contraseña",
    "signup.welcome.change_note": (
        "Se le pedirá que cambie su contraseña la primera vez que inicie sesión."
    ),
    "signup.welcome.signoff": "¡Bienvenido a bordo!",
}

# --- Portuguese (Brazil) -----------------------------------------------------
_PT_BR: dict[str, str] = {
    "notif.invoice_assigned.title": "{ref} foi atribuída a você para revisão",
    "notif.invoice_assigned.body": "{ref}{money} foi atribuída a você para revisão.",
    "notif.invoice_approved.title": "{ref} foi aprovada",
    "notif.invoice_approved.body": "{ref}{money} foi aprovada.",
    "notif.invoice_rejected.title": "{ref} foi rejeitada",
    "notif.invoice_rejected.body": "{ref}{money} foi rejeitada.",
    "notif.invoice_rejected.reason": " Motivo: {reason}",
    "notif.invoice_paid.title": "{ref} foi paga",
    "notif.invoice_paid.body": "{ref}{money} foi marcada como paga.",
    "notif.chat_message.title": "Nova mensagem em {ref}",
    "notif.chat_message.body": "Uma nova mensagem foi publicada em {ref}.",
    "notif.chat_message.note": " ({note})",
    "chat.portal_link.subject": "{org}: nova mensagem na fatura {ref}",
    "chat.portal_link.greeting": "Olá {name},",
    "chat.portal_link.body": "{org} publicou uma nova mensagem na fatura {ref}.",
    "chat.portal_link.cta": "Veja a conversa e responda:",
    "signup.verify.subject": "Verifique seu espaço de trabalho do FeohLedger",
    "signup.verify.greeting": "Olá {name},",
    "signup.verify.body": (
        "Alguém (esperamos que você) solicitou a criação do espaço de trabalho "
        "'{slug}' no FeohLedger. Clique no link abaixo para confirmar e "
        "concluir a configuração do seu locatário:"
    ),
    "signup.verify.expiry": (
        "Este link expira em 24 horas. Se você não solicitou isso, pode ignorar "
        "este e-mail com segurança."
    ),
    "signup.welcome.subject": "Seu espaço de trabalho do FeohLedger '{slug}' está pronto",
    "signup.welcome.greeting": "Olá {name},",
    "signup.welcome.body": "Seu espaço de trabalho está ativo.",
    "signup.welcome.url_label": "URL",
    "signup.welcome.email_label": "E-mail",
    "signup.welcome.password_label": "Senha",
    "signup.welcome.change_note": ("Você será solicitado a alterar sua senha no primeiro login."),
    "signup.welcome.signoff": "Bem-vindo a bordo!",
}

# --- Japanese ----------------------------------------------------------------
_JA: dict[str, str] = {
    "notif.invoice_assigned.title": "{ref} がレビュー用にあなたに割り当てられました",
    "notif.invoice_assigned.body": "{ref}{money} がレビュー用にあなたに割り当てられました。",
    "notif.invoice_approved.title": "{ref} が承認されました",
    "notif.invoice_approved.body": "{ref}{money} が承認されました。",
    "notif.invoice_rejected.title": "{ref} が却下されました",
    "notif.invoice_rejected.body": "{ref}{money} が却下されました。",
    "notif.invoice_rejected.reason": " 理由: {reason}",
    "notif.invoice_paid.title": "{ref} が支払われました",
    "notif.invoice_paid.body": "{ref}{money} が支払い済みとしてマークされました。",
    "notif.chat_message.title": "{ref} に新しいメッセージがあります",
    "notif.chat_message.body": "{ref} に新しいメッセージが投稿されました。",
    "notif.chat_message.note": "（{note}）",
    "chat.portal_link.subject": "{org}: 請求書 {ref} に新しいメッセージ",
    "chat.portal_link.greeting": "{name} さん、",
    "chat.portal_link.body": "{org} が請求書 {ref} に新しいメッセージを投稿しました。",
    "chat.portal_link.cta": "会話を表示して返信する:",
    "signup.verify.subject": "FeohLedger ワークスペースを確認してください",
    "signup.verify.greeting": "{name} さん、",
    "signup.verify.body": (
        "どなたか（おそらくあなた）が FeohLedger で '{slug}' ワークスペースの"
        "作成をリクエストしました。以下のリンクをクリックして確認し、テナントの"
        "セットアップを完了してください:"
    ),
    "signup.verify.expiry": (
        "このリンクは 24 時間で期限切れになります。心当たりがない場合は、"
        "このメールを無視していただいて構いません。"
    ),
    "signup.welcome.subject": "FeohLedger ワークスペース '{slug}' の準備が整いました",
    "signup.welcome.greeting": "{name} さん、",
    "signup.welcome.body": "ワークスペースが有効になりました。",
    "signup.welcome.url_label": "URL",
    "signup.welcome.email_label": "メール",
    "signup.welcome.password_label": "パスワード",
    "signup.welcome.change_note": "初回サインイン時にパスワードの変更を求められます。",
    "signup.welcome.signoff": "ようこそ！",
}

_CATALOGUE: dict[str, dict[str, str]] = {
    "en": _EN,
    "de": _DE,
    "fr": _FR,
    "es": _ES,
    "pt-BR": _PT_BR,
    "ja": _JA,
}


def translate(key: str, locale: str | None = None, /, **params: object) -> str:
    """Return the localized string for ``key`` in ``locale``, English-filled.

    Resolution order: the requested (normalized) locale → English → the raw key
    (so an entirely unknown key is at least visible, never an empty string).
    ``{placeholder}`` tokens are filled from ``params``; a missing param leaves
    the literal token in place (``str.format_map`` over a defaulting dict) rather
    than raising — copy quirks must never break an email send.
    """
    loc = normalize_locale(locale)
    catalogue = _CATALOGUE.get(loc, _EN)
    template = catalogue.get(key)
    if template is None:
        template = _EN.get(key, key)
    if not params:
        return template
    return template.format_map(_DefaultDict(params))


class _DefaultDict(dict):
    """``str.format_map`` helper that leaves unknown ``{tokens}`` untouched."""

    def __missing__(self, k: str) -> str:  # noqa: D401
        return "{" + k + "}"


def all_keys() -> frozenset[str]:
    """Every message key (the English source of truth defines the full set)."""
    return frozenset(_EN.keys())


def catalogue_for(locale: str) -> dict[str, str]:
    """The raw (possibly partial) dict for a supported locale — test helper."""
    return _CATALOGUE.get(locale, {})
