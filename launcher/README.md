# SignalLens Windows 启动器

零命令行启动方式：

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

本轮**没有**打包 `SignalLens.exe`：把整个 Python + Streamlit 打成单文件 EXE
会显著增加复杂度、Defender 误报风险与调试难度。`install.bat` + `start.bat`
已经把体验从“打开 PowerShell 敲命令”变成“双击两次”，足够第一阶段使用。
