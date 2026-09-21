"""Jev 聊天情绪 / 意图 / 互动亲近度分析器 — Streamlit 前端。"""

from __future__ import annotations

import json
import os

import streamlit as st
from dotenv import load_dotenv

load_dotenv()  # 从项目目录的 .env 读取 TYPESAFE_API_KEY（若存在）

from analyzer import (
    DEFAULT_MODEL,
    EMOTION_LABELS,
    INTENT_OPTIONS,
    analyze_messages,
    create_client,
)
from parser import ParseError, detect_participants, parse_chat
from privacy import mask_messages
from scoring import (
    EFFECTIVE_MESSAGE_MIN_EVIDENCE,
    INTENT_PROFILE_LABELS,
    SCORE_MAX,
    TREND_LABELS,
    compute_conversation_stats,
    confidence_label,
    evidence_level_label,
    information_coverage,
    is_low_evidence_display,
    message_metrics,
    noul_label,
    rank_relationship_signals,
    relational_ease_label,
    score_level_label,
    total_evidence_label,
)
from storage import Cache

# 报告导出（纯本地数据处理，不调用 Jev API）
from report import (
    build_json_report,
    build_markdown_report,
    build_summary_text,
    report_filename,
)

DISCLAIMER = (
    "“互动亲近信号指数”仅代表聊天文本中可以观察到的亲近、主动、投入、暧昧、"
    "疏离等信号的组合，**不代表对方真实心理状态**，更不是“TA 喜欢你的概率”。"
)

# 顶部 metric 短标签（避免窄列文本截断；完整解释放下方 caption）
TREND_SHORT = {
    "up": "↑ 上升",
    "flat": "→ 稳定",
    "down": "↓ 下降",
    "insufficient_samples": "样本不足",
    "insufficient_evidence": "信息不足",
}
EVIDENCE_SHORT = {
    "未发现明显信号": "未发现",
    "存在少量弱信号": "弱信号",
    "存在一定信号": "一定信号",
    "存在较明显信号": "较明显",
    "存在强信号": "强信号",
    "有效信息不足": "信息不足",
}

st.set_page_config(page_title="Jev 聊天信号分析器", page_icon="💬", layout="centered")


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------

def init_state() -> None:
    for key, default in (
        ("messages", None),
        ("raw_text", ""),
        ("analysis_messages", None),
        ("results", None),
        ("stats", None),
        ("run_error", None),
    ):
        if key not in st.session_state:
            st.session_state[key] = default


def get_cache() -> Cache:
    if "cache" not in st.session_state:
        st.session_state["cache"] = Cache()
    return st.session_state["cache"]


def run_analysis(messages: list[dict], only_failed: bool = False) -> None:
    """对 TA 的消息逐条分析（含缓存）。失败项不中断，写入 error 字段。"""
    st.session_state["run_error"] = None
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        st.session_state["run_error"] = (
            "未检测到 TYPESAFE_API_KEY。请复制 .env.example 为 .env 并填入 API Key 后重启。"
        )
        return

    cache = get_cache()
    only_indices = None
    if only_failed and st.session_state["results"]:
        only_indices = {e["index"] for e in st.session_state["results"] if e.get("error")}

    try:
        client = create_client(api_key=api_key)
    except Exception as exc:  # SDK 初始化失败（如无 key）
        st.session_state["run_error"] = f"初始化 TypeSafe 客户端失败：{exc}"
        return

    progress = st.progress(0.0)
    status = st.empty()

    def cb(done: int, total: int) -> None:
        progress.progress(done / max(total, 1))
        status.text(f"正在分析第 {done}/{total} 条 TA 的消息…")

    new_results = analyze_messages(
        client, messages, cache=cache, only_indices=only_indices, progress_cb=cb
    )
    progress.empty()
    status.empty()

    if only_failed and st.session_state["results"]:
        by_index = {e["index"]: e for e in st.session_state["results"]}
        by_index.update({e["index"]: e for e in new_results})
        st.session_state["results"] = [by_index[i] for i in sorted(by_index)]
    else:
        st.session_state["results"] = new_results

    st.session_state["stats"] = compute_conversation_stats(st.session_state["results"])


# ---------------------------------------------------------------------------
# 展示辅助
# ---------------------------------------------------------------------------

