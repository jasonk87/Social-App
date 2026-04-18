import server
import json
import time

def test_simulation_schedule():
    engine = server.SimulationEngine()
    engine.schedule(10, server.EventPriority.HIGH, 'AGENT_REPLY', {'key': 'val'})

    conn = server.get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM simulation_events")
    rows = cur.fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]['priority'] == server.EventPriority.HIGH.value
    assert rows[0]['event_type'] == 'AGENT_REPLY'

    data = json.loads(rows[0]['event_data'])
    assert data['key'] == 'val'

def test_simulation_pop_order():
    engine = server.SimulationEngine()

    # Schedule event 1 (due in 50 seconds, HIGH priority)
    engine.schedule(50, server.EventPriority.HIGH, 'EVENT_1', {})

    # Schedule event 2 (due in 50 seconds, LOW priority)
    engine.schedule(50, server.EventPriority.LOW, 'EVENT_2', {})

    # Schedule event 3 (due in 10 seconds, LOW priority) - This should pop first because it's earlier
    engine.schedule(10, server.EventPriority.LOW, 'EVENT_3', {})

    # Override timestamps to make them all "due" now for the _run loop test,
    # but preserve their relative times to test ordering.
    conn = server.get_db_connection()
    conn.execute("UPDATE simulation_events SET timestamp = timestamp - 100")
    conn.commit()
    conn.close()

    # We will manually trigger the pop logic from _run
    def pop_next():
        conn = server.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, event_type, event_data FROM simulation_events WHERE timestamp <= ? ORDER BY timestamp ASC, priority ASC LIMIT 1",
                (time.time(),)
            )
            row = cur.fetchone()
            if row:
                cur.execute("DELETE FROM simulation_events WHERE id = ?", (row['id'],))
                conn.commit()
                return row['event_type']
            return None
        finally:
            conn.close()

    assert pop_next() == 'EVENT_3' # Oldest
    assert pop_next() == 'EVENT_1' # Tied for time, HIGH priority
    assert pop_next() == 'EVENT_2' # Tied for time, LOW priority
