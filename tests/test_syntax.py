"""语法门禁。

此前曾出现过“Streamlit 热重载撞上正在写入的 app.py”导致的
``SyntaxError: unterminated string literal`` 假象。这里用纯语法检查
（不落 ``__pycache__``）守住每个模块都能编译。
"""

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MODULES = [
    "app.py",
    "parser.py",
    "analyzer.py",
    "merge.py",
    "scoring.py",
    "storage.py",
    "report.py",
    "privacy.py",
    "ui_helpers.py",
    "media.py",
    "rich_paste.py",
    "vision.py",
    "tools/clipboard_probe/probe.py",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_compiles(name):
    source = (ROOT / name).read_text(encoding="utf-8")
    compile(source, name, "exec")
