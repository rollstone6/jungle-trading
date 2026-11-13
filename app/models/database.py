import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "jungle.db"


def get_db():
    # 直接调用 get_db（如 scripts/refresh.py）时也要保证目录存在
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout/busy_timeout：锁等待有上限（10s）后抛错，绝不无限挂起事件循环。
    # 不再每连接执行 PRAGMA journal_mode=WAL —— 该 PRAGMA 需抢排他锁，是
    # 2026-11-13 全站超时事故里 py-spy 抓到的事件循环卡死点。WAL 是持久
    # 模式，只需在 init_db() 设置一次，后续连接自动继承。
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _migrate(conn):
    """为已存在的旧库补充新增列（ALTER TABLE 幂等，重复执行安全）。

    CREATE TABLE IF NOT EXISTS 不会给老表加新列，直接拉新代码跑旧库会
    在读取 positions 时 IndexError，这里显式做轻量迁移。
    """
    new_columns = [
        ("ma3", "REAL DEFAULT 0"),
        ("ma8", "REAL DEFAULT 0"),
        ("ma20", "REAL DEFAULT 0"),
        ("ma60", "REAL DEFAULT 0"),
        ("ma120", "REAL DEFAULT 0"),
        ("ma250", "REAL DEFAULT 0"),
        ("boll_upper", "REAL DEFAULT 0"),
        ("boll_lower", "REAL DEFAULT 0"),
        ("kdj_k", "REAL DEFAULT 0"),
        ("kdj_d", "REAL DEFAULT 0"),
        ("kdj_j", "REAL DEFAULT 0"),
        ("macd_dif", "REAL DEFAULT 0"),
        ("macd_dea", "REAL DEFAULT 0"),
        ("macd_hist", "REAL DEFAULT 0"),
        ("regime_label", "TEXT DEFAULT ''"),
    ]
    for name, ddl in new_columns:
        try:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.commit()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    # WAL 只需在此设置一次（持久化到库文件），get_db() 不再每连接重复执行，
    # 避免该 PRAGMA 抢排他锁阻塞事件循环（2026-11-13 全站超时事故根因）。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            principal REAL NOT NULL DEFAULT 300000,
            total_asset REAL NOT NULL DEFAULT 300000
        );
        CREATE TABLE IF NOT EXISTS positions (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            sector TEXT DEFAULT '',
            quantity INTEGER NOT NULL,
            cost_price REAL NOT NULL,
            latest_price REAL DEFAULT 0,
            change_pct REAL DEFAULT 0,
            high REAL DEFAULT 0,
            low REAL DEFAULT 0,
            open_price REAL DEFAULT 0,
            volume REAL DEFAULT 0,
            kline_data TEXT DEFAULT '[]',
            ma3 REAL DEFAULT 0,
            ma8 REAL DEFAULT 0,
            ma20 REAL DEFAULT 0,
            ma60 REAL DEFAULT 0,
            ma120 REAL DEFAULT 0,
            ma250 REAL DEFAULT 0,
            boll_upper REAL DEFAULT 0,
            boll_lower REAL DEFAULT 0,
            kdj_k REAL DEFAULT 0,
            kdj_d REAL DEFAULT 0,
            kdj_j REAL DEFAULT 0,
            macd_dif REAL DEFAULT 0,
            macd_dea REAL DEFAULT 0,
            macd_hist REAL DEFAULT 0,
            regime_label TEXT DEFAULT '',
            volume_ratio REAL DEFAULT 0,
            news TEXT DEFAULT '[]',
            risk_level TEXT DEFAULT '中',
            badge TEXT DEFAULT '观察',
            fundamental TEXT DEFAULT '',
            risk_notes TEXT DEFAULT '[]',
            quote_time TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reports (
            date TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO account (id, principal, total_asset) VALUES (1, 300000, 300000);
    """)
    _migrate(conn)
    conn.commit()
    conn.close()