def fmt(v, digits: int = 1) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def show_distribution(title: str, probabilities: dict, label_map: dict | None = None,
                      top_n: int = 3) -> None:
    st.markdown(f"**{title}**")
    items = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    for key, prob in items:
        name = (label_map or {}).get(key, key)
        st.progress(float(prob), text=f"{name} {prob * 100:.0f}%")


def show_top_choice(title: str, answer: dict, label_map: dict) -> None:
    """默认只展示 top-1 标签 + 概率；完整分布在折叠区。"""
    top_key, top_prob = max(answer["probabilities"].items(), key=lambda kv: kv[1])
    name = label_map.get(top_key, top_key)
    st.progress(float(top_prob), text=f"{title}：{name} {top_prob * 100:.0f}%")


def show_message_card(entry: dict) -> None:
    r = entry.get("result")
    with st.container(border=True):
        time_part = f"（{entry['time']}）" if entry.get("time") else ""
        st.markdown(f"**TA{time_part}：** “{entry['text']}”")

        if entry.get("error"):
            st.error(f"本条分析失败：{entry['error']}")
            return

        # ---- 默认可见：top-1 情绪 / 意图 + 两个解释层 Score ----
        col1, col2 = st.columns(2)
        with col1:
            show_top_choice("情绪", r["emotion"], EMOTION_LABELS)
        with col2:
            show_top_choice("意图", r["intent"], INTENT_OPTIONS)

        ev = r["relationship_evidence_strength"]
        ease = r["relational_ease"]
        st.progress(float(ev["score"]) / SCORE_MAX,
                    text=f"关系信息量：{ev['score']:.1f} / {SCORE_MAX:.0f}")
        st.progress(float(ease["score"]) / SCORE_MAX,
                    text=f"互动熟悉度：{ease['score']:.1f} / {SCORE_MAX:.0f}"
                         f"（{relational_ease_label(ease['score'])}）")

        m = message_metrics(entry)
        if m is not None and m["evidence"] < EFFECTIVE_MESSAGE_MIN_EVIDENCE:
            st.caption("关系信息量较低，本条不适合单独判断关系亲近程度。")
        elif m is not None:
            st.progress(
                m["base_score"],
                text=f"本条关系信号：{m['base_score'] * 100:.0f} / 100"
                     f"（聚合权重 {m['weight']:.2f}）",
            )

        # ---- 折叠：详细指标 ----
        with st.expander("▶ 查看详细指标"):
            for key, name in (
                ("warmth", "温暖程度"),
                ("engagement", "投入程度"),
                ("special_attention", "特殊关注"),
            ):
                a = r[key]
                st.progress(
                    float(a["score"]) / SCORE_MAX,
                    text=f"{name}：{a['score']:.1f} / {SCORE_MAX:.0f}"
                         f"（{confidence_label(a['confidence'])}）",
                )
            c1, c2 = st.columns(2)
            with c1:
                st.progress(
                    float(r["romantic_signal"]),
                    text=f"暧昧信号（原始概率）：{r['romantic_signal'] * 100:.0f}%"
                         f"（{noul_label(r['romantic_signal'])}）",
                )
            with c2:
                st.progress(
                    float(r["distancing_signal"]),
                    text=f"疏离信号（原始概率）：{r['distancing_signal'] * 100:.0f}%"
                         f"（{noul_label(r['distancing_signal'])}）",
                )
            if m is not None:
                st.caption(
                    f"relation_confidence：{m['relation_confidence']:.2f}　|　"
                    f"message_weight：{m['weight']:.3f}　|　"
                    f"base_score：{m['base_score']:.3f}"
                )
            with st.expander("查看完整概率分布"):
                show_distribution("情绪（完整分布）", r["emotion"]["probabilities"],
                                  EMOTION_LABELS, top_n=99)
                st.caption(
                    f"情绪置信度：{r['emotion']['confidence']:.2f}"
                    f"（{confidence_label(r['emotion']['confidence'])}）"
                )
                show_distribution("意图（完整分布）", r["intent"]["probabilities"],
                                  INTENT_OPTIONS, top_n=99)
                st.caption(
                    f"意图置信度：{r['intent']['confidence']:.2f}"
                    f"（{confidence_label(r['intent']['confidence'])}）"
                )
                show_distribution("关系信息量（完整分布）", ev["probabilities"],
                                  None, top_n=99)
                st.caption(
                    f"关系信息量置信度：{ev['confidence']:.2f}"
                    f"（{confidence_label(ev['confidence'])}）"
                )
                show_distribution("互动熟悉度（完整分布）", ease["probabilities"],
                                  None, top_n=99)
                st.caption(
                    f"互动熟悉度置信度：{ease['confidence']:.2f}"
                    f"（{confidence_label(ease['confidence'])}）"
                )

        # ---- 折叠：判断上下文 ----
        with st.expander("▶ 查看判断上下文"):
            if entry["context"]:
                for c in entry["context"]:
                    who = "我" if c["speaker"] == "me" else "TA"
                    t = f"（{c['time']}）" if c.get("time") else ""
                    st.markdown(f"- **{who}{t}**：{c['text']}")
            else:
                st.caption("（本条之前没有上下文）")

        # ---- 最底层：debug 原始数据 ----
        with st.expander("▶ Debug"):
            if m is not None:
                st.markdown(
                    f"- base_score：{m['base_score']:.3f}\n"
                    f"- romantic 原始概率：{m['romantic_raw']:.2f}"
                    f" → evidence：{m['romantic_ev']:.3f}\n"
                    f"- distancing 原始概率：{m['distancing_raw']:.2f}"
                    f" → evidence：{m['distancing_ev']:.3f}\n"
                    f"- relation_confidence：{m['relation_confidence']:.2f}"
                )
            if entry.get("cached"):
                st.caption("缓存结果")


