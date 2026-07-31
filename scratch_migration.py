import sqlite3
import os

def migrate_test_db():
    conn = sqlite3.connect('/tmp/test_migration.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS dataset_progress (
        user TEXT,
        lang TEXT,
        current_stage INTEGER DEFAULT 0,
        current_day INTEGER DEFAULT 0,
        sessions_done_today INTEGER DEFAULT 0,
        last_practice_date DATE,
        PRIMARY KEY (user, lang)
    )''')
    print("Migration successful")
    conn.close()

migrate_test_db()
