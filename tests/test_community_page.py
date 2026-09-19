import re
import threading

import pytest
import requests
from http.server import ThreadingHTTPServer

import server


@pytest.fixture(scope="module")
def api_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.RequestHandler)
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}"

    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=1)


def test_new_community_creation_schedules_community_drift(api_server):
    register_response = requests.post(
        f"{api_server}/api/register",
        json={"display_name": "Scheduler User", "pin": "4321"},
    )
    assert register_response.status_code == 200
    cookies = register_response.cookies

    create_response = requests.post(
        f"{api_server}/api/communities",
        json={"name": "Sched_Comm", "model": "dummy", "description": "drift test"},
        cookies=cookies,
    )
    assert create_response.status_code == 200
    community_id = create_response.json()["community_id"]

    conn = server.get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT event_type, dedupe_key, status
            FROM simulation_events
            WHERE dedupe_key = ?
            """,
            (f"COMMUNITY_DRIFT_{community_id}",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["event_type"] == "COMMUNITY_DRIFT"
    assert row["status"] == "pending"


def test_community_page_sse_refresh_condition_logic_present():
    with open("static/community.js", "r", encoding="utf-8") as f:
        source = f.read()

    # Ensure the SSE refresh guard checks the active local community state.
    assert "const shouldRefreshForEvent = (eventData) => (" in source
    assert "Number(communityPageState.community.id) === Number(eventData.community_id)" in source
    assert "eventSource.addEventListener('new_post'" in source
    assert "eventSource.addEventListener('new_comment'" in source
    assert "const scheduleSseRefresh = () => {" in source

    # Both handlers should trigger the shared debounced refresh path.
    assert source.count("scheduleSseRefresh();") >= 2


def test_community_page_initial_state_panel_load_path_present():
    with open("static/community.js", "r", encoding="utf-8") as f:
        source = f.read()

    pattern = re.compile(
        r"await loadFeed\(name,\s*\{[^}]*silentNotifications:\s*true[^}]*focusTarget:\s*true[^}]*\}\);\s*"
        r"await loadCommunityState\(\);",
        re.DOTALL,
    )
    assert pattern.search(source)
