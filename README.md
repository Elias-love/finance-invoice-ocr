# finance-invoice-ocr｜增值税发票识别与台账系统

[![tests](https://github.com/Elias-love/finance-invoice-ocr/actions/workflows/tests.yml/badge.svg)](https://github.com/Elias-love/finance-invoice-ocr/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Flask](https://img.shields.io/badge/flask-web-black)
![License](https://img.shields.io/badge/license-MIT-green)

> 上传增值税发票（PDF / 图片），自动 OCR 识别关键字段并录入台账，支持批量识别、后台管理与 Excel/CSV 导出。面向财务日常的发票数字化场景。

**本仓库的发票台账数据均为脚本生成的虚构数据（"星辰集团"体系），与任何真实企业无关；OCR 密钥、管理员密码等敏感信息均从环境变量读取，不入库。**

## 功能

| 功能 | 说明 |
|------|------|
| 发票识别 | 上传增值税发票 PDF / 图片，调用百度云 VAT OCR 提取字段 |
| PDF 解析 | PyMuPDF 将 PDF 逐页转图片后识别，支持多页 |
| 字段提取 | 发票号码、日期、购销方名称/税号、金额、税额、税率、商品明细等 |
| 台账录入 | 识别结果写入 SQLite 台账，记录操作人与时间 |
| 后台管理 | 管理员登录后查看/清空台账、导出全量历史 |
| 数据导出 | 台账一键导出 Excel / CSV |

## 架构

```
上传发票(PDF/图片)
      │  app.py（路由 + 上传大小/类型校验）
      ├─ PDF → PyMuPDF 逐页转图片
      ▼
invoice/ocr.py ──► 百度 VAT OCR（带过期的 token 缓存 + 明确错误处理）
      ▼
invoice/extract.py（字段抽取） ──► invoice/storage.py（SQLite 台账，线程安全）
      ▼
templates/（Jinja 自动转义） ──► 前台展示 / 后台管理 / Excel·CSV 导出
```

### 模块划分

```
├── app.py                    # 应用入口：路由、日志、上传校验、导出
├── config.py                 # 集中配置：环境变量 + 常量（字段定义单一来源）
├── invoice/
│   ├── ocr.py                # 百度 OCR 封装：token 缓存/过期、错误处理、PDF/图片识别
│   ├── storage.py            # SQLite 台账：线程安全，替代原 JSON 文件
│   └── extract.py            # OCR 字段抽取
├── templates/                # Jinja 模板（HTML 与 Python 解耦，自动转义防 XSS）
├── scripts/generate_mock_invoices.py   # 虚构发票数据生成器
└── tests/                    # 单元测试（pytest）
```

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env                          # 填入百度 OCR 密钥与管理员密码
python scripts/generate_mock_invoices.py      # 生成虚构"星辰集团"发票台账（演示用）
python app.py                                 # 打开 http://127.0.0.1:5007
```

- 前台 `http://127.0.0.1:5007` 无需登录即可上传识别
- 后台 `http://127.0.0.1:5007/admin` 用 `.env` 中的管理员账号登录，管理台账与导出
- 百度 OCR 密钥申请：https://cloud.baidu.com/product/ocr/invoice （不配置则识别不可用，但可浏览演示台账）

## 测试

```bash
python -m pytest tests/ -q
```

覆盖 token 缓存/过期逻辑、SQLite 存储读写、字段抽取等核心逻辑。

## 生产部署

```bash
# waitress（跨平台）
waitress-serve --port=5007 --threads=16 app:app

# 或 gunicorn（Linux）
gunicorn -w 4 -b 0.0.0.0:5007 app:app
```

## 工程化要点

- **模块解耦**：OCR / 存储 / 抽取 / 路由 / 配置分离，HTML 移出 Python 进 Jinja 模板
- **SQLite 台账**：替代原 JSON 文件，独立连接 + 文件级锁，支持并发写入不丢数据
- **安全**：密钥全部环境变量化；Jinja 自动转义防 XSS；上传大小/类型校验；付费识别接口错误明确回传
- **可靠**：百度 token 带过期缓存，长期运行不会因 token 过期而静默失败
- **可测**：核心逻辑有单元测试与 CI

## 安全说明

- 百度密钥、管理员密码、Flask 会话密钥**全部从环境变量读取**，源码零硬编码
- `.env` 与 `data/`（SQLite 台账）均在 `.gitignore` 中，不会入库
- 演示数据为虚构；真实使用时台账即业务数据，请勿公开

## 说明

本项目为个人作品集的脱敏演示版。原始版本用于真实发票批量数字化，此处替换为虚构数据、抽离密钥、并做模块化与工程化改造后开源。

## License

MIT
