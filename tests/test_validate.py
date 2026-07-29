from invoice.validate import validate_invoice


VALID = {
    "InvoiceNum": "24442000000000000001",
    "InvoiceDate": "2026年03月01日",
    "PurchaserName": "深圳星辰集团",
    "PurchaserRegisterNum": "91440300MA5F1CT001",
    "SellerName": "珠海东晟",
    "SellerRegisterNum": "91440400MA5F1CT101",
    "TotalAmount": "1000.00",
    "TotalTax": "130.00",
    "AmountInFiguers": "1130.00",
}


def test_valid_invoice_passes_but_is_not_authenticity_check():
    result = validate_invoice(VALID)
    assert result["status"] == "pass"
    assert result["authenticity_checked"] is False
    assert "非税局" in result["message"]


def test_amount_reconciliation_failure_requires_review():
    result = validate_invoice({**VALID, "AmountInFiguers": "999.00"})
    assert result["status"] == "review"
    assert any("价税合计" in error for error in result["errors"])


def test_missing_required_field_requires_review():
    result = validate_invoice({**VALID, "SellerName": ""})
    assert result["status"] == "review"
    assert any("销售方名称" in error for error in result["errors"])


def test_invalid_invoice_number_requires_review():
    result = validate_invoice({**VALID, "InvoiceNum": "=CMD()"})
    assert result["status"] == "review"
    assert any("号码格式" in error for error in result["errors"])
