"""Global AI configuration must cover every path into Ollama, including old jobs."""
import json
from unittest.mock import Mock

import pytest
import requests

import server
from test_api import api_server
from test_audit_regressions import signed_in


@pytest.fixture
def catalog(monkeypatch):
    profiles = [
        {'name': 'plain', 'completion': True, 'thinking_mode': 'none'},
        {'name': 'reasoner', 'completion': True, 'thinking_mode': 'toggle'},
        {'name': 'required-thinking', 'completion': True, 'thinking_mode': 'levels'},
        {'name': 'legacy', 'completion': True, 'thinking_mode': 'unknown'},
    ]
    monkeypatch.setattr(server, 'list_model_profiles', lambda: profiles)
    return profiles


def capture_generation(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {'response': 'A complete answer.', 'thinking': 'Do not publish this.'}
    post = Mock(return_value=response)
    monkeypatch.setattr(server.requests, 'post', post)
    return post


def test_change_covers_existing_new_and_inactive_rooms_and_agents(catalog):
    first = server.add_community('First', '', 'old-model', 60)
    second = server.add_community('Inactive', '', 'different-model', 90)
    bot = server.add_agent('bot', 'old-model', 'A participant')
    human = server.add_agent('person', 'human', 'A person')
    deleted = server.add_agent('deleted', 'none', '')
    server.SIMULATIONS[first] = server.Simulation(first, 'First', '', 'old-model', 60)
    server.save_ai_settings('reasoner', False)
    third = server.add_community('New', '', 'stale-model', 60)
    new_bot = server.add_agent('new_bot', 'stale-model', 'Another participant')
    server.update_community(second, 'Changed description', 'ignored-override', 120, 'casual')
    with server.get_db_connection() as conn:
        assert [r[0] for r in conn.execute('SELECT DISTINCT model FROM communities')] == ['reasoner']
        for aid in (bot, new_bot):
            assert conn.execute('SELECT model FROM agents WHERE id=?', (aid,)).fetchone()[0] == 'reasoner'
        assert conn.execute('SELECT model FROM agents WHERE id=?', (human,)).fetchone()[0] == 'human'
        assert conn.execute('SELECT model FROM agents WHERE id=?', (deleted,)).fetchone()[0] == 'none'
    assert server.SIMULATIONS[first].model == 'reasoner'
    server.init_db()
    assert server.get_ai_settings() == {'model': 'reasoner', 'thinking_enabled': False, 'thinking_mode': 'toggle', 'activity_interval_seconds': 60}


def test_migration_preserves_current_model_and_social_content():
    cid = server.add_community('Existing', '', 'old', 60)
    bot = server.add_agent('existing_bot', 'old', 'A participant')
    post_id = server.add_post(cid, bot, 'Keep this', 'Saved content')
    with server.get_db_connection() as conn:
        conn.execute('DROP TABLE ai_settings')
        conn.execute("UPDATE communities SET model='installed-model'")
        conn.execute("UPDATE agents SET model='other-model' WHERE id=?", (bot,))
    server.init_db()
    assert server.get_ai_settings()['model'] == 'installed-model'
    with server.get_db_connection() as conn:
        assert conn.execute('SELECT content FROM posts WHERE id=?', (post_id,)).fetchone()[0] == 'Saved content'
        assert conn.execute('SELECT model FROM agents WHERE id=?', (bot,)).fetchone()[0] == 'installed-model'


def test_next_request_ignores_stale_job_model_and_toggles_thinking(catalog, monkeypatch):
    server.save_ai_settings('reasoner', False)
    post = capture_generation(monkeypatch)
    assert server.generate_text('stale-queued-model', 'Reply', max_tokens=80) == 'A complete answer.'
    payload = post.call_args.kwargs['json']
    assert payload['model'] == 'reasoner' and payload['think'] is False
    assert payload['options']['num_predict'] == 80
    server.save_ai_settings('reasoner', True)
    server.generate_text('another-stale-model', 'Reply', max_tokens=80)
    payload = post.call_args.kwargs['json']
    assert payload['think'] is True and payload['options']['num_predict'] > 80
    server.save_ai_settings('plain', False)
    server.generate_text('reasoner', 'Reply')
    assert post.call_args.kwargs['json']['model'] == 'plain'
    assert 'think' not in post.call_args.kwargs['json']


def test_queued_evolution_uses_current_settings(catalog, monkeypatch):
    bot = server.add_agent('queued_bot', 'old', 'A participant')
    server.ENGINE.schedule(0, server.EventPriority.HIGH, 'AGENT_EVOLVE',
        {'agent_id': bot, 'model': 'old', 'memory': '', 'persona': 'A participant'})
    server.save_ai_settings('reasoner', False)
    post = capture_generation(monkeypatch)
    with server.get_db_connection() as conn:
        event = json.loads(conn.execute("SELECT event_data FROM simulation_events WHERE event_type='AGENT_EVOLVE'").fetchone()[0])
    server.ENGINE._do_agent_evolve(event)
    assert post.call_args.kwargs['json']['model'] == 'reasoner'
    assert post.call_args.kwargs['json']['think'] is False


def test_invalid_changes_are_atomic(catalog):
    previous = server.get_ai_settings()
    for name, thinking in [('missing-model', True), ('reasoner', 'false'), ('required-thinking', False), ('legacy', False)]:
        with pytest.raises(ValueError):
            server.save_ai_settings(name, thinking)
        assert server.get_ai_settings() == previous


def test_required_thinking_model_uses_supported_level(catalog, monkeypatch):
    server.save_ai_settings('required-thinking', True)
    post = capture_generation(monkeypatch)
    server.generate_text('old', 'Reply')
    assert post.call_args.kwargs['json']['think'] == 'medium'


@pytest.mark.parametrize('metadata,mode', [
    ({'capabilities': ['completion']}, 'none'),
    ({'capabilities': ['completion', 'thinking']}, 'toggle'),
    ({'capabilities': ['completion', 'thinking'], 'model_info': {'general.architecture': 'gptoss'}}, 'levels'),
    ({}, 'unknown'),
])
def test_thinking_capabilities_come_from_ollama_not_model_size(monkeypatch, metadata, mode):
    monkeypatch.setattr(server, 'MODEL_CAPABILITIES', {})
    response = Mock()
    response.json.return_value = metadata
    monkeypatch.setattr(server.requests, 'post', Mock(return_value=response))
    assert server.model_profile({'name': 'custom-alias', 'digest': 'abc'})['thinking_mode'] == mode


def test_settings_api_requires_sign_in_and_room_forms_need_no_model(api_server, catalog):
    endpoint = api_server + '/api/ai-settings'
    assert requests.post(endpoint, json={'model': 'reasoner', 'thinking_enabled': False}, timeout=5).status_code == 401
    cookies = signed_in(api_server)
    response = requests.post(endpoint, json={'model': 'reasoner', 'thinking_enabled': False}, cookies=cookies, timeout=5)
    assert response.status_code == 200 and response.json()['settings']['thinking_enabled'] is False
    assert requests.get(endpoint, timeout=5).json()['settings']['model'] == 'reasoner'
    response = requests.post(api_server + '/api/communities', cookies=cookies, json={'name': 'Global room'}, timeout=5)
    assert response.status_code == 200
    response = requests.post(api_server + '/api/community/Global%20room/update', cookies=cookies,
        json={'description': 'Updated', 'model': 'stale-room-model'}, timeout=5)
    assert response.status_code == 200
    assert server.get_community_by_name('Global room')['model'] == 'reasoner'
    response = requests.post(endpoint, cookies=cookies, json={'model': 'missing', 'thinking_enabled': True}, timeout=5)
    assert response.status_code == 400
    assert requests.get(endpoint, timeout=5).json()['settings']['model'] == 'reasoner'


def test_models_endpoint_refreshes_installed_catalog(api_server, catalog):
    response = requests.get(api_server + '/api/models', timeout=5).json()
    assert response['model_details'][1]['thinking_mode'] == 'toggle'
    catalog.append({'name': 'newly-pulled', 'completion': True, 'thinking_mode': 'none'})
    assert 'newly-pulled' in requests.get(api_server + '/api/models', timeout=5).json()['models']


def test_offline_ollama_cannot_overwrite_saved_settings(api_server, monkeypatch):
    cookies = signed_in(api_server)
    previous = server.get_ai_settings()
    monkeypatch.setattr(server, 'list_model_profiles', Mock(side_effect=server.OllamaError('Ollama unavailable')))
    response = requests.post(api_server + '/api/ai-settings', cookies=cookies,
        json={'model': 'unverified', 'thinking_enabled': True}, timeout=5)
    assert response.status_code == 503
    assert requests.get(api_server + '/api/ai-settings', timeout=5).json()['settings'] == previous
