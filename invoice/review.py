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
from invoice.validate import valid_uscc


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


def _sampled(sample_key: str, rate: float) -> bool:
    """按单张发票做确定性抽样，同一张发票每次进入相同分组。"""
    if not sample_key or rate <= 0:
        return False
    if rate >= 1:
        return True
    bucket = int(hashlib.sha256(sample_key.encode()).hexdigest()[:8], 16)
    return bucket / 0xFFFFFFFF < rate


def _field_reliability(
    data: dict,
    checks: dict[str, dict],
    control: dict,
    quality: dict,
    qr: dict,
) -> dict[str, dict]:
    """按字段汇总可解释证据；等级不是模型原生概率。"""
    header_ok = control.get("checks", {}).get("header_reconciliation") is True
    uppercase_ok = (
        control.get("checks", {}).get("uppercase_amount_reconciliation") is True
    )
    qr_matches = set(qr.get("field_matches", []))
    qr_mismatches = set(qr.get("field_mismatches", []))
    quality_status = quality.get("status", "unavailable")
    control_errors = list(control.get("errors", []))
    control_warnings = list(control.get("warnings", []))

    # 兼容升级前未记录字段键的二维码证据。
    label_matches = set(qr.get("matches", []))
    label_mismatches = set(qr.get("mismatches", []))
    if "发票号码" in label_matches:
        qr_matches.add("InvoiceNum")
    if "开票日期" in label_matches:
        qr_matches.add("InvoiceDate")
    if "发票号码" in label_mismatches:
        qr_mismatches.add("InvoiceNum")
    if "开票日期" in label_mismatches:
        qr_mismatches.add("InvoiceDate")
    if "二维码金额" in label_mismatches:
        qr_mismatches.update({"TotalAmount", "AmountInFiguers"})

    error_tokens = {
        "InvoiceNum": ("发票号码",),
        "InvoiceDate": ("开票日期",),
        "PurchaserName": ("购买方名称",),
        "PurchaserRegisterNum": ("购买方税号",),
        "SellerName": ("销售方名称",),
        "SellerRegisterNum": ("销售方税号",),
        "TotalAmount": ("金额", "税额", "价税合计"),
        "TotalTax": ("金额", "税额", "价税合计"),
        "AmountInFiguers": ("金额", "税额", "价税合计"),
    }
    reliability: dict[str, dict] = {}
    for key, label in config.REVIEW_FIELDS:
        value = field(data, key).strip()
        check = checks.get(key, {})
        corroborations: list[str] = []
        issues: list[str] = []

        if key == "InvoiceNum":
            confirm = field(data, "InvoiceNumConfirm").strip()
            if confirm and _normalized(confirm) == _normalized(value):
                corroborations.append("百度辅助校验值一致")
        if key in qr_matches:
            corroborations.append("二维码交叉一致")
        if key in qr_mismatches:
            issues.append("二维码字段冲突")
        if key in {"TotalAmount", "TotalTax", "AmountInFiguers"} and header_ok:
            corroborations.append("价税勾稽通过")
        if key in {"TotalAmount", "TotalTax"} and header_ok and qr_matches.intersection(
            {"TotalAmount", "AmountInFiguers"}
        ):
            corroborations.append("二维码金额与票头价税链路一致")
        if key == "AmountInFiguers" and uppercase_ok:
            corroborations.append("大写与小写金额一致")
        if key in {"PurchaserRegisterNum", "SellerRegisterNum"}:
            normalized_tax_id = _normalized(value)
            if len(normalized_tax_id) == 18 and valid_uscc(normalized_tax_id):
                corroborations.append("统一社会信用代码校验位通过")
        if key == "PurchaserName" and config.EXPECTED_PURCHASER_NAMES:
            normalized_name = re.sub(r"\s+", "", value)
            expected_names = {
                re.sub(r"\s+", "", item)
                for item in config.EXPECTED_PURCHASER_NAMES
            }
            if normalized_name in expected_names:
                corroborations.append("购买方主数据一致")

        tokens = error_tokens.get(key, ())
        field_errors = [
            message for message in control_errors
            if any(token in message for token in tokens)
        ]
        field_warnings = [
            message for message in control_warnings
            if any(token in message for token in tokens)
        ]
        issues.extend(field_errors)
        if check.get("status") == "error":
            issues.append(check.get("reason") or "字段校验失败")
        if quality_status == "error":
            issues.append("原图质量未通过")

        strong_corroborations = [
            reason for reason in corroborations
            if reason != "价税勾稽通过"
        ]
        if not value:
            level = "low"
            reasons = ["字段未识别"]
        elif issues:
            level = "low"
            reasons = list(dict.fromkeys(issues))
        elif strong_corroborations and quality_status != "warning":
            level = "high"
            reasons = list(dict.fromkeys(corroborations))
        else:
            level = "medium"
            reasons = list(dict.fromkeys(corroborations + field_warnings))
            if quality_status == "warning":
                reasons.append("原图质量存在警告")
            if not reasons:
                reasons.append("仅有OCR提取，缺少独立字段交叉证据")

        reliability[key] = {
            "label": label,
            "level": level,
            "reasons": list(dict.fromkeys(reasons)),
            "corroboration_count": len(set(corroborations)),
        }
    return reliability


