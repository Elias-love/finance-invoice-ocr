"""SQLite 台账存储：替代原 JSON 文件，线程安全，支持并发写入。

每次操作开独立连接（SQLite 自带文件级锁，串行化写入），避免原 JSON
方案"整体重写文件 + 全局缓存无锁"导致的并发丢数据问题。
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import config
from invoice.extract import display_fields

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    user          TEXT,
    invoice_num   TEXT,
    invoice_date  TEXT,
    purchaser_name TEXT,
    seller_name   TEXT,
    total_amount  TEXT,
    total_tax     TEXT,
    source_sha256 TEXT,
    validation_status TEXT NOT NULL DEFAULT 'review',
    validation_json TEXT NOT NULL DEFAULT '{}',
    review_status TEXT NOT NULL DEFAULT 'pending',
    risk_level TEXT NOT NULL DEFAULT 'medium',
    review_json TEXT NOT NULL DEFAULT '{}',
    corrected_json TEXT,
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_note TEXT,
    source_filename TEXT,
    source_path TEXT,
    raw_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);
"""


@contextmanager
def _connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        existing = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
        migrations = {
            "source_sha256": "ALTER TABLE invoices ADD COLUMN source_sha256 TEXT",
            "validation_status": (
                "ALTER TABLE invoices ADD COLUMN validation_status "
                "TEXT NOT NULL DEFAULT 'review'"
            ),
            "validation_json": (
                "ALTER TABLE invoices ADD COLUMN validation_json "
                "TEXT NOT NULL DEFAULT '{}'"
            ),
            "review_status": (
                "ALTER TABLE invoices ADD COLUMN review_status "
                "TEXT NOT NULL DEFAULT 'pending'"
            ),
            "risk_level": (
                "ALTER TABLE invoices ADD COLUMN risk_level "
                "TEXT NOT NULL DEFAULT 'medium'"
            ),
            "review_json": (
                "ALTER TABLE invoices ADD COLUMN review_json "
                "TEXT NOT NULL DEFAULT '{}'"
            ),
            "corrected_json": "ALTER TABLE invoices ADD COLUMN corrected_json TEXT",
            "reviewed_at": "ALTER TABLE invoices ADD COLUMN reviewed_at TEXT",
            "reviewed_by": "ALTER TABLE invoices ADD COLUMN reviewed_by TEXT",
            "review_note": "ALTER TABLE invoices ADD COLUMN review_note TEXT",
            "source_filename": "ALTER TABLE invoices ADD COLUMN source_filename TEXT",
            "source_path": "ALTER TABLE invoices ADD COLUMN source_path TEXT",
        }
        for column, sql in migrations.items():
            if column not in existing:
                conn.execute(sql)


def add_record(invoice_data: dict, user: str = "匿名",
               source_sha256: str = "", validation: dict | None = None,
               review: dict | None = None, source_filename: str = "",
               source_path: str = "") -> int:
    """写入一条发票记录，返回自增 id。"""
    d = display_fields(invoice_data)
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO invoices
               (created_at, user, invoice_num, invoice_date,
                purchaser_name, seller_name, total_amount, total_tax,
                source_sha256, validation_status, validation_json,
                review_status, risk_level, review_json,
                source_filename, source_path, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user,
             d["invoice_num"], d["invoice_date"], d["purchaser_name"],
             d["seller_name"], d["total_amount"], d["total_tax"],
             source_sha256, (validation or {}).get("status", "review"),
             json.dumps(validation or {}, ensure_ascii=False),
             (review or {}).get("review_status", "pending"),
             (review or {}).get("risk_level", "medium"),
             json.dumps(review or {}, ensure_ascii=False),
             source_filename, source_path,
             json.dumps(invoice_data, ensure_ascii=False)),
        )
        conn.execute(
            """INSERT INTO review_events
               (invoice_id, created_at, actor, action, detail_json)
               VALUES (?,?,?,?,?)""",
            (
                cur.lastrowid,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                user,
                "recognized",
                json.dumps({
                    "review_status": (review or {}).get("review_status", "pending"),
                    "risk_level": (review or {}).get("risk_level", "medium"),
                }, ensure_ascii=False),
            ),
        )
        return cur.lastrowid


def get_all(newest_first: bool = True) -> list[dict]:
    """返回全部记录（含解析后的 raw 字段）。"""
    order = "DESC" if newest_first else "ASC"
    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM invoices ORDER BY id {order}").fetchall()
    result = []
    for r in rows:
        rec = _decode_record(r)
        result.append(rec)
    return result


def _decode_record(row) -> dict:
    rec = dict(row)
    raw = json.loads(rec.pop("raw_json"))
    corrected = json.loads(rec["corrected_json"]) if rec.get("corrected_json") else None
    rec["original_data"] = raw
    rec["data"] = corrected or raw
    rec["validation"] = json.loads(rec.pop("validation_json") or "{}")
    rec["review"] = json.loads(rec.pop("review_json") or "{}")
    return rec


