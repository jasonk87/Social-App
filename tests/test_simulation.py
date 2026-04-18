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

    # We will use the new claim logic
    def pop_next():
        event = engine._claim_next_due_event()
        return event.event_type if event else None

    assert pop_next() == 'EVENT_3' # Oldest
    assert pop_next() == 'EVENT_1' # Tied for time, HIGH priority
    assert pop_next() == 'EVENT_2' # Tied for time, LOW priority


def test_simulation_dedupe_replace():
    engine = server.SimulationEngine()

    # Schedule first event
    engine.schedule(50, server.EventPriority.LOW, 'EVENT_DUP', {'v': 1}, dedupe_key='key1', replace_existing=True)

    # Schedule replacement event with same key but different priority and data
    engine.schedule(10, server.EventPriority.HIGH, 'EVENT_DUP', {'v': 2}, dedupe_key='key1', replace_existing=True)

    conn = server.get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM simulation_events WHERE dedupe_key = 'key1'")
    rows = cur.fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]['priority'] == server.EventPriority.HIGH.value
    data = json.loads(rows[0]['event_data'])
    assert data['v'] == 2


def test_simulation_lifecycle():
    engine = server.SimulationEngine()

    # Schedule an event due immediately
    engine.schedule(-10, server.EventPriority.HIGH, 'LIFECYCLE_TEST', {})

    conn = server.get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, status FROM simulation_events WHERE event_type = 'LIFECYCLE_TEST'")
    row = cur.fetchone()
    assert row['status'] == 'pending'

    # Claim it
    event = engine._claim_next_due_event()
    assert event is not None
    assert event.event_type == 'LIFECYCLE_TEST'

    cur.execute("SELECT status, attempts, claimed_at FROM simulation_events WHERE id = ?", (event.id,))
    row = cur.fetchone()
    assert row['status'] == 'processing'
    assert row['attempts'] == 1
    assert row['claimed_at'] is not None

    # Mark it failed
    engine._mark_event_failed(event.id, "some error")
    cur.execute("SELECT status, last_error FROM simulation_events WHERE id = ?", (event.id,))
    row = cur.fetchone()
    assert row['status'] == 'pending' # still under 3 attempts
    assert row['last_error'] == 'some error'

    # Mark it completed
    engine._mark_event_completed(event.id)
    cur.execute("SELECT status FROM simulation_events WHERE id = ?", (event.id,))
    row = cur.fetchone()
    assert row['status'] == 'completed'
    conn.close()