def assess_review(
    data: dict,
    control: dict,
    *,
    duplicate: bool = False,
    source_sha256: str = "",
    source_page: int | None = None,
) -> dict:
    """生成机器预审结果，决定自动通过或进入人工复核队列。"""
    checks: dict[str, dict] = {}
    business_reasons: list[str] = []
    business_warnings: list[str] = []
    detail_reasons: list[str] = list(control.get("detail_issues", []))
    evidence_gaps: list[str] = []
    policy_reasons: list[str] = []
    quality = data.get("_quality") or {}
    qr = data.get("_qr") or {}
    detail = control.get("checks", {}).get("detail_reconciliation", {})
    detail_integrity = detail.get("status", "unavailable")
    verification_scope = control.get(
        "verification_scope",
        detail.get("verification_scope", "header"),
    )
    if detail_integrity == "partial" and not detail_reasons:
        detail_reasons.append("明细OCR字段不完整，无法完成汇总校验")

    for key, label in config.REVIEW_FIELDS:
        value = field(data, key).strip()
        if key in CRITICAL_FIELDS and not value:
            checks[key] = {
                "label": label, "status": "error", "reason": "关键字段缺失",
            }
            business_reasons.append(f"{label}缺失")
        elif value:
            checks[key] = {
                "label": label, "status": "pass", "reason": "已识别",
            }
        else:
            checks[key] = {
                "label": label, "status": "warning", "reason": "未识别，可按业务需要补录",
            }

    invoice_num = field(data, "InvoiceNum").strip()
    if invoice_num and not re.fullmatch(r"[A-Za-z0-9]{8,24}", invoice_num):
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
            business_reasons.append(f"{label}辅助校验不一致")

    amount = _decimal(field(data, "TotalAmount"))
    tax = _decimal(field(data, "TotalTax"))
    total = _decimal(field(data, "AmountInFiguers"))
    if amount is not None and tax is not None and total is not None:
        if abs(amount + tax - total) <= Decimal("0.02"):
            for key in ("TotalAmount", "TotalTax", "AmountInFiguers"):
                checks[key]["reason"] = "价税勾稽通过"

    if duplicate:
        business_reasons.append("本次识别批次存在相同发票号码")

    for error in control.get("errors", []):
        if error not in business_reasons:
            business_reasons.append(error)
    business_warnings.extend(control.get("warnings", []))

    quality_errors = list(quality.get("errors", []))
    quality_warnings = list(quality.get("warnings", []))
    evidence_gaps.extend(quality_errors)
    evidence_gaps.extend(
        reason for reason in quality_warnings if reason not in evidence_gaps
    )
    if qr.get("status") == "mismatch":
        business_reasons.append(
            qr.get("message") or "二维码与 OCR 字段不一致"
        )
    elif config.REQUIRE_QR_FOR_AUTO_PASS and qr.get("status") != "verified":
        evidence_gaps.append(
            qr.get("message")
            or "二维码证据未取得，不代表发票异常"
        )

    threshold = _decimal(config.HIGH_VALUE_REVIEW_AMOUNT) or Decimal("100000")
    total_value = total if total is not None else (
        amount + tax if amount is not None and tax is not None else None
    )
    if total_value is not None and abs(total_value) >= threshold:
        policy_reasons.append(f"价税合计达到大额复核阈值 {threshold}")

    sampled = False
    if not (
        business_reasons
        or business_warnings
        or detail_reasons
        or evidence_gaps
        or policy_reasons
    ):
        sample_key = "|".join((
            source_sha256,
            str(source_page or data.get("_source_page") or ""),
            invoice_num,
        ))
        sampled = _sampled(sample_key, config.REVIEW_SAMPLE_RATE)
        if sampled:
            policy_reasons.append("命中自动通过结果的抽样质检")

    if business_reasons:
        business_risk_level = "high"
    elif business_warnings:
        business_risk_level = "medium"
    else:
        business_risk_level = "low"

    if business_reasons or quality_errors:
        processing_priority = "high"
    elif business_warnings or detail_reasons or quality_warnings or policy_reasons:
        processing_priority = "medium"
    else:
        processing_priority = "normal"

    if business_reasons or quality_errors:
        evidence_grade = "low"
        evidence_completeness = "insufficient"
    elif business_warnings or detail_reasons or evidence_gaps:
        evidence_grade = "medium"
        evidence_completeness = "partial"
    else:
        evidence_grade = "high"
        evidence_completeness = "complete"

    reasons = (
        business_reasons
        + business_warnings
        + detail_reasons
        + evidence_gaps
        + policy_reasons
    )
    has_exception = bool(
        business_reasons
        or business_warnings
        or detail_reasons
        or evidence_gaps
    )
    review_status = (
        "pending"
        if reasons or not config.AUTO_PASS_ENABLED
        else "auto_pass"
    )
    if review_status == "auto_pass":
        routing_type = "auto_pass"
        message = "机器预审通过，可直接流转并按策略抽样质检"
    elif not has_exception and sampled:
        routing_type = "sample_review"
        message = (
            "抽样质检：本票规则校验已通过，按 "
            f"{config.REVIEW_SAMPLE_RATE:.0%} 策略进入人工质检（非异常）"
        )
    elif not has_exception and policy_reasons:
        routing_type = "policy_review"
        message = (
            "大额审批复核：本票规则校验已通过；"
            + "；".join(policy_reasons)
            + "（非识别异常）"
        )
    else:
        routing_type = "exception"
        message = "需要人工复核：" + (
            "；".join(reasons) if reasons else "自动通过未启用"
        )
    field_reliability = _field_reliability(
        data, checks, control, quality, qr
    )
    reliability_summary = {
        level: sum(
            item["level"] == level for item in field_reliability.values()
        )
        for level in ("high", "medium", "low")
    }
    return {
        "policy_version": "3.6",
        "review_status": review_status,
        "routing_type": routing_type,
        # risk_level 保留给旧数据库/接口，语义调整为“业务风险”。
        "risk_level": business_risk_level,
        "business_risk_level": business_risk_level,
        "processing_priority": processing_priority,
        "evidence_grade": evidence_grade,
        "evidence_completeness": evidence_completeness,
        "verification_scope": verification_scope,
        "detail_integrity": detail_integrity,
        "confidence_type": "deterministic_controls",
        "confidence_score": None,
        "sampled": sampled,
        "reasons": reasons,
        "reason_groups": {
            "business": business_reasons,
            "business_warnings": business_warnings,
            "detail_integrity": detail_reasons,
            "evidence_gaps": evidence_gaps,
            "policy": policy_reasons,
        },
        "field_checks": checks,
        "field_reliability": field_reliability,
        "field_reliability_summary": reliability_summary,
        "field_reliability_note": "可解释证据等级，不是模型原生概率",
        "evidence_label": "规则证据",
        "evidence_channels": {
            "ocr": {
                "status": "completed",
                "label": "OCR 字段提取",
            },
            "image_quality": {
                "status": quality.get("status", "unavailable"),
                "label": "图像质量门禁",
                "detail": quality.get("metrics", {}),
            },
            "qr_crosscheck": {
                "status": qr.get("status", "unavailable"),
                "label": "二维码交叉验证",
                "decoder": qr.get("decoder"),
                "matches": qr.get("matches", []),
                "mismatches": qr.get("mismatches", []),
                "field_matches": qr.get("field_matches", []),
                "field_mismatches": qr.get("field_mismatches", []),
            },
            "business_rules": {
                "status": "pass" if control.get("status") == "pass" else "review",
                "label": "票面与财务规则",
                "detail": control.get("checks", {}),
            },
            "detail_integrity": {
                "status": detail_integrity,
                "label": (
                    "明细OCR完整性"
                    if verification_scope == "detail"
                    else "明细核验（票头模式不要求）"
                ),
                "detail": detail,
            },
            "tax_authenticity": {
                "status": "not_connected",
                "label": "税务验真（暂未接入）",
            },
        },
        "message": message,
        "authenticity_checked": False,
    }
