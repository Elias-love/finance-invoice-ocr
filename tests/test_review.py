import config
import invoice.review as review_module
from invoice.review import assess_review
from invoice.validate import validate_invoice


VALID = {
    "InvoiceCode": "044001900111",
    "InvoiceCodeConfirm": "044001900111",
    "InvoiceNum": "24442000000000000001",
    "InvoiceNumConfirm": "24442000000000000001",
    "InvoiceDate": "2026年03月01日",
    "PurchaserName": "深圳星辰集团",
    "PurchaserRegisterNum": "9144030005899241X7",
    "SellerName": "珠海东晟",
    "SellerRegisterNum": "91330281739477958A",
    "TotalAmount": "1000.00",
    "TotalTax": "130.00",
    "AmountInFiguers": "1130.00",
}


def test_clean_invoice_can_auto_pass(monkeypatch):
    monkeypatch.setattr(config, "AUTO_PASS_ENABLED", True)
    monkeypatch.setattr(config, "REVIEW_SAMPLE_RATE", 0)
    data = {
        **VALID,
        "_quality": {"status": "pass", "errors": [], "warnings": []},
        "_qr": {
            "status": "verified",
            "matches": ["发票号码", "开票日期", "合计金额"],
            "mismatches": [],
        },
    }
    review = assess_review(data, validate_invoice(data), source_sha256="abc")
    assert review["review_status"] == "auto_pass"
    assert review["risk_level"] == "low"
    assert review["confidence_score"] is None


def test_missing_field_enters_high_risk_review():
    data = {**VALID, "SellerName": ""}
    review = assess_review(data, validate_invoice(data))
    assert review["review_status"] == "pending"
    assert review["risk_level"] == "high"
    assert review["field_checks"]["SellerName"]["status"] == "error"


def test_confirmation_mismatch_requires_review():
    data = {**VALID, "InvoiceNumConfirm": "99999999"}
    review = assess_review(data, validate_invoice(data))
    assert review["review_status"] == "pending"
    assert review["risk_level"] == "high"
    assert "辅助校验" in review["field_checks"]["InvoiceNum"]["reason"]


def test_current_batch_duplicate_requires_review():
    review = assess_review(
        VALID,
        validate_invoice(VALID),
        duplicate=True,
    )
    assert review["review_status"] == "pending"
    assert review["risk_level"] == "high"
    assert "本次识别批次存在相同发票号码" in review["reasons"]


def test_high_value_invoice_requires_review(monkeypatch):
    monkeypatch.setattr(config, "HIGH_VALUE_REVIEW_AMOUNT", "100000")
    data = {
        **VALID,
        "TotalAmount": "100000.00",
        "TotalTax": "13000.00",
        "AmountInFiguers": "113000.00",
    }
    review = assess_review(data, validate_invoice(data))
    assert review["review_status"] == "pending"
    assert review["risk_level"] == "medium"


def test_sampling_key_is_per_invoice_page(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_QR_FOR_AUTO_PASS", False)
    captured = []

    def fake_sampled(key, _rate):
        captured.append(key)
        return False

    monkeypatch.setattr(review_module, "_sampled", fake_sampled)
    assess_review(
        VALID,
        validate_invoice(VALID),
        source_sha256="same-file",
        source_page=7,
    )
    assert captured == [
        "same-file|7|24442000000000000001"
    ]
