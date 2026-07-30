"""OCR token 缓存与错误处理测试。"""

import time

import pytest

import config
from invoice import ocr


@pytest.fixture(autouse=True)
def reset_token():
    ocr._token_cache["value"] = None
    ocr._token_cache["expires_at"] = 0.0
    yield


def test_missing_credentials_raises(monkeypatch):
    monkeypatch.setattr(config, "BAIDU_OCR_API_KEY", "")
    monkeypatch.setattr(config, "BAIDU_OCR_SECRET_KEY", "")
    with pytest.raises(ocr.OcrError, match="未配置百度"):
        ocr.get_baidu_token()


def test_token_error_response_raises(monkeypatch):
    monkeypatch.setattr(config, "BAIDU_OCR_API_KEY", "k")
    monkeypatch.setattr(config, "BAIDU_OCR_SECRET_KEY", "s")

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"error": "invalid_client", "error_description": "unknown client id"}

    monkeypatch.setattr(ocr.requests, "post", lambda *a, **k: Resp())
    with pytest.raises(ocr.OcrError, match="unknown client id"):
        ocr.get_baidu_token()


def test_token_network_error_does_not_leak_secret(monkeypatch):
    monkeypatch.setattr(config, "BAIDU_OCR_API_KEY", "PUBLIC_KEY")
    monkeypatch.setattr(config, "BAIDU_OCR_SECRET_KEY", "VERY_SECRET")
    monkeypatch.setattr(
        ocr.requests,
        "post",
        lambda *a, **k: (_ for _ in ()).throw(
            ocr.requests.exceptions.SSLError(
                "url?client_id=PUBLIC_KEY&client_secret=VERY_SECRET"
            )
        ),
    )
    with pytest.raises(ocr.OcrError) as exc:
        ocr.get_baidu_token()
    assert "PUBLIC_KEY" not in str(exc.value)
    assert "VERY_SECRET" not in str(exc.value)


def test_token_cached_until_expiry(monkeypatch):
    monkeypatch.setattr(config, "BAIDU_OCR_API_KEY", "k")
    monkeypatch.setattr(config, "BAIDU_OCR_SECRET_KEY", "s")
    calls = {"n": 0}

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            calls["n"] += 1
            return {"access_token": "TOK", "expires_in": 2592000}

    monkeypatch.setattr(ocr.requests, "post", lambda *a, **k: Resp())
    assert ocr.get_baidu_token() == "TOK"
    assert ocr.get_baidu_token() == "TOK"  # 第二次命中缓存
    assert calls["n"] == 1  # 只请求了一次


def test_token_refreshes_after_expiry(monkeypatch):
    monkeypatch.setattr(config, "BAIDU_OCR_API_KEY", "k")
    monkeypatch.setattr(config, "BAIDU_OCR_SECRET_KEY", "s")
    monkeypatch.setattr(
        ocr.requests,
        "post",
        lambda *a, **k: type(
            "R",
            (),
            {
                "raise_for_status": lambda s: None,
                "json": lambda s: {"access_token": "TOK", "expires_in": 2592000},
            },
        )(),
    )
    ocr.get_baidu_token()
    ocr._token_cache["expires_at"] = time.time() - 1  # 手动置为已过期
    calls_before = ocr._token_cache["value"]
    ocr.get_baidu_token()
    assert calls_before == "TOK"  # 过期后能重新获取而不报错


def test_collect_raises_on_error_code():
    with pytest.raises(ocr.OcrError, match="识别失败"):
        ocr._collect({"error_code": 216201, "error_msg": "image format error"}, [], "图片")


def test_collect_success_appends():
    results = []
    ocr._collect({"words_result": {"InvoiceNum": "X"}}, results, "图片")
    assert results == [{"InvoiceNum": "X"}]


def test_ocr_refreshes_invalid_token_once(monkeypatch):
    tokens = iter(["OLD", "NEW"])
    monkeypatch.setattr(ocr, "get_baidu_token", lambda: next(tokens))
    monkeypatch.setattr(ocr, "_clear_token", lambda: None)
    replies = iter([
        {"error_code": 110, "error_msg": "Access token invalid"},
        {"words_result": {"InvoiceNum": "X"}},
    ])

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return next(replies)

    monkeypatch.setattr(ocr.requests, "post", lambda *a, **k: Resp())
    assert ocr.ocr_image(b"image")["words_result"]["InvoiceNum"] == "X"
