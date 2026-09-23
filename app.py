"""SignalLens — Streamlit 前端（纯展示层）。

信息架构：① 粘贴聊天 → ② 确认双方 → ③ Jev 分析 → ④ 查看 / 导出结果。

本文件只负责展示与交互；所有分析结果来自 analyzer / scoring / report，
UI 操作（切 tab、筛选、展开、下载）**不会**发起任何 Jev API 请求。
"""

from __future__ import annotations

import json
import os
import sys
import time

import streamlit as st

# 配置加载优先级：进程环境变量 > data/settings.env（portable）> 开发模式仓库 .env。
# 新版本（portable）用户不需要手工创建 .env。
import paths
import settings_store

settings_store.load_settings()

from analyzer import (
    DEFAULT_MODEL,
    EMOTION_LABELS,
    INTENT_OPTIONS,
    LAST_RUN_STATS,
    analyze_messages,
    create_client,
)
from parser import ParseError, detect_participants, parse_chat
from merge import merge_messages
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

# 时间线排序（本地纯函数：合并去重后、Context Builder 之前必须排序；
# 否则“从最新往更早追加”的导入方式会把未来消息当成上下文）
from timeline import (
    page_time_range,
    preview_page,
    sort_messages,
    order_signature,
    migrate_bindings,
)
DISCLAIMER = (
    "“互动亲近信号指数”仅代表聊天文本中可以观察到的亲近、主动、投入、暧昧、"
    "疏离等信号的组合，**不代表对方真实心理状态**，更不是“TA 喜欢你的概率”。"
)
DISCLAIMER_SHORT = "结果仅描述文本中的可观察信号，不代表对方真实心理状态。"

STEPS = ["① 粘贴聊天", "② 确认双方", "③ Jev 分析", "④ 查看 / 导出结果"]

SPEAKER_BADGE = {"me": "我", "them": "TA", "unknown": "待确认"}

# ---------------------------------------------------------------------------
# 轻量计时诊断（默认关闭）
#
# 设置环境变量 SIGNALLENS_DEBUG_TIMING=1 后，每次 rerun 结束时会往 console
# 打印各阶段耗时（解析 / 分析 / 聚合 / 各结果视图渲染 / 侧栏 / 总耗时）。
# 只打印阶段名与毫秒数，**绝不记录聊天正文、昵称、API Key 或媒体内容**。
# ---------------------------------------------------------------------------

DEBUG_TIMING = os.environ.get("SIGNALLENS_DEBUG_TIMING", "").strip().lower() \
    not in ("", "0", "false", "no", "off")

_RERUN_T0 = time.perf_counter()
_TIMINGS: list[tuple[str, float]] = []


class _Stage:
    """计时上下文：仅在 DEBUG_TIMING 开启时有开销（一次 perf_counter 调用）。"""

    def __init__(self, name: str):
        self.name = name

    def __enter__(self) -> "_Stage":
        if DEBUG_TIMING:
            self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        if DEBUG_TIMING:
            _TIMINGS.append((self.name, (time.perf_counter() - self.t0) * 1000))


def dump_timings() -> None:
    """打印本次 rerun 的阶段耗时（仅在 DEBUG_TIMING 开启时）。"""
    if not DEBUG_TIMING:
        return
    total = (time.perf_counter() - _RERUN_T0) * 1000
    parts = [f"{name}={ms:.1f}ms" for name, ms in _TIMINGS]
    print("[timing] rerun total=%.1fms | %s" % (total, " ".join(parts)),
          file=sys.stderr, flush=True)


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
        ("raw_chunks", []),              # 多片段追加：每段的原始文本（仅本机）
        ("append_stats", None),          # 最近一次追加的本地统计
        ("input_notice", None),          # {"level": ..., "text": ...}（跨 rerun 的提示）
        ("chat_input", ""),              # 聊天输入 textarea（显式 key，便于安全清空）
        ("pending_clear_input", False),  # 下一次 rerun 在 widget 创建前清空输入
        ("analysis_messages", None),
        ("results", None),
        ("stats", None),
        ("run_error", None),
        ("analysis_revision", 0),         # 分析结果版本号（报告 memo 的身份）
        ("skipped_media", 0),
        ("msg_filter_mode", "全部消息"),
        ("msg_filter_last", None),       # 上一次的过滤模式（变化时回到第 1 页）
        ("msg_page", 0),                 # 全部消息分页
        ("show_media_events", False),
        ("sel_me", "（未指定）"),
        ("sel_ta", "（未指定）"),
        ("applied_me", None),
        ("applied_ta", None),
        # ---- 分析状态机（UI 生命周期）----
        # idle / pending / running / complete / error / interrupted
        ("analysis_state", "idle"),
        ("pending_target", None),        # pending 阶段待分析的消息列表
        ("result_view", "概览"),          # 结果视图导航（懒渲染）
        ("report_cache", None),          # 报告 memo（结果未变则复用）
        # ---- v0.2.0 媒体资产（来自 file_uploader，仅内存）----
        ("media_assets", []),            # list[MediaAsset]
        ("media_bindings", {}),          # message_index -> asset_id
        ("media_manual", {}),            # message_index -> asset_id | "" (不分析)
        ("order_proven", False),         # 用户是否已实测确认顺序一致
        ("probe_value", None),           # Probe 区组件返回值（若协议可用）
        ("probe_open", False),           # Clipboard Probe 是否已显式打开（默认不加载）
        # ---- 时间线（排序 / 预览 / 不确定性）----
        ("timeline_info", None),         # timeline.TimelineResult（时间校验摘要）
        ("order_signature", None),       # 消息顺序指纹（变化即失效旧结果）
        ("media_bindings_dropped", []),  # 排序后无法安全迁移而被丢弃的自动绑定
        ("media_manual_dropped", []),    # 同上：手动绑定被丢弃 → 要求重新匹配
        ("preview_mode", "recent"),      # 预览方式：recent / earliest / all
        ("preview_page", 1),             # 分页预览页码
        ("order_confirmed", False),      # 用户已确认“时间不完整消息的顺序风险”
    ):
        if key not in st.session_state:
            st.session_state[key] = default