def show_export(results: list[dict], stats: dict) -> None:
    """报告导出：摘要文本 + Markdown / JSON 下载。

    完全基于已分析完成的数据，不发起任何 Jev API 请求。
    """
    st.subheader("报告导出")
    include_text = st.checkbox(
        "报告中包含原始聊天内容",
        value=False,
        help="关闭时导出匿名报告：只保留统计与消息编号，不含聊天原文。"
             "默认关闭以降低隐私风险。",
    )
    summary_text = build_summary_text(results, stats)
    st.text_area("分析摘要（可复制）", value=summary_text, height=140)

    md = build_markdown_report(results, stats, include_text=include_text)
    payload_json = json.dumps(
        build_json_report(results, stats, include_text=include_text),
        ensure_ascii=False, indent=2,
    )
    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "下载 Markdown 报告",
            data=md,
            file_name=report_filename("md"),
            mime="text/markdown",
        )
    with c2:
        st.download_button(
            "下载 JSON 原始结果",
            data=payload_json,
            file_name=report_filename("json"),
            mime="application/json",
        )
    st.caption("导出完全基于本次已完成的本地分析结果，不会发起任何 TypeSafe API 请求。")


def _metric_row_second(stats: dict) -> None:
    """第二行：三项 Score + 互动熟悉度（解释层）。"""
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("温暖程度", f"{fmt(stats['warmth_avg'])} / 4")
    c2.metric("投入程度", f"{fmt(stats['engagement_avg'])} / 4")
    c3.metric("特殊关注", f"{fmt(stats['special_attention_avg'])} / 4")
    c4.metric("互动熟悉度", f"{fmt(stats['relational_ease_avg'])} / 4")
    st.caption(
        f"温暖{score_level_label(stats['warmth_avg'])}　|　"
        f"投入{score_level_label(stats['engagement_avg'])}　|　"
        f"特殊关注{score_level_label(stats['special_attention_avg'])}　|　"
        f"互动熟悉度：{relational_ease_label(stats['relational_ease_avg'])}"
        "（衡量自然 / 熟悉 / 默契，不等于喜欢或暧昧）"
    )


def _metric_row_third(stats: dict) -> None:
    """第三行：暧昧 / 疏离（metric 只放短文本，完整说明放 caption）。"""
    c1, c2, c3 = st.columns(3)
    rom_label = evidence_level_label(stats["romantic_evidence"])
    dis_label = evidence_level_label(stats["distancing_evidence"])
    c1.metric("暧昧", EVIDENCE_SHORT.get(rom_label, rom_label))
    c2.metric("疏离", EVIDENCE_SHORT.get(dis_label, dis_label))
    c3.empty()
    st.caption(f"暧昧信号：{rom_label}　|　疏离信号：{dis_label}")


