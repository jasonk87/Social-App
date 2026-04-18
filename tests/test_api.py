import pytest
import threading
import time
import requests
import json
from http.server import ThreadingHTTPServer

import server

@pytest.fixture(scope="module")
def api_server():
    # Because DB is recreated per test via conftest, we don't start the simulation engine in tests
    # to avoid background writes. We just test the HTTP handlers.
    server.init_db()
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.RequestHandler)
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}"

    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=1)

def test_api_session(api_server):
    resp = requests.get(f"{api_server}/api/session")
    assert resp.status_code == 200
    data = resp.json()
    assert 'current_user' in data
    assert data['current_user'] is None

def test_api_create_community_unauthorized(api_server):
    resp = requests.post(f"{api_server}/api/communities", json={
        "name": "API_Comm",
        "model": "dummy"
    })
    # Should require login
    assert resp.status_code == 401

def test_api_full_flow(api_server):
    # Register a user
    resp = requests.post(f"{api_server}/api/register", json={
        "display_name": "Test User",
        "pin": "1234"
    })
    assert resp.status_code == 200
    cookies = resp.cookies
    data = resp.json()
    assert data['success'] is True

    # Create community
    resp = requests.post(f"{api_server}/api/communities", json={
        "name": "API_Comm",
        "model": "dummy",
        "description": "desc"
    }, cookies=cookies)
    assert resp.status_code == 200

    # Verify community created
    resp = requests.get(f"{api_server}/api/communities")
    comms = resp.json().get('communities', [])
    assert len(comms) > 0
    assert any(c['name'] == 'API_Comm' for c in comms)
