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
| 入账控制 | 必填字段、发票号码格式、价税勾稽、税号格式与历史重复检查 |
| 机器预审分流 | 按硬错误、警告、大额阈值和确定性抽样分为机器预审通过 / 待人工复核 |
| 人工复核 | 原票与字段并排展示，支持修正、批准、驳回、复核意见和完整操作轨迹 |
| 台账录入 | 非重复结果写入 SQLite，保留原始 OCR、人工修正版、文件 SHA-256 与控制证据 |
| 后台管理 | 管理员登录后查看复核队列、按状态筛选、清空台账、导出全量历史 |
| 数据导出 | 台账一键导出 Excel / CSV |

## 架构

```
上传发票(PDF/图片)
      │  app.py（路由 + 上传大小/类型校验）
      ├─ PDF → PyMuPDF 逐页转图片
      ▼
invoice/ocr.py ──► 百度 VAT OCR（带过期的 token 缓存 + 明确错误处理）
      ▼
invoice/extract.py（字段抽取） ──► invoice/validate.py（确定性控制 + 重复检查）
      ▼
invoice/review.py（证据分级 + 大额/抽样分流）
      ▼
invoice/storage.py（原始值/修正值分离 + 复核审计轨迹 + 原票授权预览）
      ▼
templates/ ──► 识别结果 / 复核队列 / 并排复核 / Excel·CSV
```

### 模块划分

```
├── app.py                    # 应用入口：路由、日志、上传校验、导出
├── config.py                 # 集中配置：环境变量 + 常量（字段定义单一来源）
├── invoice/
│   ├── ocr.py                # 百度 OCR 封装：token 缓存/过期、错误处理、PDF/图片识别
│   ├── storage.py            # SQLite 台账：线程安全，替代原 JSON 文件
│   ├── extract.py            # OCR 字段抽取
│   ├── validate.py           # 必填/格式/价税勾稽控制（不冒充税局真伪查验）
│   └── review.py             # 可解释证据分级、自动通过/异常/大额/抽样分流
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

- 所有识别、导出和台账页面均须先通过 `/admin` 登录；`ADMIN_PASSWORD` 未设置时系统保持锁定
- 单来源 IP 默认每分钟最多处理 60 个 OCR 页，单个 PDF 默认最多 30 页
- 百度 OCR 密钥申请：https://cloud.baidu.com/product/ocr/invoice （不配置则识别不可用，但可浏览演示台账）
- `access_token` 按百度返回的 `expires_in` 在内存缓存，提前 5 分钟自动刷新；若接口返回 token 过期/无效，自动刷新并重试一次

## 复核策略

标准 VAT OCR 接口返回结构化字段和部分辅助校验字段，但没有统一的字段级
`probability`。系统因此不展示虚构的“98% 置信度”，而使用可审计的证据等级：

1. 关键字段完整、格式正确、价税勾稽通过、无重复、未达到大额阈值
   → `机器预审通过`
2. 缺字段、格式/勾稽错误、辅助校验值冲突、历史重复
   → `待人工复核（高风险）`
3. 税号警告或达到 `HIGH_VALUE_REVIEW_AMOUNT`
   → `待人工复核（中风险）`
4. 其余机器预审通过记录按 `REVIEW_SAMPLE_RATE` 做确定性抽样质检

人工复核不会覆盖原始 OCR JSON：修正值单独保存，并记录操作者、时间、意见和
批准/驳回动作。机器预审通过只代表基础控制通过，不代表税局验真。
待复核和已驳回记录无法通过导出接口绕过流程；若将
`ALLOW_AUTO_PASS_EXPORT=0`，则只有人工批准记录可以导出。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖 token 缓存/自动刷新、复核分流、SQLite 原值/修正值分离、审计轨迹、
字段抽取、接口鉴权和 CSRF 等核心逻辑。

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
- **安全**：密钥全部环境变量化；识别/导出强制登录；付费 OCR 限流；文件扩展名与内容签名双校验；CSV/Excel 公式注入防护
- **可靠**：百度 token 带过期缓存，长期运行不会因 token 过期而静默失败
- **财务控制闭环**：取消任何随机“真伪有效”结论；后端只给出可复核的格式/勾稽/重复控制，并明确标注“非税局真伪查验”
- **例外管理**：低风险记录机器预审通过，高风险/大额/抽样记录进入人工队列，避免所有发票重新人工录入
- **审计可追溯**：原始识别值不可覆盖，人工修正值与批准/驳回轨迹单独留存
- **可测**：核心逻辑有单元测试与 CI

## 安全说明

- 百度密钥、管理员密码、Flask 会话密钥**全部从环境变量读取**，源码零硬编码
- `.env` 与 `data/`（SQLite 台账）均在 `.gitignore` 中，不会入库
- 发票图片会发送到百度云 OCR。真实部署前必须完成数据分级、供应商合规评估和用户授权；需要数据不出域时应替换为私有 OCR
- 演示数据为虚构；真实使用时台账即业务数据，请勿公开

## 评估边界

现有单元测试验证字段抽取、token 刷新、存储迁移、价税勾稽、重复检查、
复核状态流转和接口鉴权；它们不等于 OCR 准确率评估。生产试点前需建立人工
标注发票集，分别统计发票号码、税号、金额、税额、日期的字段级完全匹配率，
并记录自动通过率、人工触碰率、单张处理时间、接口成本和抽样差错率。达到
目标前，`AUTO_PASS_ENABLED` 应设置为 `0`。

## 说明

本项目为个人作品集的脱敏演示版。原始版本用于真实发票批量数字化，此处替换为虚构数据、抽离密钥、并做模块化与工程化改造后开源。

## License

MIT