def filter_entries(results: list[dict], only_effective: bool, mode: str) -> list[dict]:
    """仅改变 UI 展示，绝不修改分析结果。"""
    entries = sorted(results, key=lambda e: e["index"])
    if only_effective or mode == "仅有效关系消息":
        entries = [
            e for e in entries
            if (m := message_metrics(e)) is not None
            and m["evidence"] >= EFFECTIVE_MESSAGE_MIN_EVIDENCE
        ]
    elif mode == "关系信息量最高 Top 5":
        entries = [item["entry"] for item in rank_relationship_signals(results, max_n=5)]
        entries.sort(key=lambda e: e["index"])
    return entries


def show_summary(results: list[dict], stats: dict) -> None:
    st.divider()
    low_evidence = is_low_evidence_display(stats)
    coverage = information_coverage(stats)

    if low_evidence:
        # ---- 低信息量模式：不用大号总分制造“关系只有 XX 分”的错觉 ----
        st.warning("⚠ 当前样本关系信息不足")
        st.caption("以下指数仅作参考，不建议据此判断整体关系亲近程度。")
        c0, c1, c2 = st.columns(3)
        c0.metric("参考指数",
                  f"{fmt(stats['overall'])} / 100" if stats["overall"] is not None else "—")
        c1.metric("有效消息", f"{stats['effective_messages']} / {stats['analyzed']}")
        c2.metric("关系信息量", total_evidence_label(stats["total_weight"]))
        if coverage is not None:
            st.caption(f"关系信息覆盖率：{coverage * 100:.0f}%"
                       "（本次聊天中有多少消息包含较明确的关系层面信息，不代表关系好坏）")
        if stats["overall"] is None:
            st.info("当前样本缺少足够的关系层面信息，暂不生成可靠的互动亲近信号指数。")
    else:
        # ---- 正常模式 ----
        c0, c1, c2, c3 = st.columns(4)
        if stats["overall"] is not None:
            c0.metric("互动亲近信号指数", f"{fmt(stats['overall'])} / 100")
        else:
            c0.metric("互动亲近信号指数", "—")
            st.info("当前样本缺少足够的关系层面信息，暂不生成可靠的互动亲近信号指数。")
        c1.metric("关系信息量", total_evidence_label(stats["total_weight"]))
        c2.metric("有效消息", f"{stats['effective_messages']} / {stats['analyzed']}")
        c3.metric("趋势", TREND_SHORT.get(stats["trend"], stats["trend"]))
        if coverage is not None:
            st.caption(f"关系信息覆盖率：{coverage * 100:.0f}%"
                       "（本次聊天中有多少消息包含较明确的关系层面信息，不代表关系好坏）")

    _metric_row_second(stats)
    _metric_row_third(stats)

    recent_text = (
        f"最近 {min(10, stats['analyzed'])} 条加权平均：{fmt(stats['recent'])}"
        if stats["recent_sufficient"] and stats["recent"] is not None
        else "近期有效关系信息不足"
    )
    halves_text = (
        f"前半段：{fmt(stats['first_half'])}　→　后半段：{fmt(stats['second_half'])}"
        if stats["first_half"] is not None and stats["second_half"] is not None
        else "前后半段趋势：有效信息不足"
    )
    st.caption(f"{recent_text}　|　{halves_text}")
    if stats["warnings"]:
        st.caption("⚠️ " + "；".join(stats["warnings"]))
    st.info(DISCLAIMER)

    # ---- 报告导出（纯本地数据，0 次 Jev API 请求）----
    show_export(results, stats)

    # conversation-level 行为统计：解释层，暂不计入总分
    if stats["intent_profiles"]:
        st.markdown("**整段互动行为**（基于完整概率分布的加权平均，仅作解释，未计入总分）")
        pos_col, neg_col = st.columns(2)
        positive = ["ask_information", "continue_topic", "show_care",
                    "share_personal", "invite", "tease"]
        negative = ["perfunctory", "end_topic", "distance"]
        with pos_col:
            for key in positive:
                v = stats["intent_profiles"].get(key, 0.0)
                st.progress(v, text=f"{INTENT_PROFILE_LABELS[key]} {v * 100:.0f}%")
        with neg_col:
            for key in negative:
                v = stats["intent_profiles"].get(key, 0.0)
                st.progress(v, text=f"{INTENT_PROFILE_LABELS[key]} {v * 100:.0f}%")

    with st.expander("debug：原始 Noul 概率统计（未转换，仅参考）"):
        st.markdown(
            f"- 平均 raw romantic probability：{fmt(stats['romantic_raw_avg'] * 100 if stats['romantic_raw_avg'] is not None else None, 1)}%\n"
            f"- 平均 raw distancing probability：{fmt(stats['distancing_raw_avg'] * 100 if stats['distancing_raw_avg'] is not None else None, 1)}%\n"
            f"- Σ(message_weight)：{stats['total_weight']:.2f}"
        )

    failed = [e for e in results if e.get("error")]
    if failed:
        ids = "、".join(str(e["index"] + 1) for e in failed)
        st.warning(f"第 {ids} 条分析失败，可重新分析。")
        if st.button("重新分析失败项", key="retry_failed"):
            run_analysis(st.session_state["analysis_messages"], only_failed=True)
            st.rerun()

    # ---- 逐条分析（含纯 UI 过滤器，不修改分析结果）----
    header_col, filter_col = st.columns([3, 2])
    with header_col:
        st.subheader("逐条分析（TA 的消息）")
    with filter_col:
        filter_mode = st.selectbox(
            "显示",
            ["全部消息", "仅有效关系消息", "关系信息量最高 Top 5"],
            index=0,
            key="msg_filter_mode",
            help="只改变展示，不修改分析结果。",
        )
    only_effective = st.checkbox(
        f"只显示关系信息量 ≥ {EFFECTIVE_MESSAGE_MIN_EVIDENCE:.0f} 的消息",
        value=False,
        key="msg_filter_effective",
    )
    visible = filter_entries(results, only_effective, filter_mode)
    if not visible:
        st.caption("当前过滤条件下没有可显示的消息。")
    for entry in visible:
        show_message_card(entry)


