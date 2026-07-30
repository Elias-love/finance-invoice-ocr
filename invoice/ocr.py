"""百度云 VAT OCR 封装：带过期的 token 缓存 + 明确的错误处理 + PDF/图片识别。"""

import base64
import logging
import threading
import time

import requests

import config

logger = logging.getLogger(__name__)

TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice"

# token 缓存：{"value": str, "expires_at": epoch秒}。带过期时间，避免永久缓存导致过期后全部失败。
_token_cache = {"value": None, "expires_at": 0.0}
_token_lock = threading.Lock()


class OcrError(Exception):
    """OCR 相关的可预期错误，携带对用户友好的中文信息。"""


def get_baidu_token() -> str:
    """获取百度 access_token，命中未过期缓存则直接返回。"""
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]

    with _token_lock:
        now = time.time()
        if _token_cache["value"] and now < _token_cache["expires_at"]:
            return _token_cache["value"]

        if not config.BAIDU_OCR_API_KEY or not config.BAIDU_OCR_SECRET_KEY:
            raise OcrError(
                "未配置百度 OCR 凭证，请在 .env 设置 "
                "BAIDU_OCR_API_KEY / BAIDU_OCR_SECRET_KEY"
            )

        try:
            resp = requests.post(TOKEN_URL, params={
                "grant_type": "client_credentials",
                "client_id": config.BAIDU_OCR_API_KEY,
                "client_secret": config.BAIDU_OCR_SECRET_KEY,
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            # requests 的异常文本可能包含带 client_id/client_secret 的完整 URL，
            # 绝不能原样返回前端或写入日志。
            raise OcrError(
                "获取百度 token 网络异常，请检查网络、TLS 证书和百度云服务状态"
            ) from None

        if "access_token" not in data:
            raise OcrError(
                f"获取百度 token 失败：{data.get('error_description', data)}"
            )

        expires_in = max(int(data.get("expires_in", 2592000)), 600)
        _token_cache["value"] = data["access_token"]
        # 官方 token 默认 30 天；按实际 expires_in 缓存，并提前 5 分钟刷新。
        _token_cache["expires_at"] = now + expires_in - 300
        logger.info("已刷新百度 OCR token，有效期约 %.1f 天", expires_in / 86400)
        return _token_cache["value"]


def _clear_token():
    with _token_lock:
        _token_cache["value"] = None
        _token_cache["expires_at"] = 0.0


def ocr_image(image_bytes: bytes) -> dict:
    """对单张图片调用百度 VAT OCR，返回原始响应字典。"""
    img_base64 = base64.b64encode(image_bytes).decode("utf-8")
    for attempt in range(2):
        token = get_baidu_token()
        try:
            resp = requests.post(
                f"{OCR_URL}?access_token={token}",
                data={"image": img_base64},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
        except (requests.RequestException, ValueError):
            # OCR URL 含 access_token，异常文本不可直接上抛。
            raise OcrError(
                "调用百度 OCR 网络异常，请检查网络、TLS 证书和百度云服务状态"
            ) from None

        # 110/111 分别表示 token 无效/过期；清缓存并自动重取一次。
        if result.get("error_code") in {110, 111} and attempt == 0:
            logger.info("百度 access_token 已失效，自动刷新后重试")
            _clear_token()
            continue
        return result
    raise OcrError("百度 access_token 刷新后仍不可用")


def estimate_units(file_bytes: bytes, filename: str) -> int:
    """估算一次上传会消耗的 OCR 页数，用于限流和成本控制。"""
    if not (filename or "").lower().endswith(".pdf"):
        return 1
    import fitz
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            return len(doc)
    except Exception as exc:
        raise OcrError(f"PDF 文件无法读取：{exc}") from exc


def recognize_bytes(file_bytes: bytes, filename: str) -> list[dict]:
    """识别上传的文件（PDF 多页或单张图片），返回每页/每张的 words_result 列表。

    Raises:
        OcrError: 凭证缺失、token 获取失败、或百度返回错误码时抛出，供上层明确告知用户。
    """
    filename = (filename or "").lower()
    results = []

    if filename.endswith(".pdf"):
        import fitz  # PyMuPDF，仅 PDF 分支需要
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            logger.info("PDF 共 %d 页", len(pdf_doc))
            if len(pdf_doc) > config.MAX_PDF_PAGES:
                raise OcrError(
                    f"PDF 共 {len(pdf_doc)} 页，超过单次上限 "
                    f"{config.MAX_PDF_PAGES} 页"
                )
            mat = fitz.Matrix(config.PDF_ZOOM, config.PDF_ZOOM)
            for page_num in range(len(pdf_doc)):
                pix = pdf_doc[page_num].get_pixmap(matrix=mat)
                ocr_result = ocr_image(pix.tobytes("png"))
                _collect(ocr_result, results, f"PDF 第 {page_num + 1} 页")
        finally:
            pdf_doc.close()
    else:
        ocr_result = ocr_image(file_bytes)
        _collect(ocr_result, results, "图片")

    return results


def _collect(ocr_result: dict, results: list, label: str):
    """把单次 OCR 结果并入 results；遇到百度错误码则抛出 OcrError。"""
    if ocr_result.get("words_result"):
        results.append(ocr_result["words_result"])
        logger.info("%s识别成功", label)
    elif ocr_result.get("error_msg") or ocr_result.get("error_code"):
        msg = ocr_result.get("error_msg", "未知错误")
        logger.warning("%s OCR 错误：%s", label, msg)
        raise OcrError(f"{label}识别失败：{msg}")
    else:
        logger.info("%s未识别到发票内容", label)
