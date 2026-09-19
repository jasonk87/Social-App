import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests

import server
from test_api import api_server


def signed_in(api_server):
    response = requests.post(f'{api_server}/api/register', json={'display_name': 'Audit User', 'pin': '1234'}, timeout=5)
    assert response.status_code == 200
    return response.cookies


def sample_thread():
    community = server.add_community('Audit Room', 'Testing', 'dummy', 60)
    agent = server.add_agent('local_agent', 'dummy', 'A friendly maker')
    post = server.add_post(community, agent, 'First post', 'A post body', 'A picture of a workbench')
    return community, agent, post


def test_community_feed_returns_ama_and_media(api_server):
    community, agent, post = sample_thread()
    with server.get_db_connection() as conn:
        conn.execute('UPDATE communities SET active_ama_agent_id = ? WHERE id = ?', (agent, community))
    response = requests.get(f'{api_server}/api/community/Audit%20Room/feed', timeout=5)
    assert response.status_code == 200
    assert response.json()['community']['active_ama_agent_id'] == agent
    assert response.json()['posts'][0]['media_url'] == 'A picture of a workbench'
    cookies = signed_in(api_server)
    assert requests.get(f'{api_server}/api/feed', cookies=cookies, timeout=5).json()['posts'][0]['media_url']


@pytest.mark.parametrize('params', ['offset=-1', 'limit=0', 'limit=101', 'limit=abc', 'offset=1.5'])
def test_invalid_feed_pagination(api_server, params):
    cookies = signed_in(api_server)
    sample_thread()
    for path in ['feed', 'community/Audit%20Room/feed']:
        response = requests.get(f'{api_server}/api/{path}?{params}', cookies=cookies, timeout=5)
        assert response.status_code == 400
        assert response.json()['code'] == 'validation_error'


@pytest.mark.parametrize('data', [
    {'display_name': [], 'pin': '1234'}, {'display_name': 'Alice', 'pin': 1234},
    {'display_name': 'Alice', 'pin': 'text'}, {'display_name': 'A' * 81, 'pin': '1234'},
    {'display_name': 'Alice', 'pin': '1' * 33},
])
def test_registration_validation(api_server, data):
    response = requests.post(f'{api_server}/api/register', json=data, timeout=5)
    assert response.status_code == 400


@pytest.mark.parametrize('value', [[], {}, 123])
def test_tone_validation(api_server, value):
    cookies = signed_in(api_server)
    response = requests.post(f'{api_server}/api/communities', cookies=cookies, json={'name': 'Invalid Tone', 'model': 'dummy', 'tone': value}, timeout=5)
    assert response.status_code == 400


def test_parent_must_belong_to_the_same_post(api_server):
    cookies = signed_in(api_server)
    community, agent, post = sample_thread()
    other = server.add_post(community, agent, 'Other', 'Other body')
    parent = server.add_comment(other, agent, None, 'Wrong thread')
    response = requests.post(f'{api_server}/api/post/{post}/comment', cookies=cookies, json={'content': 'Invalid reply', 'parent_id': parent}, timeout=5)
    assert response.status_code == 400
    with server.get_db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM comments WHERE post_id = ?', (post,)).fetchone()[0] == 0


@pytest.mark.parametrize('parent', [True, 1.5, 99999])
def test_bad_parent_id_is_rejected(api_server, parent):
    cookies = signed_in(api_server)
    _, _, post = sample_thread()
    response = requests.post(f'{api_server}/api/post/{post}/comment', cookies=cookies, json={'content': 'Invalid reply', 'parent_id': parent}, timeout=5)
    assert response.status_code == 400


def test_missing_post_is_404(api_server):
    response = requests.post(f'{api_server}/api/post/999/comment', cookies=signed_in(api_server), json={'content': 'Missing'}, timeout=5)
    assert response.status_code == 404


def test_failed_transaction_does_not_leak_or_lock_database():
    conn = server.get_db_connection()
    conn.execute("INSERT INTO communities (name) VALUES ('Uncommitted')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO communities (name) VALUES ('Uncommitted')")
    conn.close()
    conn.close()  # A repeated close must not enqueue the same connection twice.
    assert server.get_community_by_name('Uncommitted') is None
    assert server.DB_POOL.pool.qsize() == 1
    assert server.add_community('Committed', '', 'dummy', 60)


def test_context_manager_returns_connection_and_rolls_back():
    with pytest.raises(RuntimeError):
        with server.get_db_connection() as conn:
            conn.execute("INSERT INTO communities (name) VALUES ('Rollback')")
            raise RuntimeError('rollback')
    assert server.get_community_by_name('Rollback') is None


