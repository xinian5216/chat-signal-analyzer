"""Windows 启动器测试（install.bat / start.bat 的安全属性）。

批处理无法在 CI 的 Linux runner 上执行，因此这里以“脚本内容不变量”方式
覆盖关键行为：路径含空格、缺 .venv、缺 Python、单实例、不依赖 cwd。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = (ROOT / "launcher" / "start.bat").read_text(encoding="utf-8")
INSTALL = (ROOT / "launcher" / "install.bat").read_text(encoding="utf-8")


def test_start_bat_uses_script_relative_dir():
    """不依赖当前 cwd：必须用 %~dp0 定位自身目录。"""
    assert "%~dp0" in START
    # 不得硬编码某个盘的绝对路径
    assert not re.search(r'cd /d "[A-Za-z]:\\', START)


def test_start_bat_quoted_paths_support_spaces():
    """项目路径含空格时仍安全：关键路径都带引号。"""
    assert '"%PROJECT_DIR%\\app.py"' in START
    assert '".venv\\Scripts\\python.exe"' in START
    assert 'cd /d "%PROJECT_DIR%"' in START


def test_start_bat_missing_venv_gives_clear_message():
    assert "未检测到 Python 虚拟环境" in START
    assert "install.bat" in START


def test_start_bat_single_instance_guard():
    """端口已在监听时不启动第二个实例，只打开浏览器。"""
    assert "netstat" in START
    assert "TCP.*:%PORT% .*LISTENING" in START
    assert "start \"\" \"%URL%\"" in START


def test_start_bat_localhost_only_and_headless():
    assert "set \"HOST=127.0.0.1\"" in START
    assert "--server.headless true" in START
    assert "--server.address %HOST%" in START


def test_start_bat_opens_browser_after_port_ready():
    assert ":wait_loop" in START
    assert ":open_browser" in START
    assert "timeout /t 1" in START


def test_install_bat_checks_python_versions():
    assert "py -3.11 --version" in INSTALL
    assert "python --version" in INSTALL
    assert "未找到 Python" in INSTALL


def test_install_bat_creates_venv_and_installs_requirements():
    assert "-m venv .venv" in INSTALL
    assert "pip install -r requirements.txt" in INSTALL


def test_install_bat_no_admin_no_global_install():
    """不要求管理员权限、不修改系统 Python、不安装全局包。"""
    lowered = INSTALL.lower()
    for banned in ("runas", "msiexec", "start /wait", "reg add", "--user pip"):
        assert banned not in lowered, banned
    assert "pip install --user" not in lowered


def test_install_bat_script_relative_and_space_safe():
    assert "%~dp0" in INSTALL
    assert 'cd /d "%PROJECT_DIR%"' in INSTALL


def test_install_bat_mentions_env_setup():
    assert ".env.example" in INSTALL
    assert "TYPESAFE_API_KEY" in INSTALL


def test_launcher_readme_documents_double_click_flow():
    readme = (ROOT / "launcher" / "README.md").read_text(encoding="utf-8")
    assert "install.bat" in readme and "start.bat" in readme
    assert "127.0.0.1" in readme
