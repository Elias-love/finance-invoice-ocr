import app as invoice_app


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