def test_human_comment_never_schedules_human_model(api_server):
    cookies = signed_in(api_server)
    community, _, _ = sample_thread()
    user = server.get_user_by_name('Audit User')
    post = server.add_post(community, user['agent_id'], 'Human author', 'Hello')
    response = requests.post(f'{api_server}/api/post/{post}/comment', cookies=cookies, json={'content': 'Human reply'}, timeout=5)
    assert response.status_code == 200
    with server.get_db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM simulation_events WHERE event_type = 'AGENT_REPLY'").fetchone()[0] == 0


def test_human_identity_survives_community_model_edit():
    community, _, _ = sample_thread()
    user = server.create_household_user('Human User', '1234')
    server.assign_agent_to_community(user['agent_id'], community)
    server.update_community(community, '', 'different-model', 60, 'casual')
    with server.get_db_connection() as conn:
        assert conn.execute('SELECT model FROM agents WHERE id = ?', (user['agent_id'],)).fetchone()[0] == 'human'


def test_queued_reply_ignores_locked_post(monkeypatch):
    community, agent, post = sample_thread()
    server.SIMULATIONS[community] = server.Simulation(community, 'Audit Room', '', 'dummy', 60)
    with server.get_db_connection() as conn:
        conn.execute('UPDATE posts SET locked = 1 WHERE id = ?', (post,))
    generate = Mock(side_effect=AssertionError('Must not call Ollama for a locked thread'))
    monkeypatch.setattr(server, 'generate_comment_reply', generate)
    server.ENGINE._do_agent_reply({'community_id': community, 'agent_id': agent, 'post_id': post, 'parent_comment_text': 'Hi'})
    generate.assert_not_called()


def test_old_comment_deep_link_keeps_pagination(api_server):
    community, agent, post = sample_thread()
    comment = server.add_comment(post, agent, None, 'Old comment')
    for i in range(55):
        server.add_post(community, agent, str(i), 'Body')
    response = requests.get(f'{api_server}/api/community/Audit%20Room/feed?target_comment_id={comment}', timeout=5)
    assert response.status_code == 200
    data = response.json()
    assert data['has_more'] is True
    assert any(p['id'] == post for p in data['posts'])
    assert len(data['posts']) == 51  # Fifty normal results plus the requested older thread.


def test_ollama_output_limit_and_timeouts(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {'response': '  A useful reply  '}
    post = Mock(return_value=response)
    monkeypatch.setattr(server.requests, 'post', post)
    assert server.generate_text('dummy', 'hello', max_tokens=70) == 'A useful reply'
    kwargs = post.call_args.kwargs
    assert kwargs['timeout'] == (3, 180)
    assert kwargs['json']['options']['num_predict'] == 70
    assert 'max_tokens' not in kwargs['json']
    response.json.return_value = {'response': ''}
    with pytest.raises(server.OllamaError):
        server.generate_text('dummy', 'hello')


def test_model_lookup_has_timeout(monkeypatch):
    get = Mock(side_effect=requests.Timeout('Ollama not responding'))
    monkeypatch.setattr(server.requests, 'get', get)
    with pytest.raises(server.OllamaError):
        server.list_models()
    assert get.call_args.kwargs['timeout'] == (3, 10)


def test_deduplicated_event_can_be_scheduled_concurrently():
    def schedule(_):
        server.ENGINE.schedule(0, server.EventPriority.LOW, 'TEST', {}, dedupe_key='concurrent')
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(schedule, range(20)))
    with server.get_db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM simulation_events WHERE dedupe_key = 'concurrent'").fetchone()[0] == 1


def test_failed_heartbeat_does_not_cancel_its_next_run():
    engine = server.ENGINE
    engine.schedule(0, server.EventPriority.LOW, 'COMMUNITY_POST', {}, dedupe_key='heartbeat')
    event = engine._claim_next_due_event()
    engine.schedule(60, server.EventPriority.LOW, 'COMMUNITY_POST', {}, dedupe_key='heartbeat', replace_existing=True)
    engine._mark_event_failed(event.id, 'Temporary Ollama outage')
    with server.get_db_connection() as conn:
        row = conn.execute('SELECT status, attempts FROM simulation_events WHERE id = ?', (event.id,)).fetchone()
        assert row['status'] == 'pending'
        assert row['attempts'] == 0


def test_static_traversal_is_blocked(api_server):
    response = requests.get(f'{api_server}/%2e%2e%5cserver.py', timeout=5)
    assert response.status_code == 403


def test_cross_origin_write_is_blocked(api_server):
    response = requests.post(f'{api_server}/api/register', headers={'Origin': 'https://unrelated.example'}, json={'display_name': 'Cross Site', 'pin': '1234'}, timeout=5)
    assert response.status_code == 403


