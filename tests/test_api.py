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
    assert resp.json()["code"] == "auth_required"

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


def test_api_create_community_invalid_posting_rate(api_server):
    register = requests.post(f"{api_server}/api/register", json={
        "display_name": "Rate Tester",
        "pin": "9876",
    })
    assert register.status_code == 200

    resp = requests.post(f"{api_server}/api/communities", json={
        "name": "BadRateComm",
        "model": "dummy",
        "posting_rate": "fast",
    }, cookies=register.cookies)

    assert resp.status_code == 400
    assert resp.json()["error"] == "Posting rate must be a whole number."


def test_api_create_community_too_fast_posting_rate(api_server):
    register = requests.post(f"{api_server}/api/register", json={
        "display_name": "Low Rate Tester",
        "pin": "5555",
    })
    assert register.status_code == 200

    resp = requests.post(f"{api_server}/api/communities", json={
        "name": "TooFastComm",
        "model": "dummy",
        "posting_rate": 10,
    }, cookies=register.cookies)

    assert resp.status_code == 400
    assert resp.json()["error"] == "Posting rate must be at least 30 seconds."


def test_api_rejects_invalid_json_payload(api_server):
    resp = requests.post(
        f"{api_server}/api/login",
        data="{not valid json",
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "Invalid JSON payload."
    assert resp.json()["code"] == "invalid_json"


def test_api_rejects_non_object_json_payload(api_server):
    resp = requests.post(
        f"{api_server}/api/login",
        data='["not", "an", "object"]',
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "JSON body must be an object."
    assert resp.json()["code"] == "invalid_payload"


def test_api_subscribe_requires_boolean(api_server):
    register = requests.post(f"{api_server}/api/register", json={
        "display_name": "Sub Tester",
        "pin": "1212",
    })
    assert register.status_code == 200
    cookies = register.cookies

    create = requests.post(f"{api_server}/api/communities", json={
        "name": "SubBoolComm",
        "model": "dummy",
    }, cookies=cookies)
    assert create.status_code == 200

    resp = requests.post(
        f"{api_server}/api/community/SubBoolComm/subscribe",
        json={"subscribed": "yes"},
        cookies=cookies,
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"
    assert resp.json()["error"] == "subscribed must be true or false."


def test_api_comment_rejects_invalid_parent_id(api_server):
    register = requests.post(f"{api_server}/api/register", json={
        "display_name": "Comment Tester",
        "pin": "3434",
    })
    assert register.status_code == 200
    cookies = register.cookies

    create = requests.post(f"{api_server}/api/communities", json={
        "name": "CommentComm",
        "model": "dummy",
    }, cookies=cookies)
    assert create.status_code == 200

    post = requests.post(
        f"{api_server}/api/community/CommentComm/post",
        json={"title": "Post", "content": "Body"},
        cookies=cookies,
    )
    assert post.status_code == 200
    post_id = post.json()["post_id"]

    resp = requests.post(
        f"{api_server}/api/post/{post_id}/comment",
        json={"content": "reply", "parent_id": "abc"},
        cookies=cookies,
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"
    assert resp.json()["error"] == "parent_id must be an integer or null."


def test_api_smoke_core_user_flow(api_server):
    register = requests.post(f"{api_server}/api/register", json={
        "display_name": "Smoke User",
        "pin": "6677",
    })
    assert register.status_code == 200
    cookies = register.cookies

    create = requests.post(f"{api_server}/api/communities", json={
        "name": "SmokeComm",
        "model": "dummy",
        "description": "smoke-test",
    }, cookies=cookies)
    assert create.status_code == 200

    subscribe = requests.post(
        f"{api_server}/api/community/SmokeComm/subscribe",
        json={"subscribed": True},
        cookies=cookies,
    )
    assert subscribe.status_code == 200
    assert subscribe.json()["subscribed"] is True

    post = requests.post(
        f"{api_server}/api/community/SmokeComm/post",
        json={"title": "Smoke Post", "content": "All systems go"},
        cookies=cookies,
    )
    assert post.status_code == 200
    post_id = post.json()["post_id"]

    comment = requests.post(
        f"{api_server}/api/post/{post_id}/comment",
        json={"content": "Looks good"},
        cookies=cookies,
    )
    assert comment.status_code == 200

    feed = requests.get(f"{api_server}/api/feed", cookies=cookies)
    assert feed.status_code == 200
    posts = feed.json()["posts"]
    assert any(item["id"] == post_id for item in posts)
