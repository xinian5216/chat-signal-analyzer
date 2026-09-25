#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SignalLens 浏览器回归（全虚构数据，0 次真实 Jev 调用）。

设计原则（对应交接文档踩坑记录）：

1. **真按钮选择器**：「重新选择身份」等一律 ``get_by_role("button")``，
   不使用 ``label:has-text``（历史脚本在这里超时过）。
2. **等状态，不睡死**：翻页后等「第 X / 10 页」文本 / 锚点 nonce / 组件
   重挂载等真实条件；唯一的固定等待只用于让 glide-data-grid 重渲染稳定。
3. **不 force click**：点击被 glide 重渲染吞掉时，先等渲染 settle 再重试
   （最多 3 次），仍失败则记 FAIL 并附完整诊断——绝不改判断条件迁就产品。
4. **0 真实 API 断言在网络层**：监控页面所有请求，任何非 localhost 请求
   即 FAIL；缓存全预制（seed_cache.py），分析阶段必须 0 外部请求。
5. **子进程输出写文件，不用 PIPE**：Windows 管道缓冲区很小，应用写满后
   整个进程阻塞在 write 上，曾伪装成「分析卡死 240s」（见 README）。
6. **检查项命名用 ASCII**：Windows 控制台对中文不友好；断言细节可以中文
   （写进 JSON 报告）。

用法（在仓库根、venv 里）::

    .venv\\Scripts\\python -X utf8 scripts\\browser_acceptance\\run_acceptance.py
    .venv\\Scripts\\python -X utf8 scripts\\browser_acceptance\\run_acceptance.py --headful --probe-dom

依赖：``pip install playwright`` + ``python -m playwright install chromium``
（见 requirements-dev.txt / 本目录 README.md）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from fictional_data import ME, TA, TA_ALIAS_2, build_chat  # noqa: E402
import seed_cache  # noqa: E402

from playwright.sync_api import Error as PWError  # noqa: E402
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FAKE_API_KEY = "tsk_local_fixture_key_not_real_000"

# ---------------------------------------------------------------------------
# 检查记录
# ---------------------------------------------------------------------------

_RESULTS: list[dict] = []
_DIAG: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return bool(ok)


def diag(msg: str) -> None:
    _DIAG.append(msg)
    print(f"  [diag] {msg}", flush=True)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


# ---------------------------------------------------------------------------
# 应用启动
# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch_app(port: int, data_dir: Path, headless: bool = True,
               slowmo: int = 0, log_path: Path | None = None):
    env = dict(os.environ)
    env["SIGNALLENS_DATA_DIR"] = str(data_dir)
    env["TYPESAFE_API_KEY"] = FAKE_API_KEY
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 串行：浏览器回归不关心并发；也避免 worker 客户端噪音
    env["SIGNALLENS_JEV_CONCURRENCY"] = "1"
    cmd = [
        sys.executable, "-m", "streamlit", "run", "app.py",
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--server.fileWatcherType", "none",
    ]
    # **绝不能用 subprocess.PIPE 且只在结束时读取**：Windows 管道缓冲
    # 区很小，应用写满后整个进程会阻塞在 write 上，表现为「分析卡死」
    # （曾因此误判 240s）。stdout/stderr 一律写文件，随时可查。
    if log_path is None:
        log_path = Path(tempfile.gettempdir()) / "sl_acceptance_streamlit.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, cwd=str(REPO), env=env,
                            stdout=log_file, stderr=subprocess.STDOUT)
    proc._sl_log_file = log_file          # 供 finally 关闭
    return proc


