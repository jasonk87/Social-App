import sqlite3, time
conn = sqlite3.connect('social.db')
cur = conn.cursor()
cur.execute("SELECT id, title, created_at FROM posts ORDER BY created_at DESC LIMIT 5")
rows = cur.fetchall()
print(f"Current time: {time.time()}")
for r in rows:
    print(r)
conn.close()
