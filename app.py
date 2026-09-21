"""SignalLens — Streamlit 前端（纯展示层）。

信息架构：① 粘贴聊天 → ② 确认双方 → ③ Jev 分析 → ④ 查看 / 导出结果。

本文件只负责展示与交互；所有分析结果来自 analyzer / scoring / report，
UI 操作（切 tab、筛选、展开、下载）**不会**发起任何 Jev API 请求。
"""

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
from media import (
    MediaAsset,
    bind_media,
    dedupe_assets,
    image_placeholder_messages,
)
from rich_paste import (
    assets_from_uploader,
    component_available,
    component_error,
    get_component,
)
from vision import vision_status
from tools.clipboard_probe.probe import format_probe_report
from scoring import (
    EFFECTIVE_MESSAGE_MIN_EVIDENCE,
    INTENT_PROFILE_LABELS,
    SCORE_MAX,
    compute_conversation_stats,
    confidence_label,
    evidence_level_label,
    information_coverage,
    message_metrics,
    noul_label,
    rank_relationship_signals,
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

# 展示层辅助（短标签 / 徽章 / 过滤 / 排版数据，不含业务逻辑）
from ui_helpers import (
    BEHAVIOR_MIN_DISPLAY,
    filter_entries,
    hidden_behavior_count,
    media_badge,
    media_placeholder_label,
    media_summary_text,
    overview_mode,
    preview_rows,
    skipped_media_count,
    trend_short,
    evidence_short,
    visible_behaviors,
)
DISCLAIMER = (
    "“互动亲近信号指数”仅代表聊天文本中可以观察到的亲近、主动、投入、暧昧、"
    "疏离等信号的组合，**不代表对方真实心理状态**，更不是“TA 喜欢你的概率”。"
)
DISCLAIMER_SHORT = "结果仅描述文本中的可观察信号，不代表对方真实心理状态。"

STEPS = ["① 粘贴聊天", "② 确认双方", "③ Jev 分析", "④ 查看 / 导出结果"]

SPEAKER_BADGE = {"me": "我", "them": "TA", "unknown": "待确认"}

# 少量 CSS：只用于主内容宽度与轻量排版（不覆盖 Streamlit 内部结构、无 JS）
st.set_page_config(page_title="SignalLens · 聊天互动信号分析", page_icon="🔭",
                   layout="centered")
st.markdown(
    """
    <style>
    .main .block-container { max-width: 1060px; padding-top: 2.2rem; }
    .sl-hero { font-size: 1.05rem; }
    .sl-muted { color: rgba(108, 117, 125, 0.95); font-size: 0.86rem; }
    .sl-badge {
        display: inline-block; padding: 1px 8px; margin: 0 4px 2px 0;
        border: 1px solid rgba(128,128,128,0.35); border-radius: 10px;
        font-size: 0.78rem; white-space: nowrap;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


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
        ("skipped_media", 0),
        ("msg_filter_mode", "全部消息"),
        ("show_media_events", False),
        ("sel_me", "（未指定）"),
        ("sel_ta", "（未指定）"),
        ("applied_me", None),
        ("applied_ta", None),
        # ---- v0.2.0 媒体资产（来自 file_uploader，仅内存）----
        ("media_assets", []),            # list[MediaAsset]
        ("media_bindings", {}),          # message_index -> asset_id
        ("media_manual", {}),            # message_index -> asset_id | "" (不分析)
        ("order_proven", False),         # 用户是否已实测确认顺序一致
        ("probe_value", None),           # Probe 区组件返回值（若协议可用）
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

    with st.status("正在分析聊天…", expanded=True) as status:
        bar = st.progress(0.0)
        line = st.empty()

        def cb(done: int, total: int) -> None:
            bar.progress(done / max(total, 1))
            line.caption(f"正在分析第 {done} / {total} 条 TA 消息")
            status.update(label=f"正在分析聊天… {done} / {total}")

        new_results = analyze_messages(
            client, messages, cache=cache, only_indices=only_indices, progress_cb=cb
        )
        bar.empty()
        line.empty()

        if only_failed and st.session_state["results"]:
            by_index = {e["index"]: e for e in st.session_state["results"]}
            by_index.update({e["index"]: e for e in new_results})
            st.session_state["results"] = [by_index[i] for i in sorted(by_index)]
        else:
            st.session_state["results"] = new_results

        st.session_state["stats"] = compute_conversation_stats(st.session_state["results"])
        st.session_state["skipped_media"] = skipped_media_count(messages)

        cached_n = sum(1 for e in st.session_state["results"] if e.get("cached"))
        failed_n = sum(1 for e in st.session_state["results"] if e.get("error"))
        status.update(
            label=(
                f"✓ 分析完成 · 分析 {len(st.session_state['results'])} 条 · "
                f"缓存命中 {cached_n} 条 · 跳过媒体 {st.session_state['skipped_media']} 条 · "
                f"失败 {failed_n} 条"
            ),
            state="complete",
        )


# ---------------------------------------------------------------------------
# 通用展示
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
    st.caption(f"{title}")
    st.markdown(f"{name} · **{top_prob * 100:.0f}%**")


def show_badges(items: list[str]) -> None:
    st.markdown(" ".join(f'<span class="sl-badge">{x}</span>' for x in items),
                unsafe_allow_html=True)


def show_media_event_card(entry: dict) -> None:
    """媒体事件卡片：只有中性描述，没有任何分析指标。"""
    with st.container(border=True):
        who = "我" if entry["speaker"] == "me" else "TA"
        time_part = f" · {entry['time']}" if entry.get("time") else ""
        st.markdown(f"**{who}{time_part}**")
        st.markdown(media_badge(entry.get("media_kinds") or []))
        st.caption("内容未分析")


# ---------------------------------------------------------------------------
# v0.2.0 富媒体粘贴：输入阶段 / 绑定 / 手动匹配
# ---------------------------------------------------------------------------

def _asset_by_id(asset_id: str) -> MediaAsset | None:
    for a in st.session_state.get("media_assets") or []:
        if a.id == asset_id:
            return a
    return None


def _bound_asset(message_index: int) -> MediaAsset | None:
    asset_id = (st.session_state.get("media_bindings") or {}).get(message_index)
    if asset_id:
        return _asset_by_id(asset_id)
    manual = (st.session_state.get("media_manual") or {}).get(message_index)
    if manual:
        return _asset_by_id(manual)
    return None


def _asset_thumb(asset: MediaAsset, width: int = 220):
    """渲染缩略图（仅本地内存二进制，不上传、不落盘）。"""
    if not asset or not asset.data:
        return None
    import base64

    b64 = base64.b64encode(asset.data).decode("ascii")
    st.image(f"data:{asset.mime_type};base64,{b64}",
             caption=f"{asset.mime_type} · {asset.size / 1024:.0f} KB",
             width=width)
    return asset


def show_input_stage() -> tuple[str | None, list | None]:
    """① 粘贴聊天记录：文本 + 可选图片上传。

    返回 (待解析文本, 上传的图片文件列表)；未点击提交返回 (None, None)。

    说明：浏览器剪贴板的富媒体直采依赖 Streamlit 自定义组件协议，该协议在
    当前 Streamlit 版本下无法把二进制可靠回传到 Python（已实测）。因此本阶段：
    - 文本走 text_area（与 v0.1.1 完全一致）；
    - 图片走原生 file_uploader（点击 / 拖拽，稳定可靠）；
    - “剪贴板诊断”侧栏项用组件自包含展示剪贴板真实格式，用于后续阶段决策。
    """
    with st.container(border=True):
        st.markdown("#### ① 粘贴聊天记录")
        st.caption("支持微信 / QQ 等复制文本。图片、视频、动画表情等媒体占位符"
                   "不会被当作文本分析。")
        show_api_key_hint()

        with st.form("input_form"):
            raw_text = st.text_area(
                "聊天文本",
                height=220,
                label_visibility="collapsed",
                placeholder="支持三种格式（可混合）：\n"
                            "我: 你刚才怎么一直没回我\n"
                            "22:31 我\n你干嘛呢\n"
                            "昵称A\n2026年09月08日 0:09\n消息内容\n"
                            "昵称B\n2026年09月08日 0:10\n消息内容",
            )
            uploaded = st.file_uploader(
                "添加图片（可选）——用于绑定文本中的 [图片] 占位符",
                type=["png", "jpg", "jpeg", "webp", "bmp", "gif"],
                accept_multiple_files=True,
                help="从微信保存或截图后拖入；仅存于本机内存，不发送给 Jev，"
                     "不写入报告。",
            )
            submitted = st.form_submit_button("解析并预览", type="primary")

        if uploaded:
            st.caption(f"已选择 {len(uploaded)} 张图片（仅本机内存，"
                       "不会发送给 Jev，也不写入报告）。")
            cols = st.columns(min(len(uploaded), 4))
            for i, f in enumerate(uploaded[:4]):
                with cols[i]:
                    st.image(f, width=110)

    if submitted:
        return raw_text, list(uploaded or [])
    return None, None


def reset_media_state() -> None:
    """解析新聊天时清空媒体资产与绑定（图片属于上一次粘贴）。"""
    st.session_state["media_assets"] = []
    st.session_state["media_bindings"] = {}
    st.session_state["media_manual"] = {}


def show_api_key_hint() -> None:
    """未配置 API Key 时给出友好配置说明（不崩溃、不索要明文 Key）。"""
    if os.environ.get("TYPESAFE_API_KEY", "").strip():
        return
    st.info(
        "**尚未配置 TypeSafe API Key**\n\n"
        "1. 复制项目里的 `.env.example` 为 `.env`\n"
        "2. 编辑 `.env`，填入 `TYPESAFE_API_KEY=你的Key`\n"
        "3. 重启 SignalLens\n\n"
        "Key 只保存在本机 `.env` 中，不会写入代码、日志或报告。"
    )


def apply_media_bindings(messages: list[dict]) -> object:
    """把已捕获图片绑定到占位符；返回 BindingResult。

    顺序证据目前只能来自 Clipboard Probe 的人工确认（order_proven），
    因为组件值回传在 Streamlit 1.64 下尚不可用。
    """
    assets = st.session_state.get("media_assets") or []
    order_verified = bool(st.session_state.get("order_proven"))
    result = bind_media(messages, assets, order_verified=order_verified)
    st.session_state["media_bindings"] = dict(result.auto)
    st.session_state["media_manual"] = {}
    return result


def show_media_binding_panel(messages: list[dict]) -> None:
    """② 阶段：媒体捕获 + 绑定状态 + 手动匹配。"""
    assets = st.session_state.get("media_assets") or []
    if not assets:
        return

    placeholders = image_placeholder_messages(messages)
    auto = st.session_state.get("media_bindings") or {}
    manual = st.session_state.get("media_manual") or {}

    st.markdown("**🖼 图片绑定**")
    if auto:
        st.success(f"已自动绑定 {len(auto)} 张图片（保守策略：仅在唯一对应或"
                   "顺序已验证时自动绑定）。")
    unbound = [i for i in placeholders if i not in auto and i not in manual]
    if unbound:
        st.warning(
            f"检测到 {len(placeholders)} 个图片占位符、{len(assets)} 张图片，"
            "自动对应关系不确定。请手动匹配（也可以标记“不分析”）。"
        )

    for idx in unbound:
        m = messages[idx]
        time_part = f" · {m.get('time')}" if m.get("time") else ""
        with st.container(border=True):
            st.markdown(f"**聊天图片 #{idx + 1}**　TA{time_part}")
            st.caption(f"消息内容：{m.get('text', '')[:60]}")
            options = ["（不分析）"] + [
                f"图片 #{n}（{a.mime_type}，{a.size / 1024:.0f} KB）"
                for n, a in enumerate(assets, start=1)
            ]
            choice = st.selectbox(
                "请选择图片：", options, key=f"media_match_{idx}",
            )
            cols = st.columns(min(len(assets), 4))
            for n, a in enumerate(assets[:4]):
                with cols[n]:
                    _asset_thumb(a, width=110)
            if choice and choice != "（不分析）":
                n = int(choice.split("#")[1].split("（")[0]) - 1
                st.session_state["media_manual"][idx] = assets[n].id
                st.caption("已手动绑定（仅本机显示，不进入分析）。")
            elif choice == "（不分析）":
                st.session_state["media_manual"][idx] = ""
                st.caption("已标记为不分析。")

    bound_total = len(auto) + len([v for v in manual.values() if v])
    st.caption(f"已绑定 {bound_total} / {len(placeholders)} 个图片占位符 · "
               f"图片分析：{vision_status()}")


def show_message_card(entry: dict) -> None:
    """单条 TA 消息卡片：默认只显示高价值信息，细节分层折叠。"""
    r = entry.get("result")
    with st.container(border=True):
        time_part = f" · {entry['time']}" if entry.get("time") else ""
        st.markdown(f"**TA{time_part}**")
        st.markdown(f"“{entry['text']}”")

        if entry.get("error"):
            st.error(f"本条分析失败：{entry['error']}")
            return

        col1, col2 = st.columns(2)
        with col1:
            show_top_choice("主要情绪", r["emotion"], EMOTION_LABELS)
        with col2:
            show_top_choice("主要意图", r["intent"], INTENT_OPTIONS)

        ev = r["relationship_evidence_strength"]
        ease = r["relational_ease"]
        st.caption(f"关系信息量 {ev['score']:.1f} / 4 · "
                   f"互动熟悉度 {ease['score']:.1f} / 4")

        m = message_metrics(entry)
        if m is not None and m["evidence"] < EFFECTIVE_MESSAGE_MIN_EVIDENCE:
            st.caption("关系信息量较低，不建议单独判断关系亲近程度。")
        elif m is not None:
            st.markdown(f"本条关系信号 **{m['base_score'] * 100:.0f} / 100**"
                        f"（聚合权重 {m['weight']:.2f}）")

        # ---- 折叠：详细指标 ----
        with st.expander("▶ 查看详细指标"):
            for key, name in (
                ("warmth", "温暖"),
                ("engagement", "投入"),
                ("special_attention", "特殊关注"),
            ):
                a = r[key]
                st.progress(
                    float(a["score"]) / SCORE_MAX,
                    text=f"{name}：{a['score']:.1f} / {SCORE_MAX:.0f}"
                         f"（{confidence_label(a['confidence'])}）",
                )
            st.progress(float(ease["score"]) / SCORE_MAX,
                        text=f"互动熟悉度：{ease['score']:.1f} / {SCORE_MAX:.0f}"
                             f"（{confidence_label(ease['confidence'])}）")
            st.progress(float(ev["score"]) / SCORE_MAX,
                        text=f"关系信息量：{ev['score']:.1f} / {SCORE_MAX:.0f}"
                             f"（{confidence_label(ev['confidence'])}）")
            c1, c2 = st.columns(2)
            with c1:
                st.progress(
                    float(r["romantic_signal"]),
                    text=f"暧昧 raw：{r['romantic_signal'] * 100:.0f}%"
                         f"（{noul_label(r['romantic_signal'])}）",
                )
            with c2:
                st.progress(
                    float(r["distancing_signal"]),
                    text=f"疏离 raw：{r['distancing_signal'] * 100:.0f}%"
                         f"（{noul_label(r['distancing_signal'])}）",
                )
            if m is not None:
                st.caption(
                    f"relation_confidence {m['relation_confidence']:.2f} · "
                    f"message_weight {m['weight']:.3f} · "
                    f"base_score {m['base_score']:.3f}"
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
                show_distribution("互动熟悉度（完整分布）", ease["probabilities"],
                                  None, top_n=99)

        # ---- 折叠：判断上下文 ----
        with st.expander("▶ 查看判断上下文"):
            if entry.get("context"):
                for c in entry["context"]:
                    who = "我" if c["speaker"] == "me" else "TA"
                    t = f"（{c['time']}）" if c.get("time") else ""
                    st.markdown(f"- **{who}{t}**：{c['text']}")
            else:
                st.caption("（本条之前没有上下文）")

        # ---- 最底层：debug ----
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


# ---------------------------------------------------------------------------
# 阶段指示器
# ---------------------------------------------------------------------------

def steps_markdown(current: int) -> str:
    parts = []
    for i, label in enumerate(STEPS, start=1):
        if i == current:
            parts.append(f"**:blue[{label}]**")
        elif i < current:
            parts.append(label)
        else:
            parts.append(f":grey[{label}]")
    return " → ".join(parts)


# ---------------------------------------------------------------------------
# ② 确认解析
# ---------------------------------------------------------------------------

def show_confirm_stage(messages: list[dict]) -> None:
    counts = {"me": 0, "them": 0, "unknown": 0}
    for m in messages:
        counts[m["speaker"]] = counts.get(m["speaker"], 0) + 1
    media_n = sum(1 for m in messages if m.get("content_type") == "media")
    text_n = len(messages) - media_n
    participants = detect_participants(messages)

    with st.container(border=True):
        st.markdown("#### ② 确认聊天双方")
        c0, c1, c2, c3, c4 = st.columns(5)
        c0.metric("总消息", len(messages))
        c1.metric("参与者", len(participants))
        c2.metric("文本", text_n)
        c3.metric("媒体", media_n)
        c4.metric("待确认", f"{counts['unknown']} / {counts['me'] + counts['them']}")

        # ---- 昵称映射 ----
        # 映射结果保存在非 widget 专用键里：Streamlit 会在 widget 未被渲染时
        # 清除其 state，若只依赖 sel_me/sel_ta，分析后映射显示会“回退”。
        applied_me = st.session_state.get("applied_me")
        applied_ta = st.session_state.get("applied_ta")
        if not (applied_me or applied_ta):
            st.info("已识别聊天参与者，请确认谁是“我”，谁是“TA”。")
            options = ["（未指定）"] + participants
            c1, c2 = st.columns(2)
            sel_me = c1.selectbox("我是：", options, key="sel_me")
            sel_ta = c2.selectbox("TA 是：", options, key="sel_ta")
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
                        st.session_state["applied_me"] = (
                            None if sel_me == "（未指定）" else sel_me
                        )
                        st.session_state["applied_ta"] = (
                            None if sel_ta == "（未指定）" else sel_ta
                        )
                        st.session_state["messages"] = mask_messages(parsed)
                        st.session_state["analysis_messages"] = None
                        st.session_state["results"] = None
                        st.session_state["stats"] = None
                        st.rerun()
        else:
            mapping = " · ".join(
                f"{side}：{name}" for side, name in
                (("我", applied_me), ("TA", applied_ta)) if name
            )
            st.success(
                f"✓ 身份映射完成（{mapping}）— 我：{counts['me']} 条 · "
                f"TA：{counts['them']} 条 · unknown：{counts['unknown']} 条"
            )
            if st.button("重新选择身份", key="remap"):
                st.session_state["applied_me"] = None
                st.session_state["applied_ta"] = None
                st.session_state["sel_me"] = "（未指定）"
                st.session_state["sel_ta"] = "（未指定）"
                st.session_state["analysis_messages"] = None
                st.session_state["results"] = None
                st.session_state["stats"] = None
                st.rerun()

        # ---- 媒体提示（非错误）----
        if media_n:
            st.markdown(f"**检测到非文本媒体**：{media_summary_text(messages)}")
            st.caption(
                f"其中 TA 侧跳过分析：{skipped_media_count(messages)} 条"
            )
            st.caption("复制记录中只有媒体占位符，没有实际图片或视频内容，"
                       "因此不会推测媒体内容。")

        # ---- 富媒体图片绑定 / 手动匹配 ----
        show_media_binding_panel(messages)

        # ---- 预览表（安全门禁，保留）----
        st.dataframe(
            preview_rows(messages, bindings=st.session_state.get("media_bindings") or {}),
            use_container_width=True, hide_index=True,
        )
        if len(messages) > 15:
            st.caption(f"仅预览前 15 条，共 {len(messages)} 条。预览内容已本地脱敏；"
                       "unknown = 无法确定发言人（未根据内容猜测）。")
        else:
            st.caption("预览内容已本地脱敏。unknown = 该行无法确定发言人（未根据内容猜测）。")

        # ---- 分析按钮 ----
        them_n = counts["them"]
        unknown_n = counts["unknown"]
        if them_n == 0:
            st.warning("预览中没有识别出 TA 的消息，无法进行分析。请检查昵称设置。")

        ignore_unknown = False
        if unknown_n:
            st.info(
                f"存在 {unknown_n} 条 unknown 消息。为避免把第三方的消息当作 TA，"
                "默认禁止开始分析。请在上方选择正确的昵称映射，"
                "或勾选下方选项忽略这些消息。"
            )
            ignore_unknown = st.checkbox(
                f"忽略 {unknown_n} 条 unknown 消息（不发送给 Jev）",
                value=False,
            )

        can_run = them_n > 0 and (unknown_n == 0 or ignore_unknown)
        ta_text_n = sum(
            1 for m in messages
            if m["speaker"] == "them" and m.get("content_type") != "media"
        )
        st.caption(
            f"将分析 {ta_text_n} 条 TA 文本消息 · "
            f"跳过 {skipped_media_count(messages)} 条 TA 非文本媒体 · "
            "缓存命中不会重复请求 API"
        )
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


# ---------------------------------------------------------------------------
# ④ 结果 Tabs
# ---------------------------------------------------------------------------

def _score_row(stats: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("温暖", f"{fmt(stats['warmth_avg'])} / 4")
    c2.metric("投入", f"{fmt(stats['engagement_avg'])} / 4")
    c3.metric("特殊关注", f"{fmt(stats['special_attention_avg'])} / 4")
    c4.metric("互动熟悉度", f"{fmt(stats['relational_ease_avg'])} / 4")


def _signal_row(stats: dict) -> None:
    """暧昧 / 疏离：两列宽排，完整文字不截断。"""
    rom = evidence_level_label(stats["romantic_evidence"])
    dis = evidence_level_label(stats["distancing_evidence"])
    c1, c2 = st.columns(2)
    c1.metric("暧昧信号", rom)
    c2.metric("疏离信号", dis)


def show_overview_tab(results: list[dict], stats: dict) -> None:
    if overview_mode(stats) == "reference":
        # ---- 低信息量：不大字号展示总分 ----
        st.warning("⚠ 当前样本关系信息不足")
        st.markdown(
            f"本次只有 **{stats['effective_messages']} / {stats['analyzed']}** "
            "条消息包含较明确的关系层面信息。"
        )
        st.markdown("“互动亲近信号指数”仅供参考，不建议据此判断整体关系亲近程度。")
        c0, c1, c2, c3 = st.columns(4)
        c0.metric("参考指数",
                  f"{fmt(stats['overall'])} / 100" if stats["overall"] is not None else "—")
        c1.metric("关系信息量", total_evidence_label(stats["total_weight"]))
        coverage = information_coverage(stats)
        c2.metric("信息覆盖率", f"{coverage * 100:.0f}%" if coverage is not None else "-")
        c3.metric("趋势", trend_short(stats["trend"]))
    else:
        with st.container(border=True):
            st.caption("互动亲近信号指数")
            st.markdown(f"### {fmt(stats['overall'])} / 100")
            c1, c2, c3 = st.columns(3)
            c1.metric("关系信息量", total_evidence_label(stats["total_weight"]))
            c2.metric("有效关系消息", f"{stats['effective_messages']} / {stats['analyzed']}")
            c3.metric("趋势", trend_short(stats["trend"]))

    _score_row(stats)
    _signal_row(stats)
    coverage = information_coverage(stats)
    if coverage is not None:
        st.caption(f"关系信息覆盖率 {coverage * 100:.0f}%"
                   "（有多少消息包含较明确的关系层面信息，不代表关系好坏）")
    st.caption(DISCLAIMER_SHORT)

    # ---- 互动方式 ----
    profiles = stats.get("intent_profiles") or {}
    if profiles:
        st.markdown("**互动方式**")
        visible = visible_behaviors(profiles)
        if visible:
            half = (len(visible) + 1) // 2
            c1, c2 = st.columns(2)
            for i, (name, value) in enumerate(visible):
                target = c1 if i < half else c2
                target.markdown(f"{name}　**{value * 100:.0f}%**")
        hidden = hidden_behavior_count(profiles)
        if hidden:
            with st.expander(f"查看全部行为统计（另有 {hidden} 项低于 "
                             f"{BEHAVIOR_MIN_DISPLAY * 100:.0f}%）"):
                for key, value in sorted(profiles.items(), key=lambda kv: -kv[1]):
                    st.markdown(
                        f"- {INTENT_PROFILE_LABELS.get(key, key)}：{value * 100:.0f}%"
                    )
                st.caption("来自 intent 完整概率分布的加权平均，仅作解释，不计入总分。")

    # ---- 确定性摘要 ----
    st.markdown("**分析摘要**")
    st.markdown(build_summary_text(results, stats))

    if stats.get("warnings"):
        st.caption("⚠️ " + "；".join(stats["warnings"]))

    failed = [e for e in results if e.get("error")]
    if failed:
        ids = "、".join(str(e["index"] + 1) for e in failed)
        st.warning(f"第 {ids} 条分析失败，可重新分析。")
        if st.button("重新分析失败项", key="retry_failed"):
            run_analysis(st.session_state["analysis_messages"], only_failed=True)
            st.rerun()


def show_key_messages_tab(results: list[dict]) -> None:
    st.markdown("#### 关键互动消息")
    st.caption("按关系信息量与判断置信度排序，仅用于解释整体结果。")
    ranked = rank_relationship_signals(results, max_n=5)
    if not ranked:
        st.caption("没有关系信息量足够高的消息。")
        return
    for item in ranked:
        e, m = item["entry"], item["metrics"]
        r = e["result"]
        with st.container(border=True):
            time_part = f" · {e['time']}" if e.get("time") else ""
            st.markdown(f"**TA{time_part}**")
            st.markdown(f"“{e['text']}”")
            top_emotion = max(r["emotion"]["probabilities"].items(), key=lambda kv: kv[1])
            top_intent = max(r["intent"]["probabilities"].items(), key=lambda kv: kv[1])
            show_badges([
                f"{EMOTION_LABELS.get(top_emotion[0], top_emotion[0])} "
                f"{top_emotion[1] * 100:.0f}%",
                f"{INTENT_OPTIONS.get(top_intent[0], top_intent[0])} "
                f"{top_intent[1] * 100:.0f}%",
                f"熟悉度 {m['relational_ease']:.1f}/4",
                f"关系信息量 {m['evidence']:.1f}/4",
            ])
            st.markdown(f"关系信号 **{m['base_score'] * 100:.0f} / 100**")


def show_all_messages_tab(results: list[dict], stats: dict) -> None:
    st.markdown("#### 全部消息")
    c1, c2 = st.columns([2, 3])
    with c1:
        st.selectbox(
            "显示消息",
            ["全部消息", "仅有效关系消息", "关系信息量最高 Top 5"],
            key="msg_filter_mode",
        )
    with c2:
        st.checkbox("显示媒体事件", key="show_media_events")

    skipped = st.session_state.get("skipped_media", 0)
    st.caption(
        f"TA 文本消息 {stats['analyzed']} · 有效关系消息 {stats['effective_messages']} · "
        f"跳过 TA 媒体 {skipped} · 分析失败 {stats['failed']}"
    )

    visible = filter_entries(
        results,
        mode=st.session_state.get("msg_filter_mode", "全部消息"),
        include_media=st.session_state.get("show_media_events", False),
        messages=st.session_state.get("analysis_messages") or [],
    )
    if not visible:
        st.caption("当前过滤条件下没有可显示的消息。")
        return
    for entry in visible:
        if entry.get("media_event"):
            show_media_event_card(entry)
        else:
            show_message_card(entry)


def show_report_tab(results: list[dict], stats: dict) -> None:
    st.markdown("#### 报告导出")
    st.caption("导出完全基于本次已完成的本地分析结果，不会发起任何 TypeSafe API 请求。")

    st.text_area("分析摘要（可复制）", value=build_summary_text(results, stats),
                 height=150)

    include_text = st.checkbox(
        "报告中包含原始聊天文本",
        value=False,
        help="关闭时导出匿名报告：只保留统计与消息编号，不含聊天原文。",
    )
    skipped = st.session_state.get("skipped_media", 0)
    md = build_markdown_report(results, stats, include_text=include_text,
                               skipped_media=skipped)
    payload_json = json.dumps(
        build_json_report(results, stats, include_text=include_text,
                          skipped_media=skipped),
        ensure_ascii=False, indent=2,
    )
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("下载 Markdown", data=md,
                           file_name=report_filename("md"), mime="text/markdown")
    with c2:
        st.download_button("下载 JSON", data=payload_json,
                           file_name=report_filename("json"), mime="application/json")
    st.caption("需要 PDF？使用浏览器 Ctrl+P → 另存为 PDF。")


def show_results(results: list[dict], stats: dict) -> None:
    tabs = st.tabs(["概览", "关键消息", "全部消息", "报告"])
    with tabs[0]:
        show_overview_tab(results, stats)
    with tabs[1]:
        show_key_messages_tab(results)
    with tabs[2]:
        show_all_messages_tab(results, stats)
    with tabs[3]:
        show_report_tab(results, stats)


# ---------------------------------------------------------------------------
# 侧栏
# ---------------------------------------------------------------------------

def show_sidebar() -> None:
    with st.sidebar:
        st.markdown("### SignalLens")
        st.divider()
        st.caption("模型")
        st.markdown(f"`{DEFAULT_MODEL}`")
        st.caption("分析引擎")
        st.markdown("TypeSafe Jev")
        st.caption("隐私")
        st.markdown("本地脱敏后发送")
        st.divider()
        if st.button("清除本地分析缓存", use_container_width=True):
            get_cache().clear()
            st.toast("本地缓存已清除。")
        st.divider()
        st.caption("高级")
        with st.expander("▶ 隐私说明"):
            st.caption(DISCLAIMER)
            st.caption(
                "聊天内容在本地脱敏（手机号 / 邮箱 / 身份证 / IP / URL / API Key / "
                "银行卡号）后，仅将目标消息与最多 5 条上下文发送给 TypeSafe Jev；"
                "解析、缓存与报告导出均在本地完成。"
            )
        with st.expander("▶ 关于指标"):
            st.caption(
                "互动亲近信号指数 = 按 message_weight 加权的关系信号聚合，"
                "权重 = 关系信息量 × Jev 置信度；低信息量短回复不会稀释结论。"
            )
            st.caption(
                "互动熟悉度（relational_ease）衡量互动的自然与默契程度，"
                "不计入指数，也不等于浪漫兴趣或特殊关注。"
            )
        with st.expander("▶ 剪贴板诊断（Clipboard Probe）"):
            show_clipboard_probe()


def show_clipboard_probe() -> None:
    """剪贴板诊断（Clipboard Probe）——iframe 内自包含，不依赖 Python 回传。

    在微信 PC 选中聊天 → Ctrl+C → 在下面 Ctrl+V，组件内会直接显示：
    clipboardData.items / files、MIME、图片数量与尺寸、item 顺序、
    文本内媒体占位符数量，并可导出仅含元数据的诊断 JSON。

    说明：Streamlit 1.64 下自定义组件协议暂不能把二进制可靠回传 Python
    （已实测：ready 握手与消息投递均正常，但组件值不会出现在 Python 侧），
    因此 Probe 采用自包含设计；主流程的图片输入使用 file_uploader。
    """
    st.caption(
        "从微信复制一段包含文字 / 普通图片 / 动画表情 / 视频的聊天，"
        "在下面 Ctrl+V，即可看到实际收到的剪贴板格式。"
    )
    st.caption(
        "诊断结果在下方组件内直接显示；点“导出诊断信息”可下载"
        "（只含 MIME / 数量 / 尺寸 / 顺序，不含聊天文本、图片内容或文件路径）。"
    )
    if not component_available():
        st.warning("富媒体组件不可用，无法进行剪贴板诊断。")
        if component_error():
            st.caption(f"组件错误：{component_error()}")
        return

    component = get_component()
    value = component(key="rich_paste_probe")
    if value and value != st.session_state.get("probe_value"):
        st.session_state["probe_value"] = value

    # 若某天组件协议可用，这里附带给出版本；否则自包含 iframe 已满足诊断。
    value = st.session_state.get("probe_value")
    if value:
        st.success("组件值已回传到 Python（协议可用）——以下为 Python 侧视图：")
        st.markdown(format_probe_report(value))
        st.checkbox(
            "我已确认剪贴板图片顺序与文本占位符顺序一致（启用 N:N 顺序绑定）",
            key="order_proven",
        )
    else:
        st.caption("尚未粘贴（或当前 Streamlit 版本不支持组件值回传，"
                   "以上方组件内显示为准）。")


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------

def main() -> None:
    init_state()
    st.title("SignalLens")
    st.markdown("**聊天互动信号分析** · Powered by TypeSafe Jev")
    st.caption("分析聊天文本中可观察到的情绪、意图、投入、熟悉度、特殊关注等互动信号。")
    st.caption(DISCLAIMER_SHORT)

    messages = st.session_state.get("messages")
    results = st.session_state.get("results")
    stats = st.session_state.get("stats")

    # 步骤指示器用占位符预留顶部位置：分析在同一次 run 的后半段才完成，
    # 若在顶部直接渲染，步骤会落后一个 run（需再交互一次才更新）。
    steps_slot = st.empty()
    steps_slot.markdown(steps_markdown(
        4 if (results and stats) else (2 if messages else 1)
    ))

    # ① 输入
    submitted_text, uploaded = show_input_stage()
    if submitted_text is not None:
        try:
            parsed = parse_chat(submitted_text)
        except ParseError as exc:
            st.error(str(exc))
            st.session_state["messages"] = None
            st.session_state["analysis_messages"] = None
            st.session_state["results"] = None
            st.session_state["stats"] = None
            st.session_state["applied_me"] = None
            st.session_state["applied_ta"] = None
            reset_media_state()
        else:
            st.session_state["raw_text"] = submitted_text
            st.session_state["messages"] = mask_messages(parsed)
            st.session_state["analysis_messages"] = None
            st.session_state["results"] = None
            st.session_state["stats"] = None
            st.session_state["skipped_media"] = 0
            # 新文本：清空旧的昵称选择与应用记录，避免误映射
            st.session_state["sel_me"] = "（未指定）"
            st.session_state["sel_ta"] = "（未指定）"
            st.session_state["applied_me"] = None
            st.session_state["applied_ta"] = None
            # 媒体资产：来自本次上传的图片（仅内存）
            reset_media_state()
            assets, errors = assets_from_uploader(uploaded)
            st.session_state["media_assets"] = dedupe_assets(assets)
            for err in errors:
                st.warning(err)
            # 保守绑定到 [图片] 占位符
            apply_media_bindings(st.session_state["messages"])

    # ② 确认解析
    messages = st.session_state.get("messages")
    if messages:
        show_confirm_stage(messages)

    # ③④ 结果
    results = st.session_state.get("results")
    stats = st.session_state.get("stats")
    if results and stats:
        # 分析已完成 → 步骤推进到 ④（同一次 run 内更新占位符）
        steps_slot.markdown(steps_markdown(4))
        show_results(results, stats)

    show_sidebar()


if __name__ == "__main__":
    main()
