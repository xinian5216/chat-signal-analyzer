"""Offline regressions for traceable evidence and observer advice. Fictional only."""
import copy
import json
import socket

import pytest
from streamlit.testing.v1 import AppTest

from interaction_dynamics import build_interaction_events, _distinctive_anchors, report_view
from observer_advice import build_observer_advice, advice_markdown
from report import build_json_report, build_markdown_report
from scoring import compute_conversation_stats
from ui_helpers import evidence_message_rows, literal_markdown, key_evidence_items
from test_interaction_dynamics import make_result, msg, ASK
from test_profile_ui_report import (APP_PATH, CHAT_FOLLOWUP, counting_client,
                                   ask_client, _run_to_results, _texts)


def analyze(messages, **metrics):
    results = [{'index': i, 'speaker': 'them', 'text': m['text'],
                'time': m.get('time'), 'result': make_result(**metrics)}
               for i, m in enumerate(messages) if m['speaker'] == 'them'
               and m.get('content_type') != 'media']
    stats = compute_conversation_stats(results)
    interaction = build_interaction_events(messages, results)
    return results, stats, interaction


def cards(advice):
    return {c['id']: c for c in advice['cards']}


@pytest.mark.parametrize('word', ['最近', '现在', '感觉', '事情', '这个', '可能'])
def test_generic_word_is_not_personal_recall(word):
    assert not _distinctive_anchors(word + '顺利吗', '你' + word + '怎么样？')


def test_recall_has_only_causal_anchor_pair_and_does_not_leak_export_text():
    messages = [msg('me', '我的蓝莓盆栽长得不错', '2026-03-02 10:00'),
                msg('them', '收到', '2026-03-02 10:01')]
    messages += [msg('them', '安排已确认', '2026-03-02 10:02') for _ in range(50)]
    messages += [msg('them', '你的蓝莓盆栽怎么样了？', '2026-04-04 10:00')]
    result, stats, interaction = analyze(messages, **ASK)
    e = next(e for e in interaction['events'] if e['event_type'] == 'personal_recall_candidate')
    assert e['window']['anchor_indices'] == [0, len(messages) - 1]
    rows = evidence_message_rows(messages, result, e['window']['anchor_indices'])
    assert len(rows) <= 6
    assert messages[0]['text'] in [r['text'] for r in rows]
    assert messages[-1]['text'] in [r['text'] for r in rows]
    exported = json.dumps(report_view(interaction), ensure_ascii=False)
    assert '蓝莓盆栽' not in exported
    advice = cards(build_observer_advice(result, stats, interaction))
    assert advice['review']['supporting'][0]['review_required']
    assert 'care' not in advice  # candidate did not promote to actual care


def test_followup_and_boundary_do_not_count_as_reengagement():
    messages = [msg('me', '我今天不想去看电影', '2026-03-02 10:00'),
                msg('them', '那好，不勉强你', '2026-03-02 10:01'),
                msg('me', '我的蓝莓盆栽长新叶了', '2026-03-02 10:02'),
                msg('them', '长了几片？', '2026-03-02 10:03'),
                msg('me', '两片', '2026-03-02 10:04'),
                msg('them', '放在哪里？', '2026-03-02 10:05')]
    result, stats, interaction = analyze(messages, **ASK)
    counts = interaction['observations']['reciprocity']['counts']
    assert counts['ta_reengagements'] == counts['user_reengagements'] == 0
    assert interaction['events']  # other events really were generated


def test_evidence_is_exact_and_fallback_cannot_show_unrelated_chat():
    results = [{'index': 7, 'speaker': 'them', 'text': '真实对应片段', 'time': None}]
    rows = evidence_message_rows([], results, [2, 7])
    assert rows[0]['index'] == 2 and '不可用' in rows[0]['text']
    assert rows[1]['index'] == 7 and rows[1]['text'] == '真实对应片段'
    assert evidence_message_rows([], results, [-1]) == []


def test_evidence_preserves_newlines_and_neutral_media_and_escapes_markup():
    messages = [msg('me', '第一行\n第二行'), msg('them', '[图片]', content_type='media', media_kinds=['image'])]
    rows = evidence_message_rows(messages, [], [0, 1])
    assert rows[0]['text'] == '第一行\n第二行'
    assert '第一行' not in rows[1]['text']
    assert literal_markdown('[点此](https://example.com) <img src=x> **标题**').startswith(r'\[点此\]\(')


def test_key_evidence_merges_one_sequence_across_display_channels():
    from relationship_profile import build_profile
    from salience import build_salience
    messages = [msg('me', '我的蓝莓盆栽长新叶了'), msg('them', '长了几片？'),
                msg('me', '两片'), msg('them', '放在哪里？')]
    result, stats, interaction = analyze(messages, **ASK)
    profile = build_profile(result, interaction=interaction,
                            salience=build_salience(result, interaction=interaction))
    items = key_evidence_items(profile)
    followups = [i for i in items if i['event'].get('event_type') == 'followup_sequence'
                 or i['event'].get('event_class') == 'followup_sequence']
    assert len(followups) == 1
    assert len(followups[0]['labels']) >= 2
    assert followups[0]['event']['trigger']


