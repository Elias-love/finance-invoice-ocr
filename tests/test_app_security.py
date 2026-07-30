import app as invoice_app
import config
import hashlib
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
        "PurchaserRegisterNum": "9144030005899241X7",
        "SellerName": "珠海东晟",
        "SellerRegisterNum": "91330281739477958A",
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
    page_body = page.get_data(as_text=True)
    assert "机器预审证据与人工确认" in page_body
    assert "发票代码" not in page_body

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


def test_review_preview_renders_only_record_pdf_page(tmp_path, monkeypatch):
    import fitz

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "single-page-preview.db")
    storage.init_db()
    document = fitz.open()
    document.new_page().insert_text((72, 72), "PAGE ONE")
    document.new_page().insert_text((72, 72), "PAGE TWO")
    pdf_bytes = document.tobytes()
    document.close()
    source_sha = "two-page-source"
    source_path = storage.save_source(pdf_bytes, ".pdf", source_sha)
    rid = storage.add_record(
        {"InvoiceNum": "PAGE-2"},
        source_sha256=source_sha,
        source_filename="two-pages.pdf",
        source_path=source_path,
        source_page=2,
    )
    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True

    review_page = client.get(f"/admin/review/{rid}")
    body = review_page.get_data(as_text=True)
    assert "two-pages.pdf" in body
    assert "第 2 页" in body
    assert f"/admin/source/{rid}/preview" in body
    assert "<iframe" not in body

    preview = client.get(f"/admin/source/{rid}/preview")
    assert preview.status_code == 200
    assert preview.mimetype == "image/png"
    assert preview.data.startswith(b"\x89PNG\r\n\x1a\n")


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
    monkeypatch.setattr(config, "REQUIRE_QR_FOR_AUTO_PASS", False)
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
        sess["_csrf_token"] = "csrf-test"

    response = client.post(
        "/api/recognize",
        data={
            "file": (BytesIO(b"\x89PNG\r\n\x1a\nfake"), "invoice.png"),
            "batch_action": "reset",
        },
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200
    response_item = response.get_json()["items"][0]
    record_id = response_item["_record_id"]
    assert response_item["_control"]["verification_scope"] == "header"
    assert response_item["_review"]["detail_integrity"] == "not_required"
    assert storage.get_by_id(record_id)["data"]["_verification_scope"] == "header"
    with client.session_transaction() as sess:
        assert sess["current_batch_ids"] == [record_id]
    assert "24442000000000000888" in client.get("/").get_data(as_text=True)
    home_body = client.get("/").get_data(as_text=True)
    assert "正在重新识别，请稍候" in home_body
    assert "重新识别完成" in home_body
    assert "原始识别结果和审计轨迹会保留" in home_body


def test_duplicate_check_ignores_history_but_flags_current_batch(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "batch-duplicate.db")
    monkeypatch.setattr(config, "REVIEW_SAMPLE_RATE", 0)
    monkeypatch.setattr(config, "REQUIRE_QR_FOR_AUTO_PASS", False)
    storage.init_db()
    invoice_data = {
        "InvoiceNum": "24442000000000000777",
        "InvoiceDate": "2026年07月30日",
        "PurchaserName": "深圳星辰集团",
        "PurchaserRegisterNum": "9144030005899241X7",
        "SellerName": "珠海东晟",
        "SellerRegisterNum": "91330281739477958A",
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
        sess["_csrf_token"] = "csrf-test"

    response = client.post(
        "/api/recognize",
        data={
            "file": (BytesIO(b"\x89PNG\r\n\x1a\nfake"), "batch.png"),
            "batch_action": "reset",
        },
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf-test"},
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
    monkeypatch.setattr(config, "REQUIRE_QR_FOR_AUTO_PASS", False)
    storage.init_db()
    invoice_data = {
        "InvoiceNum": "24442000000000000666",
        "InvoiceDate": "2026年07月30日",
        "PurchaserName": "深圳星辰集团",
        "PurchaserRegisterNum": "9144030005899241X7",
        "SellerName": "珠海东晟",
        "SellerRegisterNum": "91330281739477958A",
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
        sess["_csrf_token"] = "csrf-test"

    first = client.post(
        "/api/recognize",
        data={
            "file": (BytesIO(b"\x89PNG\r\n\x1a\nfirst"), "first.png"),
            "batch_action": "reset",
        },
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf-test"},
    ).get_json()["items"][0]
    second = client.post(
        "/api/recognize",
        data={
            "file": (BytesIO(b"\x89PNG\r\n\x1a\nsecond"), "second.png"),
        },
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf-test"},
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
        sess["_csrf_token"] = "csrf-test"

    blocked = client.post(
        "/api/export/excel",
        json={"record_ids": [pending_id]},
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert blocked.status_code == 400
    allowed = client.post(
        "/api/export/excel",
        json={"record_ids": [auto_id]},
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert allowed.status_code == 200


def test_same_source_uses_ocr_cache_but_creates_new_audit_record(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(config, "REQUIRE_QR_FOR_AUTO_PASS", False)
    storage.init_db()
    content = b"\x89PNG\r\n\x1a\ncached"
    source_sha = hashlib.sha256(content).hexdigest()
    source_path = storage.save_source(content, ".png", source_sha)
    data = {
        "InvoiceNum": "24442000000000000555",
        "InvoiceDate": "2026年07月30日",
        "PurchaserName": "深圳星辰集团",
        "SellerName": "珠海东晟",
        "TotalAmount": "100.00",
        "TotalTax": "13.00",
        "AmountInFiguers": "113.00",
    }
    storage.add_record(
        data,
        source_sha256=source_sha,
        source_filename="cached.png",
        source_path=source_path,
        source_page=1,
    )
    monkeypatch.setattr(invoice_app.ocr, "estimate_units", lambda *_: 1)
    monkeypatch.setattr(
        invoice_app.ocr,
        "recognize_bytes",
        lambda *_: (_ for _ in ()).throw(AssertionError("不应重复调用 OCR")),
    )
    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True
        sess["_csrf_token"] = "csrf-test"

    response = client.post(
        "/api/recognize",
        data={
            "file": (BytesIO(content), "cached.png"),
            "batch_action": "reset",
        },
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200
    assert response.get_json()["items"][0]["_cache_hit"] is True
    assert storage.count() == 2


def test_single_page_rerun_preserves_first_raw_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "rerun.db")
    monkeypatch.setattr(config, "REQUIRE_QR_FOR_AUTO_PASS", False)
    storage.init_db()
    content = b"\x89PNG\r\n\x1a\nsource"
    source_sha = hashlib.sha256(content).hexdigest()
    source_path = storage.save_source(content, ".png", source_sha)
    original = {
        "InvoiceNum": "24442000000000000999",
        "InvoiceDate": "2026年07月30日",
        "PurchaserName": "深圳星辰集团",
        "SellerName": "珠海东晟",
        "TotalAmount": "100.00",
        "TotalTax": "13.00",
        "AmountInFiguers": "113.00",
    }
    rid = storage.add_record(
        original,
        source_sha256=source_sha,
        source_filename="source.png",
        source_path=source_path,
        source_page=1,
    )
    corrected_num = "24442000000000000666"
    monkeypatch.setattr(
        invoice_app.ocr,
        "recognize_page",
        lambda *_: {**original, "InvoiceNum": corrected_num},
    )
    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True
        sess["_csrf_token"] = "csrf-test"
        sess["current_batch_ids"] = [rid]

    response = client.post(
        f"/api/records/{rid}/rerun",
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200
    record = storage.get_by_id(rid)
    assert record["original_data"]["InvoiceNum"] == original["InvoiceNum"]
    assert record["data"]["InvoiceNum"] == corrected_num
    assert any(
        event["action"] == "re_recognized"
        for event in storage.get_review_events(rid)
    )


def test_human_can_override_detail_mismatch_with_mandatory_note(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "override.db")
    monkeypatch.setattr(config, "REQUIRE_QR_FOR_AUTO_PASS", False)
    storage.init_db()
    data = {
        "InvoiceNum": "24442000000000000333",
        "InvoiceDate": "2026年07月30日",
        "PurchaserName": "深圳星辰集团",
        "SellerName": "珠海东晟",
        "TotalAmount": "100.00",
        "TotalTax": "13.00",
        "AmountInFiguers": "113.00",
        "_verification_scope": "detail",
        "CommodityAmount": [{"row": "1", "word": "90.00"}],
        "CommodityTax": [{"row": "1", "word": "11.70"}],
        "CommodityTaxRate": [{"row": "1", "word": "13%"}],
    }
    control = validate_invoice(data)
    review = assess_review(data, control)
    rid = storage.add_record(data, validation=control, review=review)
    client = invoice_app.app.test_client()
    with client.session_transaction() as sess:
        sess["is_admin"] = True
        sess["_csrf_token"] = "csrf-test"

    form = {
        "_csrf_token": "csrf-test",
        "action": "approve",
        "verification_scope": "detail",
        **{key: data.get(key, "") for key, _ in config.REVIEW_FIELDS},
    }
    denied = client.post(f"/admin/review/{rid}", data=form)
    assert denied.status_code == 200
    assert "必须填写复核依据" in denied.get_data(as_text=True)
    assert "明细OCR完整性需要复核" in denied.get_data(as_text=True)
    assert "票头字段本身可以仍然正确" in denied.get_data(as_text=True)
    assert "字段可靠性" in denied.get_data(as_text=True)
    assert "可靠性依据" in denied.get_data(as_text=True)

    approved = client.post(
        f"/admin/review/{rid}",
        data={**form, "review_note": "已逐行核对原票，OCR明细漏行"},
    )
    assert approved.status_code == 302
    assert storage.get_by_id(rid)["review_status"] == "approved"
