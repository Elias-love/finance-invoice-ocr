"""集中配置：环境变量 + 常量。所有密钥从环境变量读取，源码零硬编码。"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH") or (BASE_DIR / "data" / "invoices.db"))

# —— 百度云 VAT OCR 凭证 ——
BAIDU_OCR_API_KEY = os.getenv("BAIDU_OCR_API_KEY", "")
BAIDU_OCR_SECRET_KEY = os.getenv("BAIDU_OCR_SECRET_KEY", "")
# system：沿用 HTTP(S)_PROXY；direct：仅百度 OCR 直连；auto：代理失败后直连兜底。
BAIDU_OCR_PROXY_MODE = os.getenv("BAIDU_OCR_PROXY_MODE", "auto").strip().lower()
if BAIDU_OCR_PROXY_MODE not in {"auto", "system", "direct"}:
    BAIDU_OCR_PROXY_MODE = "auto"

# —— 管理员账号 ——
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
REVIEWER_USERNAME = os.getenv("REVIEWER_USERNAME", "reviewer")
REVIEWER_PASSWORD = os.getenv("REVIEWER_PASSWORD", "")
ENFORCE_MAKER_CHECKER = os.getenv("ENFORCE_MAKER_CHECKER", "0") == "1"

# —— Flask ——
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "") or os.urandom(24).hex()
PORT = int(os.getenv("PORT", "5007"))
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
OCR_RATE_LIMIT_PER_MINUTE = int(os.getenv("OCR_RATE_LIMIT_PER_MINUTE", "60"))

# 上传大小上限（字节）：默认 16MB，超出返回 413
MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "16")) * 1024 * 1024

# 允许的上传类型
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".bmp"}

# PDF 转图片的缩放倍数。2.5 约等于 180 DPI，可明显降低小字号数字丢笔画。
PDF_ZOOM = float(os.getenv("PDF_ZOOM", "2.5"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "30"))
MAX_RENDER_PIXELS = int(os.getenv("MAX_RENDER_PIXELS", "40000000"))

# —— OCR 前图像质量门禁 ——
IMAGE_MIN_SHORT_EDGE_HARD = int(os.getenv("IMAGE_MIN_SHORT_EDGE_HARD", "700"))
IMAGE_MIN_SHORT_EDGE_WARN = int(os.getenv("IMAGE_MIN_SHORT_EDGE_WARN", "1200"))
IMAGE_BLUR_HARD = float(os.getenv("IMAGE_BLUR_HARD", "25"))
IMAGE_BLUR_WARN = float(os.getenv("IMAGE_BLUR_WARN", "65"))
IMAGE_BRIGHTNESS_MIN = float(os.getenv("IMAGE_BRIGHTNESS_MIN", "45"))
IMAGE_BRIGHTNESS_MAX = float(os.getenv("IMAGE_BRIGHTNESS_MAX", "252"))
IMAGE_CONTRAST_MIN = float(os.getenv("IMAGE_CONTRAST_MIN", "28"))
REQUIRE_QR_FOR_AUTO_PASS = os.getenv("REQUIRE_QR_FOR_AUTO_PASS", "1") == "1"
RECONCILIATION_TOLERANCE = os.getenv("RECONCILIATION_TOLERANCE", "0.01")
EXPECTED_PURCHASER_TAX_IDS = {
    value.strip().upper()
    for value in os.getenv("EXPECTED_PURCHASER_TAX_IDS", "").split(",")
    if value.strip()
}
EXPECTED_PURCHASER_NAMES = {
    value.strip()
    for value in os.getenv("EXPECTED_PURCHASER_NAMES", "").split(",")
    if value.strip()
}

# —— 复核策略 ——
# 标准 VAT OCR 接口不返回统一的字段级 probability，因此这里使用可解释的
# 确定性证据分级，不把规则结果冒充为模型置信度。
AUTO_PASS_ENABLED = os.getenv("AUTO_PASS_ENABLED", "1") == "1"
HIGH_VALUE_REVIEW_AMOUNT = os.getenv("HIGH_VALUE_REVIEW_AMOUNT", "100000")
REVIEW_SAMPLE_RATE = float(os.getenv("REVIEW_SAMPLE_RATE", "0.05"))
ALLOW_AUTO_PASS_EXPORT = os.getenv("ALLOW_AUTO_PASS_EXPORT", "1") == "1"
ALLOW_DESTRUCTIVE_CLEAR = os.getenv("ALLOW_DESTRUCTIVE_CLEAR", "0") == "1"

# —— 导出字段（单一来源，避免多处重复定义）——
EXPORT_FIELDS = ["InvoiceNum", "InvoiceDate", "PurchaserName",
                 "SellerName", "TotalAmount", "TotalTax"]
EXPORT_FIELD_NAMES = ["发票号码", "开票日期", "购买方名称",
                      "销售方名称", "合计金额", "合计税额"]

REVIEW_FIELDS = [
    ("InvoiceTypeOrg", "发票名称"),
    ("InvoiceNum", "发票号码"),
    ("InvoiceDate", "开票日期"),
    ("PurchaserName", "购买方名称"),
    ("PurchaserRegisterNum", "购买方税号"),
    ("SellerName", "销售方名称"),
    ("SellerRegisterNum", "销售方税号"),
    ("TotalAmount", "合计金额"),
    ("TotalTax", "合计税额"),
    ("AmountInFiguers", "价税合计（小写）"),
]
