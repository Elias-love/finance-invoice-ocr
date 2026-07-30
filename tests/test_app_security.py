import app as invoice_app
import config
from io import BytesIO
from invoice import storage
from invoice.review import assess_review
from invoice.validate import validate_invoice


def test_formula_like_export_cell_is_escaped():
    assert invoice_app._safe_export_cell("=1+1") == "'=1+1"
    assert invoice_app._safe_export_cell("normal") == "normal"


def test_file_signature_validation():
    assert invoice_app._looks_like_allowed_file(b"%PDF-1.7", ".pdf")
    assert invoice_app._looks_like_allowed_file(b"\xff\xd8\xffrest", ".jpg")
    assert not invoice_app._looks_like_allowed_file(b"not a pdf", ".pdf")


def test_recognize_requires_login():
    client = invoice_app.app.test_client()
    response = client.post("/api/recognize")
    assert response.status_code == 401
    assert "请先登录" in response.get_json()["error"]


def test_review_page_renders_and_approval_is_audited(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "review.db")
    monkeypatch.setattr(config, "REVIEW_SAMPLE_RATE", 0)
    storage.init_db()
    data = {
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
    control = validate_invoice(data)
    review = assess_review(data, control, source_sha256="abc")
    rid = storage.add_record(data, validation=control, review=review)

    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True
        sess["_csrf_token"] = "csrf-test"

    page = client.get(f"/admin/review/{rid}")
    assert page.status_code == 200
    assert "机器预审证据与人工确认" in page.get_data(as_text=True)

    response = client.post(
        f"/admin/review/{rid}",
        data={
            "_csrf_token": "csrf-test",
            "action": "approve",
            "review_note": "已核对原票",
            **{key: data.get(key, "") for key, _ in config.REVIEW_FIELDS},
        },
    )
    assert response.status_code == 302
    reviewed = storage.get_by_id(rid)
    assert reviewed["review_status"] == "approved"
    assert reviewed["review"]["review_status"] == "approved"


def test_current_batch_survives_review_navigation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "batch.db")
    storage.init_db()
    data = {
        "InvoiceNum": "24442000000000000999",
        "InvoiceDate": "2026年07月30日",
        "PurchaserName": "深圳星辰集团",
        "SellerName": "珠海东晟",
        "TotalAmount": "100.00",
        "TotalTax": "13.00",
    }
    rid = storage.add_record(
        data,
        validation={"status": "pass", "message": "基础控制通过"},
        review={
            "review_status": "pending",
            "risk_level": "medium",
            "message": "待人工复核",
        },
        source_filename="batch.pdf",
    )
    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True
        sess["_csrf_token"] = "csrf-test"
        sess["current_batch_ids"] = [rid]

    home = client.get("/")
    body = home.get_data(as_text=True)
    assert "已恢复本批次 1 张发票" in body
    assert data["InvoiceNum"] in body

    review_page = client.get(f"/admin/review/{rid}?return_to=/")
    assert "返回本批识别结果" in review_page.get_data(as_text=True)

    cleared = client.post(
        "/api/current-batch/clear",
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert cleared.status_code == 200
    assert storage.get_by_id(rid) is not None
    assert "已恢复本批次" not in client.get("/").get_data(as_text=True)


def test_recognize_stores_record_in_current_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "recognized-batch.db")
    monkeypatch.setattr(config, "REVIEW_SAMPLE_RATE", 0)
    storage.init_db()
    monkeypatch.setattr(invoice_app.ocr, "estimate_units", lambda *_: 1)
    monkeypatch.setattr(
        invoice_app.ocr,
        "recognize_bytes",
        lambda *_: [{
            "InvoiceNum": "24442000000000000888",
            "InvoiceDate": "2026年07月30日",
            "PurchaserName": "深圳星辰集团",
            "SellerName": "珠海东晟",
            "TotalAmount": "100.00",
            "TotalTax": "13.00",
            "AmountInFiguers": "113.00",
        }],
    )
    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    response = client.post(
        "/api/recognize",
        data={
            "file": (BytesIO(b"\x89PNG\r\n\x1a\nfake"), "invoice.png"),
            "batch_action": "reset",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    record_id = response.get_json()["items"][0]["_record_id"]
    with client.session_transaction() as sess:
        assert sess["current_batch_ids"] == [record_id]
    assert "24442000000000000888" in client.get("/").get_data(as_text=True)


def test_pending_record_cannot_be_exported(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "export.db")
    storage.init_db()
    pending_id = storage.add_record(
        {"InvoiceNum": "PENDING"},
        review={"review_status": "pending", "risk_level": "high"},
    )
    auto_id = storage.add_record(
        {"InvoiceNum": "AUTOPASS"},
        review={"review_status": "auto_pass", "risk_level": "low"},
    )
    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    blocked = client.post("/api/export/excel", json={"record_ids": [pending_id]})
    assert blocked.status_code == 400
    allowed = client.post("/api/export/excel", json={"record_ids": [auto_id]})
    assert allowed.status_code == 200
