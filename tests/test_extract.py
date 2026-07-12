"""字段抽取测试。"""

from invoice import extract


def test_field_basic():
    assert extract.field({"PurchaserName": "星辰集团"}, "PurchaserName") == "星辰集团"


def test_field_default():
    assert extract.field({}, "SellerName") == ""
    assert extract.field({}, "SellerName", "-") == "-"


def test_field_invoicenum_fallback():
    # 兼容旧字段名 InvoiceNo
    assert extract.field({"InvoiceNo": "123"}, "InvoiceNum") == "123"


def test_field_none_value():
    assert extract.field({"TotalTax": None}, "TotalTax", "-") == "-"


def test_field_stringify_number():
    assert extract.field({"TotalAmount": 1000.5}, "TotalAmount") == "1000.5"


def test_display_fields():
    data = {"InvoiceNum": "N1", "PurchaserName": "A", "SellerName": "B",
            "TotalAmount": "100", "TotalTax": "13", "InvoiceDate": "2026年"}
    d = extract.display_fields(data)
    assert d["invoice_num"] == "N1"
    assert d["purchaser_name"] == "A"
    assert d["seller_name"] == "B"
    assert set(d) == {"invoice_num", "invoice_date", "purchaser_name",
                      "seller_name", "total_amount", "total_tax"}
