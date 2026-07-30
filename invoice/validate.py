"""发票入账前的确定性控制校验（不等同于税局真伪查验）。"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import config
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


USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)


def valid_uscc(value: str) -> bool:
    """校验 18 位统一社会信用代码的字符集和校验位。"""
    code = re.sub(r"\s+", "", str(value or "")).upper()
    if len(code) != 18 or any(char not in USCC_CHARSET for char in code):
        return False
    total = sum(USCC_CHARSET.index(char) * weight for char, weight in zip(
        code[:17], USCC_WEIGHTS
    ))
    return code[-1] == USCC_CHARSET[(31 - total % 31) % 31]


def _parse_date(value: str) -> date | None:
    text = str(value or "").strip()
    for pattern in ("%Y年%m月%d日", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _line_values(data: dict, key: str) -> list[tuple[str, str]]:
    raw = data.get(key) or []
    if not isinstance(raw, list):
        return []
    values = []
    for position, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            values.append((str(item.get("row", position)), str(item.get("word", ""))))
        else:
            values.append((str(position), str(item)))
    return values


def _sum_lines(data: dict, key: str) -> Decimal | None:
    values = _line_values(data, key)
    if not values:
        return None
    total = Decimal("0")
    for _row, value in values:
        number = _decimal(value)
        if number is None:
            return None
        total += number
    return total


def _validate_line_tax(data: dict, tolerance: Decimal) -> list[str]:
    amounts = dict(_line_values(data, "CommodityAmount"))
    taxes = dict(_line_values(data, "CommodityTax"))
    rates = dict(_line_values(data, "CommodityTaxRate"))
    warnings = []
    for row in sorted(set(amounts) & set(taxes) & set(rates)):
        amount = _decimal(amounts[row])
        tax = _decimal(taxes[row])
        rate_text = rates[row].strip()
        match = re.fullmatch(r"(-?\d+(?:\.\d+)?)%", rate_text)
        if amount is None or tax is None or not match:
            continue
        rate = Decimal(match.group(1)) / Decimal("100")
        # 单行税额通常按分舍入；允许一分钱舍入差。
        if abs(amount * rate - tax) > max(tolerance, Decimal("0.01")):
            warnings.append(f"第{row}行税额与金额×税率不一致")
    return warnings


def _money_text(value: Decimal | None) -> str | None:
    return format(value, ".2f") if value is not None else None


CN_DIGITS = {
    "零": 0, "壹": 1, "贰": 2, "貳": 2, "叁": 3, "參": 3,
    "肆": 4, "伍": 5, "陆": 6, "陸": 6, "柒": 7, "捌": 8, "玖": 9,
}
CN_UNITS = {"拾": 10, "佰": 100, "仟": 1000, "万": 10000, "萬": 10000, "亿": 100000000, "億": 100000000}


def _parse_cn_integer(text: str) -> int | None:
    if not text:
        return 0
    total = section = number = 0
    seen = False
    for char in text:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
            seen = True
        elif char in CN_UNITS:
            unit = CN_UNITS[char]
            seen = True
            if unit < 10000:
                section += (number or 1) * unit
            else:
                section = (section + number) * unit
                total += section
                section = 0
            number = 0
        elif char not in {"人民币", " "}:
            return None
    return total + section + number if seen else None


def parse_chinese_amount(value: str) -> Decimal | None:
    """解析标准人民币大写金额，用于与小写价税合计交叉核对。"""
    text = re.sub(r"\s+", "", str(value or "")).replace("人民币", "")
    match = re.fullmatch(
        r"(?P<int>[零壹贰貳叁參肆伍陆陸柒捌玖拾佰仟万萬亿億]*)(?:元|圆|圓)"
        r"(?:(?P<jiao>[零壹贰貳叁參肆伍陆陸柒捌玖])角)?"
        r"(?:(?P<fen>[零壹贰貳叁參肆伍陆陸柒捌玖])分)?(?:整|正)?",
        text,
    )
    if not match:
        return None
    integer = _parse_cn_integer(match.group("int"))
    if integer is None:
        return None
    jiao = CN_DIGITS.get(match.group("jiao") or "零", 0)
    fen = CN_DIGITS.get(match.group("fen") or "零", 0)
    return Decimal(integer) + Decimal(jiao) / 10 + Decimal(fen) / 100


def validate_invoice(data: dict) -> dict:
    """返回可审计的控制结果：pass/review + errors/warnings。"""
    errors = []
    warnings = []
    verification_scope = str(
        data.get("_verification_scope") or "header"
    ).strip().lower()
    if verification_scope not in {"header", "detail"}:
        verification_scope = "header"

    for key, label in REQUIRED_FIELDS.items():
        if not field(data, key).strip():
            errors.append(f"缺少{label}")

    invoice_num = re.sub(r"\s+", "", field(data, "InvoiceNum")).upper()
    if invoice_num and not re.fullmatch(r"[A-Z0-9]{8,24}", invoice_num):
        errors.append("发票号码格式异常")

    invoice_date = _parse_date(field(data, "InvoiceDate"))
    if field(data, "InvoiceDate").strip() and invoice_date is None:
        errors.append("开票日期格式或日期值异常")
    elif invoice_date and invoice_date > date.today():
        warnings.append("开票日期晚于系统当前日期")

    amount = _decimal(field(data, "TotalAmount"))
    tax = _decimal(field(data, "TotalTax"))
    total = _decimal(field(data, "AmountInFiguers"))
    tolerance = _decimal(config.RECONCILIATION_TOLERANCE) or Decimal("0.01")
    if amount is None or tax is None:
        errors.append("金额或税额不是有效数字")
    elif amount * tax < 0:
        errors.append("金额与税额正负方向不一致")
    if total is not None and amount is not None and tax is not None:
        if (amount + tax < 0) != (total < 0):
            errors.append("价税合计与金额/税额正负方向不一致")
        if abs((amount + tax) - total) > tolerance:
            errors.append("价税合计与金额+税额不一致")
    elif total is None:
        warnings.append("未识别到价税合计，无法完成勾稽")

    line_amount = _sum_lines(data, "CommodityAmount")
    line_tax = _sum_lines(data, "CommodityTax")
    line_amount_ok = (
        None if line_amount is None or amount is None
        else abs(line_amount - amount) <= tolerance
    )
    line_tax_ok = (
        None if line_tax is None or tax is None
        else abs(line_tax - tax) <= tolerance
    )
    observed_detail_issues: list[str] = []
    if line_amount is not None and amount is not None:
        if not line_amount_ok:
            observed_detail_issues.append(
                "明细OCR提取可能不完整：金额汇总与票头合计不一致"
            )
    if line_tax is not None and tax is not None:
        if not line_tax_ok:
            observed_detail_issues.append(
                "明细OCR提取可能不完整：税额汇总与票头合计不一致"
            )
    observed_detail_issues.extend(_validate_line_tax(data, tolerance))

    detail_lists_present = any(
        _line_values(data, key)
        for key in ("CommodityName", "CommodityAmount", "CommodityTax")
    )
    if observed_detail_issues:
        observed_detail_status = "incomplete"
    elif detail_lists_present and line_amount_ok is True and line_tax_ok is True:
        observed_detail_status = "pass"
    elif detail_lists_present:
        observed_detail_status = "partial"
    else:
        observed_detail_status = "unavailable"
    if verification_scope == "detail":
        detail_status = observed_detail_status
        detail_issues = observed_detail_issues
    else:
        # 票头模式只核验入账所需票头字段。多页发票只上传其中一页时，
        # 本页明细天然不等于整票合计，不能据此判定发票异常。
        detail_status = "not_required"
        detail_issues = []
    detail_rows = max(
        (
            len(_line_values(data, key))
            for key in (
                "CommodityName",
                "CommodityAmount",
                "CommodityTax",
                "CommodityTaxRate",
            )
        ),
        default=0,
    )

    amount_words = field(data, "AmountInWords").strip()
    if amount_words and total is not None:
        words_total = parse_chinese_amount(amount_words)
        if words_total is None:
            warnings.append("价税合计大写金额无法解析")
        elif abs(words_total - abs(total)) > tolerance:
            errors.append("价税合计大写与小写金额不一致")

    for key, label in (
        ("PurchaserRegisterNum", "购买方税号"),
        ("SellerRegisterNum", "销售方税号"),
    ):
        tax_id = re.sub(r"\s+", "", field(data, key)).upper()
        if tax_id and not re.fullmatch(r"[0-9A-Z]{15,20}", tax_id):
            warnings.append(f"{label}格式需人工复核")
        elif len(tax_id) == 18 and not valid_uscc(tax_id):
            errors.append(f"{label}统一社会信用代码校验位错误")

    purchaser_tax_id = re.sub(
        r"\s+", "", field(data, "PurchaserRegisterNum")
    ).upper()
    purchaser_name = re.sub(r"\s+", "", field(data, "PurchaserName"))
    if (
        config.EXPECTED_PURCHASER_TAX_IDS
        and purchaser_tax_id
        and purchaser_tax_id not in config.EXPECTED_PURCHASER_TAX_IDS
    ):
        errors.append("购买方税号不在本单位主数据白名单")
    if config.EXPECTED_PURCHASER_NAMES and purchaser_name:
        expected_names = {
            re.sub(r"\s+", "", value)
            for value in config.EXPECTED_PURCHASER_NAMES
        }
        if purchaser_name not in expected_names:
            errors.append("购买方名称不在本单位主数据白名单")

    return {
        "status": "pass" if not errors else "review",
        "errors": errors,
        "warnings": warnings,
        "authenticity_checked": False,
        "checks": {
            "header_reconciliation": (
                total is not None
                and amount is not None
                and tax is not None
                and abs((amount + tax) - total) <= tolerance
            ),
            "line_amount_reconciliation": (
                line_amount_ok
            ),
            "line_tax_reconciliation": (
                line_tax_ok
            ),
            "uppercase_amount_reconciliation": (
                None if not amount_words or total is None
                else (
                    parse_chinese_amount(amount_words) is not None
                    and abs(parse_chinese_amount(amount_words) - abs(total)) <= tolerance
                )
            ),
            "detail_reconciliation": {
                "status": detail_status,
                "observed_status": observed_detail_status,
                "verification_scope": verification_scope,
                "required": verification_scope == "detail",
                "extracted_rows": detail_rows,
                "extracted_amount": _money_text(line_amount),
                "header_amount": _money_text(amount),
                "amount_difference": _money_text(
                    amount - line_amount
                    if amount is not None and line_amount is not None
                    else None
                ),
                "extracted_tax": _money_text(line_tax),
                "header_tax": _money_text(tax),
                "tax_difference": _money_text(
                    tax - line_tax
                    if tax is not None and line_tax is not None
                    else None
                ),
                "issues": detail_issues,
                "observed_issues": observed_detail_issues,
            },
        },
        "verification_scope": verification_scope,
        "detail_issues": detail_issues,
        "message": (
            (
                "票头必要字段校验通过"
                "（明细不参与判定；非税局真伪查验）"
                if verification_scope == "header"
                else "基础校验通过（非税局真伪查验）"
            )
            if not errors and not detail_issues
            else (
                "票头基础校验通过；明细OCR完整性需要复核"
                if not errors
                else "需要人工复核：" + "；".join(errors)
            )
        ),
    }


def manual_approval_blockers(data: dict) -> list[str]:
    """人工批准前仍不可绕过的字段级底线；明细证据异常可凭意见覆盖。"""
    blockers = []
    required = {
        **REQUIRED_FIELDS,
        "AmountInFiguers": "价税合计",
    }
    for key, label in required.items():
        if not field(data, key).strip():
            blockers.append(f"缺少{label}")

    invoice_num = re.sub(r"\s+", "", field(data, "InvoiceNum")).upper()
    if invoice_num and not re.fullmatch(r"[A-Z0-9]{8,24}", invoice_num):
        blockers.append("发票号码格式异常")
    if field(data, "InvoiceDate").strip() and _parse_date(
        field(data, "InvoiceDate")
    ) is None:
        blockers.append("开票日期无效")
    for key, label in (
        ("TotalAmount", "合计金额"),
        ("TotalTax", "合计税额"),
        ("AmountInFiguers", "价税合计"),
    ):
        if field(data, key).strip() and _decimal(field(data, key)) is None:
            blockers.append(f"{label}不是有效数字")
    return blockers
