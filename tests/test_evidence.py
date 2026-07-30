import cv2
import numpy as np
from types import SimpleNamespace

from invoice.evidence import (
    analyze_image_quality,
    compare_qr_with_ocr,
    decode_invoice_qr,
    parse_invoice_qr,
)


def test_parse_and_compare_new_einvoice_qr_grand_total():
    parsed = parse_invoice_qr(
        "01,31,,26442000005310626521,31605.00,20260514,,"
    )
    assert parsed["parsed"] is True
    compared = compare_qr_with_ocr(
        {
            "decoded": True,
            "status": "decoded",
            "fields": parsed["fields"],
        },
        {
            "InvoiceNum": "26442000005310626521",
            "InvoiceDate": "2026年05月14日",
            "TotalAmount": "27969.03",
            "AmountInFiguers": "31605.00",
        },
    )
    assert compared["status"] == "verified"
    assert compared["mismatches"] == []


def test_qr_digit_conflict_is_hard_mismatch():
    parsed = parse_invoice_qr(
        "01,31,,26442000005310626521,31605.00,20260514,,"
    )
    compared = compare_qr_with_ocr(
        {
            "decoded": True,
            "status": "decoded",
            "fields": parsed["fields"],
        },
        {
            "InvoiceNum": "26442000005310926521",
            "InvoiceDate": "2026年05月14日",
            "AmountInFiguers": "31605.00",
        },
    )
    assert compared["status"] == "mismatch"
    assert "发票号码" in compared["mismatches"]


def test_low_resolution_image_fails_quality_gate():
    image = np.full((300, 400, 3), 255, dtype=np.uint8)
    cv2.putText(
        image,
        "1234567890",
        (10, 160),
        cv2.FONT_HERSHEY_SIMPLEX,
        1,
        (0, 0, 0),
        2,
    )
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    result = analyze_image_quality(encoded.tobytes())
    assert result["status"] == "error"
    assert any("短边" in reason for reason in result["errors"])


def test_zxing_fallback_decodes_when_opencv_fails(monkeypatch):
    import zxingcpp

    class EmptyDetector:
        def detectAndDecodeMulti(self, _image):
            return False, (), None, ()

        def detectAndDecode(self, _image):
            return "", None, None

    monkeypatch.setattr(cv2, "QRCodeDetector", EmptyDetector)
    monkeypatch.setattr(
        zxingcpp,
        "read_barcodes",
        lambda _image: [
            SimpleNamespace(
                text="01,31,,26442000005310626521,31605.00,20260514,,7B05"
            )
        ],
    )
    image = np.full((1200, 1600, 3), 255, dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    result = decode_invoice_qr(encoded.tobytes())
    assert result["decoded"] is True
    assert result["decoder"] == "ZXing"
