import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "jungle.db"


def get_db():
    # 直接调用 get_db（如 scripts/refresh.py）时也要保证目录存在
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
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
    conn.commit()
    conn.close()