def bump_analysis_revision() -> int:
    revision = int(st.session_state.get("analysis_revision") or 0) + 1
    st.session_state["analysis_revision"] = revision
    return revision


def clear_analysis_results() -> None:
    st.session_state["results"] = None
    st.session_state["stats"] = None
    st.session_state["report_cache"] = None
    bump_analysis_revision()


def recover_analysis_state() -> None:
    """把上一轮遗留的 ``running`` 恢复成 ``interrupted``。

    分析是同步执行的：如果一个**新的 rerun 已经开始**，上一轮的分析就
    不可能还在运行。此时仍看到 ``running``，只可能是用户点了 Stop、
    页面被刷新 / 热重载、或异常终止。若不恢复，UI 会被永久困在忙状态。
    """
    if st.session_state.get("analysis_state") == "running":
        st.session_state["analysis_state"] = "interrupted"


def run_analysis_guarded(messages: list[dict], only_failed: bool = False) -> None:
    """同步分析的状态机包装：任何退出路径都不会留下 ``running``。"""
    st.session_state["analysis_state"] = "running"
    try:
        run_analysis(messages, only_failed=only_failed)
    except Exception:
        st.session_state["analysis_state"] = "error"
        raise
    finally:
        if st.session_state["analysis_state"] == "running":
            st.session_state["analysis_state"] = (
                "error" if st.session_state.get("run_error") else "complete"
            )


def run_pending_analysis() -> None:
    """执行上一轮点击排下的分析（pending → running → complete/error）。"""
    if st.session_state.get("analysis_state") != "pending":
        return
    target = st.session_state.get("pending_target")
    if target is None:
        target = st.session_state.get("messages")
    st.session_state["pending_target"] = None
    run_analysis_guarded(target)


def ensure_runtime_dirs() -> None:
    """启动时确保数据目录层级存在（cache / media_cache / logs）。

    只做本地目录操作，0 次 Jev API。不可写时给出友好提示而不崩溃。
    """
    try:
        paths.ensure_runtime_dirs()
    except paths.DataDirError as exc:
        st.error(str(exc))
    except OSError as exc:
        st.error(f"无法创建本机数据目录：{type(exc).__name__}")


def get_cache() -> Cache:
    """本地缓存：路径由 paths 统一管理。

    portable 模式下是 ``data/cache.sqlite3``（跟随 SignalLens 文件夹）；
    开发模式下仍然是 ``.jev_cache/cache.db``。
    """
    if "cache" not in st.session_state:
        st.session_state["cache"] = Cache(paths.cache_db_path())
    return st.session_state["cache"]


