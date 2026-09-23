"""测试进程的凭据 / 位置隔离（全 tests/ 生效，autouse）。

背景（历史测试事故）：``settings_store.save_api_key()`` 为了让“保存 Key 后
立即生效”而直接写 ``os.environ["TYPESAFE_API_KEY"]``。这是**正式应用**的
预期行为（保存后无需重启即可用），本轮不改。但在测试里，这个写操作
monkeypatch 跟踪不到：一旦某个测试保存了 Key，该变量就会泄漏给后续
测试与它们派生的子进程——曾经因此让 ``--real`` 门禁子进程拿到假 Key 并
真的向 Jev 发起请求（全部 401 拒绝，未写入任何缓存）。

这里用一个 function 级、autouse、fixture 在每个测试前后快照 / 恢复相关
环境变量，teardown 不依赖 monkeypatch，因此与测试执行顺序无关。
"""

import os

import pytest

# 会由应用代码直接写、可能跨测试泄漏的敏感 / 定位变量。
GUARDED_ENV_KEYS = (
    "TYPESAFE_API_KEY",
    "SIGNALLENS_DATA_DIR",
    "SIGNALLENS_JEV_CONCURRENCY",
    "SIGNALLENS_DEBUG_TIMING",
    "SIGNALLENS_NO_BROWSER",
)


@pytest.fixture(autouse=True)
def _restore_guarded_env():
    """每个测试结束后恢复凭据 / 定位环境变量到测试前状态。"""
    snapshot = {key: os.environ.get(key) for key in GUARDED_ENV_KEYS}
    try:
        yield
    finally:
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
