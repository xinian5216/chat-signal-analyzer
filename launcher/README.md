# SignalLens 源码模式 Windows 启动器

这是给**从源码运行**的 Windows 用户准备的零命令行启动方式：

```
1. 双击 install.bat   （首次：创建 .venv 并安装依赖）
2. 双击 start.bat     （每次：启动并自动打开浏览器）
```

## install.bat

- 自动寻找 `py -3.11` 或 `python`（要求 Python ≥ 3.11）
- 在项目根目录创建 `.venv`（**不要求管理员权限，不改系统 Python，不安全局包**）
- `pip install -r requirements.txt`
- 若不存在 `.env`，结束时提示如何配置 API Key

## start.bat

- 用脚本自身目录定位项目（**不依赖当前工作目录**，路径含空格也安全）
- 优先使用 `.venv\Scripts\python.exe`；缺失时提示先运行 `install.bat`
- 以 `127.0.0.1:8501` + `--server.headless true` 启动（仅本机可访问）
- 等待端口就绪后自动打开默认浏览器
- **单实例保护**：检测到端口已在监听时不再启动第二个实例，只打开浏览器

## 停止服务

关闭启动窗口（或在该窗口按 `Ctrl+C`）。

## 说明

普通用户应优先从 [GitHub Releases](https://github.com/xinian5216/chat-signal-analyzer/releases)
下载 Windows Portable，解压后直接运行 `SignalLens.exe`，不需要 Python 或
这两个批处理脚本。`install.bat` 和 `start.bat` 仅用于源码仓库，方便开发、
调试或自行修改代码。
