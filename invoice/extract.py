"""从百度 OCR 的 words_result 中抽取展示字段。"""


def field(data: dict, key: str, default: str = "") -> str:
    """安全取字段并转为字符串；兼容个别旧字段名。"""
    if key == "InvoiceNum":
        val = data.get("InvoiceNum", data.get("InvoiceNo", default))
    else:
        val = data.get(key, default)
    return str(val) if val is not None else default


def display_fields(data: dict) -> dict:
    """抽取台账列表/导出用的关键字段。"""
    return {
        "invoice_num": field(data, "InvoiceNum"),
        "invoice_date": field(data, "InvoiceDate"),
        "purchaser_name": field(data, "PurchaserName"),
        "seller_name": field(data, "SellerName"),
        "total_amount": field(data, "TotalAmount"),
        "total_tax": field(data, "TotalTax"),
    }
