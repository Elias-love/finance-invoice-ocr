"""发票入账前的确定性控制校验（不等同于税局真伪查验）。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from invoice.extract import field


REQUIRED_FIELDS = {
    "InvoiceNum": "发票号码",
    "InvoiceDate": "开票日期",
    "PurchaserName": "购买方名称",
    "SellerName": "销售方名称",
    "TotalAmount": "合计金额",
    "TotalTax": "合计税额",
}


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def validate_invoice(data: dict) -> dict:
    """返回可审计的控制结果：pass/review + errors/warnings。"""
    errors = []
    warnings = []

    for key, label in REQUIRED_FIELDS.items():
        if not field(data, key).strip():
            errors.append(f"缺少{label}")

    invoice_num = field(data, "InvoiceNum").strip()
    if invoice_num and not re.fullmatch(r"[A-Za-z0-9]{8,20}", invoice_num):
        errors.append("发票号码格式异常")

    amount = _decimal(field(data, "TotalAmount"))
    tax = _decimal(field(data, "TotalTax"))
    total = _decimal(field(data, "AmountInFiguers"))
    if amount is None or tax is None:
        errors.append("金额或税额不是有效数字")
    elif amount < 0 or tax < 0:
        errors.append("金额或税额不能为负数")
    if total is not None and amount is not None and tax is not None:
        if abs((amount + tax) - total) > Decimal("0.02"):
            errors.append("价税合计与金额+税额不一致")
    elif total is None:
        warnings.append("未识别到价税合计，无法完成勾稽")

    for key, label in (
        ("PurchaserRegisterNum", "购买方税号"),
        ("SellerRegisterNum", "销售方税号"),
    ):
        tax_id = field(data, key).strip()
        if tax_id and not re.fullmatch(r"[0-9A-Z]{15,20}", tax_id.upper()):
            warnings.append(f"{label}格式需人工复核")

    return {
        "status": "pass" if not errors else "review",
        "errors": errors,
        "warnings": warnings,
        "authenticity_checked": False,
        "message": (
            "基础校验通过（非税局真伪查验）"
            if not errors
            else "需要人工复核：" + "；".join(errors)
        ),
    }
