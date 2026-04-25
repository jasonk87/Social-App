import server

def test_add_community():
    comm_id = server.add_community("TestComm", "A description", "dummy-model", 60)
    assert comm_id is not None

    # Retrieve it
    row = server.get_community_by_name("TestComm")
    assert row is not None
    assert row['name'] == "TestComm"
    assert row['description'] == "A description"

def test_add_agent():
    agent_id = server.add_agent("test_agent", "dummy-model", "test persona")
    assert agent_id is not None

    conn = server.get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
    row = cur.fetchone()
    conn.close()

    assert row is not None
    assert row['username'] == "test_agent"

def test_add_post_and_comment():
    comm_id = server.add_community("PostComm", "Desc", "dummy-model", 60)
    agent_id = server.add_agent("poster", "dummy", "persona")

    post_id = server.add_post(comm_id, agent_id, "Test Title", "Test Content")
    assert post_id is not None

    comment_id = server.add_comment(post_id, agent_id, None, "Test Comment")
    assert comment_id is not None

    # Verify feed retrieval
    feed, has_more = server.fetch_community_feed(comm_id)
    assert len(feed) == 1
    assert feed[0]['title'] == "Test Title"
    assert len(feed[0]['comments']) == 1
    assert feed[0]['comments'][0]['content'] == "Test Comment"
