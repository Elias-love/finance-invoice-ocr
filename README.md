# finance-invoice-ocr｜增值税发票识别与台账系统

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Flask](https://img.shields.io/badge/flask-web-black)
![License](https://img.shields.io/badge/license-MIT-green)

> 上传增值税发票（PDF / 图片），自动 OCR 识别关键字段并录入台账，支持批量识别、后台管理与 Excel/CSV 导出。面向财务日常的发票数字化场景。

**本仓库的发票台账数据均为脚本生成的虚构数据（"星辰集团"体系），与任何真实企业无关；OCR 密钥、管理员密码等敏感信息均从环境变量读取，不入库。**

## 功能

| 功能 | 说明 |
|------|------|
| 发票识别 | 上传增值税发票 PDF / 图片，调用百度云 VAT OCR 提取字段 |
| PDF 解析 | PyMuPDF 将 PDF 转图片后识别，支持多页 |
| 字段提取 | 发票号码、日期、购销方名称/税号、金额、税额、税率、商品明细等 |
| 台账录入 | 识别结果自动追加进台账，记录操作人与时间 |
| 后台管理 | 管理员登录后查看/清空台账、导出全量历史 |
| 数据导出 | 台账一键导出 Excel / CSV |

## 技术栈

Flask（单文件 Web 应用） · 百度云 VAT OCR · PyMuPDF（PDF 解析） · pandas + openpyxl（导出） · waitress（生产 WSGI）

## 架构

```
上传发票(PDF/图片)
      │
      ├─ PDF → PyMuPDF 转图片
      │
      ▼
百度云 VAT OCR ──► 字段提取 ──► 台账(JSON) ──► 前台展示 / 后台管理 / Excel·CSV 导出
```

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env                          # 填入百度 OCR 密钥与管理员密码
python scripts/generate_mock_invoices.py      # 生成虚构"星辰集团"发票台账（演示用）
python invoice_system_simple.py               # 打开 http://127.0.0.1:5007
```

- 前台 `http://127.0.0.1:5007` 无需登录即可上传识别
- 后台 `http://127.0.0.1:5007/admin` 用 `.env` 中的管理员账号登录，管理台账与导出
- 百度 OCR 密钥申请：https://cloud.baidu.com/product/ocr/invoice （不配置则识别功能不可用，但可浏览演示台账）

## 生产部署

```bash
# waitress（跨平台）
waitress-serve --port=5007 --threads=16 invoice_system_simple:app

# 或 gunicorn（Linux）
gunicorn -w 4 -b 0.0.0.0:5007 invoice_system_simple:app
```

## 安全说明

- 百度密钥、管理员密码、Flask 会话密钥**全部从环境变量读取**，源码零硬编码
- `.env` 已在 `.gitignore` 中，不会入库
- 演示台账 `invoice_records_simple.json` 为虚构数据；真实使用时该文件即业务台账，请勿公开

## 说明

本项目为个人作品集的脱敏演示版。原始版本用于真实发票批量数字化，此处替换为虚构数据、抽离密钥后开源，用于展示 OCR + 台账 + 导出的完整实现。

## License

MIT
