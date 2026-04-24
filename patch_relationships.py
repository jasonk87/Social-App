import server
import sqlite3

def patch_schema():
    conn = server.get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_relationships (
                agent1_id INTEGER NOT NULL,
                agent2_id INTEGER NOT NULL,
                relationship_score REAL DEFAULT 0.0,
                PRIMARY KEY (agent1_id, agent2_id),
                FOREIGN KEY (agent1_id) REFERENCES agents(id) ON DELETE CASCADE,
                FOREIGN KEY (agent2_id) REFERENCES agents(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    patch_schema()
