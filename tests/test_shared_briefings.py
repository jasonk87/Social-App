import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests

import community_knowledge as knowledge
import conversation_quality as quality
import server
from test_api import api_server


def room(name='Minecraft'):
    cid = server.add_community(name, 'A room for creative conversation', 'dummy', 60)
    with server.get_db_connection() as conn:
        conn.execute('UPDATE communities SET active=1 WHERE id=?', (cid,))
    return cid


def search_response(items=None):
    response = Mock(status_code=200)
    response.json.return_value = {'items': items if items is not None else [
        {'title': 'Update announcement', 'link': 'https://www.minecraft.net/update', 'snippet': 'An official release candidate is available.'}]}
    return response


def service(get=None, budget=24, now=1_000_000):
    return knowledge.CommunityKnowledge(server.get_db_connection,
        {'api_key': 'private-test-key', 'engine_id': 'test-engine', 'daily_limit': budget},
        search_get=get or Mock(return_value=search_response()), now=lambda: now,
        article_reader=lambda url: '')


def test_one_search_shared_across_agents_and_restart():
    cid = room()
    get = Mock(return_value=search_response())
    worker = service(get)
    assert worker.refresh_one()
    for _ in range(100):
        assert worker.snapshot(cid)['sources']
    assert not service(get).refresh_one()
    get.assert_called_once()
    assert get.call_args.kwargs['timeout'] == (3, 15)
    assert get.call_args.kwargs['params']['dateRestrict'] == 'm1'
    assert 'private-test-key' not in json.dumps(worker.snapshot(cid))


def test_atomic_quota_and_community_lease():
    for i in range(6):
        room(f'Room {i}')
    get = Mock(return_value=search_response())
    workers = [service(get, budget=2) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda worker: worker.refresh_one(), workers))
    assert sum(results) == 2
    assert get.call_count == 2
    with server.get_db_connection() as conn:
        assert conn.execute('SELECT COUNT(DISTINCT community_id) FROM search_requests').fetchone()[0] == 2
    assert not service(get, budget=2).refresh_one()
    assert service(get, budget=2, now=1_000_000 + knowledge.DAY + 1).refresh_one()


def test_failed_search_keeps_old_cache_and_counts_budget():
    cid = room()
    worker = service()
    assert worker.refresh_one()
    failed = service(Mock(side_effect=requests.Timeout('private-test-key')), now=1_000_000 + knowledge.DAY + 1)
    assert not failed.refresh_one()
    snapshot = failed.snapshot(cid)
    assert snapshot['sources'] and not snapshot['stale']
    assert snapshot['status'] == 'error'
    assert 'private-test-key' not in json.dumps(snapshot)
    assert not failed.refresh_one()
    stale = service(now=1_000_000 + knowledge.MAX_AGE + 1).snapshot(cid)
    assert stale['stale']
    assert 'Update announcement' not in knowledge.briefing_prompt(stale)


def test_unconfigured_and_inactive_rooms_do_not_search():
    cid = room()
    with server.get_db_connection() as conn:
        conn.execute('UPDATE communities SET active=0 WHERE id=?', (cid,))
    get = Mock(side_effect=AssertionError('Unexpected request'))
    assert not service(get).refresh_one()
    assert not knowledge.CommunityKnowledge(server.get_db_connection, {'api_key': '', 'engine_id': '', 'daily_limit': 24}, search_get=get).refresh_one()


def test_invalid_urls_and_duplicate_sources_are_filtered():
    cid = room()
    items = [
        {'title': '<b>Bad</b>', 'link': 'javascript:alert(1)', 'snippet': 'Bad URL'},
        {'title': 'Local', 'link': 'https://127.0.0.1/private', 'snippet': 'Local URL'},
        {'title': 'Userinfo', 'link': 'https://user:pass@example.com', 'snippet': 'No credentials'},
        {'title': '<b>Good</b>', 'link': 'https://example.com/good', 'snippet': 'A &amp; B'},
        {'title': 'Duplicate', 'link': 'https://example.com/good', 'snippet': 'Duplicate'},
    ]
    worker = service(Mock(return_value=search_response(items)))
    assert worker.refresh_one()
    assert worker.snapshot(cid)['sources'] == [{'title': 'Good', 'url': 'https://example.com/good', 'summary': 'A & B'}]


def test_google_quota_failure_stops_other_communities_from_retrying():
    room('First')
    room('Second')
    get = Mock(return_value=Mock(status_code=429))
    worker = service(get)
    assert not worker.refresh_one()
    assert not service(get).refresh_one()
    get.assert_called_once()


