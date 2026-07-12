"""集中配置：环境变量 + 常量。所有密钥从环境变量读取，源码零硬编码。"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "invoices.db"

# —— 百度云 VAT OCR 凭证 ——
BAIDU_OCR_API_KEY = os.getenv("BAIDU_OCR_API_KEY", "")
BAIDU_OCR_SECRET_KEY = os.getenv("BAIDU_OCR_SECRET_KEY", "")

# —— 管理员账号 ——
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me-please")

# —— Flask ——
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "") or os.urandom(24).hex()
PORT = int(os.getenv("PORT", "5007"))

# 上传大小上限（字节）：默认 16MB，超出返回 413
MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "16")) * 1024 * 1024

# 允许的上传类型
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".bmp"}

# PDF 转图片的缩放倍数（越大越清晰但越慢）
PDF_ZOOM = float(os.getenv("PDF_ZOOM", "1.5"))

# —— 导出字段（单一来源，避免多处重复定义）——
EXPORT_FIELDS = ["InvoiceNum", "InvoiceDate", "PurchaserName",
                 "SellerName", "TotalAmount", "TotalTax"]
EXPORT_FIELD_NAMES = ["发票号码", "开票日期", "购买方名称",
                      "销售方名称", "合计金额", "合计税额"]