def run_analysis(messages: list[dict], only_failed: bool = False) -> None:
    """对 TA 的消息逐条分析（含缓存）。失败项不中断，写入 error 字段。"""
    st.session_state["run_error"] = None
    st.session_state["append_stats"] = None  # 分析已开始，追加横幅不再适用
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

    # 并发时每个 worker 线程独立创建 client：官方 SDK 未承诺线程安全，
    # 不让多个线程共享同一个 transport。
    def _client_factory():
        return create_client(api_key=api_key)

    with st.status("正在分析聊天…", expanded=True) as status:
        bar = st.progress(0.0)
        line = st.empty()

        def cb(done: int, total: int, info: dict | None = None) -> None:
            info = info or {}
            bar.progress(done / max(total, 1))
            misses = info.get("misses") or 0
            if misses:
                line.caption(
                    f"正在分析：{misses} 个未缓存消息·"
                    f"并发 {info.get('workers') or 1}·"
                    f"已完成 {info.get('done_misses') or 0} / {misses}"
                )
            else:
                line.caption(f"正在分析第 {done} / {total} 条 TA 消息")
            status.update(label=f"正在分析聊天… {done} / {total}")

        try:
            new_results = analyze_messages(
                client, messages, cache=cache, only_indices=only_indices,
                progress_cb=cb, client_factory=_client_factory,
            )
        finally:
            # 并发路径里的 worker client 由 analyzer 负责关闭；
            # 这个主 client（串行路径使用）由本次分析关闭。
            try:
                client.close()
            except Exception:
                pass
        bar.empty()
        line.empty()

        if only_failed and st.session_state["results"]:
            by_index = {e["index"]: e for e in st.session_state["results"]}
            by_index.update({e["index"]: e for e in new_results})
            st.session_state["results"] = [by_index[i] for i in sorted(by_index)]
        else:
            st.session_state["results"] = new_results

        with _Stage("analysis:stats"):
            st.session_state["stats"] = compute_conversation_stats(
                st.session_state["results"]
            )
        st.session_state["skipped_media"] = skipped_media_count(messages)
        bump_analysis_revision()   # 新的一整份结果 → 报告 memo 身份失效

        cached_n = sum(1 for e in st.session_state["results"] if e.get("cached"))
        failed_n = sum(1 for e in st.session_state["results"] if e.get("error"))
        # 完成摘要（本地统计，不含任何聊天内容）
        run_stats = LAST_RUN_STATS
        api_calls = run_stats.get("api_calls") or 0
        parts = [
            f"分析 {len(st.session_state['results'])} 条",
            f"缓存命中 {cached_n} 条",
            f"新请求 {api_calls} 条",
        ]
        if api_calls:
            wall = run_stats.get("api_wall_seconds") or 0.0
            avg = run_stats.get("avg_latency_ms")
            parts.append(f"耗时 {wall:.1f}s")
            if avg:
                parts.append(f"平均延迟 {avg:.0f}ms")
            if run_stats.get("rate_limit"):
                parts.append(f"429 {run_stats['rate_limit']} 次")
        parts.append(f"跳过媒体 {st.session_state['skipped_media']} 条")
        parts.append(f"失败 {failed_n} 条")
        status.update(
            label="✓ 分析完成 · " + " · ".join(parts),
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


def show_input_stage() -> tuple[str | None, list, str]:
    """① 粘贴聊天记录：文本 + 可选图片上传。

    返回 (待解析文本, 上传的图片文件列表, 模式)；未点击提交返回 (None, [], "")。
    模式:

    - ``replace``：解析并替换当前聊天；
    - ``append``：把这一段**追加**到当前聊天（微信一次复制的条数有限，
      分几次复制时用）。

    追加与去重全部在本机完成，**不会调用 Jev API**。
    """
    # 必须在 text_area 实例化之前清空（widget 创建后禁止再改它的 state）
    if st.session_state.get("pending_clear_input"):
        st.session_state["pending_clear_input"] = False
        st.session_state["chat_input"] = ""

    with st.container(border=True):
        st.markdown("#### ① 粘贴聊天记录")
        st.caption("支持微信 / QQ 等复制文本。图片、视频、动画表情等媒体占位符"
                   "不会被当作文本分析。")

        with st.form("input_form"):
            raw_text = st.text_area(
                "聊天文本",
                height=220,
                key="chat_input",
                label_visibility="collapsed",
                placeholder="每次粘贴请使用一种格式，例如：\n"
                            "① 我: 你刚才怎么一直没回我\n"
                            "② 22:31 我\n   你干嘛呢\n"
                            "③ 昵称A\n   2026年09月08日 0:09\n   消息内容",
            )
            uploaded = st.file_uploader(
                "添加图片（可选）——用于绑定文本中的 [图片] 占位符",
                type=["png", "jpg", "jpeg", "webp", "bmp", "gif"],
                accept_multiple_files=True,
                help="从微信保存或截图后拖入；仅存于本机内存，不发送给 Jev，"
                     "不写入报告。",
            )
            if st.session_state.get("messages"):
                st.caption("当前已有一段聊天；再次粘贴后用“解析并替换当前聊天”会整体替换。")
            st.caption("微信一次复制的条数有限：可以分几次复制，用“追加到当前聊天”"
                       "逐段拼接。追加与去重都在本机完成，不调用 Jev。")
            c1, c2 = st.columns(2)
            replaced = c1.form_submit_button("解析并替换当前聊天", type="primary")
            appended = c2.form_submit_button("追加到当前聊天")

        if uploaded:
            st.caption(f"已选择 {len(uploaded)} 张图片（仅本机内存，"
                       "不会发送给 Jev，也不写入报告）。")
            cols = st.columns(min(len(uploaded), 4))
            for i, f in enumerate(uploaded[:4]):
                with cols[i]:
                    st.image(f, width=110)

    if appended:
        return raw_text, list(uploaded or []), "append"
    if replaced:
        return raw_text, list(uploaded or []), "replace"
    return None, [], ""


def reset_media_state() -> None:
    """解析新聊天时清空媒体资产与绑定（图片属于上一次粘贴）。"""
    st.session_state["media_assets"] = []
    st.session_state["media_bindings"] = {}
    st.session_state["media_manual"] = {}


def _reset_chat_state() -> None:
    """清空当前聊天与结果（解析失败 / 替换聊天时使用）。"""
    st.session_state["messages"] = None
    st.session_state["analysis_messages"] = None
    st.session_state["run_error"] = None
    st.session_state["raw_text"] = ""
    st.session_state["raw_chunks"] = []
    st.session_state["append_stats"] = None
    st.session_state["input_notice"] = None
    st.session_state["analysis_state"] = "idle"
    st.session_state["pending_target"] = None
    clear_analysis_results()
    st.session_state["sel_me"] = "（未指定）"
    st.session_state["sel_ta"] = "（未指定）"
    st.session_state["applied_me"] = None
    st.session_state["applied_ta"] = None
    # 时间线 / 顺序状态：换聊天必须全部作废
    st.session_state["timeline_info"] = None
    st.session_state["order_signature"] = None
    st.session_state["media_bindings_dropped"] = []
    st.session_state["media_manual_dropped"] = []
    st.session_state["order_confirmed"] = False
    st.session_state["preview_page"] = 1


def set_input_notice(level: str, text: str) -> None:
    """记下一条要跨 rerun 显示的提示（成功路径会立即 rerun，直接渲染会丢失）。"""
    st.session_state["input_notice"] = {"level": level, "text": text}


def finish_input_action() -> None:
    """成功处理输入后：清空输入框并立即干净重渲染。

    清空通过 ``pending_clear_input`` 在**下一次 rerun 创建 widget 之前**
    生效——这是 Streamlit 状态模型允许修改 widget 值的唯一时机，因此不碰
    DOM、不用 JS，也不会在 widget 实例化后非法改写它的 state。
    """
    st.session_state["pending_clear_input"] = True
    st.rerun()


def _warn_media_errors(errors) -> None:
    """图片上传错误改成提示（成功路径会 rerun，直接渲染会丢失）。"""
    for err in errors:
        set_input_notice("warning", err)


def _tag_chunk(messages: list[dict], chunk_idx: int) -> list[dict]:
    """给一条片段解析出的消息打上本地 chunk 序号（仅用于时间线同刻歧义判定）。

    ``_chunk_idx`` 是纯本地 metadata：analyzer.build_state 的出站白名单只
    放行 speaker/text/time，merge fingerprint 也不含它，因此绝不进入
    Jev state / 缓存 key / 报告。
    """
    for m in messages:
        m["_chunk_idx"] = chunk_idx
    return messages


def rebuild_messages_from_chunks(my_name: str | None,
                                 them_name: str | None) -> list[dict]:
    """用**显式传入**的昵称映射，从所有已追加的片段重新解析并本地合并。

    纯本地操作（解析 + 合并去重），不调用 Jev API。

    注意：昵称必须由调用方显式传入，不能在函数内部读 ``applied_me`` /
    ``applied_ta`` —— 调用点（“应用昵称映射并重新解析”）是在把新选择写入
    session_state **之前**调用本函数的，若在内部读取只会拿到上一轮 / 空的
    映射，导致所有消息都变成 unknown。
    """
    merged: list[dict] = []
    for idx, chunk in enumerate(st.session_state.get("raw_chunks") or []):
        if not chunk or not chunk.strip():
            continue
        parsed = mask_messages(parse_chat(chunk, my_name, them_name))
        parsed = _tag_chunk(parsed, idx)
        if not merged:
            merged = parsed
        else:
            merged = merge_messages(merged, parsed).messages
    return merged

def handle_replace_chunk(text: str, uploaded: list) -> None:
    """解析并替换当前聊天（本地解析 + 本地脱敏，0 Jev API）。"""
    try:
        parsed = parse_chat(text)
    except ParseError as exc:
        st.error(str(exc))
        _reset_chat_state()
        reset_media_state()
        return

    _reset_chat_state()
    st.session_state["raw_text"] = text
    st.session_state["raw_chunks"] = [text]
    set_messages(_tag_chunk(mask_messages(parsed), 0))
    st.session_state["skipped_media"] = 0
    assets, errors = assets_from_uploader(uploaded)
    st.session_state["media_assets"] = dedupe_assets(assets)
    _warn_media_errors(errors)
    apply_media_bindings(st.session_state["messages"])
    # 只有成功才清空输入框（失败时用户原样保留可修正）
    finish_input_action()


def handle_append_chunk(text: str, uploaded: list) -> None:
    """把新复制的片段追加到当前聊天：解析 + 脱敏 + 本地合并去重（0 Jev API）。"""
    st.session_state["run_error"] = None
    if not text or not text.strip():
        st.warning("请先粘贴要追加的聊天片段。")
        return

    try:
        parsed = parse_chat(
            text,
            st.session_state.get("applied_me"),
            st.session_state.get("applied_ta"),
        )
    except ParseError as exc:
        st.error(f"这一段无法解析：{exc}")
        return

    existing = st.session_state.get("messages") or []
    before_participants = set(detect_participants(existing))
    incoming = mask_messages(parsed)
    # 追加片段打上本地 chunk 序号：时间线据此判定“跨片段同刻”歧义
    incoming = _tag_chunk(
        incoming, len(st.session_state.get("raw_chunks") or []))
    result = merge_messages(existing, incoming)

    chunks = list(st.session_state.get("raw_chunks") or [])
    chunks.append(text)
    st.session_state["raw_chunks"] = chunks
    st.session_state["raw_text"] = "\n\n".join(chunks)
    # 走唯一入口：时间线排序 + 绑定迁移 + 顺序变化时失效旧结果
    set_messages(result.messages)
    st.session_state["append_stats"] = result

    # 身份映射：参与者集合不变 → 保留；出现新参与者 → 要求重新确认
    had_mapping = bool(
        st.session_state.get("applied_me") or st.session_state.get("applied_ta")
    )
    new_participants = sorted(
        set(detect_participants(st.session_state["messages"])) - before_participants
    )
    if had_mapping and new_participants:
        st.session_state["applied_me"] = None
        st.session_state["applied_ta"] = None
        st.session_state["sel_me"] = "（未指定）"
        st.session_state["sel_ta"] = "（未指定）"
        # 映射已清空：显式传 None，按“未知发言人”重建（不猜身份）
        set_messages(rebuild_messages_from_chunks(None, None))
        set_input_notice(
            "warning",
            "追加的片段里出现新的参与者：" + "、".join(new_participants)
            + "。请重新确认谁是“我”、谁是“TA”（不会自动把第三方归为 TA）。",
        )

    # 媒体资产：保留已上传图片，并入本次新上传的（仍只在本机内存）
    assets, errors = assets_from_uploader(uploaded)
    st.session_state["media_assets"] = dedupe_assets(
        list(st.session_state.get("media_assets") or []) + assets
    )
    _warn_media_errors(errors)
    apply_media_bindings(st.session_state["messages"])
    # 追加成功 → 清空输入框，用户可直接 Ctrl+V 下一段
    finish_input_action()


def show_first_run_setup() -> bool:
    """首次运行的 API Key 配置页（无 key 时显示）。

    返回 True 表示“已经配置好，可以继续”。

    安全约束：

    - 使用 password widget，页面不回显完整 key；
    - key 只写入 ``data/settings.env``（开发模式为仓库 ``.env``）；
    - **不写日志、不进报告 / 缓存 / Clipboard Probe**。
    """
    if settings_store.has_api_key():
        return True

    st.markdown("### SignalLens 首次设置")
    st.caption(
        "需要一个 TypeSafe API Key 才能开始分析。Key 仅用于直接向 TypeSafe API "
        "鉴权，不会写入聊天内容、分析缓存、报告或日志；选择保存时仅写入本机。"
    )
    settings_path = paths.settings_env_path()
    st.caption(f"保存位置：{settings_path}")

    with st.form("first_run_api_key"):
        key = st.text_input(
            "TypeSafe API Key",
            type="password",
            label_visibility="collapsed",
            placeholder="tsk_...",
        )
        remember = st.checkbox(
            f"保存到当前 SignalLens 的 data 文件夹（{settings_path.name}）",
            value=True,
        )
        submitted = st.form_submit_button("保存并继续", type="primary")
        if submitted:
            if not key.strip():
                st.error("API Key 不能为空。")
                return False
            if not remember:
                # 不落盘：只对当前进程生效（重启后需重新输入）
                os.environ[settings_store.API_KEY_ENV] = key.strip()
                st.warning("未保存到文件：重启 SignalLens 后需重新输入。")
                st.rerun()
            try:
                settings_store.save_api_key(key)
            except (ValueError, paths.DataDirError) as exc:
                st.error(str(exc))
                return False
            st.success("已保存。")
            st.rerun()
    return False


def show_api_key_settings() -> None:
    """侧边栏高级：TypeSafe API 设置（状态 / 修改 / 清除）。

    不显示完整 key；清除只删配置，不删聊天、缓存或其它数据。
    """
    summary = settings_store.settings_summary()
    if summary["configured"]:
        st.caption("TypeSafe API Key：已配置")
    else:
        st.caption("TypeSafe API Key：未配置（分析之前需先填写）")
    st.caption(f"配置位置：{summary['settings_path']}")

    # 上一轮保存 / 清除的反馈（rerun 会丢弃本轮元素，因此走 session_state）
    notice = st.session_state.pop("api_key_notice", None)
    if notice == "updated":
        st.toast("API Key 已更新。")
    elif notice == "cleared":
        st.toast("已清除本地 API Key。")

    with st.form("api_key_settings"):
        new_key = st.text_input(
            "修改 API Key",
            type="password",
            label_visibility="collapsed",
            placeholder="留空并点“保存”只是查看状态",
        )
        saved = st.form_submit_button("保存新 Key")
        if saved:
            if not new_key.strip():
                st.error("请输入新的 API Key，或使用下方的“清除”。")
            else:
                try:
                    settings_store.save_api_key(new_key)
                except (ValueError, paths.DataDirError) as exc:
                    st.error(str(exc))
                else:
                    st.session_state["api_key_notice"] = "updated"
                    st.rerun()

    if st.button("清除本地 API Key", key="clear_api_key"):
        settings_store.clear_api_key()
        st.session_state["api_key_notice"] = "cleared"
        st.rerun()


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


def set_messages(new_messages: list[dict], *, source: str = "import") -> None:
    """消息列表唯一入口：时间线排序 + 媒体绑定迁移 + 失效旧状态。

    为什么必须走这里：合并去重后的列表是“复制顺序”，用户从最新往更早
    追加时会变成逆序 —— Context Builder 会把更晚的消息当成更早消息的
    上下文（未来泄漏）。因此任何消息赋值都要先排成可靠时间线。

    - 排序改变 order_signature → 旧分析结果与依赖 index 的状态全部失效；
    - 媒体绑定按 fingerprint 迁移：无法唯一对应的绑定被丢弃，要求用户
      重新确认，绝不允许图片错配到另一条消息；
    - 纯本地操作，不调用 Jev API。
    """
    old_messages = st.session_state.get("messages") or []
    old_bindings = dict(st.session_state.get("media_bindings") or {})
    old_manual = dict(st.session_state.get("media_manual") or {})
    timeline = sort_messages(
        new_messages,
        multi_chunk=len(st.session_state.get("raw_chunks") or []) > 1,
    )
    st.session_state["messages"] = timeline.messages
    st.session_state["timeline_info"] = timeline

    new_bindings, dropped = migrate_bindings(
        old_messages, timeline.messages, old_bindings)
    st.session_state["media_bindings"] = new_bindings
    if dropped:
        st.session_state["media_bindings_dropped"] = sorted(dropped)

    # 手动绑定同样按 fingerprint 迁移；无法唯一对应的一律清空并要求
    # 重新匹配（绝不允许旧下标指向另一条消息）
    new_manual, manual_dropped = migrate_bindings(
        old_messages, timeline.messages, old_manual)
    st.session_state["media_manual"] = new_manual
    if manual_dropped:
        st.session_state["media_manual_dropped"] = sorted(manual_dropped)

    previous_sig = st.session_state.get("order_signature")
    new_sig = order_signature(timeline.messages)
    st.session_state["order_signature"] = new_sig
    if previous_sig is not None and previous_sig != new_sig:
        # 顺序变了（不只是追加）：旧结果的下标全部失效
        st.session_state["analysis_messages"] = None
        st.session_state["analysis_state"] = "idle"
        clear_analysis_results()
        # 顺序歧义的确认也失效：新导入/新歧义必须重新显式确认，
        # 绝不让上一次勾选放行新的不确定记录
        st.session_state["order_confirmed"] = False
    if timeline.order_changed and source == "import":
        set_input_notice(
            "info",
            "已按时间重新排序（最早在前）："
            + timeline.summary_text(),
        )


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

    # 最近一次“追加片段”的本地统计（纯本机处理，未调用 Jev）
    appended = st.session_state.get("append_stats")
    if appended:
        st.success(
            f"已追加片段：本次 {appended.chunk_size} 条 · "
            f"检测重复 {appended.duplicates} 条 · "
            f"新增 {appended.added} 条 · "
            f"当前总消息 {appended.total} 条（全部本机处理，未调用 Jev）"
        )

    # 输入处理的提示（替换 / 追加成功后 rerun，提示需跨 rerun 显示一次）
    notice = st.session_state.get("input_notice")
    if notice:
        st.session_state["input_notice"] = None
        level = notice.get("level")
        text = notice.get("text") or ""
        if level == "error":
            st.error(text)
        elif level == "info":
            st.info(text)
        else:
            st.warning(text)

    if st.session_state.get("analysis_state") == "interrupted":
        st.info("上次分析被中断（可能点击了 Stop、刷新或热重载）。可重新开始；"
                "已缓存成功的消息不会重复请求。")

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
                    me_name = None if sel_me == "（未指定）" else sel_me
                    ta_name = None if sel_ta == "（未指定）" else sel_ta
                    try:
                        # 昵称必须显式传入：此时 session_state 里的 applied_* 还是旧值
                        merged = rebuild_messages_from_chunks(me_name, ta_name)
                    except ParseError as exc:
                        st.error(str(exc))
                    else:
                        st.session_state["applied_me"] = (
                            None if sel_me == "（未指定）" else sel_me
                        )
                        st.session_state["applied_ta"] = (
                            None if sel_ta == "（未指定）" else sel_ta
                        )
                        # 走唯一入口：从全部原始片段重建后仍保持时间排序，
                        # 而不是恢复到导入顺序；顺序变化自动失效旧结果
                        set_messages(merged)
                        apply_media_bindings(st.session_state["messages"])
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
                clear_analysis_results()
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

        # ---- 时间校验摘要（预览区域）----
        tlinfo = st.session_state.get("timeline_info")
        if tlinfo is not None:
            st.caption(
                f"⏱ 时间校验：{tlinfo.summary_text()}"
                + (" · 已按时间重排（最早在前）" if tlinfo.order_changed else "")
            )
        dropped = st.session_state.get("media_bindings_dropped") or []
        if dropped:
            st.warning(
                f"重新排序后 {len(dropped)} 个图片绑定无法唯一对应，已失效，"
                "请在上方重新确认绑定（不会把图片错配到别的消息）。"
            )
        manual_dropped = st.session_state.get("media_manual_dropped") or []
        if manual_dropped:
            st.warning(
                f"重新排序后 {len(manual_dropped)} 个**手动**匹配的图片无法唯一"
                "对应到原消息，已清空，请在“图片绑定”里重新匹配。"
            )

        # ---- 预览表：最近 15 / 最早 15 / 分页全部（只影响展示）----
        mode = st.radio(
            "预览方式",
            options=["recent", "earliest", "all"],
            format_func=lambda v: {"recent": "最近 15 条",
                                   "earliest": "最早 15 条",
                                   "all": "分页浏览全部"}[v],
            index={"recent": 0, "earliest": 1, "all": 2}.get(
                st.session_state.get("preview_mode") or "recent", 0),
            horizontal=True,
            key="preview_mode_radio",
        )
        st.session_state["preview_mode"] = mode
        if mode != "all":
            window, total, _pages = preview_page(messages, mode=mode, limit=15)
        else:
            page = min(max(1, st.session_state.get("preview_page") or 1),
                       10 ** 9)
            window, total, pages = preview_page(
                messages, mode="all", page=page, page_size=40)
            page = min(max(1, page), pages)
            st.session_state["preview_page"] = page
            cols = st.columns([1, 1, 3])
            with cols[0]:
                if st.button("← 上一页", key="prev_page"):
                    st.session_state["preview_page"] = max(1, page - 1)
                    st.rerun()
            with cols[1]:
                if st.button("下一页 →", key="next_page"):
                    st.session_state["preview_page"] = min(pages, page + 1)
                    st.rerun()
            with cols[2]:
                st.caption(f"第 {page} / {pages} 页 · 每页 40 条 · "
                           f"本页 {page_time_range(window)}")
        # 两种模式都按时间升序阅读；编号与媒体绑定使用真实全局 index
        window_messages = [m for _, m in window]
        st.dataframe(
            preview_rows(
                window_messages,
                limit=len(window_messages),
                bindings=st.session_state.get("media_bindings") or {},
                start=window[0][0] if window else 0,
            ),
            use_container_width=True, hide_index=True,
        )
        st.caption(
            f"共 {total} 条（预览按时间升序阅读；预览只影响展示，"
            "不改变分析列表、不调用 Jev）"
            "。预览内容已本地脱敏；unknown = 无法确定发言人（未根据内容猜测）。"
        )

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

        # 时间不完整 / 同刻顺序无法确定 → 分析前必须显式确认
        order_ok = True
        if tlinfo is not None and tlinfo.requires_order_confirm:
            st.warning(
                "有消息的时间不完整、格式非法或同刻顺序无法自动确定（"
                + tlinfo.summary_text() + "）。这些消息不会被猜测日期后插入"
                "时间线，而是按各自可确定的顺序排在完整时间消息之后。"
                "“无时间”也包含无法解析的时间格式（如不存在的日期、25:61 之类）"
                "——请回到上一步修正或排除这些片段；"
                "如果它们实际发生在中途，相关上下文判断可能不可靠。"
            )
            order_ok = st.checkbox(
                "我确认：上述时间异常的消息按当前顺序参与分析",
                value=bool(st.session_state.get("order_confirmed")),
                key="order_confirm_checkbox",
            )
            st.session_state["order_confirmed"] = order_ok

        can_run = (them_n > 0 and (unknown_n == 0 or ignore_unknown)
                   and order_ok)
        ta_text_n = sum(
            1 for m in messages
            if m["speaker"] == "them" and m.get("content_type") != "media"
        )
        st.caption(
            f"将分析 {ta_text_n} 条 TA 文本消息 · "
            f"跳过 {skipped_media_count(messages)} 条 TA 非文本媒体 · "
            "缓存命中不会重复请求 API"
        )
        if st.button("开始 Jev 分析", type="primary",
                     disabled=(not can_run)
                     or st.session_state.get("analysis_state") == "running"):
            target = (
                [m for m in messages if m["speaker"] in ("me", "them")]
                if (unknown_n and ignore_unknown)
                else messages
            )
            st.session_state["analysis_messages"] = target
            # 两段式：先 pending + rerun，下一轮再真正执行分析。
            # 这样一个新 rerun 里看到的 "running" 只可能是上一轮被中断。
            st.session_state["pending_target"] = target
            st.session_state["analysis_state"] = "pending"
            st.session_state["result_view"] = "概览"
            st.session_state["msg_page"] = 0
            st.rerun()

    # 上一轮点击排下的分析：在本轮渲染结束后同步执行
    # （状态框出现在按钮附近；完成后本轮继续渲染结果，脚本随即结束）
    run_pending_analysis()


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
            run_analysis_guarded(st.session_state["analysis_messages"],
                                 only_failed=True)
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


# 全部消息视图每页条数：分页渲染，避免一次创建上百个 expander / progress。
# 切页只读 session_state 里已有的 results，0 次 Jev API。
MESSAGES_PER_PAGE = 25


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
    mode = st.session_state.get("msg_filter_mode", "全部消息")
    # 过滤条件变化时回到第 1 页（避免停在一个越界页）
    if st.session_state.get("msg_filter_last") != mode:
        st.session_state["msg_filter_last"] = mode
        st.session_state["msg_page"] = 0

    visible = filter_entries(
        results,
        mode=mode,
        include_media=st.session_state.get("show_media_events", False),
        messages=st.session_state.get("analysis_messages") or [],
    )
    if not visible:
        st.caption("当前过滤条件下没有可显示的消息。")
        return

    # 分页：每次只渲染一页详细卡片，不一次创建几百个 expander / progress
    pages = max(1, -(-len(visible) // MESSAGES_PER_PAGE))
    page = max(0, min(int(st.session_state.get("msg_page") or 0), pages - 1))
    st.session_state["msg_page"] = page
    st.caption(
        f"TA 文本消息 {stats['analyzed']} · 有效关系消息 {stats['effective_messages']} · "
        f"跳过 TA 媒体 {skipped} · 分析失败 {stats['failed']} · "
        f"第 {page + 1} / {pages} 页（每页 {MESSAGES_PER_PAGE} 条）"
    )

    start = page * MESSAGES_PER_PAGE
    for entry in visible[start:start + MESSAGES_PER_PAGE]:
        if entry.get("media_event"):
            show_media_event_card(entry)
        else:
            show_message_card(entry)

    if pages > 1:
        c1, c2, c3 = st.columns(3)
        if c1.button("◀ 上一页", key="msg_prev_page",
                     disabled=(page == 0), use_container_width=True):
            st.session_state["msg_page"] = page - 1
            st.rerun()
        c2.markdown(f"第 {page + 1} / {pages} 页")
        if c3.button("下一页 ▶", key="msg_next_page",
                     disabled=(page >= pages - 1), use_container_width=True):
            st.session_state["msg_page"] = page + 1
            st.rerun()


def report_memo_key(revision: int, include_text: bool) -> tuple:
    """报告 memo 的身份：**分析版本号 + 是否包含原文**（纯函数）。

    统计量（条数 / analyzed / skipped_media / failed / overall …）不能作为
    身份：两份不同聊天完全可能这些数字都一样，却必须各自生成自己的报告。
    ``analysis_revision`` 在每次产生 / 替换 / 清除一整份分析结果时递增，
    因此同一份结果重复进入报告仍命中 memo，新聊天一定重新生成。
    不接触聊天正文，也不影响 Jev cache key / scoring / parser。
    """
    return (int(revision), bool(include_text))


def _build_or_reuse_reports(results: list[dict], stats: dict,
                            include_text: bool) -> tuple[str, str]:
    """报告只在进入“报告”视图时构建；结果未变则复用 session_state 里的成品。

    导出仍是纯本地数据处理（0 次 Jev API）；memo 只是避免在同一视图里反复
    rerun 时重复拼接 Markdown / JSON。
    """
    skipped = st.session_state.get("skipped_media", 0)
    cache_key = report_memo_key(
        st.session_state.get("analysis_revision") or 0, include_text
    )
    cache = st.session_state.get("report_cache") or {}
    if cache.get("key") == cache_key:
        return cache["md"], cache["json"]

    md = build_markdown_report(results, stats, include_text=include_text,
                               skipped_media=skipped)
    payload_json = json.dumps(
        build_json_report(results, stats, include_text=include_text,
                          skipped_media=skipped),
        ensure_ascii=False, indent=2,
    )
    st.session_state["report_cache"] = {"key": cache_key, "md": md,
                                        "json": payload_json}
    return md, payload_json


def show_report_tab(results: list[dict], stats: dict) -> None:
    st.markdown("#### 报告导出")
    st.caption("导出完全基于本次已完成的本地分析结果，不会发起任何 TypeSafe API 请求。")
    st.text_area("分析摘要（可复制）", value=build_summary_text(results, stats),
                 height=150)
    include_text = st.checkbox(
        "报告中包含本地脱敏后的聊天文本",
        value=False,
        help="关闭时导出匿名报告：只保留统计与消息编号，不含聊天文本。",
    )
    md, payload_json = _build_or_reuse_reports(results, stats, include_text)
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("下载 Markdown", data=md,
                           file_name=report_filename("md"), mime="text/markdown")
    with c2:
        st.download_button("下载 JSON", data=payload_json,
                           file_name=report_filename("json"), mime="application/json")
    st.caption("需要 PDF？使用浏览器 Ctrl+P → 另存为 PDF。")


RESULT_VIEWS = ["概览", "关键消息", "全部消息", "报告"]


def show_results(results: list[dict], stats: dict) -> None:
    """结果视图导航：**server-side lazy**。

    Streamlit 的 ``st.tabs`` 并不是懒执行——所有 tab 的 Python 都会在每次
    rerun 里跑一遍，于是用户停在“概览”时，隐藏的“全部消息”（上百个
    expander + progress）和“报告”（完整 Markdown/JSON）仍在后台把脚本拖住，
    表现为“结果已经出现，但右上角 Stop 迟迟不消失、控件一直灰”。

    这里改用 ``st.segmented_control`` + if/elif：每次 rerun 只渲染当前视图。
    切换视图只是读 session_state 里已有的 results / stats，0 次 Jev API。
    """
    view = st.segmented_control(
        "结果视图", RESULT_VIEWS, key="result_view",
        default="概览", label_visibility="collapsed",
    )
    if view == "关键消息":
        with _Stage("results:key_messages"):
            show_key_messages_tab(results)
    elif view == "全部消息":
        with _Stage("results:all_messages"):
            show_all_messages_tab(results, stats)
    elif view == "报告":
        with _Stage("results:report"):
            show_report_tab(results, stats)
    else:
        with _Stage("results:overview"):
            show_overview_tab(results, stats)


def dump_timings_safe() -> None:
    """诊断输出（默认关闭）。"""
    try:
        dump_timings()
    except Exception:  # 诊断绝不影响主流程
        pass


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
        if st.session_state.get("analysis_state") in ("running", "interrupted"):
            st.divider()
            st.caption("界面状态")
            if st.button("恢复界面状态", use_container_width=True,
                         key="reset_ui_state"):
                st.session_state["analysis_state"] = "idle"
                st.session_state["pending_target"] = None
                st.rerun()
        st.divider()
        st.caption("高级")
        with st.expander("▶ TypeSafe API 设置"):
            show_api_key_settings()
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

    Probe 只在用户显式点“打开 Clipboard Probe”后才创建组件——折叠 expander
    并不代表其中的 Python 不执行。本阶段 Probe 由人工微信实测，不应影响主
    流程的任何一次 rerun；其内部也没有 while / sleep / 轮询 / 重试握手。
    """
    if not st.session_state.get("probe_open"):
        if st.button("打开 Clipboard Probe", key="probe_open_btn",
                     use_container_width=True):
            st.session_state["probe_open"] = True
            st.rerun()
        st.caption("默认不加载，不影响主流程；需要诊断浏览器剪贴板实际能"
                   "拿到什么格式时再打开。")
        return

    if st.button("收起 Clipboard Probe", key="probe_close_btn",
                 use_container_width=True):
        st.session_state["probe_open"] = False
        st.rerun()
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
    # 新 rerun 已开始 → 上一轮的同步分析不可能还在跑：遗留的 running
    # 只可能是 Stop / 刷新 / 热重载 / 异常，恢复成 interrupted（UI 解锁）
    recover_analysis_state()
    st.title("SignalLens")
    st.markdown("**聊天互动信号分析** · Powered by TypeSafe Jev")

    # 首次使用：还没有 API Key 时只显示友好的配置页
    # （不白屏、不 traceback；保存后 st.rerun() 即可继续，无需重启 EXE）
    ensure_runtime_dirs()

    if not show_first_run_setup():
        st.divider()
        st.caption("配置好 API Key 之后就可以开始粘贴微信聊天记录。")
        st.caption(f"本机数据位置：{paths.data_dir()}")
        return

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

    # ① 输入（解析并替换 / 追加片段，全部本地完成）
    with _Stage("input"):
        submitted_text, uploaded, input_mode = show_input_stage()
    if submitted_text is not None:
        with _Stage("input:handle"):
            if input_mode == "append":
                handle_append_chunk(submitted_text, uploaded)
            else:
                handle_replace_chunk(submitted_text, uploaded)

    # ② 确认解析
    messages = st.session_state.get("messages")
    if messages:
        with _Stage("confirm"):
            show_confirm_stage(messages)

    # ③④ 结果
    results = st.session_state.get("results")
    stats = st.session_state.get("stats")
    if results and stats:
        # 分析已完成 → 步骤推进到 ④（同一次 run 内更新占位符）
        steps_slot.markdown(steps_markdown(4))
        show_results(results, stats)

    with _Stage("sidebar"):
        show_sidebar()

    dump_timings_safe()


if __name__ == "__main__":
    main()
