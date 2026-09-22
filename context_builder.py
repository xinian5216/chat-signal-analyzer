"""Context Builder v2：turn-aware + 有界预算的对话上下文选择（纯逻辑）。

为什么不再是 ``previous 5 messages``：机械取最近 5 条会切断真实对话回合——
TA 连发数条、或“我”连发数条后 TA 统一回复时，最近 5 条可能只剩半句话，
Jev 看不到 TA 正在回应什么。这里改为**按 dialogue turn 回溯**：

- turn = 连续同一 speaker 的消息（微信里一次“连发”就是一个 turn）；
- 从 target 向前逐 turn 回溯，优先完整保留最近的 turn（尤其最近的 me turn，
  即 TA 最可能正在回应的内容）；
- turn 数 / 消息数 / 字符数三种预算同时生效，尽量不把 turn 从中间拦腰截断；
- 输出恢复原始时间顺序（chronological），且只含 target **之前**的消息
  （绝不偷看未来）。

纯函数模块：绝不调用 Jev / 任何网络，绝不打印或记录聊天内容；字符预算按
消息 text 长度近似计算，不引入 tokenizer 依赖。
"""

from __future__ import annotations

# 默认预算（三种同时生效，集中配置于此）：
# - 8 turns ≈ 4 个来回，覆盖“多轮问答 / 自我披露→回应”等常见场景，同时避免
#   远古历史稀释 Jev 对当前互动的判断；
# - 12 messages 防止单个 turn 连发过多时窗口被一条 turn 吃满；
# - 4000 字符 ≈ 旧“最近 5 条短消息”窗口的典型上界，同时挡住长正文（代码 /
#   JSON / 小作文）把上下文撑爆。
CONTEXT_MAX_TURNS = 8
CONTEXT_MAX_MESSAGES = 12
CONTEXT_MAX_CHARS = 4000


def group_turns(messages: list[dict]) -> list[dict]:
    """把消息序列按“连续同一 speaker”分组为 turn（保持输入顺序）。

    返回 ``[{"speaker": str, "messages": [dict, ...]}, ...]``。文本内容
    （中文 / 英文 / 多行 / URL / JSON / 代码 / 媒体 marker）完全不影响
    分组——只看 speaker 是否变化。
    """
    turns: list[dict] = []
    for m in messages:
        speaker = m.get("speaker")
        if turns and turns[-1]["speaker"] == speaker:
            turns[-1]["messages"].append(m)
        else:
            turns.append({"speaker": speaker, "messages": [m]})
    return turns


def select_context(
    prefix: list[dict],
    max_turns: int = CONTEXT_MAX_TURNS,
    max_messages: int = CONTEXT_MAX_MESSAGES,
    max_chars: int = CONTEXT_MAX_CHARS,
) -> list[dict]:
    """从 target 之前的全部历史消息中选出进入 Jev state 的上下文窗口。

    选择规则（确定性、纯函数）：

    1. 从最近的 turn 开始向前回溯，逐 turn 决定纳入与否；
    2. turn 内部从“最靠近 target 的消息”开始取，预算不足时取该 turn 靠近
       target 的**尾部**（消息粒度，保证确定性），尽量不把 turn 拦腰截断；
    3. P0：target 紧邻的上一条消息永远保留——即使它单条就超字符预算也整条
       纳入（**不做消息内截断**，避免截断 URL / 代码 / 媒体 marker 产生
       误导性碎片）；
    4. 任一预算（turn 数 / 消息数 / 字符数）耗尽即停止。

    ``prefix`` 必须只包含 target 之前的消息（调用方保证，见
    ``analyzer.analyze_messages``）。返回按 chronological 顺序排列的消息
    列表（元素即输入中的消息 dict，不复制、不附加任何内部 metadata）。
    """
    if not prefix:
        return []
    turns = group_turns(prefix)
    chosen: list[dict] = []            # 回溯顺序（最新 → 最旧）
    messages_left = max_messages
    chars_left = max_chars
    nearest_turn = True
    for turn in reversed(turns):
        if len(chosen) >= max_turns or messages_left <= 0:
            break
        take: list[dict] = []
        take_chars = 0
        for msg in reversed(turn["messages"]):
            if messages_left - len(take) <= 0:
                break
            length = len(msg.get("text") or "")
            if take_chars + length > chars_left:
                if nearest_turn and not take:
                    # P0：紧邻 target 的上一条消息整条保留（见 docstring）。
                    # 其字符仍计入预算（chars_left 可能因此为负），从而阻断
                    # 更早 turn 的纳入。
                    take.append(msg)
                    take_chars += length
                break
            take.append(msg)
            take_chars += length
        if not take:
            break
        messages_left -= len(take)
        chars_left -= take_chars
        chosen.append({"speaker": turn["speaker"],
                       "messages": list(reversed(take))})
        nearest_turn = False

    out: list[dict] = []
    for turn in reversed(chosen):
        out.extend(turn["messages"])
    return out


def debug_context_view(target_index: int, context: list[dict]) -> dict:
    """测试 / 调试辅助：查看某个 target 的上下文**结构统计**。

    只在测试或显式调试时调用；**绝不自动打印、不写日志**。只返回计数与
    speaker 序列，**不含任何消息文本**，避免泄露私人聊天内容
    （``SIGNALLENS_DEBUG_TIMING`` 的约束同样适用于这里）。
    """
    turns = group_turns(context)
    return {
        "target_index": target_index,
        "context_message_count": len(context),
        "turn_count": len(turns),
        "char_count": sum(len(m.get("text") or "") for m in context),
        "speaker_sequence": [m.get("speaker") for m in context],
        "turn_speakers": [t["speaker"] for t in turns],
    }
