"""百度云 VAT OCR 封装：带过期的 token 缓存 + 明确的错误处理 + PDF/图片识别。"""

import base64
import logging
import time

import requests

import config

logger = logging.getLogger(__name__)

TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice"

# token 缓存：{"value": str, "expires_at": epoch秒}。带过期时间，避免永久缓存导致过期后全部失败。
_token_cache = {"value": None, "expires_at": 0.0}


class OcrError(Exception):
    """OCR 相关的可预期错误，携带对用户友好的中文信息。"""


def get_baidu_token() -> str:
    """获取百度 access_token，命中未过期缓存则直接返回。"""
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]

    if not config.BAIDU_OCR_API_KEY or not config.BAIDU_OCR_SECRET_KEY:
        raise OcrError("未配置百度 OCR 凭证，请在 .env 设置 BAIDU_OCR_API_KEY / BAIDU_OCR_SECRET_KEY")

    resp = requests.post(TOKEN_URL, params={
        "grant_type": "client_credentials",
        "client_id": config.BAIDU_OCR_API_KEY,
        "client_secret": config.BAIDU_OCR_SECRET_KEY,
    }, timeout=10)
    data = resp.json()
    if "access_token" not in data:
        # 百度返回 error/error_description（如密钥错误）时明确抛出，而非 KeyError
        raise OcrError(f"获取百度 token 失败：{data.get('error_description', data)}")

    _token_cache["value"] = data["access_token"]
    # 提前 5 分钟过期，留出刷新余量；百度 token 默认有效期约 30 天
    _token_cache["expires_at"] = now + data.get("expires_in", 2592000) - 300
    logger.info("已刷新百度 OCR token")
    return _token_cache["value"]


def ocr_image(image_bytes: bytes) -> dict:
    """对单张图片调用百度 VAT OCR，返回原始响应字典。"""
    token = get_baidu_token()
    img_base64 = base64.b64encode(image_bytes).decode("utf-8")
    resp = requests.post(f"{OCR_URL}?access_token={token}",
                         data={"image": img_base64}, timeout=15)
    return resp.json()


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
