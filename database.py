import sqlite3
import os

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "fitness_app.db")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _column_exists(cursor, table_name, column_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row["name"] for row in cursor.fetchall()]
    return column_name in columns


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            age INTEGER,
            height REAL,
            weight REAL,
            goal TEXT,
            diet TEXT,
            allergies TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan_id TEXT NOT NULL UNIQUE,
            parent_plan_id TEXT,
            title TEXT,
            user_input TEXT,
            json_file TEXT,
            nutrition_pdf TEXT,
            workout_pdf TEXT,
            daily_calories REAL,
            protein_g REAL,
            carbs_g REAL,
            fat_g REAL,
            version INTEGER DEFAULT 1,
            revision_request TEXT,
            plan_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan_id TEXT NOT NULL,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    if not _column_exists(cursor, "users", "is_admin"):
        cursor.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")

    if not _column_exists(cursor, "plans", "parent_plan_id"):
        cursor.execute("ALTER TABLE plans ADD COLUMN parent_plan_id TEXT")

    if not _column_exists(cursor, "plans", "plan_hash"):
        cursor.execute("ALTER TABLE plans ADD COLUMN plan_hash TEXT")

    conn.commit()
    conn.close()