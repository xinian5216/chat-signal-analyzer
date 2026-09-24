"""统一滚动锚点（只由明确的用户翻页操作触发，单次定位）。

设计要点：

- **nonce 与待执行请求分开保存**：每个区域维护一个单调递增的 nonce 计数器
  + 一个待执行滚动请求。``consume_scroll()`` 只清除待执行请求，**不**重置
  nonce；因此 ``request → consume → request → consume`` 序列里 nonce 持续
  递增，每次渲染的锚点 HTML 都不同。Streamlit 会去重完全相同的 HTML
  （不重新挂载），这也是第 2、3、4 页翻页必须依赖 nonce 的原因。
- **只由翻页触发**：待执行请求只在按钮回调里置位；普通 rerun、展开指标、
  勾选复选框、编辑其它控件都不置位，因此不会自动滚动。
- **会话隔离**：正常运行 Streamlit 时，待执行请求与 nonce 都保存在
  ``st.session_state`` 中——不同浏览器会话各自拥有独立状态。只有非
  Streamlit 环境（单元测试）才退化为模块级兜底存储。
- **单次定位，不再双阶段滚动**：旧实现先把**所有**可滚动祖先的
  ``scrollTop`` 一律归零、再 ``scrollIntoView``，浏览器因此先整体跳到顶部、
  再滚到锚点——用户看到的就是“先明显位移、再定位”。现在按锚点在每个
  滚动容器内的真实偏移**一次算好、一次赋值**，没有中间状态。
- **按操作来源区分**：

  - ``position="top"``（点顶部翻页）：**不做页面滚动**，只把表格自身的
    内部滚动复位，顶部导航保持原位，表格内容原地替换；
  - ``position="bottom"``（点底部翻页）：执行一次明确定位，让新页的顶部
    导航、表头与第一行进入视口；
  - 其它来源（例如表格内部）：同样只做一次定位，不做二次修正。

- **不使用私有 DOM 选择器作为唯一可靠机制**：主路径是从锚点元素向上遍历
  祖先并计算偏移；表格内部滚动的复位优先靠 Streamlit 的**组件 key**
  （app.py 把预览模式 / 页码 / 消息集版本纳入 ``st.dataframe`` 的 key，
  换页即新组件，内部滚动状态自然重置），DOM 复位只是兜底，且失败静默。
- HTML 不含任何聊天文本，只含锚点 id 与 nonce。
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


def request_scroll(area: str, page: int, position: str = "auto") -> None:
    """翻页按钮回调调用：登记一次待执行滚动请求并递增该区域 nonce。

    ``position`` 记录这次翻页的来源（``"top"`` / ``"bottom"`` / ``"auto"``），
    渲染时据此决定要不要滚动、以及滚动到什么位置。
    """
    nonces = _nonce_store()
    nonce = int(nonces.get(area) or 0) + 1
    nonces[area] = nonce
    _pending_store()[area] = {"page": page, "nonce": nonce,
                              "position": position}


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


def _scroll_anchor(anchor_id: str, nonce: int,
                   position: str = "auto") -> None:
    """渲染带 nonce 的零高度锚点，并按来源做**一次**定位。

    ``anchor_id`` 由调用方保证在同一页面上唯一（预览与结果视图使用不同
    的 id）。HTML 只包含锚点 id 与 nonce，不含任何聊天文本。
    """
    st.html(
        f"""
        <div id="{anchor_id}" style="height:0;margin:0;padding:0"></div>
        <script>
          (function () {{
            var nonce = {json.dumps(int(nonce))};
            var position = {json.dumps(position or "auto")};
            var anchor = document.getElementById({json.dumps(anchor_id)});
            if (!anchor) {{ return; }}
            anchor.setAttribute("data-scroll-nonce", String(nonce));

            function isScrollable(el) {{
              if (!el || el === document.body || el === document.documentElement) {{
                return false;
              }}
              var s = getComputedStyle(el);
              var scrollable = (s.overflowY === "auto" || s.overflowY === "scroll"
                                || s.overflowY === "overlay");
              return scrollable && el.scrollHeight > el.clientHeight + 4;
            }}

            // 找到锚点之后的数据表（向后走几个兄弟节点：中间可能夹着
            // st.html 渲染出来的 <script>，不能假设表格一定紧邻锚点）。
            function findTableAfter() {{
              var node = anchor.nextElementSibling;
              for (var i = 0; node && i < 6; i++) {{
                if (node.querySelector) {{
                  var df = node.querySelector('[data-testid="stDataFrame"]');
                  if (df) {{ return df; }}
                }}
                node = node.nextElementSibling;
              }}
              return null;
            }}

            // 顶部翻页：不做页面滚动，只把表格自身的内部滚动复位，
            // 顶部导航保持原位（用户继续在原地翻页）。
            if (position === "top") {{
              var dfTop = findTableAfter();
              if (dfTop) {{ resetInner(dfTop); }}
              return;
            }}

            // 其它来源：按锚点在每个滚动容器内的真实偏移一次算好、一次赋值。
            // 先把旧位置归零会让浏览器“先跳顶部再定位”，这里绝不那么做。
            function resetInner(root) {{
              var walk = [root].concat([].slice.call(root.querySelectorAll('*')));
              for (var i = 0; i < walk.length; i++) {{
                if (isScrollable(walk[i])) {{ walk[i].scrollTop = 0; }}
              }}
            }}

            function offsetWithin(ancestor) {{
              var top = 0, node = anchor;
              while (node && node !== ancestor) {{
                top += node.offsetTop || 0;
                node = node.offsetParent;
              }}
              return top;
            }}
            var node = anchor.parentElement;
            while (node && node !== document.body) {{
              if (isScrollable(node)) {{
                var target = offsetWithin(node) - 8;   // 留 8px 呼吸空间
                if (target >= 0) {{ node.scrollTop = target; }}
              }}
              node = node.parentElement;
            }}

            // 兜底：表格内部滚动复位（静默失败；主路径已用组件 key 保证）
            try {{
              var dfAny = findTableAfter();
              if (dfAny) {{ resetInner(dfAny); }}
            }} catch (e) {{ /* 内部结构变化不影响主路径 */ }}
          }})();
        </script>
        """,
        unsafe_allow_javascript=True,
    )
