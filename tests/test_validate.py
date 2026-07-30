from invoice.validate import (
    manual_approval_blockers,
    parse_chinese_amount,
    validate_invoice,
    valid_uscc,
)


VALID = {
    "InvoiceNum": "24442000000000000001",
    "InvoiceDate": "2026年03月01日",
    "PurchaserName": "深圳星辰集团",
    "PurchaserRegisterNum": "9144030005899241X7",
    "SellerName": "珠海东晟",
    "SellerRegisterNum": "91330281739477958A",
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


def test_uscc_checksum_and_uppercase_amount():
    assert valid_uscc("9144030005899241X7")
    assert not valid_uscc("9144030005899241X9")
    assert parse_chinese_amount("壹仟壹佰叁拾圆整") == 1130
    result = validate_invoice({
        **VALID,
        "AmountInWords": "壹仟壹佰叁拾圆整",
    })
    assert result["status"] == "pass"
    assert result["checks"]["uppercase_amount_reconciliation"] is True


def test_line_sum_and_tax_rate_reconciliation():
    result = validate_invoice({
        **VALID,
        "CommodityAmount": [{"row": "1", "word": "1000.00"}],
        "CommodityTax": [{"row": "1", "word": "130.00"}],
        "CommodityTaxRate": [{"row": "1", "word": "13%"}],
    })
    assert result["status"] == "pass"
    assert result["checks"]["line_amount_reconciliation"] is True
    assert result["checks"]["line_tax_reconciliation"] is True

    mismatch = validate_invoice({
        **VALID,
        "CommodityAmount": [{"row": "1", "word": "900.00"}],
        "CommodityTax": [{"row": "1", "word": "117.00"}],
        "CommodityTaxRate": [{"row": "1", "word": "13%"}],
    })
    assert mismatch["status"] == "pass"
    detail = mismatch["checks"]["detail_reconciliation"]
    assert detail["status"] == "incomplete"
    assert detail["extracted_rows"] == 1
    assert detail["amount_difference"] == "100.00"
    assert detail["tax_difference"] == "13.00"
    assert any("明细OCR" in issue for issue in mismatch["detail_issues"])


def test_coherent_red_invoice_is_supported():
    result = validate_invoice({
        **VALID,
        "TotalAmount": "-1000.00",
        "TotalTax": "-130.00",
        "AmountInFiguers": "-1130.00",
    })
    assert result["status"] == "pass"


def test_purchaser_master_data_mismatch_requires_review(monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "EXPECTED_PURCHASER_TAX_IDS",
        {"91110000123456789X"},
    )
    result = validate_invoice(VALID)
    assert result["status"] == "review"
    assert any("主数据白名单" in error for error in result["errors"])


def test_manual_approval_blockers_only_cover_entry_critical_fields():
    assert manual_approval_blockers(VALID) == []
    assert any(
        "价税合计" in reason
        for reason in manual_approval_blockers({**VALID, "AmountInFiguers": ""})
    )
