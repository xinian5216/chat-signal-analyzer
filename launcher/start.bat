@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem ============================================================
rem  SignalLens start.bat — 一键启动
rem  · 用脚本自身目录定位项目（不依赖当前 cwd）
rem  · 优先使用 .venv\Scripts\python.exe
rem  · 只监听 127.0.0.1，无头模式，自动打开浏览器
rem  · 已在运行时不会启动第二个实例
rem ============================================

set "LAUNCHER_DIR=%~dp0"
if "%LAUNCHER_DIR:~-1%"=="\" set "LAUNCHER_DIR=%LAUNCHER_DIR:~0,-1%"
set "PROJECT_DIR=%LAUNCHER_DIR%\.."
if not exist "%PROJECT_DIR%\app.py" set "PROJECT_DIR=%LAUNCHER_DIR%"

cd /d "%PROJECT_DIR%"

rem ---- 端口与地址配置 ----
set "HOST=127.0.0.1"
set "PORT=8501"
set "URL=http://%HOST%:%PORT%"

rem ---- 单实例检查：端口已被占用则只打开浏览器 ----
netstat -ano | findstr /r /c:"TCP.*:%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo SignalLens 似乎已在运行，正在为你打开浏览器...
    start "" "%URL%"
    timeout /t 2 >nul
    exit /b 0
)

rem ---- 检查虚拟环境 ----
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [错误] 未检测到 Python 虚拟环境 ^(.venv^)。
    echo.
    echo 请先双击运行 install.bat 完成安装。
    echo.
    pause
    exit /b 1
)

rem ---- 检查应用入口 ----
if not exist "app.py" (
    echo.
    echo [错误] 未找到 app.py，请确认 start.bat 位于项目的 launcher 目录。
    echo.
    pause
    exit /b 1
)

rem ---- 启动 Streamlit（无头，仅本机）----
echo 正在启动 SignalLens ...
echo 启动后会自动打开浏览器：%URL%
echo （关闭本窗口即可停止服务）
echo.

start "" /b ".venv\Scripts\python.exe" -m streamlit run app.py --server.address %HOST% --server.port %PORT% --server.headless true

rem ---- 等待端口就绪后打开浏览器 ----
set /a _tries=0
:wait_loop
set /a _tries+=1
netstat -ano | findstr /r /c:"TCP.*:%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 goto :open_browser
if %_tries% geq 30 (
    echo [提示] 等待超时，请手动打开 %URL%
    exit /b 0
)
timeout /t 1 >nul
goto :wait_loop

:open_browser
start "" "%URL%"
rem 保持窗口存活，避免后台进程被回收
echo SignalLens 正在运行。按 Ctrl+C 或关闭本窗口停止。
pause >nul
exit /b 0
