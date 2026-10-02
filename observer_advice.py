"""Evidence-linked observer advice over finished results. Pure, offline, no scores.

Preferences change actions, never facts. Unreviewed sequence candidates cannot
trigger a relationship conclusion. Each card keeps contrary evidence visible.
"""
from __future__ import annotations

from relationship_profile import build_profile
from salience import build_salience

ADVICE_VERSION = 'observer-advice-v1'
GOALS = {'comprehensive': '全面看待这段关系', 'romance': '暧昧 / 恋爱',
         'friendship': '朋友相处', 'communication': '沟通与合作'}
FEELINGS = {'unspecified': '暂不提供', 'comfortable': '大多舒服',
            'uncertain': '经常猜测、拿不准', 'drained': '经常疲惫或委屈'}


def _references(events: list[dict]) -> list[dict]:
    refs = {}
    for e in events:
        if e.get('source') == 'interaction_event':
            w = e.get('window') or {}
            start, end = w.get('start_index'), w.get('end_index')
            anchors = w.get('anchor_indices') or [start, end]
        else:
            start = end = e.get('message_index')
            anchors = [start]
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        refs[(start, end, e.get('reason'))] = {
            'start_index': start, 'end_index': end,
            'anchor_indices': list(anchors), 'reason': e.get('reason', ''),
            'review_required': e.get('review_status') == 'review_required',
        }
    return list(refs.values())


