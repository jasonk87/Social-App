import server
import sqlite3

def patch_schema():
    conn = server.get_db_connection()
    try:
        cur = conn.cursor()
        columns = {row[1] for row in cur.execute("PRAGMA table_info(simulation_events)").fetchall()}

        if "status" not in columns:
            cur.execute("ALTER TABLE simulation_events ADD COLUMN status TEXT DEFAULT 'pending'")
        if "attempts" not in columns:
            cur.execute("ALTER TABLE simulation_events ADD COLUMN attempts INTEGER DEFAULT 0")
        if "claimed_at" not in columns:
            cur.execute("ALTER TABLE simulation_events ADD COLUMN claimed_at REAL")
        if "last_error" not in columns:
            cur.execute("ALTER TABLE simulation_events ADD COLUMN last_error TEXT")
        if "dedupe_key" not in columns:
            cur.execute("ALTER TABLE simulation_events ADD COLUMN dedupe_key TEXT")

        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sim_events_dedupe ON simulation_events (dedupe_key) WHERE dedupe_key IS NOT NULL")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sim_events_status_time ON simulation_events (status, timestamp, priority)")

        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    patch_schema()
