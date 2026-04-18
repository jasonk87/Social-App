import sqlite3

conn = sqlite3.connect(':memory:')
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS simulation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT
)
""")
conn.commit()

try:
    cur.execute("ALTER TABLE simulation_events ADD COLUMN dedupe_key TEXT")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sim_events_dedupe ON simulation_events (dedupe_key) WHERE dedupe_key IS NOT NULL")
    cur.execute("""
        INSERT INTO simulation_events (dedupe_key) VALUES ('abc')
    """)
    cur.execute("""
        INSERT INTO simulation_events (dedupe_key) VALUES ('abc')
    """)
    print("Success")
except Exception as e:
    print(e)