def build_observer_advice(results: list[dict], stats: dict,
                          interaction: dict | None = None,
                          goal: str = 'comprehensive',
                          feeling: str = 'unspecified') -> dict:
    if goal not in GOALS or feeling not in FEELINGS:
        raise ValueError('Unknown observer preference')
    interaction = interaction or {}
    profile = build_profile(results, stats=stats,
                            salience=build_salience(results, stats=stats,
                                                    interaction=interaction),
                            interaction=interaction)
    dims = profile['dimensions']
    cards = []

    def add(key, title, dim_keys, observation, blind_spot, action, wording,
            watch_for, stop_condition, extra_support=None, extra_counter=None):
        support, counter = [], []
        caveats = []
        for dim_key in dim_keys:
            dim = dims[dim_key]
            support += dim.get('salient_events') or []
            counter += dim.get('counter_events') or []
            if dim['status'] in ('insufficient', 'unsupported', 'evidence_limited'):
                caveats.append(f"{dim['label']}：{dim['conclusion']}")
            if dim['reliability']['level'] in ('不足', '较低'):
                caveats.append(f"{dim['label']}可靠性有限，请先核对原文与上下文。")
            if any((e.get('evidence_consistency') or {}).get('level') == 'conflicted'
                   for e in support + counter):
                caveats.append('部分指标互相冲突，先核对证据再决定。')
        support += extra_support or []
        counter += extra_counter or []
        cards.append({'id': key, 'title': title, 'observation': observation,
                      'blind_spot': blind_spot, 'action': action,
                      'suggested_words': wording, 'watch_for': watch_for,
                      'stop_condition': stop_condition,
                      'supporting': _references(support),
                      'counter': _references(counter),
                      'caveats': list(dict.fromkeys(caveats))})

    boundary = dims['boundary_pressure']
    withdrawal = dims['withdrawal']
    care = dims['care_responsiveness']
    initiative = dims['initiative_engagement']
    romantic = dims['romantic']
    special = dims['special_attention']
    if boundary.get('salient_events'):
        add('boundary', '先保护自己的边界', ['boundary_pressure'],
            '本样本出现明确拒绝后继续推进同一请求的结构；请逐条核对。',
            '其他时候的友好容易让你忽略这一次拒绝是否被接受；两类证据应分别看。',
            '清楚重申你不接受的事项；不必反复证明拒绝理由。',
            '“这件事我已经决定不做，请不要继续劝我。我们可以聊别的。”',
            '对方是否停止同一请求，后续是否按你表达的边界调整。',
            '明确表达后仍持续施压时，结束该话题并减少接触；无需继续说服。')
    if withdrawal.get('salient_events'):
        add('withdrawal', '按明确的后撤表达调整投入', ['withdrawal', 'care_responsiveness'],
            '已有结果标出了关系层面的后撤信号；它与单纯忙碌、回复慢不同，需要看原文。',
            '一次温暖回应不能自动撤销明确的后撤表达。',
            '询问对方愿意维持怎样的联系；给对方表达和拒绝的空间。',
            '“我想确认一下，你希望我们接下来保持怎样的联系？你可以直接告诉我。”',
            '对方是否给出明确意愿，并在之后的实际互动中一致地执行。',
            '对方明确不愿继续时接受答案；若一直含糊而你已很消耗，降低投入。')
    reciprocal = (interaction.get('observations') or {}).get('reciprocity') or {}
    counts = reciprocal.get('counts') or {}
    one_sided = (reciprocal.get('sufficient') and
                 counts.get('user_reengagements', 0) >= 2 and
                 counts.get('ta_reengagements', 0) == 0)
    if one_sided or initiative.get('counter_events'):
        restarts = [e for e in interaction.get('events', [])
                    if e.get('event_type') == 'conversation_reengagement']
        add('balance', '把注意力放到双方是否共同推进', ['initiative_engagement'],
            (f"可观察的间隔后重启：你 {counts.get('user_reengagements', 0)} 次，"
             f"TA {counts.get('ta_reengagements', 0)} 次；只描述已导入片段。"
             if one_sided else '本样本有低投入 / 拒绝继续的证据，也应检查是否存在主动推进的相反线索。'),
            '你的长消息、连发和解释可以增加聊天量，不能替代对方主动投入。',
            '保持自然联系，少承担每一次开启话题和安排的工作；提出一次清楚、可拒绝的请求。',
            '“我也希望有时由你来提话题或安排。你愿意下次选个合适的时间吗？”',
            '对方是否主动补充内容、提问、提出安排或执行承诺；不要只数回复快慢。',
            '清楚表达需要后仍长期单方面推进且让你疲惫，可以降低投入；不以冷处理或故意失联测试对方。',
            extra_counter=restarts if one_sided else None)
    if care.get('salient_events') or care.get('counter_events'):
        add('care', '看具体回应，也看让你不舒服的部分', ['care_responsiveness'],
            '关心与冷淡证据分别保留；一段对话里的多条消息可能属于同一次互动。',
            '普通关心、熟悉的玩笑或单条冷回复，都不能独自决定整段关系性质。',
            '表达一次具体的支持需求，并说明什么回应会对你有帮助。',
            '“我现在更需要你听我说一会儿，不急着给建议。你这会儿方便吗？”',
            '回应是否针对你的实际需求，后续行动是否匹配；也考虑事务语境和现实限制。',
            '反复沟通后你的需求仍得不到回应，可以调整期待，并向其他可信的人寻求支持。')
    if goal in ('comprehensive', 'romance'):
        add('nature', '把关心、特殊关注和恋爱意愿分开看',
            ['special_attention', 'romantic'],
            ('本样本有明确档浪漫线索，但仍需结合语境确认关系意愿。'
             if romantic.get('salient_events') else
             '本样本尚无明确档浪漫证据；这不证明对方没有感情。'),
            '不要把关心、记住细节、夜聊或群体邀约自动解释为恋爱意愿。',
            '如果你想发展关系，选择合适时机直接、低压力地询问；如果只是朋友，按朋友的互相支持判断。',
            '“我想了解你是把我们当朋友，还是愿意尝试进一步发展？不想也没关系。”',
            '对方的明确回答、双人安排是否落实，以及你们想要的关系是否一致。',
            '对方明确只想做朋友时接受界限；不要靠不断增加付出去换一个不同答案。')
    elif goal == 'friendship':
        add('friendship', '按你希望的朋友相处方式沟通', ['care_responsiveness', 'initiative_engagement'],
            '朋友相处可以分别看具体关心、共同推进和边界回应。',
            '聊天频率不同不等于友情价值不同，先确认双方的相处期待。',
            '说清你希望怎样互相联系、提供什么支持，也询问对方的习惯。',
            '“我希望我们有事可以互相找，也偶尔一起安排活动。你觉得怎样联系最舒服？”',
            '需求是否能商量，联系与支持是否长期由双方承担。',
            '期待长期不匹配时调整联系频率，保留对双方舒服的距离。')
    else:
        add('communication', '把任务与关系信号分开', ['initiative_engagement'],
            '事务性的简短回复需要结合任务内容看；不能直接解释为关系疏远。',
            '对任务的拒绝或意见不同可能是正常协作。',
            '把请求、责任和可接受的完成时间说清楚，并确认对方是否愿意承担。',
            '“这件事你方便负责吗？如果不方便，我们再调整分工和时间。”',
            '是否明确回应请求、协调分工并兑现约定。',
            '多次明确协商仍无法推进时调整分工或联系渠道。')
    invitations = [e for e in interaction.get('events', [])
                   if e.get('event_type') == 'invitation_progression'
                   and e.get('review_status') == 'auto_supported']
    if invitations:
        add('plans', '用安排落实情况验证互动', ['initiative_engagement'],
            '本样本中有邀约从模糊走向具体时间 / 地点的结构；是否实际赴约仍未知。',
            '具体邀约与兑现承诺是不同证据；群体活动也不直接说明浪漫意愿。',
            '确认一个双方都方便的安排，留意后续是否落实，改期时是否主动提供替代方案。',
            '“我们确认一下时间和地点？如果临时不方便，直接告诉我就好。”',
            '实际参与、及时协调和后续补约，而不只看邀约措辞。',
            '长期只有口头安排而没有落实时，减少预留时间和额外付出。',
            extra_support=invitations)
    candidates = [e for e in interaction.get('events', [])
                  if e.get('review_status') == 'review_required']
    if candidates:
        add('review', '先核对待确认线索', [],
            f'有 {len(candidates)} 条互动线索需要人工核对，尚未作为已确认事实。',
            '共同词语不等于记住具体事项，含糊回应也不等于施压或接受边界。',
            '打开对应原文与上下文，核对是否谈同一件事；核对前不要据此增加投入或给对方定性。',
            '“你是指我上次提到的那件事吗？”',
            '是否能明确对应具体事项、请求或边界。',
            '无法对应时保留不确定，不把候选升级为结论。', extra_support=candidates)
    if feeling in ('drained', 'uncertain'):
        add('self', '把你的感受纳入决定', [],
            '你选择了“' + FEELINGS[feeling] + '”；这是你的主观感受，独立于对方意图证据。',
            '即使有友好信号，你也不需要因此忽略自己的消耗。',
            '写下你想要的相处方式和不能接受的事项；先降低超出精力的付出，再明确表达一个具体需求。',
            '“这样的相处让我有些累，我想调整一下联系和安排方式。”',
            '沟通之后你是否更轻松，需求和边界是否能实际商量。',
            '若持续疲惫且无法协调，可以拉开距离；无需先证明对方有恶意。')
    if not cards or stats.get('effective_messages', 0) < 2:
        add('insufficient', '资料有限，先做小而清楚的沟通', [],
            '当前可用的关系证据有限，缺少完整聊天或现实互动背景。',
            '没有找到证据与证明不存在是两回事。',
            '补充双方完整的相关片段，或先提出一个清楚、可拒绝的请求；不要反复刷分寻找确定感。',
            '“我想了解你希望怎样相处，我们找个方便的时间聊聊好吗？”',
            '明确回答与后续行动是否一致。',
            '资料仍不足时保留判断，以自己的需求与界限决定投入。')
    return {'version': ADVICE_VERSION, 'goal': goal, 'feeling': feeling,
            'cards': cards,
            'limitations': ['仅依据当前导入样本和已有分析，缺少现实互动与双方关系背景。',
                            '建议是可选择的沟通行动，不预测感情、人格或关系结果。',
                            '多条消息不代表多个独立事件；待核对线索不会升级为确定结论。']}


def advice_markdown(advice: dict) -> str:
    lines = ['## 旁观者建议', '', f"关注目标：{GOALS[advice['goal']]}", '']
    for c in advice['cards']:
        lines += [f"### {c['title']}", '', f"- 观察：{c['observation']}",
                  f"- 容易忽略：{c['blind_spot']}", f"- 下一步：{c['action']}",
                  f"- 可以这样说：{c['suggested_words']}",
                  f"- 观察什么：{c['watch_for']}", f"- 调整投入的条件：{c['stop_condition']}"]
        for field, label in (('supporting', '支持 / 待核对依据'), ('counter', '相反 / 需留意依据')):
            refs = c[field]
            lines.append(f"- {label}：" + ('；'.join(
                f"消息 #{r['start_index'] + 1} ~ #{r['end_index'] + 1}"
                + ('（待核对）' if r['review_required'] else '') for r in refs)
                if refs else '无可追溯消息；属于一般行动建议或缺证据提醒'))
        lines += [f'- 限制：{note}' for note in c['caveats']]
        lines.append('')
    lines += [f'> {note}' for note in advice['limitations']]
    return '\n'.join(lines)
