"""SQLite 连接、建表与基础工具。

设计要点：
- 所有写操作在同一事务内完成，失败即回滚，保证“改稿失败时原校样和签样照常可用”。
- WAL + busy_timeout，降低并发写入时的锁冲突。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS manuscripts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id),
    seq           INTEGER NOT NULL,          -- 该稿件第几次整稿提交，从 1 开始
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (manuscript_id, seq)
);

CREATE TABLE IF NOT EXISTS rectangles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES revisions(id),
    color       TEXT NOT NULL,               -- red / black
    layer       INTEGER NOT NULL,            -- 提交顺序，从 0 开始，小者在底
    x           INTEGER NOT NULL,
    y           INTEGER NOT NULL,
    w           INTEGER NOT NULL,
    h           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plates (
    manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id),
    color         TEXT NOT NULL,              -- red / black
    version       INTEGER NOT NULL DEFAULT 0, -- 当前编号，0 表示尚未出片
    bitmap        BLOB,                       -- 当前色版可印格位图（MSB-first，行优先）
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (manuscript_id, color)
);

CREATE TABLE IF NOT EXISTS signatures (
    manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id),
    color         TEXT NOT NULL,
    version       INTEGER NOT NULL,           -- 签样时对应的色版编号
    inspector     TEXT NOT NULL,
    signed_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (manuscript_id, color)
);
"""


def connect(db_path=None) -> sqlite3.Connection:
    path = str(db_path or config.DB_PATH)
    parent = config.DB_PATH.parent if db_path is None else Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path=None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务：正常结束提交，抛异常则回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