def test_failed_draft_advances_the_conversation_mix(monkeypatch):
    cid = room()
    monkeypatch.setattr(server, 'generate_text', Mock(return_value='{}'))
    with pytest.raises(server.OllamaError):
        server.generate_post('dummy', 'Builder', 'Minecraft', 'Minecraft', 'casual')
    with server.get_db_connection() as conn:
        assert conn.execute('SELECT editorial_turn FROM communities WHERE id=?', (cid,)).fetchone()[0] == 1


def test_factual_reviewer_rejects_unsupported_measurements(monkeypatch):
    monkeypatch.setattr(server, 'generate_text', Mock(return_value=json.dumps({'supported': False, 'reason': 'The numerical test result has no evidence.'})))
    context = {'plan': {'mode': 'question'}, 'briefing': {}}
    result = server.review_factual_claims('dummy', {'title': 'A test', 'content': 'I tested terrain and got 50% longer hyperspace jumps.'}, context, "No Man's Sky")
    assert 'Unsupported factual claims' in result


def test_persona_migration_preserves_accounts_and_runs_only_once():
    cid = room()
    ai = server.add_agent('old_alias', 'dummy', 'Made up expert with false statistics')
    server.assign_agent_to_community(ai, cid)
    user = server.create_household_user('Existing Human', '1234')
    with server.get_db_connection() as conn:
        conn.execute('UPDATE agents SET persona_revision=0,memory=? WHERE id=?', ('false memories', ai))
    server.init_db()
    with server.get_db_connection() as conn:
        row = conn.execute('SELECT * FROM agents WHERE id=?', (ai,)).fetchone()
        assert row['username'] == 'old_alias' and row['memory'] is None
        assert 'Minecraft' in row['persona'] and row['persona_revision'] == 1
        conn.execute('UPDATE agents SET persona=? WHERE id=?', ('New and improved persona', ai))
    server.init_db()
    assert server.get_user_by_name('Existing Human')['id'] == user['id']
    with server.get_db_connection() as conn:
        assert conn.execute('SELECT persona FROM agents WHERE id=?', (ai,)).fetchone()[0] == 'New and improved persona'


def test_article_fetch_does_not_follow_redirects_to_local_or_unknown_hosts():
    response = Mock(status_code=302, headers={'Location': 'http://127.0.0.1/private'})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    get = Mock(return_value=response)
    assert knowledge.official_excerpt('https://www.nomanssky.com/news', get=get) == ''
    get.assert_called_once()
    assert get.call_args.kwargs['allow_redirects'] is False
    assert knowledge.official_excerpt('https://untrusted.example/news', get=get) == ''
    get.assert_called_once()


def test_conversation_mix_and_unavailable_briefing_fallback():
    fresh = {'enabled': True, 'stale': False, 'sources': [{'title': 'A source'}]}
    modes = [quality.choose_plan(i, fresh)['mode'] for i in range(8)]
    assert modes.count('news') == 2
    assert len(set(modes)) == 7
    assert all(quality.choose_plan(i, {})['mode'] != 'news' for i in range(8))
    assert not quality.choose_plan(1, fresh)['sources']


def test_repeated_draft_is_replaced_before_publication(monkeypatch):
    cid = room()
    agent = server.add_agent('writer', 'dummy', 'A curious builder')
    server.add_post(cid, agent, 'A tiny seaside library', 'I built a tiny seaside library with a reading nook.')
    drafts = [json.dumps({'title': 'A tiny seaside library', 'content': 'I built a tiny seaside library with a reading nook.'}),
              json.dumps({'title': 'A town without roads?', 'content': 'What if we linked our houses with garden paths instead? I would love a village that feels like a park.'})]
    generate = Mock(side_effect=drafts)
    monkeypatch.setattr(server, 'generate_text', generate)
    result = server.generate_post('dummy', 'A curious builder', 'Minecraft', 'Minecraft', 'casual')
    assert result['title'] == 'A town without roads?'
    assert result['generation_mode'] == 'question'
    assert generate.call_count == 2
    assert 'Previous draft rejected' in generate.call_args.args[1]


