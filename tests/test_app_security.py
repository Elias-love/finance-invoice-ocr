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


def test_empty_legacy_batch_session_recovers_latest_source(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "legacy-empty-batch.db")
    storage.init_db()
    rid = storage.add_record(
        {"InvoiceNum": "LEGACY-BATCH"},
        source_sha256="legacy-source",
    )
    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True
        # 旧版本重复识别会留下空 ID 列表，但没有明确清除标记。
        sess["current_batch_ids"] = []

    home = client.get("/")
    assert "LEGACY-BATCH" in home.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert sess["current_batch_ids"] == [rid]
        assert sess["current_batch_cleared"] is False


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


def test_duplicate_check_ignores_history_but_flags_current_batch(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "batch-duplicate.db")
    monkeypatch.setattr(config, "REVIEW_SAMPLE_RATE", 0)
    storage.init_db()
    invoice_data = {
        "InvoiceNum": "24442000000000000777",
        "InvoiceDate": "2026年07月30日",
        "PurchaserName": "深圳星辰集团",
        "PurchaserRegisterNum": "91440300MA5F1CT001",
        "SellerName": "珠海东晟",
        "SellerRegisterNum": "91440400MA5F1CT101",
        "TotalAmount": "100.00",
        "TotalTax": "13.00",
        "AmountInFiguers": "113.00",
    }
    historical_id = storage.add_record(invoice_data, source_sha256="history")
    monkeypatch.setattr(invoice_app.ocr, "estimate_units", lambda *_: 2)
    monkeypatch.setattr(
        invoice_app.ocr,
        "recognize_bytes",
        lambda *_: [dict(invoice_data), dict(invoice_data)],
    )
    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    response = client.post(
        "/api/recognize",
        data={
            "file": (BytesIO(b"\x89PNG\r\n\x1a\nfake"), "batch.png"),
            "batch_action": "reset",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    first, second = response.get_json()["items"]
    assert first["_control"]["status"] != "duplicate"
    assert first["_review"]["review_status"] == "auto_pass"
    assert second["_control"]["status"] == "duplicate"
    assert "本次识别批次" in second["_review"]["message"]
    assert first["_record_id"] != second["_record_id"]
    assert storage.count() == 3
    with client.session_transaction() as sess:
        assert sess["current_batch_ids"] == [
            first["_record_id"],
            second["_record_id"],
        ]
        assert historical_id not in sess["current_batch_ids"]


def test_duplicate_check_spans_multiple_files_in_same_batch(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "multi-file-batch.db")
    monkeypatch.setattr(config, "REVIEW_SAMPLE_RATE", 0)
    storage.init_db()
    invoice_data = {
        "InvoiceNum": "24442000000000000666",
        "InvoiceDate": "2026年07月30日",
        "PurchaserName": "深圳星辰集团",
        "PurchaserRegisterNum": "91440300MA5F1CT001",
        "SellerName": "珠海东晟",
        "SellerRegisterNum": "91440400MA5F1CT101",
        "TotalAmount": "100.00",
        "TotalTax": "13.00",
        "AmountInFiguers": "113.00",
    }
    monkeypatch.setattr(invoice_app.ocr, "estimate_units", lambda *_: 1)
    monkeypatch.setattr(
        invoice_app.ocr,
        "recognize_bytes",
        lambda *_: [dict(invoice_data)],
    )
    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    first = client.post(
        "/api/recognize",
        data={
            "file": (BytesIO(b"\x89PNG\r\n\x1a\nfirst"), "first.png"),
            "batch_action": "reset",
        },
        content_type="multipart/form-data",
    ).get_json()["items"][0]
    second = client.post(
        "/api/recognize",
        data={
            "file": (BytesIO(b"\x89PNG\r\n\x1a\nsecond"), "second.png"),
        },
        content_type="multipart/form-data",
    ).get_json()["items"][0]
    assert first["_control"]["status"] != "duplicate"
    assert second["_control"]["status"] == "duplicate"


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