# ---------------------------------------------------------------------------
# 解析预览
# ---------------------------------------------------------------------------

SPEAKER_BADGE = {"me": "🙋 我", "them": "💗 TA", "unknown": "❓ unknown"}


def show_preview(messages: list[dict]) -> dict:
    """展示解析预览（总条数、我/TA/unknown 数量、前 15 条明细）。

    返回计数 {"me": x, "them": y, "unknown": z}。
    """
    st.subheader("解析预览")
    counts = {"me": 0, "them": 0, "unknown": 0}
    for m in messages:
        counts[m["speaker"]] = counts.get(m["speaker"], 0) + 1

    c0, c1, c2, c3 = st.columns(4)
    c0.metric("总条数", len(messages))
    c1.metric("我", counts["me"])
    c2.metric("TA", counts["them"])
    c3.metric("unknown", counts["unknown"])

    rows = []
    for idx, m in enumerate(messages[:15]):
        text = m["text"].replace("\n", " ⏎ ")
        if len(text) > 50:
            text = text[:50] + "…"
        rows.append(
            {
                "#": idx + 1,
                "发言人": SPEAKER_BADGE.get(m["speaker"], m["speaker"]),
                "时间": m.get("time") or "-",
                "内容": text,
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)
    if len(messages) > 15:
        st.caption(f"仅预览前 15 条，共 {len(messages)} 条。预览内容已本地脱敏。")
    else:
        st.caption("预览内容已本地脱敏。unknown = 该行无法确定发言人（未根据内容猜测）。")
    return counts


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------

def main() -> None:
    init_state()
    st.title("💬 Jev 聊天信号分析器")
    st.caption(
        "使用 TypeSafe Jev（System One）对 TA 的每条消息做结构化判断："
        "情绪 · 意图 · 温暖 / 投入 / 特殊关注 · 暧昧与疏离信号。"
    )
    st.info("🔒 聊天内容会经过**本地脱敏**（手机号 / 邮箱 / 身份证 / IP / URL / API Key / 银行卡号）"
            "后发送给 TypeSafe Jev 进行分析。")

    with st.form("input_form"):
        raw_text = st.text_area(
            "粘贴聊天文本",
            height=220,
            placeholder="支持三种格式（可混合）：\n"
            "我: 你刚才怎么一直没回我\n"
            "22:31 我\n你干嘛呢\n"
            "昵称A\n2026年09月08日 0:09\n消息内容\n"
            "昵称B\n2026年09月08日 0:10\n消息内容",
        )
        submitted = st.form_submit_button("解析并预览", type="primary")

    if submitted:
        try:
            parsed = parse_chat(raw_text)
        except ParseError as exc:
            st.error(str(exc))
            st.session_state["messages"] = None
            st.session_state["analysis_messages"] = None
            st.session_state["results"] = None
            st.session_state["stats"] = None
        else:
            st.session_state["raw_text"] = raw_text
            st.session_state["messages"] = mask_messages(parsed)
            st.session_state["analysis_messages"] = None
            st.session_state["results"] = None
            st.session_state["stats"] = None
            # 新文本：清空旧的昵称选择，避免误映射
            st.session_state["sel_me"] = "（未指定）"
            st.session_state["sel_ta"] = "（未指定）"

    messages = st.session_state.get("messages")
    if messages:
        counts = show_preview(messages)

        # ---- 参与者昵称检测与“我 / TA”映射（不擅自判断，由用户选择）----
        participants = detect_participants(messages)
        options = ["（未指定）"] + participants
        c1, c2 = st.columns(2)
        sel_me = c1.selectbox("哪个昵称是“我”？", options, key="sel_me")
        sel_ta = c2.selectbox("哪个昵称是“TA”？", options, key="sel_ta")
        if len(participants) == 2:
            st.caption(f"检测到 2 个参与者昵称：{participants[0]}、{participants[1]}，请分别指定哪个是“我”、哪个是“TA”。")
        if st.button(
            "应用昵称映射并重新解析",
            disabled=(sel_me == "（未指定）" and sel_ta == "（未指定）"),
        ):
            if sel_me != "（未指定）" and sel_me == sel_ta:
                st.error("“我”和“TA”不能选择同一个昵称。")
            else:
                try:
                    parsed = parse_chat(
                        st.session_state["raw_text"],
                        None if sel_me == "（未指定）" else sel_me,
                        None if sel_ta == "（未指定）" else sel_ta,
                    )
                except ParseError as exc:
                    st.error(str(exc))
                else:
                    st.session_state["messages"] = mask_messages(parsed)
                    st.session_state["analysis_messages"] = None
                    st.session_state["results"] = None
                    st.session_state["stats"] = None
                    st.rerun()

        unknown_n = counts.get("unknown", 0)
        them_n = counts.get("them", 0)

        if them_n == 0:
            st.warning("预览中没有识别出 TA 的消息，无法进行分析。请检查昵称设置。")

        ignore_unknown = False
        if unknown_n:
            st.info(
                f"存在 {unknown_n} 条 unknown 消息。为避免把第三方的消息当作 TA，"
                "默认禁止开始分析。你可以：在上方选择正确的昵称映射并"
                "“应用昵称映射并重新解析”，或勾选下方选项忽略这些消息。"
            )
            ignore_unknown = st.checkbox(
                f"忽略 {unknown_n} 条 unknown 消息（不发送给 Jev）",
                value=False,
            )

        can_run = them_n > 0 and (unknown_n == 0 or ignore_unknown)
        if st.button("开始 Jev 分析", type="primary", disabled=not can_run):
            target = (
                [m for m in messages if m["speaker"] in ("me", "them")]
                if (unknown_n and ignore_unknown)
                else messages
            )
            st.session_state["analysis_messages"] = target
            run_analysis(target)
            if st.session_state["run_error"]:
                st.error(st.session_state["run_error"])
                st.session_state["results"] = None
                st.session_state["stats"] = None

    if st.session_state.get("results") and st.session_state.get("stats"):
        show_summary(st.session_state["results"], st.session_state["stats"])

    with st.sidebar:
        st.markdown("**关于本工具**")
        st.caption(DISCLAIMER)
        st.caption(f"模型：`{DEFAULT_MODEL}`（可在环境变量 TYPESAFE_DEFAULT_MODEL 中修改）")
        st.caption("需要 PDF？在浏览器中按 Ctrl+P → 另存为 PDF（打印当前页面即可）。")
        if st.button("清除本地分析缓存"):
            get_cache().clear()
            st.toast("本地缓存已清除。")


if __name__ == "__main__":
    main()
