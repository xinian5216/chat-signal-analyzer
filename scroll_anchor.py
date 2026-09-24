"""统一滚动锚点：只在明确的用户翻页操作后触发，用 nonce 保证每次都重新挂载。

设计要点：

- **nonce**：每次翻页把 ``(area, page, nonce)`` 写进一个 pending-scroll
  请求；渲染的 HTML 里带 nonce。Streamlit 对相同 HTML 会去重不重新挂载，
  因此相同 HTML 的第 2、3、4 页翻页不会触发滚动；nonce 让每次翻页的
  HTML 都不同，保证连续翻页每页都触发。
- **只由翻页触发**：pending-scroll 只在按钮回调里置位；普通 rerun、
  展开指标、勾选复选框、编辑其它控件都不置位，因此不会自动滚动。
- **正确的滚动容器**：Streamlit 的真实滚动容器是 ``section.stMain``
  （``window`` / ``documentElement`` 的 scrollY 始终为 0），数据表自身还有
  一个 ``dvn-scroller`` 内部容器。锚点脚本同时处理两者：
  ``scrollIntoView`` 交给原生（内部滚动容器按需使用），再把所有祖先
  可滚动容器（含 stMain 与表格 scroller）的 scrollTop 归零。
- **不使用私有 DOM 选择器猜测**：仅用官方 ``st.html`` 的受信 HTML/JS
  通道（``unsafe_allow_javascript=True``），从锚点元素向上遍历祖先节点
  查找可滚动容器，不对 Streamlit 内部结构做强假设。HTML 不含聊天文本。
"""

import json

import streamlit as st

# 每个区域一个待执行的滚动请求：{"page": int, "nonce": int}
_PENDING_SCROLL_ATTR = "pending_scroll_requests"

# 进程内兜底（AppTest / 单元测试没有 session_state 时也能跑）
_PROCESS_SCROLL_REQUESTS: dict[str, dict] = {}


def _scroll_requests() -> dict:
    store = getattr(st, _PENDING_SCROLL_ATTR, None)
    if store is None:
        # 测试 / 非 Streamlit 环境下退化为进程内字典
        store = _PROCESS_SCROLL_REQUESTS
    return store


def request_scroll(area: str, page: int) -> None:
    """翻页按钮回调调用：登记一次滚动请求（递增 nonce）。"""
    store = _scroll_requests()
    entry = dict(store.get(area) or {"nonce": 0})
    entry["page"] = page
    entry["nonce"] = int(entry.get("nonce") or 0) + 1
    store[area] = entry


def consume_scroll(area: str) -> dict | None:
    """渲染时取走并清除该区域的滚动请求（返回 None 表示本轮不滚动）。"""
    store = _scroll_requests()
    entry = store.pop(area, None)
    if entry is None:
        return None
    if int(entry.get("nonce") or 0) <= 0:
        return None
    return entry


def clear_scroll_request(area: str) -> None:
    """清除待执行的滚动请求（重建 / 重新导入 / 重新分析时调用）。"""
    _scroll_requests().pop(area, None)


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
              var s = getComputedStyle(el);
              var scrollable = (s.overflowY === "auto" || s.overflowY === "scroll"
                                || s.overflowY === "overlay");
              return scrollable && el.scrollHeight > el.clientHeight + 4;
            }}

            // 1) 收集锚点之后、且在锚点上方的可滚动祖先（含 Streamlit 的
            //    stMain 主滚动容器）：把它们的 scrollTop 归零，使视口落在
            //    锚点（= 新一页开头）而不是旧页底部。
            var node = anchor.parentElement;
            while (node && node !== document.body) {{
              if (isScrollable(node)) {{
                node.scrollTop = 0;
              }}
              node = node.parentElement;
            }}
            // 2) 表格内部滚动容器：翻页后从第一行开始显示。
            var scopes = [anchor.closest("[data-test-scope]"), document];
            for (var i = 0; i < scopes.length; i++) {{
              if (!scopes[i]) {{ continue; }}
              var grids = scopes[i].querySelectorAll(
                '[data-testid="stDataFrame"], [data-testid="stDataFrame"] *');
              for (var j = 0; j < grids.length; j++) {{
                if (isScrollable(grids[j])) {{
                  grids[j].scrollTop = 0;
                }}
              }}
            }}
            // 3) 最后让锚点进入视口（浏览器原生行为，兜底）。
            if (anchor.scrollIntoView) {{
              anchor.scrollIntoView({{block: "start", behavior: "auto"}});
            }}
          }})();
        </script>
        """,
        unsafe_allow_javascript=True,
    )
