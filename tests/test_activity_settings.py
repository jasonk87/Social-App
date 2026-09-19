from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest
import requests

import server
from test_api import api_server
from test_audit_regressions import signed_in


@pytest.fixture
def clock(monkeypatch):
    now = [10000.0]
    monkeypatch.setattr(server, 'time', SimpleNamespace(time=lambda: now[0], sleep=time.sleep))
    return now


@pytest.mark.parametrize('interval', server.ACTIVITY_INTERVALS)
def test_activity_presets_persist_after_restart(interval):
    old_model = server.get_ai_settings()['model']
    server.save_activity_settings(interval)
    server.init_db()
    assert server.get_ai_settings()['activity_interval_seconds'] == interval
    assert server.get_ai_settings()['model'] == old_model


@pytest.mark.parametrize('value', [-30, 1, 45, 3600, True, '300', 300.0, None])
def test_invalid_activity_does_not_modify_settings(value):
    original = server.get_ai_settings()
    with pytest.raises(ValueError):
        server.save_activity_settings(value)
    assert server.get_ai_settings() == original


def test_global_pace_includes_replies_and_waits_after_slow_generation(clock):
    engine = server.SimulationEngine()
    server.save_activity_settings(300)
    engine.schedule(0, server.EventPriority.LOW, 'COMMUNITY_POST', {'community_id': 1})
    engine.schedule(0, server.EventPriority.LOW, 'AGENT_REPLY', {'community_id': 2})
    assert engine._claim_next_due_event() is None
    clock[0] += 300
    post = engine._claim_next_due_event()
    assert post.event_type == 'COMMUNITY_POST'
    assert engine._claim_next_due_event() is None
    clock[0] += 400  # A slow turn must not cause a burst afterward.
    engine._mark_event_completed(post.id)
    engine._finish_activity_turn()
    assert engine._claim_next_due_event() is None
    clock[0] += 299
    assert engine._claim_next_due_event() is None
    clock[0] += 1
    assert engine._claim_next_due_event().event_type == 'AGENT_REPLY'


def test_restart_keeps_shared_deadline(clock):
    server.save_activity_settings(1800)
    engine = server.SimulationEngine()
    for i in range(10):
        engine.schedule(-10, server.EventPriority.LOW, 'COMMUNITY_POST', {'community_id': i})
    clock[0] += 1800
    assert engine._claim_next_due_event() is not None
    server.init_db()
    assert server.SimulationEngine()._claim_next_due_event() is None


def test_pause_defers_all_bot_actions_but_allows_maintenance_and_humans(clock):
    engine = server.SimulationEngine()
    server.save_activity_settings(0)
    for event_type in server.AI_ACTIVITY_EVENTS:
        engine.schedule(-10, server.EventPriority.HIGH, event_type, {})
    engine.schedule(0, server.EventPriority.LOW, 'COMMUNITY_DRIFT', {})
    assert engine._claim_next_due_event().event_type == 'COMMUNITY_DRIFT'
    assert engine._claim_next_due_event() is None
    cid = server.add_community('Paused room', '', 'ignored', 30)
    person = server.add_agent('human_poster', 'human', 'A human')
    assert server.add_post(cid, person, 'Still here', 'Manual posts remain available.')
    server.save_activity_settings(30)
    assert engine._claim_next_due_event() is None
    clock[0] += 30
    assert engine._claim_next_due_event().event_type in server.AI_ACTIVITY_EVENTS


def test_faster_pace_updates_pending_work_without_waiting_old_interval(clock):
    engine = server.SimulationEngine()
    server.save_activity_settings(1800)
    engine.schedule(1800, server.EventPriority.LOW, 'COMMUNITY_POST', {})
    server.save_activity_settings(30)
    clock[0] += 30
    assert engine._claim_next_due_event() is not None


def test_community_rate_tone_and_energy_cannot_override_global_pace():
    server.save_activity_settings(1800)
    cid = server.add_community('Fast room', '', 'ignored', 30, 'funny')
    sim = server.Simulation(cid, 'Fast room', '', 'ignored', 30, 'funny', energy=5)
    assert sim.effective_posting_rate() == 1800
    server.update_community(cid, 'Updated', 'ignored', 30, 'funny')
    assert server.get_community_by_name('Fast room')['posting_rate'] == 1800


def test_concurrent_claims_reserve_one_global_turn(clock):
    engine = server.SimulationEngine()
    for i in range(4):
        engine.schedule(0, server.EventPriority.LOW, 'COMMUNITY_POST', {'community_id': i})
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _: engine._claim_next_due_event(), range(4)))
    assert sum(event is not None for event in claimed) == 1


def test_activity_api_works_when_ollama_is_offline(api_server, monkeypatch):
    endpoint = api_server + '/api/activity-settings'
    assert requests.post(endpoint, json={'activity_interval_seconds': 0}, timeout=5).status_code == 401
    cookies = signed_in(api_server)
    model = server.get_ai_settings()['model']
    monkeypatch.setattr(server, 'list_model_profiles', Mock(side_effect=server.OllamaError('Offline')))
    response = requests.post(endpoint, cookies=cookies, json={'activity_interval_seconds': 0}, timeout=5)
    assert response.status_code == 200
    assert response.json()['settings']['activity_interval_seconds'] == 0
    assert response.json()['settings']['model'] == model
    assert requests.post(endpoint, cookies=cookies, json={'activity_interval_seconds': '300'}, timeout=5).status_code == 400
    assert server.get_ai_settings()['activity_interval_seconds'] == 0


def test_upgrade_from_model_only_settings_keeps_user_choice():
    with server.get_db_connection() as conn:
        conn.execute('DROP TABLE ai_settings')
        conn.execute('CREATE TABLE ai_settings (id INTEGER PRIMARY KEY,model TEXT,thinking_enabled INTEGER,thinking_mode TEXT)')
        conn.execute("INSERT INTO ai_settings VALUES (1,'gemma4:e2b',0,'toggle')")
    server.init_db()
    assert server.get_ai_settings() == {'model': 'gemma4:e2b', 'thinking_enabled': False,
                                       'thinking_mode': 'toggle', 'activity_interval_seconds': 60}


@pytest.mark.parametrize('same_digest', [True, False])
def test_gemma_labels_preserve_exact_tags_and_only_mark_verified_aliases(monkeypatch, same_digest):
    monkeypatch.setattr(server, 'MODEL_CAPABILITIES', {})
    response = Mock()
    response.json.return_value = {'models': [
        {'name': 'gemma4:e2b', 'digest': 'small'},
        {'name': 'gemma4:e4b', 'digest': 'large'},
        {'name': 'gemma4:4b', 'digest': 'large' if same_digest else 'different'},
    ]}
    monkeypatch.setattr(server.requests, 'get', Mock(return_value=response))
    details = Mock()
    details.json.return_value = {'capabilities': ['completion', 'thinking']}
    monkeypatch.setattr(server.requests, 'post', Mock(return_value=details))
    profiles = {p['name']: p for p in server.list_model_profiles()}
    assert profiles['gemma4:e2b']['display_name'] == 'Gemma 4 E2B'
    assert profiles['gemma4:e4b']['display_name'] == 'Gemma 4 E4B'
    assert ('E4B' in profiles['gemma4:4b']['display_name']) is same_digest
