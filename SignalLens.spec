# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 规格：构建 SignalLens Windows Portable。

构建（开发者）：

    .venv\\Scripts\\pyinstaller --noconfirm --clean SignalLens.spec

产物：``dist/SignalLens/``（SignalLens.exe + _internal；data 目录首次运行时创建）。

为什么用 onedir 而不是 onefile：

- Streamlit 静态资源很多，onefile 每次启动都要先解压到临时目录，启动更慢；
- onefile 更容易触发 Defender / 杀软误报；
- onedir 的 data 目录就在 exe 旁边，portable 数据路径直观、也更好排查缺包；
- 不使用 UPX（进一步降低误报风险）。
"""

import os

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# ---- 需要整体收集（数据文件 + 二进制 + 隐式依赖）的包 --------------------
# Streamlit 依赖大量动态 import，并需要包内 static 资源，必须整包收集。
FULL_COLLECT = (
    "streamlit",
    "altair",
    "pyarrow",
    "pandas",
    "numpy",
    "jinja2",
    "tenacity",
    "blinker",
    "cachetools",
    "watchdog",
    "dotenv",
    "httpx2",
)

datas = []
binaries = []
hiddenimports = []

for package in FULL_COLLECT:
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception as exc:  # pragma: no cover - 仅构建期
        print(f"[spec] WARN collect_all({package}) failed: {exc}")
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Streamlit / Altair 运行时的补充隐式依赖
hiddenimports += [
    "streamlit.web.cli",
    "streamlit.runtime.scriptrunner.magic_funcs",
    "pyarrow.lib",
]

# PyInstaller \u6ce8\u5165\u7684 SPECPATH \u5c31\u662f spec \u6587\u4ef6\u6240\u5728\u76ee\u5f55\uff08\u4e0d\u9700\u8981\u518d dirname\uff09
PROJECT_ROOT = os.path.abspath(SPECPATH)  # noqa: F821
# ---- 本项目模块 ------------------------------------------------------------
# 既作为隐藏模块冻结进 PYZ（保证 import 可用），也作为数据文件放进 bundle
# （保证 streamlit run <bundle>/app.py 找得到入口脚本）。
PROJECT_MODULES = [
    "app.py",
    "paths.py",
    "parser.py",
    "analyzer.py",
    "merge.py",
    "privacy.py",
    "report.py",
    "scoring.py",
    "settings_store.py",
    "storage.py",
    "ui_helpers.py",
    "media.py",
    "rich_paste.py",
    "vision.py",
    "portable_launcher.py",
    "tools/clipboard_probe/probe.py",
]

for name in PROJECT_MODULES:
    source = os.path.join(PROJECT_ROOT, name)
    if os.path.exists(source):
        datas.append((source, os.path.dirname(name) or "."))
        hiddenimports.append(name[:-3].replace("/", "."))
    else:
        print(f"[spec] WARN missing {name}")


# ---- 本项目模块与随包静态资源 -------------------------------------------
# (源路径, bundle 内目标目录)
data_files = [
    ("components/rich_paste/index.html", "components/rich_paste"),
    ("README-\u542f\u52a8\u8bf4\u660e.txt", "."),
    ("LICENSE", "."),
    ("VERSION", "."),
    (".env.example", "."),
]

for name, target in data_files:
    source = os.path.join(PROJECT_ROOT, name)
    if os.path.exists(source):
        datas.append((source, target))
    else:
        print(f"[spec] WARN missing {name}")

a = Analysis(
    [os.path.join(PROJECT_ROOT, "portable_launcher.py")],  # noqa: F821
    pathex=[PROJECT_ROOT],  # noqa: F821
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 不需要的桌面/开发依赖，缩小体积
        "matplotlib",
        "tkinter",
        "IPython",
        "notebook",
        "pytest",
        "_pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SignalLens",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 第一版保留控制台：看得见“关闭此窗口即可退出”
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,              # 不用 UPX：降低 Defender 误报风险
    upx_exclude=[],
    name="SignalLens",
)
