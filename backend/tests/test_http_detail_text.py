"""`utils/http.detail_text` — an `HTTPException.detail` read back as text.

A refusal the clients localize (the GL-chart refusal) is an OBJECT carrying a
stable `code` beside its English `message`; every server-side catcher that
turns a refusal into prose (the exception-agent escalation rationale, the bulk
status skip reason, the email-approval info page) reads it through this, so a
structured detail yields its sentence rather than a stringified dict.
"""

from __future__ import annotations

from app.services.csv_import import ImportRowError
from app.services.gl_chart import ChartRefusal
from app.utils.http import detail_text


def test_a_string_detail_is_itself():
    assert detail_text("Vendor is blocked") == "Vendor is blocked"


def test_a_structured_detail_yields_its_message():
    refusal = ChartRefusal(foreign=("6000",))
    assert detail_text(refusal.body()) == refusal.detail()


def test_a_shape_with_no_text_is_none():
    assert detail_text(None) is None
    assert detail_text({"code": "nope"}) is None
    assert detail_text({"message": 42}) is None
    assert detail_text([{"loc": ["body"], "msg": "bad"}]) is None


def test_a_plain_csv_row_error_serializes_as_row_and_message_only():
    assert ImportRowError(row=3, message="invoice_number is required").to_dict() == {
        "row": 3,
        "message": "invoice_number is required",
    }


def test_a_structured_csv_row_error_carries_the_refusal_beside_its_row():
    body = ChartRefusal(unknown=("9999",)).body()
    out = ImportRowError(row=4, message=body["message"], structured=body).to_dict()
    assert out == {**body, "row": 4}
