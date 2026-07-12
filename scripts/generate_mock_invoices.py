"""模拟发票台账生成器：生成虚构"星辰集团"体系的增值税发票记录到 invoice_records_simple.json

用法：
    python scripts/generate_mock_invoices.py

数据特点：
- 全部虚构，与任何真实企业无关；税号、银行账号、金额均为编造
- 结构完全匹配百度云 VAT OCR 返回字段，可直接被主程序的台账/导出功能读取
- 确定性生成（无随机数），便于演示复现
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

OUT = Path(__file__).parent.parent / "invoice_records_simple.json"

# 虚构"星辰集团"主体（购方，与 finance-rag 项目共用同一套虚构体系）+ 虚构外部供应商（销方）
BUYERS = [
    ("深圳星辰数字科技集团股份有限公司", "91440300MA5F1CT001X"),
    ("深圳市辰拓智能设备有限公司",       "91440300MA5F1CT002Y"),
    ("深圳星美智能材料有限公司",         "91440300MA5F1CT003Z"),
    ("惠州星辰实业有限公司",             "91441300MA5F1CT004A"),
]
SELLERS = [
    ("珠海东晟新材料科技有限公司",   "91440400MA5F1CT101B", "中国建设银行珠海分行", "44050100XXXXXX10001"),
    ("广东星源智能科技有限公司",     "91440600MA5F1CT102C", "中国工商银行佛山分行", "62220200XXXXXX20002"),
    ("上海鼎新包装材料有限公司",     "91310000MA5F1CT103D", "招商银行上海分行",     "12190800XXXXXX30003"),
    ("苏州锐驰精密机械有限公司",     "91320500MA5F1CT104E", "中国银行苏州分行",     "48730100XXXXXX40004"),
]
COMMODITIES = [
    ("*塑料制品*20L包装桶", "正方桶,灰色,20L", "个", "13%", 17.70),
    ("*机械设备*精密轴承",   "型号6205-2RS",     "套", "13%", 128.50),
    ("*化工产品*工业色浆",   "蓝色,25kg/桶",     "桶", "13%", 340.00),
    ("*五金件*不锈钢螺栓",   "M8x40,304材质",    "千个", "13%", 96.00),
    ("*电子元件*控制模块",   "PLC-X200",         "块", "13%", 560.00),
    ("*运输服务*物流费",     "整车运输",         "次", "9%",  1200.00),
]


def money(x):
    return round(x, 2)


def build_record(rec_id, buyer, seller, commodity, qty, date_str):
    b_name, b_tax = buyer
    s_name, s_tax, s_bank, s_acct = seller
    c_name, c_type, c_unit, c_rate_str, c_price = commodity
    rate = float(c_rate_str.strip("%")) / 100

    amount = money(c_price * qty)          # 不含税金额
    tax = money(amount * rate)             # 税额
    total_with_tax = money(amount + tax)   # 价税合计

    inv_num = f"244420000{rec_id:011d}"[:20]
    return {
        "id": rec_id,
        "time": date_str + " 09:%02d:00" % (rec_id % 60),
        "user": "演示用户",
        "data": {
            "InvoiceType": "电子发票(专用发票)",
            "InvoiceTypeOrg": "电子发票(增值税专用发票)",
            "InvoiceNum": inv_num,
            "InvoiceNumConfirm": inv_num,
            "InvoiceDate": date_str.replace("-", "年", 1).replace("-", "月", 1) + "日",
            "PurchaserName": b_name,
            "PurchaserRegisterNum": b_tax,
            "PurchaserAddress": "",
            "PurchaserBank": "",
            "SellerName": s_name,
            "SellerRegisterNum": s_tax,
            "SellerAddress": "",
            "SellerBank": "",
            "CommodityName": [{"row": "1", "word": c_name}],
            "CommodityType": [{"row": "1", "word": c_type}],
            "CommodityUnit": [{"row": "1", "word": c_unit}],
            "CommodityNum": [{"row": "1", "word": str(qty)}],
            "CommodityPrice": [{"row": "1", "word": str(c_price)}],
            "CommodityAmount": [{"row": "1", "word": str(amount)}],
            "CommodityTaxRate": [{"row": "1", "word": c_rate_str}],
            "CommodityTax": [{"row": "1", "word": str(tax)}],
            "TotalAmount": str(amount),
            "TotalTax": str(tax),
            "AmountInFiguers": str(total_with_tax),
            "AmountInWords": "（金额大写略）",
            "ServiceType": "其他",
            "Remarks": f"销方开户银行:{s_bank};银行账号:{s_acct}",
            "Agent": "否",
            "InvoiceTag": "其他",
        },
    }


def main():
    records = []
    base = datetime(2026, 3, 1)
    rec_id = 1
    for i in range(24):  # 生成 24 条演示记录
        buyer = BUYERS[i % len(BUYERS)]
        seller = SELLERS[i % len(SELLERS)]
        commodity = COMMODITIES[i % len(COMMODITIES)]
        qty = 10 + (i * 7) % 90
        date_str = (base + timedelta(days=i * 2)).strftime("%Y-%m-%d")
        records.append(build_record(rec_id, buyer, seller, commodity, qty, date_str))
        rec_id += 1

    OUT.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(float(r["data"]["AmountInFiguers"]) for r in records)
    print(f"已生成 {len(records)} 条虚构发票记录 → {OUT.name}")
    print(f"价税合计总额（虚构）：{total:,.2f} 元")


if __name__ == "__main__":
    main()