def stop_app(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    log_file = getattr(proc, "_sl_log_file", None)
    if log_file:
        try:
            log_file.close()
        except Exception:
            pass


def wait_ready(page, port: int, timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/"
    last = ""
    while time.time() < deadline:
        try:
            import urllib.request
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/_stcore/healthz",
                    timeout=2) as r:
                if r.status == 200:
                    break
        except Exception as exc:      # 刚启动还没起来
            last = repr(exc)
        time.sleep(0.4)
    else:
        raise RuntimeError(f"streamlit 健康检查超时: {last}")
    page.goto(url, wait_until="domcontentloaded")
    page.get_by_text("① 粘贴聊天记录", exact=False).first.wait_for(
        state="visible", timeout=60000)


# ---------------------------------------------------------------------------
# 状态读取（页面 JS）
# ---------------------------------------------------------------------------

_JS_STATE = r"""
(anchorId) => {
  const anchor = anchorId ? document.getElementById(anchorId) : null;
  const df = document.querySelector('[data-testid="stDataFrame"]');
  const scroller = df ? df.querySelector('.dvn-scroller') : null;
  const main = document.querySelector('section.stMain');
  const running = document.querySelector('[data-testid="stStatusWidget"]');
  const body = document.querySelector('.stAppViewContainer') || document.body;
  return {
    main_top: main ? Math.round(main.scrollTop) : null,
    inner_top: scroller ? Math.round(scroller.scrollTop) : null,
    inner_max: scroller ? Math.round(scroller.scrollHeight - scroller.clientHeight) : null,
    df_top: df ? Math.round(df.getBoundingClientRect().top) : null,
    df_height: df ? Math.round(df.getBoundingClientRect().height) : null,
    df_mark: (df && df.__probeMark) || null,
    rows: document.querySelectorAll('[data-testid="stDataFrame"] tbody tr').length,
    nonce: anchor ? anchor.getAttribute('data-scroll-nonce') : null,
    running: running ? (running.textContent || '').slice(0, 40) : '',
    viewport_h: window.innerHeight,
    app_scroll: body ? Math.round(body.scrollTop) : null,
  };
}
"""


def js_state(page, anchor_id: str = "") -> dict:
    return page.evaluate(_JS_STATE, anchor_id or "")


def set_inner_scroll_bottom(page) -> int | None:
    """把表格内部滚动条推到最底部，返回推之前的 scrollTop（诊断用）。"""
    return page.evaluate(r"""
() => {
  const df = document.querySelector('[data-testid="stDataFrame"]');
  const scroller = df ? df.querySelector('.dvn-scroller') : null;
  if (!scroller) return null;
  scroller.scrollTop = scroller.scrollHeight;
  return Math.round(scroller.scrollTop);
}
""")


def mark_df(page) -> None:
    page.evaluate(r"""
() => {
  const df = document.querySelector('[data-testid="stDataFrame"]');
  if (df) df.__probeMark = String(Math.random()).slice(2, 10);
}
""")


_START_SAMPLER = r"""
() => {
  window.__sampler = null;
  const main = document.querySelector('section.stMain');
  const samples = [];
  const t0 = performance.now();
  let last = main ? main.scrollTop : 0;
  let moves = 0;
  const positions = new Set();
  function tick(now) {
    const v = main ? main.scrollTop : 0;
    if (v !== last) { moves += 1; }
    last = v;
    positions.add(Math.round(v));
    samples.push(Math.round(v));
    if (now - t0 < 1400) { requestAnimationFrame(tick); }
    else {
      const first = samples.length ? samples[0] : null;
      const end = samples.length ? samples[samples.length - 1] : null;
      const tail = samples.slice(-15);
      const tailStable = tail.length > 0 && tail.every(v => v === tail[0]);
      window.__sampler = {frames: samples.length, moves: moves,
                          first: first, last: end, tail_stable: tailStable,
                          positions: Array.from(positions).sort((a, b) => a - b)};
    }
  }
  requestAnimationFrame(tick);
}
"""


def start_sampler(page) -> None:
    """在点击**之前**启动 1.4s 的 rAF 滚动采样（捕获锚点定位的整段过程）。"""
    page.evaluate(_START_SAMPLER)


def collect_sampler(page, timeout_ms: int = 6000) -> dict | None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        val = page.evaluate("() => window.__sampler")
        if val:
            return val
        time.sleep(0.1)
    return None


def sample_scroll_trajectory(page, seconds: float = 1.2) -> dict:
    """requestAnimationFrame 采样主滚动容器位置，用于「是否只动一次」。"""
    return page.evaluate(
        """seconds => new Promise(resolve => {
            const main = document.querySelector('section.stMain');
            const samples = [];
            const t0 = performance.now();
            let last = main ? main.scrollTop : 0;
            let moves = 0;
            const positions = new Set();
            function tick(now) {
                const v = main ? main.scrollTop : 0;
                samples.push(Math.round(v));
                if (v !== last) { moves += 1; }
                last = v;
                positions.add(Math.round(v));
                if (now - t0 < seconds * 1000) {
                    requestAnimationFrame(tick);
                } else {
                    const first = samples.length ? samples[0] : null;
                    const end = samples.length ? samples[samples.length - 1] : null;
                    const tail = samples.slice(-15);
                    const tailStable = tail.length > 0 && tail.every(v => v === tail[0]);
                    resolve({frames: samples.length, moves, first, last,
                             tail_stable: tailStable,
                             positions: [...positions].sort((a,b)=>a-b)});
                }
            }
            requestAnimationFrame(tick);
        })""", seconds)


def _no_jump(traj: dict | None, tol_px: int = 2) -> bool:
    """翻页后页面**停在原地**：净位移为 0 且末段稳定。

    判据不是「一帧都不动」：浏览器回归观察到连续翻页时偶发 42px / 2 帧的
    瞬时回弹（glide-data-grid 换数据瞬间的渲染 churn，净位移为 0，肉眼
    不可感知）。交接文档记录的真实缺陷——「先明显位移再定位」的双阶段
    滚动——表现为**净位移非 0** 或**多段滚动后停到别处**，由
    net==0 + tail_stable 抓住。identity 选择等确定性场景仍用严格
    drift==0（见 phase_import）。
    """
    if not traj:
        return False
    first, end = traj.get("first"), traj.get("last")
    if first is None or end is None:
        return False
    if abs(end - first) > tol_px:
        return False
    return bool(traj.get("tail_stable"))


# ---------------------------------------------------------------------------
# 通用 UI 操作
# ---------------------------------------------------------------------------


def rerender_settled(page, timeout_ms: int = 8000) -> bool:
    """等 stStatusWidget 的 Running 指示消失（稳态）。返回是否曾看到运行中。"""
    deadline = time.time() + timeout_ms / 1000
    saw_running = False
    while time.time() < deadline:
        try:
            txt = page.evaluate(
                "() => (document.querySelector('[data-testid=\"stStatusWidget\"]')"
                " || {}).textContent || ''")
        except Exception:
            return saw_running
        if "Running" in txt:
            saw_running = True
        elif saw_running:
            return True
        time.sleep(0.1)
    return saw_running


def wait_text(page, text: str, timeout_ms: int = 15000,
              state: str = "visible"):
    return page.get_by_text(text, exact=False).first.wait_for(
        state=state, timeout=timeout_ms)


def wait_dataframe(page, timeout_ms: int = 20000) -> bool:
    """等 glide-data-grid 真正挂载（``.dvn-scroller`` 出现）。"""
    try:
        page.locator("[data-testid='stDataFrame'] .dvn-scroller").first\
            .wait_for(state="attached", timeout=timeout_ms)
        return True
    except PWTimeout:
        return False


def wait_preview_page(page, page_no: int, pages: int = 10,
                      timeout_ms: int = 15000) -> bool:
    """等预览导航的居中页码文本（顶部/底部任一处刷新即成）。"""
    try:
        wait_text(page, f"第 {page_no} / {pages} 页", timeout_ms)
        wait_dataframe(page, 8000)
        return True
    except PWTimeout:
        return False


def wait_anchor_nonce(page, anchor_id: str, min_nonce: int = 1,
                      timeout_ms: int = 10000) -> str | None:
    """等翻页锚点渲染并带上 data-scroll-nonce（JS 挂载后由锚点脚本写入）。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            n = page.evaluate(
                """id => { const a = document.getElementById(id);
                            return a ? a.getAttribute('data-scroll-nonce') : null; }""",
                anchor_id)
        except Exception:
            return None
        if n is not None and n != "" and int(n) >= min_nonce:
            return n
        time.sleep(0.1)
    return None


def click_and_expect_preview_page(page, position: str, direction: str,
                                  from_page: int, to_page: int,
                                  pages: int = 10) -> dict:
    """点预览翻页钮并等页码变化；吞点击/竞态时 settle 后重试（不 force）。

    返回诊断 dict。页码始终不变 -> 记为翻页失败（测试侧时序 / 产品问题
    的判定交给上层检查）。点击前先启动 rAF 采样，捕获锚点定位全过程。
    """
    attempts = []
    name = "下一页 ▶" if direction == "next" else "◀ 上一页"
    btn = page.get_by_role("button", name=name)
    target = btn.last if position == "bottom" else btn.first
    for attempt in range(1, 4):
        # 先把按钮滚到视口中央（测试侧行为，不计入产品滚动账；同时避开
        # sticky header 对边缘位置元素的遮挡）
        try:
            handle = target.element_handle(timeout=5000)
            if handle is not None:
                handle.evaluate(
                    "el => el.scrollIntoView({block: 'center'})")
        except PWTimeout:
            pass
        before = js_state(page, "preview-page-anchor")
        start_sampler(page)
        try:
            target.click(timeout=5000)
        except PWTimeout as exc:
            attempts.append({"attempt": attempt, "click_error": str(exc)[:120]})
            rerender_settled(page, 4000)
            continue
        if wait_preview_page(page, to_page, pages, timeout_ms=6000):
            nonce = wait_anchor_nonce(page, "preview-page-anchor", 1, 5000)
            after = js_state(page, "preview-page-anchor")
            traj = collect_sampler(page)
            attempts.append({"attempt": attempt, "ok": True,
                             "before": before, "after": after,
                             "nonce": nonce, "traj": traj})
            return {"changed": True, "attempts": attempts,
                    "before": before, "after": after, "traj": traj}
        # 没翻上：等渲染 settle（glide 重渲染瞬间的点击会被丢弃）再重试
        rerender_settled(page, 5000)
        collect_sampler(page, 1500)
        attempts.append({"attempt": attempt, "ok": False,
                         "before": before,
                         "after": js_state(page, "preview-page-anchor")})
    after = js_state(page, "preview-page-anchor")
    return {"changed": False, "attempts": attempts,
            "before": attempts[0].get("before") if attempts else None,
            "after": after, "traj": None}


def pick_radio(page, label: str) -> None:
    page.get_by_text(label, exact=True).first.click()


def pick_select_option(page, selectbox_label: str, option: str) -> None:
    """在指定 label 的 selectbox 里选一项（表单内控件不触发 rerun）。

    Streamlit 1.64 的 selectbox 是 react-aria ComboBox：**必须点两次**
    输入框（第一次只聚焦，第二次才展开 ``stSelectboxVirtualDropdown``），
    chevron 按钮与合成键盘事件都不可靠（均已实测）。
    """
    box = page.locator("[data-testid='stSelectbox']",
                       has_text=selectbox_label).first
    inp = box.locator("input[role='combobox']").first
    # 先把目标滚进视口再记录基线：Playwright 点击前的自动滚动是测试侧
    # 行为，不能算进「产品是否跳滚动」的账。
    inp.scroll_into_view_if_needed()
    inp.click(timeout=8000)            # 聚焦
    inp.click(timeout=8000)            # 展开
    dd = page.locator("[data-testid='stSelectboxVirtualDropdown']").first
    dd.wait_for(state="visible", timeout=8000)
    opt = dd.locator("[role='option']", has_text=option).last
    try:
        opt.click(timeout=8000, no_wait_after=False)
    except PWTimeout:
        # Streamlit 1.64 虚拟下拉行偶发「元素不稳定 / detached」抖动
        # （既不是产品缺陷也不改变断言）：退回 dispatch click，仍然是
        # react-aria 的正式选项事件。
        opt.dispatch_event("click")


# ---------------------------------------------------------------------------
# 各阶段
# ---------------------------------------------------------------------------


def phase_import(page) -> None:
    section("A. import 400 fictional messages + identity mapping")
    text = build_chat()
    assert len(text.split("\n\n")) == 400
    page.locator("textarea").first.fill(text)
    ui_click(page, page.get_by_role("button", name="解析并替换当前聊天"))
    wait_text(page, "② 确认聊天双方", 30000)
    check("confirm_stage_rendered", True, "400 fictional wechat blocks")

    # 身份表单：选择不触发整页 rerun（form 化的目的），滚动不应移动。
    # 先把表单滚进视口再记基线——Playwright 点击前的自动滚动是测试侧
    # 行为，不能算进「产品是否跳滚动」的账。
    page.get_by_text("已识别聊天参与者").first.scroll_into_view_if_needed()
    page.evaluate("""() => { window.__t0 = document.querySelector('section.stMain').scrollTop; }""")
    pick_select_option(page, "我是：", ME)
    pick_select_option(page, "TA 是：", TA)
    time.sleep(0.8)
    drift = page.evaluate(
        "() => (document.querySelector('section.stMain').scrollTop - window.__t0)")
    check("identity_pick_no_scroll", drift == 0, f"main scrollTop drift={drift}")

    # 标点昵称端到端回归（parser 真实缺陷）：身份表单里选中的「我」必须
    # 逐字节保留全角感叹号——parse 丢了标点或把昵称挡掉，这里就会失败。
    me_shown = page.locator("[data-testid='stSelectbox']",
                            has_text="我是：").first \
        .locator("input[role='combobox']").first.input_value()
    check("punctuation_nickname_parsed_verbatim",
          me_shown == ME and "！" in me_shown,
          f"我是 combobox shows {me_shown!r}")
    ui_click(page, page.get_by_role("button", name="应用昵称映射并重新解析"))
    wait_text(page, "身份映射完成", 30000)
    check("identity_applied", True, f"me={ME} ta={TA}")
    counts = page.get_by_text("unknown：0 条", exact=False).first
    check("identity_no_unknown", counts.is_visible(), "unknown=0")


def phase_preview_scroll(page) -> None:
    section("B. preview pagination + scroll (incl inner_scroll_then_next_first_row)")
    pick_radio(page, "分页浏览全部")
    wait_preview_page(page, 1, 10, 30000)
    wait_dataframe(page, 20000)
    st = js_state(page, "preview-page-anchor")
    check("preview_starts_at_1_of_10", True,
          f"rows={st['rows']} inner_max={st['inner_max']}")

    # --- 顶部翻页 1->2 ---
    mark_df(page)
    res = click_and_expect_preview_page(page, "top", "next", 1, 2)
    if not check("top_turn_changes_page", res["changed"],
                 json.dumps(res["attempts"][-1], ensure_ascii=False)[:220]
                 if res["attempts"] else "no attempts"):
        return
    traj = res["traj"] or {}
    after = res["after"]
    check("top_turn_no_page_scroll", _no_jump(traj),
          f"moves={traj.get('moves')} positions={traj.get('positions')}")
    check("top_turn_single_movement", (traj.get("frames") or 0) > 10,
          f"frames={traj.get('frames')}")
    check("top_turn_inner_scroll_reset", (after["inner_top"] or 0) <= 4,
          f"inner_top={after['inner_top']}")
    check("top_turn_first_row_visible",
          after["df_top"] is not None and after["df_top"] >= -8
          and (after["inner_top"] or 0) <= 4,
          f"df_top={after['df_top']} inner_top={after['inner_top']}")
    check("top_turn_table_remounted", after["df_mark"] is None,
          f"df_mark={after['df_mark']}")
    check("top_turn_nonce_advanced",
          after["nonce"] is not None and int(after["nonce"]) >= 1,
          f"nonce={after['nonce']}")

    # --- 连续翻页 2->3->4->5：位置纹丝不动 + 内部滚动归零 ---
    stable_ok, reset_ok, diag_rows = True, True, []
    for cur, nxt in ((2, 3), (3, 4), (4, 5)):
        mark_df(page)
        res = click_and_expect_preview_page(page, "top", "next", cur, nxt)
        if not res["changed"]:
            stable_ok = False
            diag_rows.append(f"{cur}->{nxt} click ineffective: "
                             + json.dumps(res["attempts"][-1],
                                          ensure_ascii=False)[:160])
            break
        traj = res["traj"] or {}
        after = res["after"]
        before = res["before"]
        stable_ok = stable_ok and _no_jump(traj)
        reset_ok = reset_ok and (after["inner_top"] or 0) <= 4
        diag_rows.append(f"{cur}->{nxt}: moves={traj.get('moves')} "
                         f"positions={traj.get('positions')} "
                         f"inner={before['inner_top']}->{after['inner_top']} "
                         f"main={before['main_top']}->{after['main_top']} "
                         f"nonce={before['nonce']}->{after['nonce']} "
                         f"mark={'remounted' if after['df_mark'] is None else 'same'}")
    check("top_consecutive_3_5_stable", stable_ok, "; ".join(diag_rows))
    check("top_consecutive_3_5_inner_reset", reset_ok, "; ".join(diag_rows))

    # --- 底部翻页 5->6：一次定位 + 第一行可见 ---
    mark_df(page)
    before = js_state(page, "preview-page-anchor")
    res = click_and_expect_preview_page(page, "bottom", "next", 5, 6)
    if check("bottom_turn_lands_once", res["changed"],
             f"main_top={before['main_top']} -> "
             f"{res['after']['main_top'] if res['after'] else '?'}"):
        traj = res["traj"] or {}
        after = res["after"]
        check("bottom_turn_single_movement",
              len(traj.get("positions") or []) <= 2
              and (traj.get("frames") or 0) > 10,
              f"moves={traj.get('moves')} positions={traj.get('positions')} "
              f"(prescroll already aligned -> no-op is OK)")
        check("bottom_turn_inner_scroll_reset", (after["inner_top"] or 0) <= 4,
              f"inner_top={after['inner_top']}")
        check("bottom_turn_first_row_visible",
              after["df_top"] is not None and after["df_top"] >= -8,
              f"df_top={after['df_top']}")

    # --- P0 复现：连续翻页后内部滚到底 -> 点底部下一页 ---
    diag("P0 repro: after consecutive turns, inner scroll to bottom "
         "-> bottom next page")
    for cur, nxt in ((6, 7), (7, 8)):
        res = click_and_expect_preview_page(page, "top", "next", cur, nxt)
        if not res["changed"]:
            diag(f"warm-up turn {cur}->{nxt} ineffective; repro state incomplete")
    pushed = set_inner_scroll_bottom(page)
    before = js_state(page, "preview-page-anchor")
    diag(f"before: pushed_inner_from={pushed} "
         f"state={json.dumps(before, ensure_ascii=False)}")
    res = click_and_expect_preview_page(page, "bottom", "next", 8, 9)
    after = res["after"] or js_state(page, "preview-page-anchor")
    diag(f"after : {json.dumps(after, ensure_ascii=False)}")
    diag(f"attempts: {json.dumps(res['attempts'], ensure_ascii=False)}")
    check("p0_click_changes_page", res["changed"],
          f"attempts={len(res['attempts'])}")
    check("p0_inner_scroll_reset", (after["inner_top"] or 0) <= 4,
          f"inner_top {before['inner_top']} -> {after['inner_top']}")
    check("p0_first_row_visible",
          after["df_top"] is not None and after["df_top"] >= -8
          and (after["inner_top"] or 0) <= 4,
          f"df_top={after['df_top']} inner_top={after['inner_top']} "
          f"main_top={after['main_top']}")
    check("p0_nonce_advanced",
          after["nonce"] is not None
          and (before["nonce"] is None
               or int(after["nonce"]) > int(before["nonce"])),
          f"nonce {before['nonce']} -> {after['nonce']}")


def click_centered(page, locator, timeout_ms: int = 15000) -> None:
    """把元素滚到视口**中央**再点击（避开 sticky header/toolbar 的遮挡）。

    Playwright 默认的 scroll_into_view_if_needed 会把元素送到视口边缘，
    固定在顶部的 stHeader/stToolbar 会挡住它 → 点击被拦截重试直到超时。
    """
    handle = locator.first.element_handle(timeout=timeout_ms)
    if handle is not None:
        handle.evaluate(
            "el => el.scrollIntoView({block: 'center', inline: 'center'})")
    locator.first.click(timeout=timeout_ms)


def ui_click(page, locator, timeout_ms: int = 15000) -> None:
    """统一的按钮 / 文本点击入口：先居中滚动再点击。"""
    click_centered(page, locator, timeout_ms)


def ui_fill(page, locator, value: str, timeout_ms: int = 15000) -> None:
    """统一的文本输入入口：先居中滚动再 fill（避开 header 遮挡）。"""
    handle = locator.first.element_handle(timeout=timeout_ms)
    if handle is not None:
        handle.evaluate("el => el.scrollIntoView({block: 'center'})")
    locator.first.fill(value, timeout=timeout_ms)


def ui_check(page, label_locator, timeout_ms: int = 15000) -> None:
    """切换 checkbox 状态：点击它的 **label 文本**（真实用户的操作方式）。

    Streamlit 的 checkbox 输入框被 ``<label>`` 覆盖，直接点输入框会被
    Playwright 判定「label 拦截指针事件」；点 label 文本本身就是用户
    在浏览器里的真实点击路径，不需要 force。
    """
    ui_click(page, label_locator, timeout_ms)


def read_friend_evidence(data_dir: Path) -> list[dict]:
    """直接读好友档案数据库里的证据行（验证「编辑 -> 保存 -> 落库」）。"""
    import sqlite3
    db = data_dir / "friend_history.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT msg_index, stance, note, snippet FROM history_evidence"
        ).fetchall()
    finally:
        conn.close()
    return [{"msg_index": r[0], "stance": r[1], "note": r[2],
             "snippet": r[3]} for r in rows]


def phase_analysis(page) -> None:
    section("C. run analysis (preseeded cache, assert 0 external requests)")
    btn = page.get_by_role("button", name="开始 Jev 分析")
    handle = btn.first.element_handle(timeout=15000)
    if handle is not None:
        handle.evaluate("el => el.scrollIntoView({block: 'center'})")
    started = time.time()
    btn.first.click()
    # 结果阶段的稳定标记：概览视图的「温暖」指标（确认阶段不渲染它）
    try:
        page.get_by_text("温暖", exact=True).first.wait_for(
            state="visible", timeout=240000)
        ok = True
    except PWTimeout:
        ok = False
    check("analysis_completes", ok, f"wait={time.time()-started:.1f}s")
    page.get_by_role("radio", name="全部消息").first.click()
    # 「每页 25 条」只在全部消息视图出现（确认阶段的「TA 文本消息」 caption
    # 会造成竞态误匹配，不能用它当就绪信号）
    wait_text(page, "每页 25 条", 30000)
    check("analysis_zero_failures",
          page.get_by_text("分析失败 0", exact=False).first.is_visible(),
          "182 targets all cache hits")


def phase_messages_pagination(page) -> None:
    section("D. results all-messages pagination + plain rerun no jump")
    wait_text(page, "每页 25 条", 20000)
    m = re.search(r"第 1 / (\d+) 页（每页 25 条）", page.content())
    pages = int(m.group(1)) if m else 0
    check("messages_pages_count", pages >= 7, f"pages={pages}")

    # 普通 rerun（显示媒体事件 checkbox）：内容不变（纯文本聊天），
    # 不应触发滚动锚点、页面不应跳动。先测这个再翻页。
    nonce_before = js_state(page, "msg-page-anchor")["nonce"]
    ui_check(page, page.get_by_text("显示媒体事件", exact=True))
    traj = sample_scroll_trajectory(page, 0.9)
    st = js_state(page, "msg-page-anchor")
    check("plain_rerun_no_jump", _no_jump(traj),
          f"moves={traj['moves']} positions={traj['positions']}")
    check("plain_rerun_no_scroll_anchor",
          st["nonce"] == nonce_before,
          f"anchor nonce {nonce_before} -> {st['nonce']}")
    ui_check(page, page.get_by_text("显示媒体事件", exact=True))  # 再点 = 取消

    before = js_state(page, "msg-page-anchor")
    btn = page.get_by_role("button", name="下一页 ▶")
    # 先做测试侧居中滚动，再启动采样，最后点击（采样只覆盖产品行为）
    handle = btn.last.element_handle(timeout=15000)
    if handle is not None:
        handle.evaluate(
            "el => el.scrollIntoView({block: 'center', inline: 'center'})")
    start_sampler(page)
    btn.last.click()
    try:
        wait_text(page, "第 2 / ", 15000)
        changed = True
    except PWTimeout:
        changed = False
    check("messages_next_changes_page", changed, f"pages={pages}")
    traj = collect_sampler(page)
    after = js_state(page, "msg-page-anchor")
    check("messages_anchor_rendered",
          after["nonce"] is not None
          and (before["nonce"] is None
               or int(after["nonce"]) > int(before["nonce"])),
          f"nonce {before['nonce']} -> {after['nonce']}")
    check("messages_turn_no_oscillation",
          len(traj.get("positions") or []) <= 2,
          f"positions={traj.get('positions')}")


def phase_friend_panel(page, data_dir: Path) -> None:
    section("E. friend profiles / evidence editor / cross-friend isolation / delete")
    ui_click(page, page.get_by_role("radio", name="长期观察"))
    alias_input = page.get_by_role("textbox",
                                   name="用微信昵称 / 备注 / 别名查找已有档案"
                                        "（只在本机查找）")
    ui_fill(page, alias_input, TA)
    ui_click(page, page.get_by_role("button", name="用这个名字新建档案"))
    wait_text(page, "已选择档案", 30000)
    check("friend_profile_created", True, f"profile={TA}")

    form = page.locator("[data-testid='stForm']",
                        has_text="保留匿名化证据片段").first
    form.scroll_into_view_if_needed()
    # 混合信号标签（P0 回归）：失败时 dump 候选 caption 便于定位
    mixed_visible = page.get_by_text("混合信号，需核对",
                                     exact=False).count() > 0
    if not mixed_visible:
        caps = form.locator("[data-testid='stCaptionContainer']")\
            .all_text_contents()
        diag("candidate captions: "
             + " || ".join(c.replace("\n", " ")[:60] for c in caps[:8]))
    check("evidence_editor_mixed_label", mixed_visible,
          "mixed stance candidate present")
    check("evidence_editor_no_duplicate_key",
          page.locator("[data-testid='stException']").count() == 0,
          "no StreamlitDuplicateElementKey")
    tas = form.locator("textarea")
    n_ta = tas.count()
    check("evidence_textareas", n_ta >= 5, f"count={n_ta}")
    # 勾选「保留证据」、改写第一条、删除第二条，然后保存（表单提交后
    # 编辑才进入 session_state / 档案库）
    # 注意：必须把点击限制在 label 元素上——get_by_text 会连包含全部
    # 候选的共享容器一起匹配，nth(1) 会错点到第 0 条的删除框。
    ui_click(page, form.locator("label", has_text="保留匿名化证据片段").first)
    if n_ta:
        tas.first.fill("PROFILE1-EDIT-rewritten fictional evidence")
        ui_click(page, form.locator("label", has_text="删除这条").nth(1))
        check("evidence_delete_toggled", True, "second candidate marked for delete")
    ui_click(page, form.get_by_role("button", name="保存至好友档案"))
    wait_text(page, "已保存为一条历史分析快照", 30000)
    check("saved_with_evidence", True, "submitted with edited evidence")

    # 落库校验：改写后的片段必须原样进入档案数据库，被勾删的候选不得出现
    evidence = read_friend_evidence(data_dir)
    snippets = [e["snippet"] for e in evidence]
    diag("db evidence: " + json.dumps(
        [{"idx": e["msg_index"], "stance": e["stance"],
          "snippet": e["snippet"][:24]} for e in evidence],
        ensure_ascii=False))
    check("evidence_edit_persisted_to_db",
          "PROFILE1-EDIT-rewritten fictional evidence" in snippets,
          f"db evidence rows={len(evidence)}")
    stances = {e["stance"] for e in evidence}
    check("evidence_mixed_stance_persisted", "mixed" in stances,
          f"stances={sorted(stances)}")

    # 换档案 -> profile2 的编辑器必须是全新作用域（不得带上 profile1 的改写）
    ui_click(page, page.get_by_role("button", name="换一个档案"))
    ui_fill(page, alias_input, TA_ALIAS_2)
    ui_click(page, page.get_by_role("button", name="用这个名字新建档案"))
    wait_text(page, "已选择档案", 30000)
    form2 = page.locator("[data-testid='stForm']",
                         has_text="保留匿名化证据片段").first
    form2.scroll_into_view_if_needed()
    first_val = form2.locator("textarea").first.input_value()
    check("cross_friend_state_isolated", "PROFILE1-EDIT" not in first_val,
          f"profile2 first textarea={first_val[:30]!r}")

    # profile2 首次保存：同一份分析已存到 profile1 -> 串档警告出现；
    # 未勾选确认直接提交必须被拒绝（提交后校验，不是禁用按钮——表单内
    # 控件的值提交前不到 Python，禁用按钮会造成真实浏览器死锁）
    submit = form2.get_by_role("button", name="保存至好友档案")
    check("cross_friend_confirmation_required",
          form2.locator("label", has_text="仍然保存").count() > 0,
          "saved_elsewhere warning shown")
    check("cross_friend_save_button_enabled", not submit.is_disabled(),
          "submit stays clickable; guard validates after submit")
    evidence_before = len(read_friend_evidence(data_dir))
    ui_click(page, submit)
    wait_text(page, "请勾选上面的确认框", 15000)
    check("cross_friend_unconfirmed_save_rejected", True,
          "error shown, nothing written")
    evidence_after = len(read_friend_evidence(data_dir))
    check("cross_friend_unconfirmed_save_no_write",
          evidence_after == evidence_before,
          f"evidence rows {evidence_before} -> {evidence_after}")
    ui_click(page, form2.locator("label", has_text="仍然保存").first)
    ui_click(page, form2.get_by_role("button", name="保存至好友档案"))
    wait_text(page, "已保存为一条历史分析快照", 30000)
    check("saved_to_second_profile", True)

    # profile1 的历史列表 + 删除档案（真实删除 + 界面回落）
    ui_click(page, page.get_by_role("button", name="换一个档案"))
    ui_fill(page, alias_input, TA)
    ui_click(page, page.get_by_role("button", name="查找档案"))
    wait_text(page, "找到", 20000)
    ui_click(page, page.get_by_role("button", name="确认使用这个档案"))
    wait_text(page, "已选择档案", 30000)
    wait_text(page, "该档案的历史分析", 10000)
    check("history_list_has_run", True, "one immutable run listed")
    ui_click(page, page.get_by_text("删除这个档案（不可恢复）"))
    ui_click(page, page.locator("label",
                                has_text="我确认要删除这个档案及其全部历史").first)
    ui_click(page, page.get_by_role("button", name="删除档案"))
    wait_text(page, "档案及其全部历史分析已从本机档案数据库删除", 30000)
    check("friend_profile_deleted", True)


def phase_history_panel(page) -> None:
    section("F. confirm-stage history: lookup / cancel / identity invalidation")
    # （E 阶段已保存过两次档案，其中 profile1 已删；用 profile2 的别名验证）
    ui_click(page, page.get_by_text("历史档案（可选：查看这位好友以前的分析）"))
    hin = page.get_by_role("textbox",
                           name="好友昵称 / 备注 / 别名（只在本机查找）")
    ui_fill(page, hin, TA_ALIAS_2)
    ui_click(page, page.get_by_role("button", name="查找历史档案"))
    wait_text(page, "找到档案", 20000)
    check("history_lookup_finds_profile", True, f"alias={TA_ALIAS_2}")
    wait_text(page, "正在查看档案", 20000)
    check("history_auto_view_single_match", True)
    ui_click(page, page.get_by_role("button", name="取消查看历史").last)
    wait_text(page, "已取消查看历史档案", 20000)
    check("history_cancel_view", True)
    check("history_view_cleared",
          page.get_by_text("正在查看档案", exact=False).count() == 0,
          "panel back to search state")

    # 重新查找并查看，然后重新选择身份 -> 历史视图与好友绑定必须失效
    ui_click(page, page.get_by_role("button", name="查找历史档案"))
    wait_text(page, "正在查看档案", 20000)
    ui_click(page, page.get_by_role("button", name="重新选择身份"))
    wait_text(page, "已识别聊天参与者", 30000)
    check("remap_button_is_real_button", True,
          "button:has-text selector works (old label: selector timed out)")
    check("friend_binding_cleared_notice",
          page.get_by_text("身份需要重新确认", exact=False).count() >= 1,
          "remap clears friend binding with notice")
    pick_select_option(page, "我是：", TA)
    pick_select_option(page, "TA 是：", ME)
    ui_click(page, page.get_by_role("button", name="应用昵称映射并重新解析"))
    wait_text(page, "身份映射完成", 30000)
    check("identity_swapped_applied", True, f"me={TA} ta={ME}")
    check("history_view_invalidated_after_identity_change",
          page.get_by_text("正在查看档案", exact=False).count() == 0,
          "old TA history view invalidated after identity switch")


def _pick_select_in(page, scope, selectbox_label: str, option: str) -> None:
    """在指定容器内的 selectbox 里选一项（候选表单与手动表单各有自己的
    「行为方向」下拉，必须限定容器，不能取页面上第一个）。

    展开下拉最多重试 3 次：react-aria ComboBox 的「聚焦 → 展开」两连击
    可能被一次异步 rerun 打断（元素被重建后第二次点击只是重新聚焦），
    此时补第三击；每次重试前等渲染 settle。选中优先真实 click，
    虚拟行不稳定（长面板深处）时退回 dispatch click——仍然是
    react-aria 的正式选项事件，不伪造页面状态。
    """
    # selectbox（表单外）改变值会触发 rerun：先等稳态再定位，避免拿到
    #  rerun 过程中被替换掉的元素（Element is not attached to the DOM）。
    rerender_settled(page, 6000)
    dd = page.locator("[data-testid='stSelectboxVirtualDropdown']").first
    opened = False
    for attempt in range(3):
        inp = scope.locator("[data-testid='stSelectbox']",
                            has_text=selectbox_label).first \
            .locator("input[role='combobox']").first
        try:
            inp.scroll_into_view_if_needed(timeout=4000)
        except PWError:
            rerender_settled(page, 4000)
            continue
        inp.click(timeout=8000)
        try:
            dd.wait_for(state="visible", timeout=2500)
            opened = True
            break
        except PWTimeout:
            rerender_settled(page, 3000)
    if not opened:
        raise AssertionError(f"selectbox {selectbox_label!r} dropdown "
                             "did not open after 3 attempts")
    opt = dd.locator("[role='option']", has_text=option).last
    try:
        opt.click(timeout=4000, no_wait_after=False)
    except PWTimeout:
        opt.dispatch_event("click")
    # 选中后同样可能触发 rerun：等稳态再交还控制权
    rerender_settled(page, 6000)


def _open_expander(page, fragment: str, index: int = 0):
    """展开页面上第 index 个包含指定文本的 stExpander（内容在 DOM 里但被
    折叠隐藏，必须先展开才能与其内部控件交互；不 force click）。"""
    expanders = page.locator("[data-testid='stExpander']",
                             has_text=fragment)
    target = expanders.nth(index)
    ui_click(page, target.locator("summary").first)
    return target


def _preview_then_click(page, scope_locator, button_name: str) -> None:
    """两阶段保存：先点同作用域的「查看最终预览」，等「最终预览」块出现，
    再点保存 / 排除按钮（预览未打开时保存按钮根本不渲染）。"""
    ui_click(page, scope_locator.get_by_role("button", name="查看最终预览"))
    scope_locator.get_by_text("最终预览", exact=False).first.wait_for(
        state="visible", timeout=20000)
    ui_click(page, scope_locator.get_by_role("button", name=button_name))


def read_behavior_events(data_dir: Path) -> list[dict]:
    """直接读行为事件表（验证「确认 / 排除 / 手动添加 -> 落库」）。"""
    import sqlite3
    db = data_dir / "friend_history.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT dimension, behavior_type, status, source_kind,"
            " stance, notes, snippet, fingerprints_json, event_identity"
            " FROM behavior_events"
        ).fetchall()
    finally:
        conn.close()
    return [{"dimension": r[0], "behavior_type": r[1], "status": r[2],
             "source_kind": r[3], "stance": r[4], "notes": r[5],
             "snippet": r[6], "fingerprints": json.loads(r[7] or "[]"),
             "event_identity": r[8]}
            for r in rows]


def read_run_fingerprints(data_dir: Path) -> set[str]:
    """档案里全部历史快照的消息指纹（验证历史候选指纹来自原 run）。"""
    import sqlite3
    db = data_dir / "friend_history.db"
    if not db.exists():
        return set()
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT fingerprint FROM history_messages").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def phase_behavior_panel(page, data_dir: Path) -> None:
    section("G. longitudinal behavior events: candidates -> manual review -> report")
    # （E 阶段结束后 profile1 已删、当前无选中档案；用 profile2 的别名找回）
    alias_input = page.get_by_role("textbox",
                                   name="用微信昵称 / 备注 / 别名查找已有档案"
                                        "（只在本机查找）")
    ui_fill(page, alias_input, TA_ALIAS_2)
    ui_click(page, page.get_by_role("button", name="查找档案"))
    wait_text(page, "找到", 20000)
    ui_click(page, page.get_by_role("button", name="确认使用这个档案"))
    wait_text(page, "长期行为观察", 30000)
    check("behavior_panel_visible", True, "panel rendered under 长期观察")

    wait_text(page, "待人工核对的候选", 20000)
    check("behavior_no_false_truncation_warning",
          page.get_by_text("部分候选未显示", exact=False).count() == 0,
          "normal scale shows no truncation warning")
    content = page.content()
    m = re.search(r"第 1 / (\d+) 批（每批最多 5 条）", content)
    pages = int(m.group(1)) if m else 0
    check("behavior_candidates_paginated", pages >= 2,
          f"batch pages={pages} (400-msg chat + history run)")

    # 翻到下一批再翻回来（分页只读本地候选，0 API）
    ui_click(page, page.get_by_role("button", name="下一批 ▶"))
    wait_text(page, "第 2 / ", 20000)
    check("behavior_next_batch", True, "paged to batch 2")
    ui_click(page, page.get_by_role("button", name="◀ 上一批"))
    wait_text(page, "第 1 / ", 20000)
    check("behavior_prev_batch", True, "back to batch 1")

    # 历史来源候选必须标注"无聊天正文"（不得凭旧总结编造上下文）。
    # 历史候选追加在候选列表末尾：先逐批翻到最后一页再检查。
    for step in range(2, pages + 1):
        ui_click(page, page.get_by_role("button", name="下一批 ▶"))
        wait_text(page, f"第 {step} / ", 30000)
    check("behavior_history_candidate_no_text",
          page.get_by_text("历史来源，无聊天正文", exact=False).count() > 0,
          f"history-sourced candidates flagged context-missing (last "
          f"batch of {pages})")
    # 编号归属提示：历史候选的消息编号属于原 run，不是当前导入
    check("behavior_history_index_ownership",
          page.get_by_text("原分析快照", exact=False).count() > 0,
          "history candidate shows run-relative numbering note")

    # --- 在最后一批确认历史候选（Phase 2A.1：不得与当前聊天混）---
    hist_cand = _open_expander(page, "第一步：核对并修正")
    note_field = hist_cand.get_by_role("textbox", name="你的说明（可选）")
    note_field.fill("历史候选：我记得当时的上下文")
    _preview_then_click(page, hist_cand, "确认这条事件")
    wait_text(page, "已确认事件", 30000)
    rows = read_behavior_events(data_dir)
    history_rows = [e for e in rows if e["source_kind"] == "history"]
    check("behavior_history_confirm_persisted",
          len(history_rows) == 1
          and history_rows[0]["status"] == "confirmed",
          f"history rows={len(history_rows)}")
    run_fps = read_run_fingerprints(data_dir)
    check("behavior_history_fingerprint_from_run",
          history_rows
          and set(history_rows[0]["fingerprints"]) <= run_fps,
          "history event fingerprints come from the original run "
          "(not current-chat indices)")

    for step in range(pages - 1, 0, -1):
        ui_click(page, page.get_by_role("button", name="◀ 上一批"))
        wait_text(page, f"第 {step} / ", 30000)

    # --- 确认第一条当前导入候选（带可识别备注，供跨好友隔离检查）---
    cand = _open_expander(page, "第一步：核对并修正")
    ui_click(page, cand.locator("label", has_text="支持性").first)
    # 交互 1：勾选「保留一段脱敏片段」→ 编辑框立即出现（不提交表单）
    vis_before = page.evaluate(
        """() => Array.from(document.querySelectorAll('textarea'))
            .filter(t => t.offsetWidth > 0).length""")
    ui_click(page, cand.locator("label", has_text="保留一段脱敏片段").first)
    page.wait_for_timeout(1500)
    vis_after = page.evaluate(
        """() => Array.from(document.querySelectorAll('textarea'))
            .filter(t => t.offsetWidth > 0).length""")
    check("behavior_keep_shows_textarea_immediately",
          vis_after == vis_before + 1,
          f"visible textareas {vis_before} -> {vis_after} without submit")
    # 交互 3（两阶段门控）：预览未打开前保存按钮不渲染
    check("behavior_two_stage_gate",
          cand.get_by_role("button", name="确认这条事件").count() == 0
          and cand.get_by_role("button", name="排除这条").count() == 0,
          "save buttons hidden until final preview is opened")
    # 交互 3：输入虚构 PII → 查看最终预览给出脱敏结果（不提交表单）
    snippet = cand.get_by_role("textbox", name="脱敏片段（可编辑）")
    # 两阶段第二步：输入虚构 PII 后点「查看最终预览」（点击前的失焦恰好
    # 提交 textarea 值——该 Streamlit 版本击键不 rerun、失焦才提交），
    # 预览块给出最终将写入档案的脱敏 + 截断内容
    snippet.press_sequentially("电话 13812345678 邮箱 lin@example.com",
                               timeout=30000)
    ui_click(page, cand.get_by_role("button", name="查看最终预览"))
    deadline = time.time() + 15
    preview_ok = False
    while time.time() < deadline:
        if (cand.get_by_text("<PHONE>", exact=False).count() > 0
                and cand.get_by_text("<EMAIL>", exact=False).count() > 0
                and cand.get_by_text("最终预览", exact=False).count() > 0):
            preview_ok = True
            break
        time.sleep(0.5)
    check("behavior_final_preview_shows_masked_content", preview_ok,
          "final preview shows masked+truncated content before save")
    cand.get_by_role("textbox", name="你的说明（可选）").fill(
        "PROFILE2-NOTE-cross-friend")
    # 第二步最终预览汇总在场
    check("behavior_final_summary_visible",
          cand.get_by_text("最终预览", exact=False).count() > 0,
          "two-stage final summary rendered before save")
    ui_click(page, cand.get_by_role("button", name="确认这条事件"))
    wait_text(page, "已确认事件", 30000)
    check("behavior_confirm_notice", True, "confirmed candidate notice shown")

    # 排除下一条候选（排除记录保留）；排除前验证方向→类型即时联动
    cand2 = _open_expander(page, "第一步：核对并修正")
    _pick_select_in(page, cand2, "行为方向", "尊重与边界")
    page.wait_for_timeout(1500)
    type_input = cand2.locator("input[role='combobox']").nth(1)
    type_input.click(timeout=8000)
    type_input.click(timeout=8000)
    dd = page.locator("[data-testid='stSelectboxVirtualDropdown']").first
    dd.wait_for(state="visible", timeout=8000)
    opts = [dd.locator("[role='option']").nth(i).text_content()
            for i in range(dd.locator("[role='option']").count())]
    respect_labels = {"不同意见时的回应", "明确拒绝后的反应",
                      "施压或贬低", "冲突修复"}
    check("behavior_dimension_updates_types_immediately",
          set(opts) == respect_labels,
          f"type options follow new dimension without submit: {opts}")
    page.keyboard.press("Escape")
    _preview_then_click(page, cand2, "排除这条")
    wait_text(page, "已排除该候选", 30000)
    check("behavior_exclude_notice", True, "rejected candidate notice shown")

    # 落库校验：2 确认（1 历史 + 1 规则）+ 1 排除
    rows = read_behavior_events(data_dir)
    statuses = sorted(e["status"] for e in rows)
    diag("db behavior events: " + json.dumps(
        [{k: e[k] for k in ("dimension", "behavior_type", "status",
                            "source_kind", "notes")} for e in rows],
        ensure_ascii=False)[:400])
    check("behavior_events_persisted",
          statuses == ["confirmed", "confirmed", "rejected"],
          f"rows={len(rows)} statuses={statuses}")
    check("behavior_note_persisted",
          any(e["notes"] == "PROFILE2-NOTE-cross-friend" for e in rows),
          "edited note written to db")
    rule_rows = [e for e in rows if e["source_kind"] == "rule"]
    check("behavior_candidate_snippet_masked",
          any("<PHONE>" in e["snippet"] and "13812345678" not in e["snippet"]
              for e in rule_rows),
          "candidate confirm path stores masked snippet")

    # 长期行为事件报告（本地聚合，无评分输出）
    wait_text(page, "长期行为事件报告", 20000)
    check("behavior_report_rendered", True, "report section after confirming")
    check("behavior_report_download_button",
          page.get_by_role("button",
                           name="下载长期行为事件报告（Markdown）").count() > 0,
          "markdown download available")
    check("behavior_report_no_score_text",
          page.get_by_text("尊重分：", exact=False).count() == 0
          and page.get_by_text("喜欢概率：", exact=False).count() == 0,
          "no 尊重分 / 喜欢概率 output anywhere on the page")

    # 手动添加事件（用户自己圈定范围；默认编号 1/2，不 fill——number_input
    # 的 fill 会触发异步 rerun 落在 combobox 两连击之间）
    manual = _open_expander(page, "手动添加一个行为事件")
    _pick_select_in(page, manual, "行为方向", "关心与回应性")
    check("behavior_manual_panel_stays_open",
          page.evaluate(
              """() => { let open = null;
                  document.querySelectorAll('details').forEach(d => {
                      const s = d.querySelector('summary');
                      if (s && s.textContent.includes('手动添加'))
                          open = d.open;
                  });
                  return open; }""") is True,
          "manual add panel stays open across selectbox rerun "
          "(regression guard for unkeyed st.expander collapse)")
    _pick_select_in(page, manual, "行为类型", "认真回应困难")
    ui_click(page, manual.locator("label", has_text="支持性").first)
    keep = manual.locator("label", has_text="保留一段脱敏片段").first
    ui_click(page, keep)
    snippet = manual.get_by_role("textbox", name="脱敏片段（可编辑）")
    # 虚构 PII：保存路径必须脱敏（store 层兜底 + UI 最终预览）
    snippet.fill("我叫林小满，电话 13812345678，邮箱 lin@example.com")
    _preview_then_click(page, manual, "添加事件")
    wait_text(page, "已手动添加事件", 30000)
    check("behavior_manual_add_notice", True, "manual event added")
    rows = read_behavior_events(data_dir)
    manual_rows = [e for e in rows if e["source_kind"] == "manual"]
    check("behavior_manual_add_persisted",
          len(manual_rows) == 1
          and manual_rows[0]["dimension"] == "care"
          and manual_rows[0]["behavior_type"] == "care_response",
          f"manual rows={len(manual_rows)}")
    check("behavior_manual_snippet_masked",
          manual_rows
          and "<PHONE>" in manual_rows[0]["snippet"]
          and "<EMAIL>" in manual_rows[0]["snippet"]
          and "13812345678" not in manual_rows[0]["snippet"]
          and "lin@example.com" not in manual_rows[0]["snippet"],
          f"manual snippet stored masked: "
          f"{manual_rows[0]['snippet'][:60] if manual_rows else 'NONE'}")

    # --- 跨好友切换：profile3（同一份聊天）不得继承 profile2 的备注 ---
    # 用另一个虚构称呼（安安）建档——同一份聊天 + 不同档案正是跨好友
    # 隔离要覆盖的场景；同时保持 F 阶段按「予安」查找时唯一匹配。
    ui_click(page, page.get_by_role("button", name="换一个档案"))
    ui_fill(page, alias_input, "安安")
    ui_click(page, page.get_by_role("button", name="用这个名字新建档案"))
    wait_text(page, "长期行为观察", 30000)
    cand3 = _open_expander(page, "第一步：核对并修正")
    note3 = cand3.get_by_role("textbox", name="你的说明（可选）")
    leaked = note3.input_value()
    check("behavior_cross_friend_state_isolated",
          leaked == "" and "PROFILE2-NOTE" not in leaked,
          f"profile3 note field value: {leaked!r}")
    _preview_then_click(page, cand3, "确认这条事件")
    wait_text(page, "已确认事件", 30000)
    rows = read_behavior_events(data_dir)
    profile2_notes = [e["notes"] for e in rows
                      if e["notes"] == "PROFILE2-NOTE-cross-friend"]
    check("behavior_cross_friend_separate_events",
          len(profile2_notes) == 1,
          f"profile2 note intact; total rows={len(rows)}")

    # 已确认事件可在面板里编辑 / 删除（先编辑保存，验证审计落库）
    expanders = page.locator("[data-testid='stExpander']")
    event_expander = None
    for i in range(expanders.count()):
        label = expanders.nth(i).locator("summary").first.text_content() or ""
        if "理解情绪" in label or "认真回应困难" in label:
            event_expander = expanders.nth(i)
            break
    if event_expander is not None:
        ui_click(page, event_expander.locator("summary").first)
        notes = event_expander.locator("textarea")
        if notes.count() >= 1:
            notes.first.fill("浏览器回归：改过的人工说明")
        _preview_then_click(page, event_expander, "保存修改")
        wait_text(page, "已更新该事件", 20000)
        check("behavior_event_edit_persisted",
              any("改过的人工说明" in (e["notes"] or "")
                  for e in read_behavior_events(data_dir)),
              "edited note written to db")
        ui_click(page, event_expander.get_by_role("button",
                                                  name="删除这个事件"))
        wait_text(page, "确认删除这个行为事件吗", 20000)
        ui_click(page, page.get_by_role("button", name="确认删除").last)
        wait_text(page, "已删除该行为事件", 20000)
        after = read_behavior_events(data_dir)
        check("behavior_event_deleted",
              all("改过的人工说明" not in (e["notes"] or "")
                  for e in after),
              f"rows after delete={len(after)}")


# ---------------------------------------------------------------------------
# 探针模式（首次在新环境跑时确认 DOM 结构）
# ---------------------------------------------------------------------------


def probe_dom(page) -> None:
    section("PROBE DOM")
    html = page.evaluate("""() => {
        const out = {};
        const df = document.querySelector('[data-testid="stDataFrame"]');
        if (df) out.dataframe = df.outerHTML.slice(0, 1200);
        const sb = document.querySelector('[data-testid="stSelectbox"]');
        if (sb) out.selectbox = sb.outerHTML.slice(0, 600);
        const rd = document.querySelector('[data-testid="stRadio"]');
        if (rd) out.radio = rd.outerHTML.slice(0, 600);
        const seg = document.querySelector('[role="radiogroup"]');
        if (seg) out.segmented = seg.outerHTML.slice(0, 600);
        const anchor = document.querySelector('[id$="-anchor"]');
        if (anchor) out.anchor = anchor.outerHTML;
        return out;
    }""")
    for k, v in html.items():
        print(f"--- {k} ---\n{v}\n", flush=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--slowmo", type=int, default=0)
    ap.add_argument("--probe-dom", action="store_true",
                    help="start, import, then dump key DOM and exit")
    ap.add_argument("--keep-data", action="store_true")
    ap.add_argument("--phases", default="import,preview,analysis,messages,"
                                        "friend,behavior,history")
    args = ap.parse_args()

    data_dir = Path(tempfile.gettempdir()) / "sl_acceptance_data"
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    info = seed_cache._seed(data_dir)
    diag(f"seed: {info}")

    port = free_port()
    proc = launch_app(port, data_dir)
    external_requests: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    result = 0
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful,
                                         slow_mo=args.slowmo or None)
            ctx = browser.new_context(viewport={"width": 1360, "height": 900})
            page = ctx.new_page()
            page.on("request", lambda r: external_requests.append(r.url)
                    if r.url.startswith("http")
                    and "127.0.0.1" not in r.url and "localhost" not in r.url
                    else None)
            page.on("console", lambda m: console_errors.append(m.text[:200])
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)[:200]))

            wait_ready(page, port)
            if args.probe_dom:
                phase_import(page)
                pick_radio(page, "分页浏览全部")
                wait_preview_page(page, 1, 10, 30000)
                probe_dom(page)
                click_and_expect_preview_page(page, "top", "next", 1, 2)
                probe_dom(page)
                browser.close()
                return 0

            phases = args.phases.split(",")
            if "import" in phases:
                phase_import(page)
            if "preview" in phases:
                phase_preview_scroll(page)
            if "analysis" in phases:
                phase_analysis(page)
            if "messages" in phases:
                phase_messages_pagination(page)
            if "friend" in phases:
                phase_friend_panel(page, data_dir)
            if "behavior" in phases:
                phase_behavior_panel(page, data_dir)
            if "history" in phases:
                phase_history_panel(page)
            browser.close()
    finally:
        stop_app(proc)

    section("Z. global assertions")
    check("zero_external_requests", not external_requests,
          f"external={external_requests[:3]}")
    check("no_page_errors", not page_errors, f"errors={page_errors[:3]}")
    real_console = [e for e in console_errors
                    if "favicon" not in e and "DevTools" not in e]
    check("no_console_errors", not real_console,
          f"errors={real_console[:3]}")

    failed = [r for r in _RESULTS if not r["ok"]]
    report = {"passed": len(_RESULTS) - len(failed),
              "failed": len(failed),
              "results": _RESULTS,
              "external_requests": external_requests,
              "page_errors": page_errors,
              "console_errors": real_console}
    rp = Path(tempfile.gettempdir()) / "sl_acceptance_report.json"
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print(f"\nTOTAL {len(_RESULTS)}  PASS {len(_RESULTS) - len(failed)}  "
          f"FAIL {len(failed)}", flush=True)
    if failed:
        print("FAILED: " + ", ".join(f["name"] for f in failed), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
