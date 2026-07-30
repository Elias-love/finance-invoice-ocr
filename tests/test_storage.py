"""SQLite 台账存储测试。"""

from pathlib import Path

import pytest

import config
from invoice import storage


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    storage.init_db()
    yield


SAMPLE = {
    "InvoiceNum": "24442000000000000001",
    "InvoiceDate": "2026年03月01日",
    "PurchaserName": "深圳星辰数字科技集团股份有限公司",
    "SellerName": "珠海东晟新材料科技有限公司",
    "TotalAmount": "1000.00",
    "TotalTax": "130.00",
}


def test_add_and_count(temp_db):
    assert storage.count() == 0
    rid = storage.add_record(SAMPLE, user="tester")
    assert rid == 1
    assert storage.count() == 1


def test_get_all_roundtrip(temp_db):
    storage.add_record(SAMPLE)
    records = storage.get_all()
    assert len(records) == 1
    r = records[0]
    assert r["invoice_num"] == SAMPLE["InvoiceNum"]
    assert r["data"]["SellerName"] == SAMPLE["SellerName"]  # 原始 JSON 完整保留


def test_audit_metadata_roundtrip(temp_db):
    validation = {"status": "pass", "message": "基础校验通过"}
    storage.add_record(
        SAMPLE,
        user="auditor",
        source_sha256="abc123",
        validation=validation,
    )
    record = storage.get_all()[0]
    assert record["source_sha256"] == "abc123"
    assert record["validation_status"] == "pass"
    assert record["validation"]["message"] == "基础校验通过"


def test_source_page_roundtrip(temp_db):
    storage.add_record(SAMPLE, source_page=7)
    assert storage.get_all()[0]["source_page"] == 7


def test_newest_first_order(temp_db):
    storage.add_record({**SAMPLE, "InvoiceNum": "A"})
    storage.add_record({**SAMPLE, "InvoiceNum": "B"})
    ids = [r["id"] for r in storage.get_all(newest_first=True)]
    assert ids == [2, 1]


def test_count_on_date(temp_db):
    storage.add_record(SAMPLE)
    # created_at 用当天时间，count_on 未来日期应为 0
    assert storage.count_on("1999-01-01") == 0


def test_clear(temp_db):
    storage.add_record(SAMPLE)
    storage.clear()
    assert storage.count() == 0
    assert storage.get_all() == []


def test_clear_removes_authorized_source_files(temp_db):
    source = storage.save_source(b"%PDF-demo", ".pdf", "abc123")
    storage.add_record(SAMPLE, source_path=source)
    storage.clear()
    assert not Path(source).exists()


def test_review_update_preserves_original_and_writes_audit(temp_db):
    review = {
        "review_status": "pending",
        "risk_level": "medium",
        "message": "待复核",
    }
    rid = storage.add_record(SAMPLE, review=review)
    corrected = {**SAMPLE, "SellerName": "人工修正销售方"}
    validation = {"status": "pass", "message": "基础校验通过"}
    storage.update_review(
        rid,
        corrected_data=corrected,
        review_status="approved",
        reviewer="auditor",
        note="已核对原票",
        validation=validation,
        review={**review, "risk_level": "low"},
    )

    record = storage.get_by_id(rid)
    assert record["original_data"]["SellerName"] == SAMPLE["SellerName"]
    assert record["data"]["SellerName"] == "人工修正销售方"
    assert record["review_status"] == "approved"
    events = storage.get_review_events(rid)
    assert [e["action"] for e in events] == ["approved", "recognized"]


def test_latest_source_batch_returns_all_pages_of_latest_file(temp_db):
    storage.add_record(
        {"InvoiceNum": "OLD"},
        source_sha256="old-source",
    )
    first = storage.add_record(
        {"InvoiceNum": "NEW-1"},
        source_sha256="new-source",
    )
    second = storage.add_record(
        {"InvoiceNum": "NEW-2"},
        source_sha256="new-source",
    )
    batch = storage.get_latest_source_batch()
    assert [record["id"] for record in batch] == [first, second]
    assert [record["data"]["InvoiceNum"] for record in batch] == [
        "NEW-1",
        "NEW-2",
    ]
