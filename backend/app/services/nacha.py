"""NACHA ACH file generation — the bank-file path of the no-rail pilot.

Issue #517 / `docs/decisions.md` §251. FeohLedger does not move money for a
record-only tenant; instead it can write the customer a standard NACHA file of
ACH credits for a draft payment run, which the customer uploads to **its own
bank**. The customer is the originator under its own bank agreement, so none of
the Third-Party Sender / money-transmitter obligations attach to FeohLedger.
After the bank takes the file the customer records the run as paid
(`POST /api/payments/runs/{id}/record-outside`).

Pure: no I/O, no clock. ``build_nacha_file`` is handed every fact and returns the
file text; the endpoint (`api/payments.export_run_nacha`) owns loading, gating
and the audit row.

Format: one file, one batch, credits only (service class 220), CCD (business
payee — the AP default) or PPD (consumer payee). 94-character records, blocking
factor 10, padded with all-9 filler records to a whole block. The file is
**unbalanced** (no offsetting debit to the customer's own account), which is
what most ODFIs expect from a corporate originator; a bank that wants a
balanced file is a per-bank setting to add when a customer needs it.

The file carries vendor account numbers — banking data. It never enters a log
line or an audit row: the audit records counts, totals and the file's SHA-256.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.utils.banking import validate_aba_routing

RECORD_LENGTH = 94
BLOCKING_FACTOR = 10

SEC_CODES: tuple[str, ...] = ("CCD", "PPD")

# Transaction codes for a CREDIT to the receiver's account.
_TXN_CHECKING_CREDIT = "22"
_TXN_SAVINGS_CREDIT = "32"

#: The largest amount one entry's 10-digit cents field can carry.
MAX_ENTRY_AMOUNT = Decimal("99999999.99")

_COMPANY_ENTRY_DESCRIPTION = "PAYABLES"

# NACHA permits upper-case alphanumerics and a small punctuation set; anything
# else (accents folded first) becomes a space.
_DISALLOWED = re.compile(r"[^A-Z0-9 .,&/\-'()]")
_ACCOUNT_RE = re.compile(r"^[A-Z0-9]{1,17}$")


class NachaError(ValueError):
    """A fact the file needs is missing or malformed. Message carries no bank data."""


@dataclass(frozen=True)
class NachaOriginator:
    """The customer's side of the file (``settings.payments.nacha``)."""

    company_name: str  # ≤16
    company_id: str  # exactly 10 — usually "1" + EIN, as the bank assigned it
    odfi_routing: str  # 9-digit ABA of the customer's own bank
    bank_name: str = ""  # ≤23, the immediate-destination name


@dataclass(frozen=True)
class NachaEntry:
    routing_number: str
    account_number: str
    account_type: str  # "checking" | "savings"
    amount: Decimal
    individual_id: str  # what the payee sees — the invoice number
    name: str  # the payee's name


#: The ``settings.payments.nacha`` keys an export needs, in display order.
REQUIRED_ORIGINATOR_FIELDS: tuple[str, ...] = ("company_name", "company_id", "odfi_routing")


