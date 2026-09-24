"""统一滚动锚点：只在明确的用户翻页操作后触发，用 nonce 保证每次都重新挂载。

设计要点：

- **nonce 与待执行请求分开保存**：每个区域维护一个单调递增的 nonce 计数器
  + 一个待执行滚动请求。``consume_scroll()`` 只清除待执行请求，**不**重置
  nonce；因此 ``request → consume → request → consume`` 序列里 nonce 持续
  递增，每次渲染的锚点 HTML 都不同。Streamlit 会去重完全相同的 HTML
  （不重新挂载），这也是第 2、3、4 页翻页必须依赖 nonce 的原因。
- **只由翻页触发**：待执行请求只在按钮回调里置位；普通 rerun、展开指标、
  勾选复选框、编辑其它控件都不置位，因此不会自动滚动。
- **会话隔离（安全）**：正常运行 Streamlit 时，待执行请求与 nonce 都保存在
  ``st.session_state`` 中——不同浏览器会话（不同用户 / 不同标签页）各自
  拥有独立状态，绝不共享进程级滚动状态。只有非 Streamlit 环境（单元测试）
  才退化为模块级兜底存储。
- **正确的滚动容器**：Streamlit 的真实滚动容器是 ``section.stMain``
  （``window`` / ``documentElement`` 的 scrollY 始终为 0）。锚点脚本从
  锚点元素向上遍历祖先，把所有可滚动容器（含 stMain）的 scrollTop 归零，
  使视口落在锚点（= 新一页开头）而不是旧页底部。表格内部容器只做尽力
  而为的归零：``stDataFrame`` 的 ``data-testid`` 是 Streamlit 内部选择器，
  即使不存在也不影响主路径（祖先归零 + ``scrollIntoView`` 已保证行为），
  任何单一内部选择器都不是滚动机制的依赖项。
- **不使用私有 DOM 选择器作为唯一可靠机制**：仅用官方 ``st.html`` 的受信
  HTML/JS 通道（``unsafe_allow_javascript=True``），主路径是祖先遍历 +
  原生 ``scrollIntoView``；HTML 不含聊天文本。
"""

import json

import streamlit as st

# session_state 中的键名
_SESSION_PENDING_KEY = "signalens_scroll_pending"
_SESSION_NONCE_KEY = "signalens_scroll_nonce"

# 非 Streamlit 环境（单元测试）的模块级兜底：待执行请求 + nonce 计数器
# 分开保存 —— consume 只清前者，nonce 永不重置。
_PROCESS_PENDING: dict[str, dict] = {}
_PROCESS_NONCE: dict[str, int] = {}


def _use_process_store() -> bool:
    """是否处于非 Streamlit 运行时（无 session_state 可用）。"""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is None
    except Exception:                       # pragma: no cover - 极端环境
        return True


def _pending_store() -> dict:
    if _use_process_store():
        return _PROCESS_PENDING
    store = st.session_state.get(_SESSION_PENDING_KEY)
    if not isinstance(store, dict):
        store = {}
        st.session_state[_SESSION_PENDING_KEY] = store
    return store


def _nonce_store() -> dict:
    if _use_process_store():
        return _PROCESS_NONCE
    store = st.session_state.get(_SESSION_NONCE_KEY)
    if not isinstance(store, dict):
        store = {}
        st.session_state[_SESSION_NONCE_KEY] = store
    return store


def request_scroll(area: str, page: int) -> None:
    """翻页按钮回调调用：登记一次待执行滚动请求并递增该区域 nonce。

    nonce 与待执行请求分开保存：本函数只动 nonce 计数器和待执行请求，
    ``consume_scroll`` 之后计数器不回退，保证连续翻页 HTML 不重复。
    """
    nonces = _nonce_store()
    nonce = int(nonces.get(area) or 0) + 1
    nonces[area] = nonce
    _pending_store()[area] = {"page": page, "nonce": nonce}


def consume_scroll(area: str) -> dict | None:
    """渲染时取走并清除该区域的待执行请求（不触碰 nonce 计数器）。"""
    entry = _pending_store().pop(area, None)
    if entry is None:
        return None
    if int(entry.get("nonce") or 0) <= 0:
        return None
    return entry


def clear_scroll_request(area: str) -> None:
    """清除待执行的滚动请求（重建 / 重新导入 / 重新分析时调用）。

    只清待执行请求；nonce 计数器保留（重新导入后首次翻页的 HTML 仍与
    之前的任何一次不同）。
    """
    _pending_store().pop(area, None)


def current_nonce(area: str) -> int:
    """仅供测试/调试：读取该区域当前 nonce（不改变任何状态）。"""
    return int(_nonce_store().get(area) or 0)


def _scroll_anchor(anchor_id: str, nonce: int) -> None:
    """渲染带 nonce 的零高度锚点并在挂载后滚动进视口。

    ``anchor_id`` 由调用方保证在同一页面上唯一（预览与结果视图使用不同
    的 id）。HTML 只包含锚点 id 与 nonce，不含任何聊天文本。
    """
    st.html(
        f"""
        <div id="{anchor_id}" style="height:0;margin:0;padding:0"></div>
        <script>
          (function () {{
            var nonce = {json.dumps(int(nonce))};
            var anchor = document.getElementById({json.dumps(anchor_id)});
            if (!anchor) {{ return; }}
            anchor.setAttribute("data-scroll-nonce", String(nonce));

            function isScrollable(el) {{
              if (!el || el === document.body) {{ return false; }}
              var s = getComputedStyle(el);
              var scrollable = (s.overflowY === "auto" || s.overflowY === "scroll"
                                || s.overflowY === "overlay");
              return scrollable && el.scrollHeight > el.clientHeight + 4;
            }}

            // 主路径：从锚点向上遍历祖先，把所有可滚动容器（含 Streamlit 的
            // stMain）的 scrollTop 归零，使视口落在锚点（= 新一页开头）。
            var node = anchor.parentElement;
            while (node && node !== document.body) {{
              if (isScrollable(node)) {{
                node.scrollTop = 0;
              }}
              node = node.parentElement;
            }}

            // 尽力而为：表格内部滚动容器归零。data-testid 是 Streamlit 内部
            // 选择器，缺失时静默跳过——主路径不依赖它。
            try {{
              var grids = document.querySelectorAll(
                '[data-testid="stDataFrame"], [data-testid="stDataFrame"] *');
              for (var j = 0; j < grids.length; j++) {{
                if (isScrollable(grids[j])) {{
                  grids[j].scrollTop = 0;
                }}
              }}
            }} catch (e) {{ /* 内部选择器变化不影响主路径 */ }}

            // 兜底：让锚点进入视口（浏览器原生行为）。
            if (anchor.scrollIntoView) {{
              anchor.scrollIntoView({{block: "start", behavior: "auto"}});
            }}
          }})();
        </script>
        """,
        unsafe_allow_javascript=True,
    )
