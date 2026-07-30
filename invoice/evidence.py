"""图像质量与二维码证据。

这些证据用于降低 OCR 数字混淆风险，不等同于税务机关真伪查验。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import config


def _decode_image(image_bytes: bytes):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    encoded = np.frombuffer(image_bytes, dtype=np.uint8)
    return cv2.imdecode(encoded, cv2.IMREAD_COLOR)


def analyze_image_quality(image_bytes: bytes) -> dict:
    """返回可解释的图像质量指标，过差时阻止自动通过。"""
    image = _decode_image(image_bytes)
    if image is None:
        return {
            "status": "error",
            "errors": ["无法解码图像，不能执行质量检查"],
            "warnings": [],
            "metrics": {},
        }

    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    short_edge = min(width, height)
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    contrast = float(gray.std())
    errors: list[str] = []
    warnings: list[str] = []

    if short_edge < config.IMAGE_MIN_SHORT_EDGE_HARD:
        errors.append(
            f"图像短边仅 {short_edge}px，低于最低识别要求 "
            f"{config.IMAGE_MIN_SHORT_EDGE_HARD}px"
        )
    elif short_edge < config.IMAGE_MIN_SHORT_EDGE_WARN:
        warnings.append(
            f"图像短边仅 {short_edge}px，关键数字可能缺少笔画"
        )

    if blur_score < config.IMAGE_BLUR_HARD:
        errors.append(f"图像严重模糊（清晰度 {blur_score:.1f}）")
    elif blur_score < config.IMAGE_BLUR_WARN:
        warnings.append(f"图像清晰度偏低（{blur_score:.1f}）")

    if brightness < config.IMAGE_BRIGHTNESS_MIN:
        warnings.append(f"图像整体过暗（亮度 {brightness:.1f}）")
    elif brightness > config.IMAGE_BRIGHTNESS_MAX:
        warnings.append(f"图像整体过亮（亮度 {brightness:.1f}）")
    if contrast < config.IMAGE_CONTRAST_MIN:
        warnings.append(f"图像对比度偏低（{contrast:.1f}）")

    return {
        "status": "error" if errors else ("warning" if warnings else "pass"),
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "width": width,
            "height": height,
            "short_edge": short_edge,
            "blur_score": round(blur_score, 1),
            "brightness": round(brightness, 1),
            "contrast": round(contrast, 1),
        },
    }


def parse_invoice_qr(text: str) -> dict:
    """解析常见增值税发票二维码的逗号分隔载荷。

    常见格式为 ``01,类型,发票代码,发票号码,不含税金额,日期,校验码,...``。
    新版全电发票的发票代码可能为空，因此只解析有明确位置含义的字段。
    """
    raw = str(text or "").strip()
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) < 6 or parts[0] != "01":
        return {"parsed": False, "raw": raw, "fields": {}}

    fields = {
        "InvoiceCode": parts[2] if len(parts) > 2 else "",
        "InvoiceNum": parts[3] if len(parts) > 3 else "",
        "TotalAmount": parts[4] if len(parts) > 4 else "",
        "InvoiceDate": parts[5] if len(parts) > 5 else "",
        "CheckCode": parts[6] if len(parts) > 6 else "",
    }
    if not re.fullmatch(r"[A-Za-z0-9]{8,24}", fields["InvoiceNum"]):
        return {"parsed": False, "raw": raw, "fields": fields}
    return {"parsed": True, "raw": raw, "fields": fields}


def decode_invoice_qr(image_bytes: bytes) -> dict:
    """使用 OpenCV 解码二维码；二维码不可读时返回明确的非验真状态。"""
    image = _decode_image(image_bytes)
    if image is None:
        return {
            "status": "unavailable",
            "decoded": False,
            "message": "图像无法解码，未完成二维码交叉验证",
            "fields": {},
        }

    import cv2

    payloads: list[str] = []
    height, width = image.shape[:2]
    top_left = image[: max(1, int(height * 0.6)), : max(1, int(width * 0.48))]
    enlarged = cv2.resize(top_left, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
    _threshold, binary = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    for candidate in (image, top_left, enlarged, binary):
        detector = cv2.QRCodeDetector()
        try:
            ok, decoded_info, _points, _straight = detector.detectAndDecodeMulti(
                candidate
            )
            if ok:
                payloads.extend(value for value in decoded_info if value)
        except (cv2.error, ValueError):
            pass
        try:
            value, _points, _straight = detector.detectAndDecode(candidate)
            if value:
                payloads.append(value)
        except (cv2.error, ValueError):
            pass
        if payloads:
            break

    for payload in payloads:
        parsed = parse_invoice_qr(payload)
        if parsed["parsed"]:
            return {
                "status": "decoded",
                "decoded": True,
                "message": "已读取发票二维码，等待与 OCR 字段交叉核对",
                "fields": parsed["fields"],
            }
    return {
        "status": "unavailable",
        "decoded": False,
        "message": "未读取到可解析的发票二维码",
        "fields": {},
    }


def _normalize_date(value: str) -> str:
    digits = re.findall(r"\d+", str(value or ""))
    if len(digits) >= 3:
        return f"{int(digits[0]):04d}{int(digits[1]):02d}{int(digits[2]):02d}"
    compact = re.sub(r"\D", "", str(value or ""))
    return compact[:8]


def _normalize_money(value: str) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").replace("¥", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def compare_qr_with_ocr(qr: dict, data: dict) -> dict:
    """将二维码与 OCR 做独立通道交叉核对。"""
    if not qr.get("decoded"):
        return {**qr, "matches": [], "mismatches": []}

    fields = qr.get("fields") or {}
    matches: list[str] = []
    mismatches: list[str] = []
    labels = {
        "InvoiceNum": "发票号码",
        "InvoiceDate": "开票日期",
        "TotalAmount": "二维码金额",
    }
    for key in ("InvoiceNum", "InvoiceDate", "TotalAmount"):
        qr_value = str(fields.get(key) or "").strip()
        ocr_value = str(data.get(key) or data.get("InvoiceNo", "")).strip()
        if not qr_value or not ocr_value:
            continue
        if key == "InvoiceDate":
            equal = _normalize_date(qr_value) == _normalize_date(ocr_value)
        elif key == "TotalAmount":
            left = _normalize_money(qr_value)
            right = _normalize_money(ocr_value)
            grand_total = _normalize_money(data.get("AmountInFiguers", ""))
            # 旧版二维码常放不含税金额，新版全电票常放价税合计；
            # 任一与票面字段精确一致即可作为交叉证据。
            equal = left is not None and (
                (right is not None and left == right)
                or (grand_total is not None and left == grand_total)
            )
        else:
            equal = re.sub(r"\s+", "", qr_value).upper() == re.sub(
                r"\s+", "", ocr_value
            ).upper()
        target = matches if equal else mismatches
        target.append(labels[key])

    verified = "发票号码" in matches and len(matches) >= 2 and not mismatches
    return {
        **qr,
        "status": "verified" if verified else (
            "mismatch" if mismatches else "partial"
        ),
        "matches": matches,
        "mismatches": mismatches,
        "message": (
            "二维码与 OCR 关键字段一致"
            if verified
            else (
                "二维码与 OCR 字段不一致：" + "、".join(mismatches)
                if mismatches
                else "二维码可读，但可比较字段不足"
            )
        ),
    }