def originator_problems(config: dict | None) -> list[str]:
    """Names of the ``settings.payments.nacha`` fields that are missing or malformed.

    Empty list = usable. Field names only — never the values.
    """
    cfg = config if isinstance(config, dict) else {}
    problems: list[str] = []
    name = cfg.get("company_name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 16:
        problems.append("company_name")
    company_id = cfg.get("company_id")
    if not isinstance(company_id, str) or len(company_id) != 10 or not company_id.isascii():
        problems.append("company_id")
    if not isinstance(cfg.get("odfi_routing"), str) or not validate_aba_routing(
        cfg.get("odfi_routing")
    ):
        problems.append("odfi_routing")
    bank_name = cfg.get("bank_name")
    if bank_name is not None and (not isinstance(bank_name, str) or len(bank_name) > 23):
        problems.append("bank_name")
    return problems


def originator_from_settings(config: dict) -> NachaOriginator:
    problems = originator_problems(config)
    if problems:
        raise NachaError(f"payments.nacha is incomplete: {', '.join(problems)}")
    return NachaOriginator(
        company_name=config["company_name"].strip(),
        company_id=config["company_id"],
        odfi_routing="".join(config["odfi_routing"].split()),
        bank_name=(config.get("bank_name") or "").strip(),
    )


def normalize_account_number(raw: object) -> str | None:
    """The DFI account number as NACHA carries it, or ``None`` if unusable."""
    if not isinstance(raw, str):
        return None
    cleaned = re.sub(r"[\s\-]", "", raw).upper()
    return cleaned if _ACCOUNT_RE.match(cleaned) else None


def entry_from_bank_details(
    bank_details: dict | None, *, amount: Decimal, individual_id: str, name: str
) -> NachaEntry | None:
    """An entry from a vendor's ``bank_details``, or ``None`` if it can't carry one.

    Reads the ACH routing field (``routing_number``, the domestic ABA — see
    `payment_adapters/base.resolve_routing_number`) and ``account_number``;
    ``account_type: "savings"`` selects the savings credit code.
    """
    bank = bank_details if isinstance(bank_details, dict) else {}
    routing = bank.get("routing_number")
    if not isinstance(routing, str) or not validate_aba_routing(routing):
        return None
    account = normalize_account_number(bank.get("account_number"))
    if account is None:
        return None
    account_type = (
        "savings" if str(bank.get("account_type") or "").lower() == "savings" else "checking"
    )
    return NachaEntry(
        routing_number="".join(routing.split()),
        account_number=account,
        account_type=account_type,
        amount=amount,
        individual_id=individual_id,
        name=name,
    )


def _text(value: str, width: int) -> str:
    folded = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    cleaned = _DISALLOWED.sub(" ", folded.upper())
    return cleaned[:width].ljust(width)


def _num(value: int, width: int) -> str:
    text = str(value)
    if len(text) > width:
        raise NachaError("a numeric field overflows its NACHA width")
    return text.rjust(width, "0")


def _cents(amount: Decimal) -> int:
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise NachaError("an amount has more than two decimal places")
    return int(cents)


def _record(*parts: str) -> str:
    line = "".join(parts)
    if len(line) != RECORD_LENGTH:  # a builder bug, never user input
        raise AssertionError(f"NACHA record is {len(line)} chars, not {RECORD_LENGTH}")
    return line


def build_nacha_file(
    originator: NachaOriginator,
    entries: list[NachaEntry],
    *,
    sec_code: str,
    effective_date: date,
    created_at: datetime,
    file_id_modifier: str = "A",
) -> str:
    """The NACHA file text (records joined by CRLF, trailing CRLF)."""
    if sec_code not in SEC_CODES:
        raise NachaError(f"sec_code must be one of {', '.join(SEC_CODES)}")
    if not entries:
        raise NachaError("a NACHA file needs at least one entry")
    if not (len(file_id_modifier) == 1 and file_id_modifier.isalnum()):
        raise NachaError("file_id_modifier must be one character A-Z / 0-9")

    odfi8 = originator.odfi_routing[:8]
    company_id = originator.company_id
    batch_number = 1
    records: list[str] = []

    records.append(
        _record(
            "1",
            "01",
            " " + originator.odfi_routing,
            company_id.ljust(10)[:10],
            created_at.strftime("%y%m%d"),
            created_at.strftime("%H%M"),
            file_id_modifier.upper(),
            "094",
            "10",
            "1",
            _text(originator.bank_name, 23),
            _text(originator.company_name, 23),
            " " * 8,
        )
    )
    records.append(
        _record(
            "5",
            "220",
            _text(originator.company_name, 16),
            " " * 20,
            company_id.ljust(10)[:10],
            sec_code,
            _text(_COMPANY_ENTRY_DESCRIPTION, 10),
            " " * 6,
            effective_date.strftime("%y%m%d"),
            " " * 3,
            "1",
            odfi8,
            _num(batch_number, 7),
        )
    )

    entry_hash = 0
    total_credit = 0
    for seq, entry in enumerate(entries, start=1):
        if entry.amount <= 0 or entry.amount > MAX_ENTRY_AMOUNT:
            raise NachaError("an entry amount is outside what one ACH entry can carry")
        cents = _cents(entry.amount)
        entry_hash += int(entry.routing_number[:8])
        total_credit += cents
        records.append(
            _record(
                "6",
                _TXN_SAVINGS_CREDIT if entry.account_type == "savings" else _TXN_CHECKING_CREDIT,
                entry.routing_number[:8],
                entry.routing_number[8],
                entry.account_number.ljust(17),
                _num(cents, 10),
                _text(entry.individual_id, 15),
                _text(entry.name, 22),
                "  ",
                "0",
                odfi8 + _num(seq, 7),
            )
        )

    hash10 = _num(entry_hash % 10**10, 10)
    records.append(
        _record(
            "8",
            "220",
            _num(len(entries), 6),
            hash10,
            _num(0, 12),
            _num(total_credit, 12),
            company_id.ljust(10)[:10],
            " " * 19,
            " " * 6,
            odfi8,
            _num(batch_number, 7),
        )
    )

    # File control counts the filler-padded block total.
    record_count = len(records) + 1
    block_count = -(-record_count // BLOCKING_FACTOR)
    records.append(
        _record(
            "9",
            _num(1, 6),
            _num(block_count, 6),
            _num(len(entries), 8),
            hash10,
            _num(0, 12),
            _num(total_credit, 12),
            " " * 39,
        )
    )
    filler = block_count * BLOCKING_FACTOR - len(records)
    records.extend(["9" * RECORD_LENGTH] * filler)
    return "\r\n".join(records) + "\r\n"