def test_news_has_valid_source_attribution_and_api_exposes_it(monkeypatch, api_server):
    cid = room()
    worker = service()
    assert worker.refresh_one()
    monkeypatch.setattr(server, 'KNOWLEDGE', worker)
    monkeypatch.setattr(server, 'review_factual_claims', lambda *args: '')
    monkeypatch.setattr(server, 'generate_text', Mock(return_value=json.dumps({
        'title': 'Trying the release candidate?', 'content': 'Would you test the release candidate in a separate world first?', 'source_ids': [1, 1, 99]})))
    post = server.generate_post('dummy', 'A curious builder', 'Minecraft', 'Minecraft', 'casual')
    assert post['generation_mode'] == 'news' and len(post['source_urls']) == 1
    agent = server.add_agent('writer', 'dummy', 'Builder')
    server.add_post(cid, agent, post['title'], post['content'], generation_mode=post['generation_mode'], source_urls=post['source_urls'])
    feed = requests.get(api_server + '/api/community/Minecraft/feed', timeout=5).json()
    assert feed['posts'][0]['sources'][0]['url'] == 'https://www.minecraft.net/update'
    state = requests.get(api_server + f'/api/community_state/{cid}', timeout=5).json()
    assert state['briefing']['searches_used'] == 1
    assert 'private-test-key' not in json.dumps(state)


def test_heartbeat_reply_uses_real_parent_and_avoids_self_replies(monkeypatch):
    cid = room()
    bot = server.add_agent('builder', 'dummy', 'Curious Minecraft builder')
    other = server.add_agent('neighbor', 'human', 'Human participant')
    server.assign_agent_to_community(bot, cid)
    for i in range(3):
        post = server.add_post(cid, other, f'Building {i}', 'A Minecraft building conversation')
    parent = server.add_comment(post, other, None, 'Could a roof garden fit here?')
    server.SIMULATIONS[cid] = server.Simulation(cid, 'Minecraft', 'Minecraft builders', 'dummy', 60)
    generate = Mock(return_value='A stepped roof could leave a sunny corner for flowers.')
    monkeypatch.setattr(server, 'generate_comment_reply', generate)
    randoms = iter([.99, .99, .0])
    monkeypatch.setattr(server.random, 'random', lambda: next(randoms))
    server.ENGINE._do_community_post({'community_id': cid})
    assert generate.call_args.args[4] == 'Could a roof garden fit here?'
    with server.get_db_connection() as conn:
        replies = conn.execute('SELECT id FROM comments WHERE parent_id=? AND agent_id=?', (parent, bot)).fetchall()
        assert len(replies) == 1
    generate.reset_mock()
    server.ENGINE._do_agent_reply({'community_id': cid, 'agent_id': bot, 'post_id': post,
                                  'reply_to_comment_id': replies[0]['id'], 'parent_comment_text': 'Fake old payload'})
    generate.assert_not_called()


def test_near_duplicate_rewording_is_rejected():
    old = [{'title': 'Deep Dark Doorway Design Fail: A Lesson in Piston Placement',
            'content': 'The piston doorway keeps sticking and the build needs a wider gap to work.'}]
    assert quality.repetition_reason('Deep Dark Doorway Design Fail: Another Lesson in Piston Placement', 'A totally different body.', old)
    assert not quality.repetition_reason('An underwater reading room', 'Could an underwater library make a fun weekend build?', old)


def test_structured_post_schema_is_sent_to_ollama(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {'response': '{"title":"A title","content":"A complete post."}'}
    post = Mock(return_value=response)
    monkeypatch.setattr(server.requests, 'post', post)
    server.generate_text('dummy', 'Write a post', json_output=quality.POST_SCHEMA)
    assert post.call_args.kwargs['json']['format'] == quality.POST_SCHEMA


def test_configured_reviewer_is_used_without_changing_writer(monkeypatch):
    reviewer = Mock(return_value=json.dumps({'supported': True, 'reason': ''}))
    monkeypatch.setattr(server, 'generate_text', reviewer)
    worker = service()
    worker.settings['review_model'] = 'strong-local-reviewer'
    monkeypatch.setattr(server, 'KNOWLEDGE', worker)
    assert server.review_factual_claims('small-writer', {'title': 'The update', 'content': 'The update is available.'},
        {'plan': {'mode': 'news', 'sources': []}, 'briefing': {}}, 'A room') == ''
    assert reviewer.call_args.args[0] == 'strong-local-reviewer'
    assert reviewer.call_args.kwargs['temperature'] == 0.1


def test_unfinished_story_is_not_published():
    assert quality.post_problem({'title': 'The prank', 'content': 'Then we decided to play a trick...'}, [])
