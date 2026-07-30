"""发票机器预审与人工复核分流。

百度标准 VAT OCR 接口没有统一的字段级 probability。本模块只根据可审计的
确定性证据分级：字段完整性、格式、价税勾稽、辅助校验码、批内重复、大额阈值
和抽样质检。这里的 ``evidence_grade`` 不是模型置信度，更不是税局验真结果。
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

import config
from invoice.extract import field


FIELD_LABELS = dict(config.REVIEW_FIELDS)
CRITICAL_FIELDS = {
    "InvoiceNum", "InvoiceDate", "PurchaserName", "SellerName",
    "TotalAmount", "TotalTax",
}


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _sampled(source_sha256: str, rate: float) -> bool:
    """用文件哈希做确定性抽样，同一文件每次进入相同分组。"""
    if not source_sha256 or rate <= 0:
        return False
    if rate >= 1:
        return True
    bucket = int(hashlib.sha256(source_sha256.encode()).hexdigest()[:8], 16)
    return bucket / 0xFFFFFFFF < rate


def assess_review(
    data: dict,
    control: dict,
    *,
    duplicate: bool = False,
    source_sha256: str = "",
) -> dict:
    """生成机器预审结果，决定自动通过或进入人工复核队列。"""
    checks: dict[str, dict] = {}
    hard_reasons: list[str] = []
    soft_reasons: list[str] = []

    for key, label in config.REVIEW_FIELDS:
        value = field(data, key).strip()
        if key in CRITICAL_FIELDS and not value:
            checks[key] = {
                "label": label, "status": "error", "reason": "关键字段缺失",
            }
            hard_reasons.append(f"{label}缺失")
        elif value:
            checks[key] = {
                "label": label, "status": "pass", "reason": "已识别",
            }
        else:
            checks[key] = {
                "label": label, "status": "warning", "reason": "未识别，可按业务需要补录",
            }

    invoice_num = field(data, "InvoiceNum").strip()
    if invoice_num and not re.fullmatch(r"[A-Za-z0-9]{8,20}", invoice_num):
        checks["InvoiceNum"] = {
            "label": FIELD_LABELS["InvoiceNum"],
            "status": "error",
            "reason": "号码格式异常",
        }

    for value_key, confirm_key in (
        ("InvoiceNum", "InvoiceNumConfirm"),
    ):
        value = field(data, value_key).strip()
        confirm = field(data, confirm_key).strip()
        if not value or not confirm:
            continue
        if _normalized(value) == _normalized(confirm):
            checks[value_key]["reason"] = "识别值与百度辅助校验值一致"
        else:
            label = FIELD_LABELS[value_key]
            checks[value_key] = {
                "label": label,
                "status": "error",
                "reason": f"识别值与辅助校验值不一致（{confirm}）",
            }
            hard_reasons.append(f"{label}辅助校验不一致")

    amount = _decimal(field(data, "TotalAmount"))
    tax = _decimal(field(data, "TotalTax"))
    total = _decimal(field(data, "AmountInFiguers"))
    if amount is not None and tax is not None and total is not None:
        if abs(amount + tax - total) <= Decimal("0.02"):
            for key in ("TotalAmount", "TotalTax", "AmountInFiguers"):
                checks[key]["reason"] = "价税勾稽通过"

    if duplicate:
        hard_reasons.append("本次识别批次存在相同发票号码")

    for error in control.get("errors", []):
        if error not in hard_reasons:
            hard_reasons.append(error)
    soft_reasons.extend(control.get("warnings", []))

    threshold = _decimal(config.HIGH_VALUE_REVIEW_AMOUNT) or Decimal("100000")
    total_value = total if total is not None else (
        amount + tax if amount is not None and tax is not None else None
    )
    if total_value is not None and abs(total_value) >= threshold:
        soft_reasons.append(f"价税合计达到大额复核阈值 {threshold}")

    sampled = False
    if not hard_reasons and not soft_reasons:
        sampled = _sampled(source_sha256, config.REVIEW_SAMPLE_RATE)
        if sampled:
            soft_reasons.append("命中自动通过结果的抽样质检")

    if hard_reasons:
        risk_level = "high"
        review_status = "pending"
        evidence_grade = "low"
    elif soft_reasons:
        risk_level = "medium" if not sampled else "low"
        review_status = "pending"
        evidence_grade = "medium"
    else:
        risk_level = "low"
        review_status = "auto_pass" if config.AUTO_PASS_ENABLED else "pending"
        evidence_grade = "high"

    reasons = hard_reasons + soft_reasons
    message = {
        "auto_pass": "机器预审通过，可直接流转并按策略抽样质检",
        "pending": "需要人工复核：" + ("；".join(reasons) if reasons else "自动通过未启用"),
    }[review_status]

    return {
        "review_status": review_status,
        "risk_level": risk_level,
        "evidence_grade": evidence_grade,
        "confidence_type": "deterministic_controls",
        "confidence_score": None,
        "sampled": sampled,
        "reasons": reasons,
        "field_checks": checks,
        "message": message,
        "authenticity_checked": False,
    }
