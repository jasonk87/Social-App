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

    cur.execute("SELECT status, attempts, claimed_at, timestamp FROM simulation_events WHERE id = ?", (event.id,))
    row = cur.fetchone()
    assert row['status'] == 'processing'
    assert row['attempts'] == 1
    assert row['claimed_at'] is not None
    orig_timestamp = row['timestamp']

    # Mark it failed
    engine._mark_event_failed(event.id, "some error")
    cur.execute("SELECT status, last_error, timestamp, claimed_at FROM simulation_events WHERE id = ?", (event.id,))
    row = cur.fetchone()
    assert row['status'] == 'pending' # still under 3 attempts
    assert row['last_error'] == 'some error'
    assert row['claimed_at'] is None
    assert row['timestamp'] > orig_timestamp # backoff applied

    # Claim it again to move to attempts=2
    event2 = engine._claim_next_due_event()
    # It won't be claimable immediately because timestamp moved forward, so we hack the timestamp back
    conn.execute("UPDATE simulation_events SET timestamp = timestamp - 100 WHERE id = ?", (event.id,))
    conn.commit()

    event2 = engine._claim_next_due_event()
    assert event2 is not None

    # Fail it up to terminal
    engine._mark_event_failed(event.id, "error 2") # attempts=2

    # We must claim it again so attempts increments from 2 to 3
    conn.execute("UPDATE simulation_events SET timestamp = timestamp - 100 WHERE id = ?", (event.id,))
    conn.commit()
    engine._claim_next_due_event()

    engine._mark_event_failed(event.id, "error 3") # attempts=3
    cur.execute("SELECT status, attempts, claimed_at FROM simulation_events WHERE id = ?", (event.id,))
    row = cur.fetchone()
    assert row['attempts'] == 3
    assert row['status'] == 'failed' # Reached terminal failure
    assert row['claimed_at'] is None

    # Verify terminal status dedupe key override behavior
    engine.schedule(0, server.EventPriority.HIGH, 'TERM_TEST', {}, dedupe_key='term_key', replace_existing=False)
    # Set to failed
    conn.execute("UPDATE simulation_events SET status = 'failed' WHERE dedupe_key = 'term_key'")
    conn.commit()

    # Schedule with replace_existing=False. It SHOULD overwrite because it's terminal.
    engine.schedule(0, server.EventPriority.HIGH, 'TERM_TEST', {}, dedupe_key='term_key', replace_existing=False)
    cur.execute("SELECT status, claimed_at FROM simulation_events WHERE dedupe_key = 'term_key'")
    row = cur.fetchone()
    assert row['status'] == 'pending'
    assert row['claimed_at'] is None

    conn.close()

def test_simulation_restart_semantics():
    engine = server.SimulationEngine()

    # 1. Processing events reset cleanly on restart
    engine.schedule(-10, server.EventPriority.HIGH, 'STUCK_PROCESS', {})
    event = engine._claim_next_due_event()
    assert event is not None

    conn = server.get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT status FROM simulation_events WHERE id = ?", (event.id,))
    assert cur.fetchone()['status'] == 'processing'

    # Simulate restart logic
    conn.execute("UPDATE simulation_events SET status = 'pending', claimed_at = NULL WHERE status = 'processing'")
    conn.commit()

    cur.execute("SELECT status, claimed_at FROM simulation_events WHERE id = ?", (event.id,))
    row = cur.fetchone()
    assert row['status'] == 'pending'
    assert row['claimed_at'] is None

    # 2. No duplicate COMMUNITY_POST scheduling after restart
    engine.schedule(100, server.EventPriority.LOW, 'COMMUNITY_POST', {'community_id': 99}, dedupe_key='COMMUNITY_POST_99', replace_existing=False)

    # Count rows before "restart" schedule
    cur.execute("SELECT COUNT(*) as count FROM simulation_events WHERE dedupe_key = 'COMMUNITY_POST_99'")
    assert cur.fetchone()['count'] == 1

    # Simulate restart trying to schedule again
    engine.schedule(200, server.EventPriority.LOW, 'COMMUNITY_POST', {'community_id': 99}, dedupe_key='COMMUNITY_POST_99', replace_existing=False)

    # Verify no duplicate was added and the timestamp wasn't overridden (it shouldn't replace existing non-terminal)
    cur.execute("SELECT COUNT(*) as count FROM simulation_events WHERE dedupe_key = 'COMMUNITY_POST_99'")
    assert cur.fetchone()['count'] == 1

    cur.execute("SELECT timestamp FROM simulation_events WHERE dedupe_key = 'COMMUNITY_POST_99'")
    # Still the original +100s stamp
    ts = cur.fetchone()['timestamp']
    assert ts < time.time() + 150

    conn.close()

def test_community_state_update():
    # Insert dummy community
    conn = server.get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO communities (name) VALUES ('test_comm')")
    comm_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Initialize simulation instance
    server.SIMULATIONS[comm_id] = server.Simulation(comm_id, "test_comm", "desc", "none", 60)

    # Test update triggers correctly
    server.update_community_state(comm_id, "This is a new test topic about apples", is_argumentative=True, is_new_post=True)

    sim = server.SIMULATIONS[comm_id]

    # Energy should go up for new post
    assert sim.energy > 1.0
    # Conflict should go up, mood down
    assert sim.conflict_level > 0.0
    assert sim.mood < 0.0

    # Topics should contain "test" and "topic" and "about" (due to stopword filtering and count logic)
    topics = [t['topic'] for t in sim.current_topics]
    assert "test" in topics
    assert "topic" in topics
    assert len(topics) <= 3 # extract_topics grabs top 3

    # Test drift decay
    sim.energy = 2.0
    sim.mood = -0.5
    sim.conflict_level = 0.5

    engine = server.SimulationEngine()
    engine._do_community_drift({'community_id': comm_id})

    # Verify decay
    assert sim.energy < 2.0
    assert sim.mood > -0.5
    assert sim.conflict_level < 0.5
