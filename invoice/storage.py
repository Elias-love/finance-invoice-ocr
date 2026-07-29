"""SQLite 台账存储：替代原 JSON 文件，线程安全，支持并发写入。

每次操作开独立连接（SQLite 自带文件级锁，串行化写入），避免原 JSON
方案"整体重写文件 + 全局缓存无锁"导致的并发丢数据问题。
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

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
    raw_json      TEXT NOT NULL
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
        }
        for column, sql in migrations.items():
            if column not in existing:
                conn.execute(sql)


def add_record(invoice_data: dict, user: str = "匿名",
               source_sha256: str = "", validation: dict | None = None) -> int:
    """写入一条发票记录，返回自增 id。"""
    d = display_fields(invoice_data)
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO invoices
               (created_at, user, invoice_num, invoice_date,
                purchaser_name, seller_name, total_amount, total_tax,
                source_sha256, validation_status, validation_json, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user,
             d["invoice_num"], d["invoice_date"], d["purchaser_name"],
             d["seller_name"], d["total_amount"], d["total_tax"],
             source_sha256, (validation or {}).get("status", "review"),
             json.dumps(validation or {}, ensure_ascii=False),
             json.dumps(invoice_data, ensure_ascii=False)),
        )
        return cur.lastrowid


def get_all(newest_first: bool = True) -> list[dict]:
    """返回全部记录（含解析后的 raw 字段）。"""
    order = "DESC" if newest_first else "ASC"
    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM invoices ORDER BY id {order}").fetchall()
    result = []
    for r in rows:
        rec = dict(r)
        rec["data"] = json.loads(rec.pop("raw_json"))
        rec["validation"] = json.loads(rec.pop("validation_json") or "{}")
        result.append(rec)
    return result


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
    with _connect() as conn:
        conn.execute("DELETE FROM invoices")