def test_large_request_rejected(api_server):
    response = requests.post(f'{api_server}/api/register', data='a' * 65537, timeout=5)
    assert response.status_code == 413

def test_burnout_evolution_does_not_deadlock():
    _, agent, _ = sample_thread()
    with server.get_db_connection() as conn:
        conn.execute('UPDATE agents SET burnout = 0.55 WHERE id = ?', (agent,))
    server.check_and_update_agent_burnout(agent, 0.7, '', 'persona', 'dummy')
    with server.get_db_connection() as conn:
        assert conn.execute('SELECT burnout FROM agents WHERE id = ?', (agent,)).fetchone()[0] == pytest.approx(0.7)
        assert conn.execute("SELECT COUNT(*) FROM simulation_events WHERE event_type = 'AGENT_EVOLVE'").fetchone()[0] == 1


def test_expired_session_is_rejected():
    user = server.create_household_user('Expired User', '1234')
    token = server.create_session(user['id'])
    assert server.get_user_by_session(token) is not None
    with server.get_db_connection() as conn:
        conn.execute('UPDATE user_sessions SET created_at = 0 WHERE token = ?', (token,))
    assert server.get_user_by_session(token) is None


def test_startup_restores_state_before_starting_engine(monkeypatch):
    community, _, _ = sample_thread()
    inactive = server.add_community('Merged Room', '', 'dummy', 60)
    with server.get_db_connection() as conn:
        conn.execute('UPDATE communities SET active = 1 WHERE id = ?', (community,))
    started = []
    def start():
        assert community in server.SIMULATIONS
        assert inactive not in server.SIMULATIONS
        started.append(True)
    monkeypatch.setattr(server.ENGINE, 'start', start)
    class FakeHTTP:
        def __init__(self, *args): pass
        def serve_forever(self): pass
        def server_close(self): pass
    monkeypatch.setattr(server, 'SocialHTTPServer', FakeHTTP)
    server.run_server('127.0.0.1', 0)
    assert started == [True]


def test_home_feed_pages_keep_best_sort_and_media():
    community, agent, first = sample_thread()
    newest = server.add_post(community, agent, 'Newest', 'Recent')
    server.add_comment(first, agent, None, 'Popular')
    user = server.create_household_user('Feed User', '1234')
    best, more = server.fetch_home_feed(user['id'], 'best', limit=1)
    assert best[0]['id'] == first and more
    latest, more = server.fetch_home_feed(user['id'], 'latest', limit=1)
    assert latest[0]['id'] == newest and more
    second, more = server.fetch_home_feed(user['id'], 'latest', offset=1, limit=1)
    assert second[0]['id'] == first and not more

def test_embedding_models_are_not_offered_for_simulation(monkeypatch):
    monkeypatch.setattr(server, 'MODEL_CAPABILITIES', {})
    tags = Mock()
    tags.json.return_value = {'models': [{'name': 'chat'}, {'name': 'embed'}]}
    monkeypatch.setattr(server.requests, 'get', Mock(return_value=tags))
    def show(url, *, json, timeout):
        response = Mock()
        response.json.return_value = {'capabilities': ['completion'] if json['model'] == 'chat' else ['embedding']}
        assert timeout == (3, 5)
        return response
    monkeypatch.setattr(server.requests, 'post', Mock(side_effect=show))
    assert server.list_models() == ['chat']
    assert server.list_models() == ['chat']
    assert server.requests.post.call_count == 2  # Model details are cached by digest.

def test_startup_repairs_orphan_account_metadata_only():
    community, agent, post = sample_thread()
    user = server.create_household_user('Retained User', '1234')
    raw = sqlite3.connect(server.DB_PATH)
    raw.execute("INSERT INTO user_sessions VALUES ('orphan-token', 9999, 0)")
    raw.execute('INSERT INTO community_subscriptions VALUES (9999, ?, 0)', (community,))
    raw.commit()
    raw.close()
    server.init_db()
    with server.get_db_connection() as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('SELECT title FROM posts WHERE id = ?', (post,)).fetchone()[0] == 'First post'
        assert conn.execute('SELECT id FROM users WHERE id = ?', (user['id'],)).fetchone()


@pytest.mark.parametrize('payload', [{'title': {}, 'content': 'Hello'}, ['title', 'content'], {'title': 'Hi', 'content': ''}])
def test_invalid_generated_post_never_reaches_sqlite(monkeypatch, payload):
    monkeypatch.setattr(server, 'generate_text', lambda *args, **kwargs: json.dumps(payload))
    with pytest.raises(server.OllamaError):
        server.generate_post('dummy', 'persona', 'Room', 'Description', 'casual')