def get_by_id(record_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (record_id,)).fetchone()
    return _decode_record(row) if row else None


def get_by_ids(record_ids: list[int]) -> list[dict]:
    ids = list(dict.fromkeys(int(i) for i in record_ids if str(i).isdigit()))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM invoices WHERE id IN ({placeholders})", ids
        ).fetchall()
    decoded = [_decode_record(row) for row in rows]
    order = {rid: idx for idx, rid in enumerate(ids)}
    return sorted(decoded, key=lambda r: order.get(r["id"], len(order)))


def get_review_events(record_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM review_events WHERE invoice_id = ? ORDER BY id DESC",
            (record_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json") or "{}")
        result.append(item)
    return result


def count_by_review_status(status: str | None = None) -> int:
    with _connect() as conn:
        if status:
            return conn.execute(
                "SELECT COUNT(*) FROM invoices WHERE review_status = ?", (status,)
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE review_status = 'pending'"
        ).fetchone()[0]


def update_review(
    record_id: int,
    *,
    corrected_data: dict,
    review_status: str,
    reviewer: str,
    note: str,
    validation: dict,
    review: dict,
) -> None:
    if review_status not in {"pending", "approved", "rejected"}:
        raise ValueError("不支持的复核状态")
    review = {**review, "review_status": review_status}
    d = display_fields(corrected_data)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        conn.execute(
            """UPDATE invoices SET
               invoice_num=?, invoice_date=?, purchaser_name=?, seller_name=?,
               total_amount=?, total_tax=?, corrected_json=?,
               validation_status=?, validation_json=?,
               review_status=?, risk_level=?, review_json=?,
               reviewed_at=?, reviewed_by=?, review_note=?
               WHERE id=?""",
            (
                d["invoice_num"], d["invoice_date"], d["purchaser_name"],
                d["seller_name"], d["total_amount"], d["total_tax"],
                json.dumps(corrected_data, ensure_ascii=False),
                validation.get("status", "review"),
                json.dumps(validation, ensure_ascii=False),
                review_status, review.get("risk_level", "medium"),
                json.dumps(review, ensure_ascii=False),
                now, reviewer, note, record_id,
            ),
        )
        conn.execute(
            """INSERT INTO review_events
               (invoice_id, created_at, actor, action, detail_json)
               VALUES (?,?,?,?,?)""",
            (
                record_id, now, reviewer, review_status,
                json.dumps({
                    "note": note,
                    "validation_status": validation.get("status"),
                    "changed": corrected_data,
                }, ensure_ascii=False),
            ),
        )


def initialize_review_metadata(
    record_id: int,
    *,
    validation: dict,
    review: dict,
) -> None:
    """为升级前的历史记录补齐机器预审元数据，不改动原始 OCR 内容。"""
    with _connect() as conn:
        current = conn.execute(
            "SELECT review_json FROM invoices WHERE id = ?", (record_id,)
        ).fetchone()
        if not current or (current[0] and current[0] != "{}"):
            return
        conn.execute(
            """UPDATE invoices SET validation_status=?, validation_json=?,
               review_status=?, risk_level=?, review_json=? WHERE id=?""",
            (
                validation.get("status", "review"),
                json.dumps(validation, ensure_ascii=False),
                review.get("review_status", "pending"),
                review.get("risk_level", "medium"),
                json.dumps(review, ensure_ascii=False),
                record_id,
            ),
        )


def save_source(content: bytes, ext: str, source_sha256: str) -> str:
    """保存原始凭证供授权复核查看，文件名只使用哈希避免路径注入。"""
    upload_dir = config.DB_PATH.parent / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{source_sha256}{ext.lower()}"
    if not path.exists():
        path.write_bytes(content)
    return str(path)


def count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]


def count_on(date_str: str) -> int:
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE created_at LIKE ?",
            (date_str + "%",)).fetchone()[0]


def invoice_exists(invoice_num: str) -> bool:
    """按发票号码检查历史台账重复；空号码不参与查重。"""
    if not invoice_num:
        return False
    with _connect() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM invoices WHERE invoice_num = ? LIMIT 1",
            (invoice_num,),
        ).fetchone())


def clear():
    paths = []
    with _connect() as conn:
        paths = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT source_path FROM invoices "
                "WHERE source_path IS NOT NULL AND source_path <> ''"
            ).fetchall()
        ]
        conn.execute("DELETE FROM review_events")
        conn.execute("DELETE FROM invoices")
    allowed_dir = (config.DB_PATH.parent / "uploads").resolve()
    for raw_path in paths:
        try:
            path = Path(raw_path).resolve()
            if path.parent == allowed_dir and path.exists():
                path.unlink()
        except OSError:
            pass
