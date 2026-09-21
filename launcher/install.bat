@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem ============================================================
rem  SignalLens install.bat — 一键创建虚拟环境并安装依赖
rem  不要求管理员权限，不修改系统 Python，不安装全局包
rem ============================================================

rem 用脚本自身目录定位项目（不依赖当前 cwd，路径含空格也安全）
set "LAUNCHER_DIR=%~dp0"
if "%LAUNCHER_DIR:~-1%"=="\" set "LAUNCHER_DIR=%LAUNCHER_DIR:~0,-1%"
set "PROJECT_DIR=%LAUNCHER_DIR%\.."
if not exist "%PROJECT_DIR%\app.py" set "PROJECT_DIR=%LAUNCHER_DIR%"

cd /d "%PROJECT_DIR%"
echo.
echo ============================================
echo   SignalLens 安装向导
echo   项目目录: %PROJECT_DIR%
echo ============================================
echo.

rem ---- 1. 寻找可用的 Python 3.11+ ----
set "PYTHON_EXE="

py -3.11 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=py -3.11"
    goto :found_python
)

python --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=python"
    goto :found_python
)

echo [错误] 未找到 Python。
echo.
echo 请先安装 Python 3.11 或更高版本：
echo   https://www.python.org/downloads/
echo.
echo 安装时请勾选 “Add python.exe to PATH”。
echo.
pause
exit /b 1

:found_python
echo [1/3] 找到 Python: %PYTHON_EXE%
%PYTHON_EXE% --version

rem ---- 2. 创建虚拟环境 ----
if exist ".venv\Scripts\python.exe" (
    echo [2/3] 检测到已有虚拟环境 .venv，跳过创建。
) else (
    echo [2/3] 正在创建虚拟环境 .venv ...
    %PYTHON_EXE% -m venv .venv
    if errorlevel 1 (
        echo.
        echo [错误] 创建虚拟环境失败。
        echo 如果提示缺少 venv 模块，请重装 Python 并确保勾选 pip / venv 组件。
        echo.
        pause
        exit /b 1
    )
)

rem ---- 3. 安装依赖 ----
echo [3/3] 正在安装依赖 ^(pip install -r requirements.txt^) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。请检查网络后重试。
    echo.
    pause
    exit /b 1
)

rem ---- 完成提示 ----
if not exist ".env" (
    echo.
    echo [提示] 尚未检测到 .env 配置文件。
    echo        SignalLens 需要 TypeSafe API Key 才能分析：
    echo        1. 复制 .env.example 为 .env
    echo        2. 编辑 .env，填入 TYPESAFE_API_KEY=你的Key
    echo.
)

echo.
echo ============================================
echo   安装完成。
echo   双击 start.bat 启动 SignalLens。
echo ============================================
echo.
pause
exit /b 0