def test_support_and_counter_are_kept_even_when_average_is_moderate():
    messages = [msg('me', '我今天有点累'), msg('them', '可以听你说'),
                msg('me', '我想聊聊'), msg('them', '别来烦我')]
    result, stats, interaction = analyze(messages)
    result[0]['result'] = make_result(warmth=3.6)
    result[1]['result'] = make_result(warmth=0.3)
    stats = compute_conversation_stats(result)
    advice = cards(build_observer_advice(result, stats, interaction))
    assert [r['start_index'] for r in advice['care']['supporting']] == [1]
    assert [r['start_index'] for r in advice['care']['counter']] == [3]


def test_boundary_has_priority_and_acceptance_does_not_cancel_pressure():
    messages = [msg('me', '我今天不想去看电影'), msg('them', '就来嘛，再想想'),
                msg('me', '不要再劝了'), msg('them', '好的，不勉强你')]
    result, stats, interaction = analyze(messages)
    advice = build_observer_advice(result, stats, interaction)
    assert advice['cards'][0]['id'] == 'boundary'
    card = advice['cards'][0]
    assert card['supporting'] and card['counter']
    assert '不必反复证明' in card['action']


def test_busy_neutral_and_high_information_alone_do_not_trigger_rejection():
    messages = [msg('me', '今晚方便吗'), msg('them', '今晚忙，明天可以'),
                msg('me', '好'), msg('them', '收到')]
    result, stats, interaction = analyze(messages, evidence=4, warmth=1, engagement=2)
    advice = cards(build_observer_advice(result, stats, interaction))
    assert not {'withdrawal', 'boundary', 'care', 'balance'} & advice.keys()
    assert 'nature' in advice


def test_preferences_change_advice_not_facts_and_feelings_are_independent():
    messages = [msg('me', '晚上聊聊吗'), msg('them', '好的')]
    result, stats, interaction = analyze(messages)
    before = copy.deepcopy((result, stats, interaction))
    romantic = cards(build_observer_advice(result, stats, interaction, goal='romance'))
    friendship = cards(build_observer_advice(result, stats, interaction, goal='friendship', feeling='drained'))
    assert 'nature' in romantic and 'friendship' in friendship
    assert 'self' in friendship and not friendship['self']['supporting']
    assert '主观感受' in friendship['self']['observation']
    assert (result, stats, interaction) == before
    with pytest.raises(ValueError):
        build_observer_advice(result, stats, goal='invented')


def test_advice_and_reports_are_offline_deterministic_and_default_text_free(monkeypatch):
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network forbidden'))
    messages = [msg('me', '私密虚构原文甲'), msg('them', '私密虚构原文乙')]
    result, stats, interaction = analyze(messages)
    first = build_observer_advice(result, stats, interaction)
    assert first == build_observer_advice(result, stats, interaction)
    exported = build_json_report(result, stats, interaction=interaction)
    md = build_markdown_report(result, stats, interaction=interaction)
    assert 'observer_advice' in exported and '## 旁观者建议' in md
    for m in messages:
        assert m['text'] not in json.dumps(exported, ensure_ascii=False)
        assert m['text'] not in md and m['text'] not in advice_markdown(first)


def test_empty_and_failed_only_do_not_invent_evidence():
    results = [{'index': 0, 'text': '无效消息', 'error': 'test'}]
    advice = build_observer_advice(results, compute_conversation_stats(results))
    assert 'insufficient' in cards(advice)
    assert all(not c['supporting'] and not c['counter'] for c in advice['cards'])


def test_ui_interaction_and_key_messages_show_both_sides_without_api(ask_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60).run()
    _run_to_results(at, CHAT_FOLLOWUP)
    body = _texts(at)
    assert '最近胃不舒服' in body and '现在还疼吗' in body
    at.segmented_control[0].set_value('关键消息').run()
    assert not at.exception
    body = _texts(at)
    assert '关键证据与对话原文' in body
    assert '最近胃不舒服' in body and '现在还疼吗' in body


def test_ui_advice_preferences_and_navigation_are_zero_api(counting_client):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60).run()
    _run_to_results(at, '我: 晚上聊聊吗\nTA: 可以，怎么了\n我: 有些事情想说\nTA: 你说吧')
    api_calls = len(counting_client)
    at.segmented_control[0].set_value('旁观建议').run()
    assert not at.exception and '调整投入的条件' in _texts(at)
    at.radio(key='observer_goal').set_value('friendship').run()
    at.radio(key='observer_feeling').set_value('drained').run()
    assert not at.exception and '把你的感受纳入决定' in _texts(at)
    assert '朋友相处方式' in _texts(at)
    assert len(counting_client) == api_calls
