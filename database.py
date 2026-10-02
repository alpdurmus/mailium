import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "automation.db")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Enable WAL for better concurrency and set busy timeout
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # SMTP Settings table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS smtp_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT,
            password TEXT,
            sender_email TEXT NOT NULL,
            sender_name TEXT,
            use_tls INTEGER DEFAULT 1,
            use_ssl INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Campaigns table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            is_html INTEGER DEFAULT 1,
            email_column TEXT DEFAULT 'Email',
            recipients_data TEXT NOT NULL, -- JSON string of parsed excel rows
            columns_data TEXT NOT NULL,    -- JSON string of available columns
            schedule_type TEXT NOT NULL,   -- 'immediate', 'once', 'monthly', 'yearly', 'daily'
            schedule_time TEXT,           -- ISO string or time string e.g. "09:00" or "2026-09-01T09:00"
            day_of_month INTEGER,         -- For monthly scheduling
            month_of_year INTEGER,        -- For yearly scheduling
            day_of_week INTEGER,          -- For weekly scheduling
            status TEXT DEFAULT 'scheduled', -- 'draft', 'scheduled', 'completed', 'paused', 'failed'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_run_at TIMESTAMP,
            next_run_at TIMESTAMP
        )
    """)
    # Migration: add attachments_data column if missing (for file attachments)
    try:
        cursor.execute("SELECT attachments_data FROM campaigns LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE campaigns ADD COLUMN attachments_data TEXT DEFAULT '[]'")

    # Audit Logs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            recipient_email TEXT,
            subject TEXT,
            status TEXT NOT NULL, -- 'SUCCESS', 'FAILED', 'INFO'
            message TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
        )
    """)

    # Users table for auth
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Add user_id to existing tables for future per-user isolation (migration)
    for tbl in ["campaigns", "smtp_settings", "audit_logs"]:
        try:
            cursor.execute(f"SELECT user_id FROM {tbl} LIMIT 1")
        except sqlite3.OperationalError:
            try:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER REFERENCES users(id)")
            except Exception:
                pass

    # Per-user AI settings (LLM API) — each account has its own endpoint/api_key/model
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            provider TEXT DEFAULT 'openai',
            endpoint TEXT NOT NULL,
            api_key TEXT NOT NULL,
            model TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Per-user Company branding — used by AI for fancy HTML & writings when configured
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS company_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            company_name TEXT,
            slogan TEXT,
            website TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            primary_color TEXT DEFAULT '#4f46e5',
            logo_filename TEXT,
            logo_url TEXT,
            use_in_ai INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Migrate legacy global ai_config.json to first user if exists and no per-user rows yet
    try:
        if os.path.exists(os.path.join(os.path.dirname(__file__), "ai_config.json")):
            cursor.execute("SELECT COUNT(*) as c FROM ai_settings")
            if cursor.fetchone()["c"] == 0:
                cursor.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1")
                first = cursor.fetchone()
                if first:
                    import json as _j
                    with open(os.path.join(os.path.dirname(__file__), "ai_config.json"), "r", encoding="utf-8") as f:
                        data = _j.load(f)
                    cursor.execute("INSERT OR IGNORE INTO ai_settings (user_id, provider, endpoint, api_key, model) VALUES (?,?,?,?,?)",
                                   (first["id"], data.get("provider","openai"), data.get("endpoint","https://api.openai.com/v1"), data.get("api_key",""), data.get("model","gpt-4o-mini")))
    except Exception:
        pass

    # Migrate legacy global data (user_id IS NULL) to first user for backward compat
    # After this, per-user isolation is strict — new accounts start empty
    try:
        cursor.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1")
        first_user = cursor.fetchone()
        if first_user:
            fid = first_user["id"]
            for tbl in ["smtp_settings", "campaigns"]:
                try:
                    cursor.execute(f"UPDATE {tbl} SET user_id=? WHERE user_id IS NULL", (fid,))
                except Exception:
                    pass
            # audit_logs: try to infer user_id from campaign, else assign to first user
            try:
                cursor.execute("SELECT COUNT(*) as c FROM audit_logs WHERE user_id IS NULL")
                if cursor.fetchone()["c"] > 0:
                    cursor.execute("UPDATE audit_logs SET user_id=? WHERE user_id IS NULL", (fid,))
            except Exception:
                pass
    except Exception:
        pass

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
